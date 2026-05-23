"""Place-wide system broadcast for HBM demo (Runner-side only)."""

from __future__ import annotations

from typing import Any


async def broadcast_place(
    world_db: Any,
    place_store: Any,
    place_id: str,
    message: str,
    *,
    t: int,
) -> int:
    """Insert one RDC row per agent at ``place_id`` (sender_id=-1, delivered=1).

    Returns the number of messages inserted.
    """
    recipients = place_store.agents_at(place_id)
    count = 0
    for agent_id in recipients:
        await world_db.insert_message(
            sender_id=-1,
            recipient_id=int(agent_id),
            group_id=None,
            channel_type="RDC",
            content=str(message),
            place_id=place_id,
            attempted_at=int(t),
            arrive_at=int(t),
            delivered=1,
        )
        count += 1
    return count
