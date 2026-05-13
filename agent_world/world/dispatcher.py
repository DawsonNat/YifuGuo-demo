"""ActionDispatcher (L4, LAYOUT §2.A / §6.1 step 7 / §6.2).

Single routing layer that fans an LLM ``tool_call`` out to one of six
backends:

1. :class:`FaceToFaceBus`        - ``SPEAK_TO_LOCAL``
2. :class:`RemoteMessageBus`     - ``SEND_MESSAGE``
3. :class:`GroupMessageBus`      - ``SEND_TO_GROUP / CREATE_GROUP /
                                    JOIN_GROUP / LEAVE_GROUP``
4. ``MultiPoolPlatformManager``  - the OASIS FEED-class actions, each via
                                    ``pool_manager.dispatch(action_type,
                                    sender=agent_id, **kwargs)``
5. world-state direct writes     - ``RELATION_CHANGE`` -> ``world.relations``;
                                    ``CAPABILITY_CHANGE`` -> ``world.caps``;
                                    ``REQUEST_MOVE`` queued for step-9 commit
                                    with ``BehaviorCompressor.on_move`` pre-hook
6. ``UPDATE_STATE``              - bypasses every Bus / ScriptEngine and
                                    writes ``world.set_current_state(agent_id,
                                    new_state)`` (LAYOUT B5).

After every successful dispatch we ``segment_store.append`` the raw entry so
``BehaviorCompressor`` can later summarise it (LAYOUT §2.F / §6.1 step 9).
B4 retry (parse_error / arg_missing) lives in ``agent.perform_action_by_llm``;
unknown / failing actions here just return ``{'success': False, 'reason': ...}``.
Capacity conflicts in ``commit_pending_moves`` silently fail (LAYOUT B9).
"""

from __future__ import annotations

import enum
import logging
from typing import Any, Dict, List, Optional, Tuple

# OASIS optional: when the heavy OASIS deps aren't installed (demo/tests),
# fall back to a stand-in ActionType enum that exposes the values this file
# actually consumes (FEED + 6 v0.3 + 4 group + UPDATE_STATE / REQUEST_MOVE /
# RELATION_CHANGE / CAPABILITY_CHANGE).
try:  # pragma: no cover — env-dependent
    from oasis.social_platform.typing import ActionType  # type: ignore
except Exception:  # noqa: BLE001
    class ActionType(str, enum.Enum):  # type: ignore[no-redef]
        # FEED-class
        CREATE_POST = "create_post"
        REPOST = "repost"
        QUOTE_POST = "quote_post"
        LIKE_POST = "like_post"
        UNLIKE_POST = "unlike_post"
        DISLIKE_POST = "dislike_post"
        UNDO_DISLIKE_POST = "undo_dislike_post"
        SEARCH_POSTS = "search_posts"
        SEARCH_USER = "search_user"
        TREND = "trend"
        REFRESH = "refresh"
        DO_NOTHING = "do_nothing"
        CREATE_COMMENT = "create_comment"
        LIKE_COMMENT = "like_comment"
        UNLIKE_COMMENT = "unlike_comment"
        DISLIKE_COMMENT = "dislike_comment"
        UNDO_DISLIKE_COMMENT = "undo_dislike_comment"
        FOLLOW = "follow"
        UNFOLLOW = "unfollow"
        MUTE = "mute"
        UNMUTE = "unmute"
        PURCHASE_PRODUCT = "purchase_product"
        INTERVIEW = "interview"
        REPORT_POST = "report_post"
        # Group actions (route to GroupMessageBus)
        CREATE_GROUP = "create_group"
        JOIN_GROUP = "join_group"
        LEAVE_GROUP = "leave_group"
        SEND_TO_GROUP = "send_to_group"
        # v0.3 / B3 additions
        SPEAK_TO_LOCAL = "speak_to_local"
        SEND_MESSAGE = "send_message"
        REQUEST_MOVE = "request_move"
        RELATION_CHANGE = "relation_change"
        CAPABILITY_CHANGE = "capability_change"
        UPDATE_STATE = "update_state"
        UPDATE_SHORT_TERM_GOAL = "update_short_term_goal"

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# FEED action set: routed to MultiPoolPlatformManager.dispatch(...)            #
# --------------------------------------------------------------------------- #

class _UnknownAction(Exception):
    """Raised by ``_route`` when no branch matches the action_type.

    Distinguished from generic ``KeyError`` so that ``kwargs['x']`` lookups
    inside a matched branch don't get mis-classified as 'unknown action'.
    """


