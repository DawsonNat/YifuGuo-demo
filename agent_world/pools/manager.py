"""``MultiPoolPlatformManager`` — multi-pool orchestrator (LAYOUT §2.D).

Owns the ``N`` OASIS Platform instances (one per ``(place, feed)`` pool), wires
follow-table projection from ``world.db.relation`` (LAYOUT §3.5 double-track),
and routes FEED-class actions from agents to the right pool's Channel.

This MVP follows the L4 task brief contract (constructor / add_pool / update_all
/ platform_for / feeds_at / dispatch / register_relation_change_hook /
rebuild_follow_from_world).  See ``docs/impl/pools_manager.md`` for the
fuller class diagram (PoolHandle, async build flow); for the demo we keep the
shape minimal and let :class:`PlatformFactory` do the wiring.

Upstream:  ``WorldStep`` (step 3 update_all), ``ActionDispatcher`` (FEED routing
via :meth:`dispatch`), ``PerceptionBuilder`` (``feeds_at``), ``WorldState``.
Downstream: ``PlatformFactory.build_platform``, :class:`PoolDB`,
``RelationGraph.register_on_change``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set

from agent_world.persistence.pool_db import PoolDB

if TYPE_CHECKING:  # pragma: no cover - type hints only
    from oasis.social_platform.channel import Channel
    from oasis.social_platform.platform import Platform

    from agent_world.persistence.world_db import WorldDB
    from agent_world.pools.platform_factory import PlatformFactory
    from agent_world.world.capability_table import CapabilityTable
    from agent_world.world.connectivity import ConnectivityResolver
    from agent_world.world.place_store import PlaceStore
    from agent_world.world.relation_graph import RelationGraph

log = logging.getLogger(__name__)


# Relation types that project to ``pool_*.db.follow`` (LAYOUT §3.5).
_FOLLOW_PROJECTING_TYPES = frozenset({"mutual_follow", "follower"})
_SYMMETRIC_FOLLOW_TYPES = frozenset({"mutual_follow"})

# Non-FEED ActionTypes that must NOT be written to OASIS Channel — these are
# the L4 v0.3 additions (B3 + UPDATE_STATE) plus the 4 group actions which
# now go through agent_world.buses.GroupMessageBus rather than OASIS Platform.
_NON_FEED_ACTION_VALUES = frozenset({
    "speak_to_local",
    "send_message",
    "request_move",
    "relation_change",
    "capability_change",
    "update_state",
    "create_group",
    "join_group",
    "leave_group",
    "send_to_group",
})


@dataclass
class PoolHandle:
    """One pool = one OASIS Platform + Channel + PoolDB + feed metadata."""

    pool_id: str
    feed: str
    pool_db: PoolDB
    platform: Any                       # forked oasis Platform
    channel: Any                        # oasis Channel (per-pool)
    places: Set[str] = field(default_factory=set)
    feeds: List[str] = field(default_factory=list)


class MultiPoolPlatformManager:
    """Orchestrates N OASIS Platform instances across (place, feed) pools."""

    def __init__(
        self,
        places: "PlaceStore",
        caps: "CapabilityTable",
        connectivity: "ConnectivityResolver",
        factory: "PlatformFactory",
        base_dir: str,
    ) -> None:
        self.places = places
        self.caps = caps
        self.connectivity = connectivity
        self.factory = factory
        self.base_dir = base_dir

        # pool_id -> PoolHandle
        self.pools: Dict[str, PoolHandle] = {}
        # (place_id, feed) -> pool_id  (routing index)
        self._by_place_feed: Dict[tuple, str] = {}
        # place_id -> [pool_id, ...]   (feeds_at index)
        self._by_place: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------ build

    def add_pool(
        self,
        pool_id: str,
        place_ids: List[str],
        feed: str,
        recsys_type: str = "twitter",
    ) -> PoolHandle:
        """Build one pool: PoolDB + forked Platform + Channel.

        ``pool_id`` is the logical key; the on-disk DB path is rendered as
        ``{base_dir}/pools/pool__{place_id}__{feed}.db`` (taking the *first*
        place as the canonical one when multiple places share a pool).
        """
        if pool_id in self.pools:
            raise ValueError(f"pool_id {pool_id!r} already exists")
        if not place_ids:
            raise ValueError(f"add_pool({pool_id!r}): place_ids must be non-empty")

        canonical_place = place_ids[0]
        db_dir = os.path.join(self.base_dir, "pools")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, f"pool__{canonical_place}__{feed}.db")

        pool_db = PoolDB(pool_path=db_path, feed=feed)
        pool_db.init()

        # PlatformFactory builds Channel + RecSys + Platform; share connection
        # by passing the existing db_path so create_db is idempotent.
        platform = self.factory.build_platform(
            pool_id=pool_id,
            db_path=db_path,
            recsys_type=recsys_type,
        )
        channel = getattr(platform, "channel", None)

        handle = PoolHandle(
            pool_id=pool_id,
            feed=feed,
            pool_db=pool_db,
            platform=platform,
            channel=channel,
            places=set(place_ids),
            feeds=[feed],
        )
        self.pools[pool_id] = handle
        for p in place_ids:
            self._by_place_feed[(p, feed)] = pool_id
            self._by_place.setdefault(p, []).append(pool_id)

        log.info(
            "MultiPoolPlatformManager: added pool=%s feed=%s places=%s db=%s",
            pool_id, feed, place_ids, db_path,
        )
        return handle

    # ----------------------------------------------------------------- update

    async def update_all(self) -> None:
        """Concurrently call ``Platform.update_rec_table()`` on every pool.

        LAYOUT §6.1 step 3: ``await asyncio.gather(*[p.update_rec_table()])``.
        Single-pool failures are logged but don't abort the gather, so
        WorldStep can keep advancing time.
        """
        coros = []
        for handle in self.pools.values():
            update = getattr(handle.platform, "update_rec_table", None)
            if update is None:
                continue
            coros.append(update())
        if not coros:
            return
        results = await asyncio.gather(*coros, return_exceptions=True)
        for handle, result in zip(self.pools.values(), results):
            if isinstance(result, Exception):
                log.warning(
                    "update_rec_table failed for pool=%s: %s",
                    handle.pool_id, result,
                )

    # ---------------------------------------------------------------- routing

    def platform_for(self, place_id: str, feed: str) -> Optional[Any]:
        """Return the OASIS Platform serving (place_id, feed), if any."""
        pool_id = self._by_place_feed.get((place_id, feed))
        if pool_id is None:
            return None
        return self.pools[pool_id].platform

    def feeds_at(self, place_id: str) -> List[str]:
        """List ``feed`` ids exposed at ``place_id`` (deduped, stable order)."""
        seen: Set[str] = set()
        out: List[str] = []
        for pool_id in self._by_place.get(place_id, []):
            f = self.pools[pool_id].feed
            if f not in seen:
                seen.add(f)
                out.append(f)
        return out

    # --------------------------------------------------------------- dispatch

    def attach_action_dispatcher(self, action_dispatcher: Any) -> None:
        """Wire the central :class:`ActionDispatcher` so non-FEED actions
        (B3 + group + UPDATE_STATE) are routed through it rather than the
        OASIS Channel. Called by the runner after both objects are built.
        """
        self.action_dispatcher = action_dispatcher

    async def dispatch(self, action_type: Any, sender: int, **kwargs: Any) -> Any:
        """Route an action from ``sender`` to the appropriate subsystem.

        Non-FEED actions (the 6 v0.3 additions + the 4 group actions) go to
        the central :class:`ActionDispatcher`, which fans them out to F2F /
        RDC / GRP buses, the WorldState (UPDATE_STATE), or the pending-move
        queue (REQUEST_MOVE).  FEED-class actions go to the matching pool's
        OASIS Channel.

        ``kwargs`` may include ``place_id`` / ``feed`` to hint the FEED
        routing key; otherwise the sender's current location is consulted
        and the first feed exposed there is used.
        """
        action_value = getattr(action_type, "value", action_type)
        # ---- non-FEED routing (LAYOUT §6.1 step 7 dispatch table) ---------
        if action_value in _NON_FEED_ACTION_VALUES:
            ad = getattr(self, "action_dispatcher", None)
            if ad is None:
                raise RuntimeError(
                    f"dispatch: action_dispatcher not attached for {action_type!r}"
                )
            t = kwargs.pop("t", None)
            if t is None:
                # Best-effort: read from world clock if we have one.
                t = getattr(getattr(self, "_world", None), "t", 0)
                clock = getattr(self, "_clock", None)
                if clock is not None:
                    t = getattr(clock, "t", t)
            return await ad.dispatch(sender, action_type, int(t), **kwargs)

        # ---- FEED routing (OASIS Channel) ---------------------------------
        place_id = kwargs.pop("place_id", None) or self.places.L_t(sender)
        feed = kwargs.pop("feed", None)
        if place_id is None:
            raise RuntimeError(
                f"dispatch: agent {sender} has no location for action {action_type!r}"
            )
        if feed is None:
            feeds = self.feeds_at(place_id)
            if not feeds:
                raise RuntimeError(
                    f"dispatch: no feed at place {place_id!r} for action {action_type!r}"
                )
            feed = feeds[0]

        platform = self.platform_for(place_id, feed)
        if platform is None:
            raise RuntimeError(
                f"dispatch: no platform for ({place_id!r}, {feed!r})"
            )

        channel = getattr(platform, "channel", None)
        if channel is None:
            raise RuntimeError(f"dispatch: platform for {feed!r} has no Channel")
        message_id = await channel.write_to_receive_queue(
            (sender, kwargs, action_value)
        )
        return message_id

    # --------------------------------------------------- relation projection

    def register_relation_change_hook(self, relations: "RelationGraph") -> None:
        """Wire :meth:`RelationGraph.register_on_change` so that follow-class
        relation edges are projected to ``pool_*.db.follow`` synchronously.

        LAYOUT §3.5: ``mutual_follow`` writes both directions (symmetric);
        ``follower`` writes only (src, dst). Cross-DB atomicity is given up.
        """

        def _hook(src: int, dst: int, relation_type: str, op: str) -> None:
            if relation_type not in _FOLLOW_PROJECTING_TYPES:
                return
            symmetric = relation_type in _SYMMETRIC_FOLLOW_TYPES
            for handle in self.pools.values():
                # Only project to pools where both endpoints have an account.
                try:
                    pool_users = set(handle.pool_db.list_user_ids())
                except Exception:  # noqa: BLE001
                    pool_users = set()
                if src not in pool_users or dst not in pool_users:
                    continue
                try:
                    if op == "add":
                        handle.pool_db.add_follow(src, dst)
                        if symmetric:
                            handle.pool_db.add_follow(dst, src)
                    elif op == "remove":
                        handle.pool_db.remove_follow(src, dst)
                        if symmetric:
                            handle.pool_db.remove_follow(dst, src)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "follow-projection hook failed pool=%s op=%s "
                        "(%s,%s,%s): %s",
                        handle.pool_id, op, src, dst, relation_type, exc,
                    )

        relations.register_on_change(_hook)

    def rebuild_follow_from_world(
        self,
        world_db: "WorldDB",
        agent_ids: Iterable[int],
    ) -> Dict[str, int]:
        """Drop & re-insert ``pool_*.db.follow`` from ``world.db.relation``
        for every pool (LAYOUT §3.5 startup recovery — A2).

        Returns a ``{pool_id: rows_inserted}`` map for logging / tests.
        """
        agent_set = set(int(a) for a in agent_ids)
        out: Dict[str, int] = {}
        for handle in self.pools.values():
            # Restrict to agents that actually have an account in this pool.
            try:
                pool_users = set(handle.pool_db.list_user_ids()) & agent_set
            except Exception:  # noqa: BLE001
                pool_users = agent_set
            inserted = handle.pool_db.rebuild_follow_from_world(
                world_db, pool_users, t=0
            )
            out[handle.pool_id] = inserted
            log.info(
                "rebuild_follow_from_world pool=%s rows=%d",
                handle.pool_id, inserted,
            )
        return out

    # ----------------------------------------------------------------- misc

    def all_pools(self) -> List[PoolHandle]:
        return list(self.pools.values())

    async def shutdown(self) -> None:
        """Close every PoolDB; Platform/Channel cleanup is best-effort."""
        for handle in self.pools.values():
            try:
                handle.pool_db.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("PoolDB close failed pool=%s: %s", handle.pool_id, exc)


__all__ = ["MultiPoolPlatformManager", "PoolHandle"]
