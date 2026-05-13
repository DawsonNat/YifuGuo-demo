"""FaceToFaceBus (LAYOUT §2.C / impl/bus_face_to_face.md).

Zero-delay, same-place broadcaster. When an agent emits ``SPEAK_TO_LOCAL``
the dispatcher routes here; we write one ``direct_message`` row
(``channel_type='F2F'``, ``arrive_at=t``, ``delivered=1``) for every
co-located agent (excluding the sender) and a parallel ``overhear`` row
so PerceptionBuilder can surface the utterance both as an incoming
message and as overheard chatter — supporting the micro-tick "next
decider in this place sees the prior speaker's words" guarantee
(LAYOUT §6.1 step 7).

Single-writer rule (LAYOUT B8): every INSERT goes through
``WorldDB.insert_message`` / ``WorldDB.insert_overhear``, both of which
acquire the ``WorldDB._write_lock``. This module owns no additional
lock. Pool traces are not written (LAYOUT §3.4 / A5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:  # pragma: no cover — typing only
    from agent_world.persistence.world_db import WorldDB
    from agent_world.world.clock import Clock
    from agent_world.world.connectivity import ConnectivityResolver
    from agent_world.world.place_store import PlaceStore


class FaceToFaceBus:
    """F2F same-place broadcast bus (no Channel; no extra lock)."""

    def __init__(
        self,
        world_db: "WorldDB",
        places: "PlaceStore",
        connectivity: "ConnectivityResolver",
        clock: "Clock",
    ) -> None:
        self.world_db = world_db
        self.places = places
        self.connectivity = connectivity
        self.clock = clock

    # ------------------------------------------------------------------ send

    async def send(
        self,
        sender_id: int,
        content: str,
        t: int,
    ) -> List[int]:
        """Broadcast ``content`` to every co-located agent at world tick ``t``.

        For each co-located agent (excluding ``sender_id``) we INSERT one
        ``direct_message(channel_type='F2F', arrive_at=t, delivered=1,
        place_id=L_t(sender))`` row, then a matching ``overhear`` row so
        the same agent shows up under both the "incoming" and "overheard"
        perception slices. Returns the inserted ``message_id`` values in
        iteration order.
        """
        place_id = self.places.L_t(sender_id)
        if place_id is None:
            # Sender has no location; defensive — dispatcher should pre-filter.
            return []

        co_located = sorted(self.places.agents_at(place_id) - {sender_id})
        if not co_located:
            return []

        inserted: List[int] = []
        for recipient_id in co_located:
            message_id = await self.world_db.insert_message(
                sender_id=sender_id,
                recipient_id=recipient_id,
                group_id=None,
                channel_type="F2F",
                content=content,
                place_id=place_id,
                attempted_at=t,
                arrive_at=t,
                delivered=1,
            )
            inserted.append(message_id)
            # Brief: "Also write to overhear table for them too."
            await self.world_db.insert_overhear(
                message_id=message_id,
                overhearer_id=recipient_id,
                place_id=place_id,
            )
        return inserted


__all__ = ["FaceToFaceBus"]