def _feed_actions() -> frozenset:
    """Build the FEED-action set from whichever ActionType is in scope.

    Some legacy OASIS members (UPDATE_REC_TABLE / SIGNUP / EXIT) are absent
    from the demo fallback enum; we filter by ``getattr`` so the fallback
    enum doesn't blow up imports.
    """
    names = (
        "CREATE_POST", "REPOST", "QUOTE_POST",
        "LIKE_POST", "UNLIKE_POST", "DISLIKE_POST", "UNDO_DISLIKE_POST",
        "REPORT_POST",
        "CREATE_COMMENT", "LIKE_COMMENT", "UNLIKE_COMMENT",
        "DISLIKE_COMMENT", "UNDO_DISLIKE_COMMENT",
        "FOLLOW", "UNFOLLOW", "MUTE", "UNMUTE",
        "SEARCH_USER", "SEARCH_POSTS", "TREND", "REFRESH",
        "PURCHASE_PRODUCT", "INTERVIEW",
        "UPDATE_REC_TABLE", "SIGNUP", "DO_NOTHING", "EXIT",
    )
    out = []
    for n in names:
        v = getattr(ActionType, n, None)
        if v is not None:
            out.append(v)
    return frozenset(out)


_FEED_ACTIONS = _feed_actions()


class ActionDispatcher:
    """Route LLM tool_calls to the right subsystem.

    Constructor follows the task spec; all dependencies are duck-typed so
    parallel L4 work (e.g. ``pools/manager.py``, ``world/state.py``) can
    land independently. ``compressor`` and ``segment_store`` may be
    ``None`` during the P0 minimal demo.
    """

    def __init__(
        self,
        world_state: Any,
        f2f_bus: Any,
        rdc_bus: Any,
        grp_bus: Any,
        pool_manager: Any,
        script_engine: Any,
        compressor: Any = None,
        segment_store: Any = None,
    ) -> None:
        self.world = world_state
        self.f2f = f2f_bus
        self.rdc = rdc_bus
        self.grp = grp_bus
        self.pools = pool_manager
        self.script = script_engine
        self.compressor = compressor
        self.segments = segment_store

        # ``pending_moves`` queues approved REQUEST_MOVE actions for
        # ``commit_pending_moves`` (LAYOUT §6.1 step 9). Tuples of
        # ``(agent_id, new_place_id, attempted_at_t)``.
        self.pending_moves: List[Tuple[int, Any, int]] = []

    # ------------------------------------------------------------------ #
    # main entry                                                         #
    # ------------------------------------------------------------------ #

    async def dispatch(
        self,
        agent_id: int,
        action_type: ActionType,
        t: int,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Route one parsed ``(agent_id, action_type, kwargs)`` action.

        Returns a small envelope ``{'success': bool, ...}``. Any backend
        exception (including unknown actions) becomes a failure envelope
        instead of propagating, so a misbehaving handler cannot abort the
        per-agent micro-tick.
        """
        # Coerce string tool-call names ("speak_to_local") to ActionType.
        # Required because OASIS's upstream ActionType is a plain Enum (not a
        # str subclass), so direct equality with the LLM-emitted string fails.
        if isinstance(action_type, str):
            try:
                action_type = ActionType(action_type)
            except ValueError:
                # leave as-is; _route will raise KeyError → unknown_action
                pass

        try:
            result = await self._route(agent_id, action_type, t, kwargs)
        except _UnknownAction:
            logger.warning(
                "ActionDispatcher: unknown action %r (agent=%s)",
                action_type, agent_id,
            )
            return {"success": False, "reason": "unknown_action"}
        except Exception as e:  # noqa: BLE001 - never raise into world_step
            logger.error(
                "ActionDispatcher: handler raised for action=%s agent=%s err=%s",
                action_type, agent_id, e,
            )
            return {"success": False, "reason": "handler_error", "error": str(e)}

        # Append to segment store on success (LAYOUT §2.F).
        if self.segments is not None and result.get("success", True):
            try:
                kind = action_type.value if isinstance(action_type, ActionType) else str(action_type)
                self.segments.append(agent_id, t, kind, dict(kwargs))
            except Exception as e:  # noqa: BLE001 - segment append is best-effort
                logger.debug(
                    "ActionDispatcher: segment.append failed agent=%s err=%s",
                    agent_id, e,
                )

        return result

    # ------------------------------------------------------------------ #
    # router                                                             #
    # ------------------------------------------------------------------ #

    async def _route(
        self,
        agent_id: int,
        action_type: ActionType,
        t: int,
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        # 1. Direct communication ----------------------------------------
        if action_type == ActionType.SPEAK_TO_LOCAL:
            recipients = await self.f2f.send(agent_id, kwargs["content"], t)
            return {"success": True, "recipients": recipients}

        if action_type == ActionType.SEND_MESSAGE:
            res = await self.rdc.send(
                agent_id, kwargs["target"], kwargs["content"], t
            )
            ok = bool(getattr(res, "success", True))
            return {
                "success": ok,
                "arrive_at": getattr(res, "arrive_at", t),
                "message_id": getattr(res, "message_id", None),
            }

        # 2. Group bus ----------------------------------------------------
        if action_type == ActionType.SEND_TO_GROUP:
            msg_id = await self.grp.send_to_group(
                agent_id, kwargs["group_id"], kwargs["content"], t
            )
            return {"success": True, "message_id": msg_id}

        if action_type == ActionType.CREATE_GROUP:
            group_id = await self.grp.create_group(agent_id, kwargs["name"])
            return {"success": True, "group_id": group_id}

        if action_type == ActionType.JOIN_GROUP:
            ok = await self.grp.join_group(agent_id, kwargs["group_id"])
            return {"success": bool(ok)}

        if action_type == ActionType.LEAVE_GROUP:
            ok = await self.grp.leave_group(agent_id, kwargs["group_id"])
            return {"success": bool(ok)}

        # 3. Move (queued; pre-hook + commit deferred to step 9) ---------
        if action_type == ActionType.REQUEST_MOVE:
            place_id = kwargs.get("place_id") or kwargs.get("target")
            self.enqueue_move(agent_id, place_id, t)
            return {"success": True, "queued": True, "place_id": place_id}

        # 4. World-state direct writes -----------------------------------
        if action_type == ActionType.RELATION_CHANGE:
            return await self._relation_change(agent_id, t, kwargs)

        if action_type == ActionType.CAPABILITY_CHANGE:
            return await self._capability_change(agent_id, t, kwargs)

        if action_type == ActionType.UPDATE_STATE:
            new_state = kwargs["new_state"]
            self._set_current_state(agent_id, new_state, t=t)
            return {"success": True, "new_state": new_state}

        # UPDATE_SHORT_TERM_GOAL — same write pattern as UPDATE_STATE, but
        # targets ``agent.short_term_goal`` (5th segment of system prompt).
        # Tolerate upstream OASIS ActionType not having this member yet by
        # falling back to a string compare on the tool-call name.
        ustg = getattr(ActionType, "UPDATE_SHORT_TERM_GOAL", None)
        if (
            (ustg is not None and action_type == ustg)
            or action_type == "update_short_term_goal"
        ):
            new_goal = kwargs["new_goal"]
            self._set_short_term_goal(agent_id, new_goal)
            return {"success": True, "new_goal": new_goal}

        # 5. No-op (kernel-internal; not routed to pools / buses).
        do_nothing_t = getattr(ActionType, "DO_NOTHING", None)
        if do_nothing_t is not None and action_type == do_nothing_t:
            return {"success": True, "noop": True}

        # 6. FEED actions (pool router) ----------------------------------
        if action_type in _FEED_ACTIONS:
            ok = await self.pools.dispatch(action_type, sender=agent_id, **kwargs)
            return {"success": bool(ok) if ok is not None else True}

        # Unknown action -> consistent failure envelope (B4 silent path).
        raise _UnknownAction(f"no handler for {action_type!r}")

    # ------------------------------------------------------------------ #
    # MOVE queue + commit (LAYOUT §6.1 step 9)                           #
    # ------------------------------------------------------------------ #

    def enqueue_move(
        self, agent_id: int, place_id: Any, t: int = 0
    ) -> None:
        """Append a REQUEST_MOVE to ``pending_moves`` for the round-end flush."""
        self.pending_moves.append((int(agent_id), place_id, int(t)))

    async def commit_pending_moves(self, t: int) -> List[Dict[str, Any]]:
        """Drain ``pending_moves`` (LAYOUT §6.1 step 9).

        For each queued move:
          1. Resolve the agent's current place ``old`` from PlaceStore.
          2. Fire the BehaviorCompressor MOVE pre-hook (``on_move``) so the
             raw segment from the prior place is summarised before the
             location actually changes (LAYOUT §6.1 / N4).
          3. Call ``places.move(agent, new)``.
          4. On capacity-conflict / KeyError / generic failure: silently
             record the failure and continue (LAYOUT B9).

        Returns a per-move status list (useful for tests / world_step).
        """
        results: List[Dict[str, Any]] = []
        moves = self.pending_moves
        self.pending_moves = []

        places = self._places()
        for agent_id, new_place, attempted_at in moves:
            old_place = places.L_t(agent_id) if places is not None else None

            # MOVE pre-hook: behaviour compressor summarises the segment
            # accumulated under ``old_place`` before we commit.
            if self.compressor is not None:
                try:
                    await self.compressor.on_move(agent_id, old_place, new_place)
                except Exception as e:  # noqa: BLE001 - pre-hook never blocks MOVE
                    logger.warning(
                        "compressor.on_move failed agent=%s err=%s -- continuing",
                        agent_id, e,
                    )

            # Actual move; capacity-conflict / unknown-place is silent (B9).
            ok = await self._do_move(agent_id, new_place, t)
            results.append({
                "agent_id": agent_id,
                "new_place": new_place,
                "old_place": old_place,
                "success": ok,
                "attempted_at": attempted_at,
            })
        return results

    # ------------------------------------------------------------------ #
    # internals                                                          #
    # ------------------------------------------------------------------ #

    async def _do_move(self, agent_id: int, new_place: Any, t: int) -> bool:
        places = self._places()
        if places is None:
            return False
        old = places.L_t(agent_id) if hasattr(places, "L_t") else None
        try:
            await places.move(agent_id, new_place, world=self.world, t=t)
            logger.info(
                "MOVE agent=%s %s -> %s @ t=%s", agent_id, old, new_place, t,
            )
            return True
        except Exception as e:  # noqa: BLE001 - capacity / unknown / hook
            logger.info(
                "REQUEST_MOVE silent fail agent=%s -> %s err=%s",
                agent_id, new_place, e,
            )
            return False

    async def _relation_change(
        self, agent_id: int, t: int, kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        relations = getattr(self.world, "relations", None)
        if relations is None:
            return {"success": False, "reason": "no_relation_graph"}
        op = (kwargs.get("op") or "add").lower()
        src = int(kwargs.get("src", agent_id))
        dst = int(kwargs["dst"])
        rtype = kwargs["relation_type"]
        if op == "remove":
            await relations.remove(src, dst, rtype, t=t)
        else:
            await relations.add(
                src, dst, rtype, t=t, metadata=kwargs.get("metadata")
            )
        return {"success": True, "op": op}

    async def _capability_change(
        self, agent_id: int, t: int, kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        caps = getattr(self.world, "caps", None)
        if caps is None:
            return {"success": False, "reason": "no_capability_table"}
        op = (kwargs.get("op") or "grant").lower()
        target = int(kwargs.get("agent_id", agent_id))
        cap = kwargs["capability"]
        if op == "revoke":
            await caps.revoke(target, cap, world=self.world, t=t)
        else:
            await caps.grant(
                target, cap, world=self.world, t=t,
                metadata=kwargs.get("metadata"),
            )
        return {"success": True, "op": op}

    def _set_current_state(
        self, agent_id: int, new_state: str, t: Optional[int] = None,
    ) -> None:
        """Write ``current_state`` directly (LAYOUT B5; bypasses Bus).

        ``t`` is forwarded so the world can stamp the write tick for
        staleness detection in :class:`PerceptionBuilder`.
        """
        # Preferred API (per task spec).
        setter = getattr(self.world, "set_current_state", None)
        if callable(setter):
            try:
                setter(agent_id, new_state, t=t)
            except TypeError:
                # World implementation may not accept ``t`` yet; fall back.
                setter(agent_id, new_state)
            return
        # Fallback: poke ``world.agents[agent_id].current_state`` directly.
        agents = getattr(self.world, "agents", None)
        if agents is not None and agent_id in agents:
            try:
                setattr(agents[agent_id], "current_state", new_state)
                if t is not None:
                    try:
                        setattr(
                            agents[agent_id], "current_state_set_at", int(t),
                        )
                    except AttributeError:
                        pass
                return
            except Exception:  # noqa: BLE001
                pass
        logger.warning(
            "ActionDispatcher: cannot set current_state on world for agent=%s",
            agent_id,
        )

    def _set_short_term_goal(self, agent_id: int, new_goal: str) -> None:
        """Write ``short_term_goal`` directly (mirrors :meth:`_set_current_state`)."""
        setter = getattr(self.world, "set_short_term_goal", None)
        if callable(setter):
            setter(agent_id, new_goal)
            return
        agents = getattr(self.world, "agents", None)
        if agents is not None and agent_id in agents:
            try:
                setattr(agents[agent_id], "short_term_goal", new_goal)
                return
            except Exception:  # noqa: BLE001
                pass
        logger.warning(
            "ActionDispatcher: cannot set short_term_goal on world for agent=%s",
            agent_id,
        )

    def _places(self) -> Optional[Any]:
        return getattr(self.world, "places", None)


__all__ = ["ActionDispatcher"]
