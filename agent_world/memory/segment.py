"""Behavior Segment store (L2, v0.3 NEW).

Per-agent in-memory sliding buffer of raw action / event records that
accumulate between two consecutive behavior compressions (typically
triggered by MOVE; see ``memory.compressor``). The compressor reads the
buffer, summarises it via Haiku, then clears it; the raw entries here are
NOT the audit truth (that lives in world.db.trace / direct_message), so
losing this buffer on crash is acceptable.

LAYOUT v0.3 §2.F segment / §6.1 step 9 / §9.5.1 N1.

Design:
    - One ``List[RawEntry]`` per agent_id; never persisted.
    - ``append`` is fire-and-forget: ActionDispatcher / Bus call it after
      each action lands in world.db.
    - ``over_max`` is the polled fallback trigger -- when True, the
      compressor must drain even though no MOVE has fired (B5 / v0.3
      ``max_raw_actions = 30`` safety net).

Public API (matches caller-side spec, NOT the maximalist variant from
``docs/impl/memory_segment.md``):
    RawEntry(t, kind, payload)
    SegmentStore.append(agent_id, t, kind, payload)
    SegmentStore.get(agent_id) -> list[RawEntry]
    SegmentStore.clear(agent_id) -> None
    SegmentStore.len(agent_id) -> int
    SegmentStore.over_max(agent_id, max_raw_actions=30) -> bool
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class RawEntry:
    """Single raw action / event record kept in an agent's segment.

    Attributes:
        t: world tick at which the action / event occurred.
        kind: short tag, typically the lowercased ActionType value or one
            of ``incoming_message`` / ``overhear`` / ``relation_change`` /
            ``capability_change`` / ``state_change`` / ``group_event``.
        payload: original structured dict (already-translated text MAY be
            stored in ``payload['text']`` by the caller; this module makes
            no assumption).
    """

    t: int
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)


class SegmentStore:
    """``Dict[agent_id, List[RawEntry]]`` in-memory store.

    Not thread-safe by itself; the surrounding async event loop ensures
    serialised access in MVP (single-thread micro-tick loop). If multiple
    coroutines later mutate concurrently, wrap externally with an
    ``asyncio.Lock``.
    """

    def __init__(self, max_raw_actions: int = 30) -> None:
        self._segments: Dict[int, List[RawEntry]] = {}
        self._default_max = int(max_raw_actions)

    # ------------------------------------------------------------------ #
    # write path                                                         #
    # ------------------------------------------------------------------ #

    def append(
        self,
        agent_id: int,
        t: int,
        kind: str,
        payload: Dict[str, Any] | None = None,
    ) -> None:
        """Append one raw entry to ``agent_id``'s current segment.

        The dispatcher / bus calls this after the action has been written
        to world.db (so trace truth is already on disk).
        """
        bucket = self._segments.setdefault(agent_id, [])
        bucket.append(
            RawEntry(t=int(t), kind=str(kind), payload=dict(payload or {}))
        )

    # ------------------------------------------------------------------ #
    # read / inspection                                                  #
    # ------------------------------------------------------------------ #

    def get(self, agent_id: int) -> List[RawEntry]:
        """Return a *copy* of ``agent_id``'s current segment.

        Compressor uses this to assemble its prompt; copying avoids
        accidental mutation of the live buffer between read and clear.
        """
        return list(self._segments.get(agent_id, ()))

    def len(self, agent_id: int) -> int:
        """Number of entries currently buffered for ``agent_id``."""
        return len(self._segments.get(agent_id, ()))

    # Pythonic alias kept around for callers that prefer ``len(store, a)``
    # via a wrapper, plus future test ergonomics.
    length = len

    def over_max(self, agent_id: int, max_raw_actions: int = 30) -> bool:
        """B5 / v0.3 fallback trigger.

        Returns True iff the segment has reached / exceeded the threshold,
        signalling that the compressor must drain even without a MOVE.
        Caller passes ``max_raw_actions`` from config; if absent, uses the
        constructor default (30).
        """
        cap = int(max_raw_actions) if max_raw_actions is not None else self._default_max
        return self.len(agent_id) >= cap

    # ------------------------------------------------------------------ #
    # clear                                                              #
    # ------------------------------------------------------------------ #

    def clear(self, agent_id: int) -> None:
        """Drop all buffered entries for ``agent_id`` (compressor flush)."""
        self._segments.pop(agent_id, None)

    # convenience for tests / report tooling -------------------------- #

    def agents(self) -> List[int]:
        """List of agent_ids currently holding any buffered entries."""
        return [a for a, lst in self._segments.items() if lst]


__all__ = ["RawEntry", "SegmentStore"]
