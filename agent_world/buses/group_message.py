"""GroupMessageBus (L3, LAYOUT §2.C / §3.2 / §6.1 step 5 / B6).

Self-written SQL bus for group lifecycle + group fan-out.  Owns no state of
its own; persists via :class:`WorldDB` (which holds the single-writer lock,
LAYOUT B8).  Five public methods cover the agent-action surface
(``create_group / join_group / leave_group / kick_member / send_to_group``)
and one tick-driven method (``sweep_undelivered``) implements the B6
persistent-queue retry called by ``WorldStep`` step 5.

Key behaviours:

- Each ``send_to_group`` writes one ``group_message`` plus one
  ``direct_message`` row per non-sender member; rows whose recipient is not
  reachable via ``phi_grp`` (or for whom the sender lacks group reachability)
  land with ``delivered=0`` and wait for the next sweep.
- ``leave_group`` / ``kick_member`` clear that recipient's pending unread
  rows (``DELETE ... WHERE delivered=0``) so the sweep cannot re-deliver
  group messages to a former member.
- ``create_group / join_group / leave_group / kick_member`` write a
  ``group_event`` row whose PerceptionBuilder one-tick passthrough drives
  ``obs.group_events``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover — type-checking only
    from agent_world.persistence.world_db import WorldDB
    from agent_world.world.clock import Clock
    from agent_world.world.connectivity import ConnectivityResolver
    from agent_world.world.place_store import PlaceStore


# Default GRP delay (ticks) when no ChannelConfig is wired in. Mirrors
# ``ChannelConfig.default_delays.GRP`` (LAYOUT §7.1, default 1).
_DEFAULT_GRP_DELAY = 1


class GroupMessageBus:
    """Many-to-many group-chat bus backed by ``world.db`` (LAYOUT §2.C)."""

    def __init__(
        self,
        world_db: "WorldDB",
        places: "PlaceStore",
        connectivity: "ConnectivityResolver",
        clock: "Clock",
        *,
        grp_delay: int = _DEFAULT_GRP_DELAY,
    ) -> None:
        self.world_db = world_db
        self.places = places
        self.connectivity = connectivity
        self.clock = clock
        self.grp_delay = int(grp_delay)

    # ------------------------------------------------------------------ #
    # group lifecycle
    # ------------------------------------------------------------------ #

    async def create_group(self, creator_id: int, name: str) -> int:
        """Create a chat_group, add ``creator_id`` as first member, write a
        'join' group_event.  Returns the new ``group_id``."""
        t = self.clock.t
        group_id = await self.world_db.insert_group(name)
        await self.world_db.insert_group_member(group_id, creator_id)
        await self.world_db.insert_group_event(
            group_id=group_id,
            agent_id=creator_id,
            event_type="join",
            occurred_at=t,
            actor_id=creator_id,
        )
        # Mirror membership into ConnectivityResolver so phi_grp resolves.
        self._touch_connectivity_members(group_id)
        return group_id

    async def join_group(self, agent_id: int, group_id: int) -> bool:
        """Insert group_member + 'join' group_event.  Returns False when
        already a member (membership table uses INSERT OR IGNORE)."""
        existing = self.world_db.list_group_members(group_id)
        if agent_id in existing:
            return False
        t = self.clock.t
        await self.world_db.insert_group_member(group_id, agent_id)
        await self.world_db.insert_group_event(
            group_id=group_id,
            agent_id=agent_id,
            event_type="join",
            occurred_at=t,
            actor_id=agent_id,
        )
        self._touch_connectivity_members(group_id)
        return True

    async def leave_group(self, agent_id: int, group_id: int) -> bool:
        """Delete group_member + 'leave' group_event + purge unread (B6).
        Returns False when ``agent_id`` was not a member."""
        rows = await self.world_db.delete_group_member(group_id, agent_id)
        if rows == 0:
            return False
        t = self.clock.t
        await self.world_db.insert_group_event(
            group_id=group_id,
            agent_id=agent_id,
            event_type="leave",
            occurred_at=t,
            actor_id=agent_id,
        )
        await self._purge_unread_for(agent_id, group_id)
        self._touch_connectivity_members(group_id)
        return True

    async def kick_member(
        self, actor_id: int, agent_id: int, group_id: int
    ) -> bool:
        """Delete target member, write 'kick' event with ``actor_id`` as
        initiator, purge target's unread group rows.  Returns False when
        target was not a member.  MVP does no admin authorisation; the
        dispatcher / scripts decide who may call this."""
        rows = await self.world_db.delete_group_member(group_id, agent_id)
        if rows == 0:
            return False
        t = self.clock.t
        await self.world_db.insert_group_event(
            group_id=group_id,
            agent_id=agent_id,
            event_type="kick",
            occurred_at=t,
            actor_id=actor_id,
        )
        await self._purge_unread_for(agent_id, group_id)
        self._touch_connectivity_members(group_id)
        return True

    # ------------------------------------------------------------------ #
    # message fan-out
    # ------------------------------------------------------------------ #

    async def send_to_group(
        self, sender_id: int, group_id: int, content: str, t: int
    ) -> int:
        """Write the canonical ``group_message`` row plus one
        ``direct_message`` per non-sender member.  Members for whom either
        ``phi_grp(sender, group_id)`` or coverage from the sender's place to
        the recipient's place fails get ``delivered=0`` so the next
        ``sweep_undelivered`` can retry.  Returns ``group_message.message_id``."""
        msg_id = await self.world_db.insert_group_message(
            group_id, sender_id, content
        )

        sender_place = self.places.L_t(sender_id)
        sender_reachable = (
            sender_place is not None
            and self.connectivity.phi_grp(sender_id, group_id)
        )

        members = self.world_db.list_group_members(group_id)
        for member_id in members:
            if member_id == sender_id:
                continue
            delivered, arrive_at = self._resolve_delivery(
                sender_place, sender_reachable, member_id, group_id, t
            )
            await self.world_db.insert_message(
                sender_id=sender_id,
                recipient_id=member_id,
                group_id=group_id,
                channel_type="GRP",
                content=content,
                place_id=sender_place,
                attempted_at=t,
                arrive_at=arrive_at,
                delivered=delivered,
            )
        return msg_id

    async def sweep_undelivered(self, t: int) -> int:
        """Retry the B6 persistent queue.  For every undelivered group
        ``direct_message`` whose recipient is now reachable via ``phi_grp``,
        flip ``delivered=1`` and set ``arrive_at=t``.  Returns the number of
        rows promoted.  Called by ``WorldStep`` step 5."""
        candidates = self.world_db.fetch_undelivered_group_messages()
        if not candidates:
            return 0
        ready: list[tuple[int, int]] = []
        for message_id, recipient_id, group_id in candidates:
            if self.connectivity.phi_grp(recipient_id, group_id):
                ready.append((message_id, t))
        if not ready:
            return 0
        return await self.world_db.sweep_undelivered(ready)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _resolve_delivery(
        self,
        sender_place: Optional[str],
        sender_reachable: bool,
        member_id: int,
        group_id: int,
        t: int,
    ) -> tuple[int, int]:
        """Decide ``(delivered, arrive_at)`` for one member fan-out copy."""
        if not sender_reachable or sender_place is None:
            return 0, t
        member_place = self.places.L_t(member_id)
        if member_place is None:
            return 0, t
        latency = self.places.coverage_latency(sender_place, member_place)
        if latency is None:
            return 0, t
        if not self.connectivity.phi_grp(member_id, group_id):
            return 0, t
        # Use configured GRP delay (MVP) instead of per-edge latency to keep
        # group fan-out timing uniform; per-member latency can replace this
        # later without changing the public API.
        return 1, t + self.grp_delay

    async def _purge_unread_for(self, agent_id: int, group_id: int) -> None:
        """LAYOUT B6: clear pending direct_message rows for ``agent_id`` in
        ``group_id``.  Uses ``UPDATE delivered=-1`` (the WorldDB helper
        provided) which both removes them from the sweep candidate set and
        from PerceptionBuilder's incoming filter (which keeps only
        ``delivered=1`` rows)."""
        await self.world_db.purge_undelivered_for_group(group_id, agent_id)

    def _touch_connectivity_members(self, group_id: int) -> None:
        """Sync membership into the ConnectivityResolver shim so phi_grp
        sees the latest member set immediately after a write.  Safe no-op
        when the resolver is not the in-memory MVP variant."""
        setter = getattr(self.connectivity, "set_group_members", None)
        if setter is None:
            return
        try:
            members = self.world_db.list_group_members(group_id)
        except Exception:  # noqa: BLE001 — connectivity sync is best-effort
            return
        setter(group_id, members)


__all__ = ["GroupMessageBus"]
