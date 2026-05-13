"""``actions.jsonl`` writer (LAYOUT §2.I + v0.3 schema).

COPY + ADAPT from MiroFish ``run_parallel_simulation.py`` (inline
``action_logger``). Extracted to its own module so the L5 runner can
unit-test it independently and so other tooling (replay / report agent)
can read it without depending on the runner.

v0.3 schema additions (LAYOUT §2.I + §3.2 ``direct_message`` DDL):

* ``place_id``     -- sender's location at attempt time
* ``channel_type`` -- ``"F2F" | "RDC" | "GRP"`` (None for FEED actions)
* ``arrive_at``    -- delivery tick (None when the action isn't a message)
* ``attempted_at`` -- ``world.t`` at which the action was attempted
* ``delivered``    -- ``-1`` cancelled / ``0`` failed / ``1`` delivered

JSONL format: one ``json.dumps`` per line, append-only. Buffered with
optional batch + interval flush so the file stays consistent for the
Flask-side ``simulation_runner`` tail reader.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger("agent_world.runner.action_logger")


# Required keys; callers can pass partial dicts and we fill the rest with
# safe defaults so the runner never has to remember the full v0.3 schema.
_REQUIRED_KEYS: tuple[str, ...] = (
    "t",
    "agent_id",
    "action_type",
    "place_id",
    "channel_type",
    "arrive_at",
    "attempted_at",
    "delivered",
    "payload",
)


class ActionLogger:
    """Append-only ``actions.jsonl`` writer.

    Args:
        path: target file path (will be created if missing).
        flush_interval: max seconds between auto-flushes when buffered.
        batch_size: flush the buffer once it reaches this many records.

    Thread-safety: a ``threading.Lock`` guards both the buffer and the
    open file handle so concurrent ``log()`` calls from different
    coroutines (running in the same thread under asyncio) and from the
    optional flush daemon never interleave a single record.
    """

    def __init__(
        self,
        path: str,
        flush_interval: float = 1.0,
        batch_size: int = 10,
    ) -> None:
        self.path = path
        self.flush_interval = float(flush_interval)
        self.batch_size = int(batch_size)

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # ``open(..., "a")`` is line-buffered when ``buffering=1``.
        self._fh = open(path, "a", encoding="utf-8", buffering=1)

        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._last_flush_ts = time.time()
        self._closed = False

    # --------------------------------------------------------------- API

    def log(self, action: Dict[str, Any]) -> None:
        """Append one action record to the buffer (auto-flushes by batch).

        Missing v0.3 fields are filled with ``None`` defaults; ``payload``
        defaults to ``{}``. Mutates a shallow copy of ``action``; the
        caller's dict is left untouched.
        """
        record = self._normalize(action)
        line = json.dumps(record, ensure_ascii=False, default=_json_default)
        with self._lock:
            if self._closed:
                logger.debug("ActionLogger.log on closed logger; dropping")
                return
            self._buf.append(line)
            now = time.time()
            if (
                len(self._buf) >= self.batch_size
                or now - self._last_flush_ts >= self.flush_interval
            ):
                self._flush_locked()

    # MiroFish-compatible alias.
    append = log

    def log_many(self, actions: Iterable[Dict[str, Any]]) -> None:
        """Bulk-log a sequence of actions (single flush)."""
        records = [self._normalize(a) for a in actions]
        if not records:
            return
        lines = [
            json.dumps(r, ensure_ascii=False, default=_json_default)
            for r in records
        ]
        with self._lock:
            if self._closed:
                return
            self._buf.extend(lines)
            self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._flush_locked()
            try:
                self._fh.close()
            except OSError as exc:
                logger.warning("ActionLogger close failed (%s): %s", self.path, exc)
            self._closed = True

    # ------------------------------------------------------------- helpers

    def _normalize(self, action: Dict[str, Any]) -> Dict[str, Any]:
        record: Dict[str, Any] = dict(action) if action else {}
        for key in _REQUIRED_KEYS:
            record.setdefault(key, None)
        if record.get("payload") is None:
            record["payload"] = {}
        # ``attempted_at`` is the only field we promise is non-None.
        if record.get("attempted_at") is None:
            record["attempted_at"] = record.get("t", 0)
        return record

    def _flush_locked(self) -> None:
        if not self._buf:
            self._last_flush_ts = time.time()
            return
        try:
            self._fh.write("\n".join(self._buf) + "\n")
            self._fh.flush()
        except OSError as exc:
            logger.error("ActionLogger flush failed (%s): %s", self.path, exc)
            return
        self._buf.clear()
        self._last_flush_ts = time.time()

    # context-manager sugar --------------------------------------------------

    def __enter__(self) -> "ActionLogger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _json_default(value: Any) -> Any:
    """Best-effort JSON encoder fallback for dataclasses / enums / sets."""
    # dataclasses
    try:
        import dataclasses

        if dataclasses.is_dataclass(value):
            return dataclasses.asdict(value)
    except Exception:  # noqa: BLE001
        pass
    # enums
    val = getattr(value, "value", None)
    if val is not None and isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(value, set):
        return sorted(value)
    return repr(value)


__all__ = ["ActionLogger"]
