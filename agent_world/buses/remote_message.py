"""RemoteMessageBus (LAYOUT §2.C / impl/bus_remote_message.md).

Lockstep point-to-point bus for ``SEND_MESSAGE``. Validates ``φ_RDC``
(capability ∧ relation ∧ coverage) via :class:`ConnectivityResolver`,
then writes a single ``direct_message`` row (``channel_type='RDC'``).

- Failure (``φ_RDC`` rejects):  ``delivered=0, arrive_at=attempted_at=t``.
  PerceptionBuilder surfaces this row in the sender's
  ``obs.recent_failed_attempts`` next tick (LAYOUT §B9, 1 tick only).
- Success:                       ``delivered=1, arrive_at=t + delay``
  where ``delay`` comes from the coverage matrix (via
  ``connectivity.latency``); the recipient does not see it until
  ``world.t' >= arrive_at`` (lockstep).

Single-writer rule (LAYOUT B8): INSERTs go through
``WorldDB.insert_message`` which owns the lock; this module adds no
lock of its own. Pool trace is **not** written (LAYOUT §3.4 / A5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Optional

if TYPE_CHECKING:  # pragma: no cover — typing only
    from agent_world.persistence.world_db import WorldDB
    from agent_world.world.capability_table import CapabilityTable
    from agent_world.world.clock import Clock
    from agent_world.world.connectivity import ConnectivityResolver
    from agent_world.world.place_store import PlaceStore
    from agent_world.world.relation_graph import RelationGraph


# Default fallback when neither coverage matrix nor channel_config supplies a
# delay (LAYOUT §7.1: F2F=0, RDC=1; we default to 1 so RDC stays lockstep).
_DEFAULT_RDC_DELAY = 1


@dataclass(frozen=True)
class SendResult:
    """Outcome of a single ``RemoteMessageBus.send`` call."""

    success: bool
    arrive_at: int
    message_id: int


class RemoteMessageBus:
    """RDC bus: φ_RDC gate + coverage-aware ``arrive_at`` write."""

    def __init__(
        self,
        world_db: "WorldDB",
        places: "PlaceStore",
        relations: "RelationGraph",
        caps: "CapabilityTable",
        connectivity: "ConnectivityResolver",
        clock: "Clock",
        *,
        channel_config: Optional[Mapping[str, int]] = None,
    ) -> None:
        self.world_db = world_db
        self.places = places
        self.relations = relations
        self.caps = caps
        self.connectivity = connectivity
        self.clock = clock
        # ``channel_config`` is the ``default_delays`` mapping (see LAYOUT §7.1).
        # Accept None for the MVP path; tests / runner can pass a dict.
        self._channel_config: Mapping[str, int] = dict(channel_config or {})

    # ------------------------------------------------------------------ send

    async def send(
        self,
        sender_id: int,
        recipient_id: int,
        content: str,
        t: int,
    ) -> SendResult:
        """Validate φ_RDC and INSERT one ``direct_message`` row.

        Returns a :class:`SendResult` describing success, the resolved
        ``arrive_at`` (``t`` on failure, ``t + delay`` on success) and the
        new ``direct_message.message_id``.
        """
        src_place = self.places.L_t(sender_id)
        # ``dst_place`` is best-effort: φ_RDC false short-circuits if it is None.
        dst_place = self.places.L_t(recipient_id)

        ok = self.connectivity.phi_rdc(sender_id, recipient_id)

        if not ok:
            # B9 silent failure — write a placeholder row so PerceptionBuilder
            # can transmit it in the next tick's obs.recent_failed_attempts.
            message_id = await self.world_db.insert_message(
                sender_id=sender_id,
                recipient_id=recipient_id,
                group_id=None,
                channel_type="RDC",
                content=content,
                place_id=src_place,
                attempted_at=t,
                arrive_at=t,
                delivered=0,
            )
            return SendResult(success=False, arrive_at=t, message_id=message_id)

        # φ_RDC passed → both src/dst places are non-None and reachable.
        delay = self._resolve_delay(src_place, dst_place)
        arrive_at = t + delay

        message_id = await self.world_db.insert_message(
            sender_id=sender_id,
            recipient_id=recipient_id,
            group_id=None,
            channel_type="RDC",
            content=content,
            place_id=src_place,
            attempted_at=t,
            arrive_at=arrive_at,
            delivered=1,
        )
        return SendResult(success=True, arrive_at=arrive_at, message_id=message_id)

    # ------------------------------------------------------------ delay calc

    def _resolve_delay(
        self, src_place: Optional[str], dst_place: Optional[str]
    ) -> int:
        """Three-tier priority: coverage > channel_config[RDC] > default 1.

        ``ConnectivityResolver.latency`` already collapses the coverage
        matrix into "ticks if reachable, ``-1`` if not"; on ``-1`` we fall
        back to the configured channel default and finally to
        ``_DEFAULT_RDC_DELAY``.
        """
        if src_place is not None and dst_place is not None:
            ticks = self.connectivity.latency(src_place, dst_place)
            if ticks >= 0:
                return int(ticks)
        if "RDC" in self._channel_config:
            return int(self._channel_config["RDC"])
        return _DEFAULT_RDC_DELAY


__all__ = ["RemoteMessageBus", "SendResult"]
