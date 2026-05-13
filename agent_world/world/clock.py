"""Agent World global Clock.

Thin shim that wraps OASIS ``oasis.clock.clock.Clock`` so the canonical
import path ``agent_world.world.clock.Clock`` resolves to the upstream
implementation (KEEP as-is per ``docs/impl/world_clock.md``).

Semantics (LAYOUT B2 / v0.3):
    * Single global integer tick ``t``.
    * ``t >= 0``; monotonically non-decreasing.
    * ``advance(delta=1)`` is the ONLY writer (called by ``WorldStep``
      step 11). All other modules read ``world.clock.t`` only.
    * No wall-clock; no per-place clocks. ``place.attrs.timezone`` is
      narrative-only and does not influence ticks.

Channel delays are NOT a Clock concern; they live in
``ConnectivityResolver`` / Bus modules and only consume ``t``.
"""

from __future__ import annotations

# We try to wrap the upstream OASIS Clock when available, but the OASIS
# package's ``__init__.py`` pulls heavy ML deps (sentence_transformers, sklearn,
# torch).  For demos / tests that don't need OASIS Platform, we fall back to a
# minimal local Clock so importing ``agent_world.world.clock`` stays cheap.
try:  # pragma: no cover — env-dependent
    from oasis.clock.clock import Clock as _OasisClock  # type: ignore
except Exception:  # noqa: BLE001
    from datetime import datetime

    class _OasisClock:  # type: ignore[no-redef]
        """Minimal stand-in for ``oasis.clock.clock.Clock``."""

        def __init__(self, k: int = 1) -> None:
            self.real_start_time = datetime.now()
            self.k = k
            self.time_step = 0


class Clock(_OasisClock):
    """Single global simulation clock.

    Adds the Agent World contract on top of the OASIS Clock:
        * ``t: int``  — current tick (alias of ``self.time_step``).
        * ``advance(delta: int = 1) -> int`` — bump ``t`` and return it.

    The OASIS Clock keeps an integer ``time_step`` field; we expose it as
    ``t`` (read/write) so SQL writers and perception filters can use the
    short name documented in ``world_clock.md``.
    """

    def __init__(self, t0: int = 0, k: int = 1) -> None:
        super().__init__(k=k)
        self.time_step = int(t0)

    @property
    def t(self) -> int:
        """Current tick. Read by every module; written only by ``advance``."""
        return int(self.time_step)

    @t.setter
    def t(self, value: int) -> None:
        self.time_step = int(value)

    def advance(self, delta: int = 1) -> int:
        """Advance ``t`` by ``delta`` (default 1). Returns the new ``t``.

        Sole writer: ``WorldStep._phase_c_lockstep_post`` step 11.
        """
        if delta < 0:
            raise ValueError("Clock.advance: delta must be non-negative")
        self.time_step = int(self.time_step) + int(delta)
        return self.time_step


__all__ = ["Clock"]
