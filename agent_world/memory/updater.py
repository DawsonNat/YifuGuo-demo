"""MultiGraphUpdater (L2, ADAPTed from MiroFish ZepGraphMemoryUpdater).

Source: ``ref/MiroFish/backend/app/services/zep_graph_memory_updater.py``
        L202-477 (single-graph, platform-keyed buffers, threading worker).

Adaptations for Agent World v0.3 (LAYOUT §2.F):
    1. ``_platform_buffers`` key changed from ``platform`` (str) to
       ``(graph_id, kind)`` (tuple). One Agent World simulation has many
       graph_ids (``agent_{id}`` / ``place_{id}`` / ``world``); each plus
       its kind owns an independent buffer.
    2. The per-action write path is REMOVED. Writes are triggered by the
       ``BehaviorCompressor`` (L3) only -- it calls ``enqueue(graph_id,
       episode_text)`` after a Haiku summary completes. ``translator.py``
       no longer writes Zep directly (LAYOUT v0.3 §2.F note).
    3. ``BATCH_SIZE`` / ``SEND_INTERVAL`` constants kept, default values
       slightly raised (see class attrs) but tunable via config layer.
    4. The Zep client is duck-typed: if no real ``zep_cloud.client.Zep``
       is wired in (MVP / dry-run), pass ``client=None`` and the updater
       degrades to a no-op flush (still buffers, still logs).

Public API:
    MultiGraphUpdater(client, sim_id, *, batch_size=20, send_interval=5.0)
        .start()              -> async, spawn background worker
        .stop()               -> async, flush + cancel worker
        .enqueue(graph_id, episode_text, *, kind="behavior_summary",
                 metadata=None)
        .flush()              -> async, drain ALL buckets now (called by
                                 world_step at round end)
        .buffer_size(graph_id, kind="behavior_summary") -> int
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent_world.memory.updater")


@dataclass
class BufferedEpisode:
    """One queued Zep episode, post-translation."""

    graph_id: str
    kind: str
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.monotonic)


class MultiGraphUpdater:
    """Async batched Zep writer keyed by ``(graph_id, kind)``.

    A single instance serves one ``sim_id``; the surrounding
    ``MultiGraphManager`` is the per-process registry that hands out
    instances.
    """

    BATCH_SIZE: int = 20
    SEND_INTERVAL: float = 5.0
    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 2.0  # seconds; exponential backoff multiplier
    WORKER_TICK: float = 0.5  # how often the loop wakes up

    def __init__(
        self,
        client: Any,
        sim_id: str,
        *,
        batch_size: Optional[int] = None,
        send_interval: Optional[float] = None,
    ) -> None:
        self.client = client  # duck-typed; may be None in dry-run
        self.sim_id = str(sim_id)
        if batch_size is not None:
            self.BATCH_SIZE = int(batch_size)
        if send_interval is not None:
            self.SEND_INTERVAL = float(send_interval)

        # buffers + per-key locks
        self._buffers: Dict[Tuple[str, str], List[BufferedEpisode]] = {}
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._last_flush_at: Dict[Tuple[str, str], float] = {}

        # worker control
        self._running: bool = False
        self._worker_task: Optional[asyncio.Task] = None

        # stats
        self._total_enqueued: int = 0
        self._total_sent: int = 0   # episodes successfully written
        self._failed_count: int = 0

        if self.client is None:
            logger.warning(
                "MultiGraphUpdater(sim_id=%s) initialised without a Zep client "
                "-- running in dry-run mode (enqueues kept in-memory only).",
                self.sim_id,
            )
        else:
            logger.info(
                "MultiGraphUpdater initialised: sim_id=%s batch=%d interval=%.1fs",
                self.sim_id, self.BATCH_SIZE, self.SEND_INTERVAL,
            )

    # ------------------------------------------------------------------ #
    # lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Spawn the background worker loop (idempotent)."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(
            self._worker_loop(), name=f"MultiGraphUpdater-{self.sim_id}"
        )
        logger.info("MultiGraphUpdater started: sim_id=%s", self.sim_id)

    async def stop(self) -> None:
        """Stop the worker, flushing remaining buffers."""
        self._running = False
        try:
            await self.flush()
        except Exception as e:  # noqa: BLE001
            logger.error("flush() during stop failed: %s", e)

        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

        logger.info(
            "MultiGraphUpdater stopped: sim_id=%s enqueued=%d sent=%d failed=%d",
            self.sim_id, self._total_enqueued, self._total_sent, self._failed_count,
        )

    # ------------------------------------------------------------------ #
    # producer-side API                                                  #
    # ------------------------------------------------------------------ #

    async def enqueue(
        self,
        graph_id: str,
        episode_text: str,
        *,
        kind: str = "behavior_summary",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a single episode to the (graph_id, kind) buffer.

        Called by ``BehaviorCompressor`` after a Haiku summary completes.
        If the bucket reaches ``BATCH_SIZE``, an immediate flush is
        scheduled (does not block the caller).
        """
        if not episode_text:
            return
        key = (str(graph_id), str(kind))
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            buf = self._buffers.setdefault(key, [])
            buf.append(
                BufferedEpisode(
                    graph_id=str(graph_id),
                    kind=str(kind),
                    text=str(episode_text),
                    metadata=dict(metadata or {}),
                )
            )
            self._total_enqueued += 1
            should_flush = len(buf) >= self.BATCH_SIZE
        if should_flush:
            # fire-and-forget; don't block the caller
            asyncio.create_task(self._flush_one(key))

    async def flush(self) -> None:
        """Synchronously flush every non-empty buffer (round-end hook)."""
        keys = list(self._buffers.keys())
        if not keys:
            return
        await asyncio.gather(
            *[self._flush_one(k) for k in keys], return_exceptions=True
        )

    # backward-compat alias matching docs §5.1
    flush_all = flush

    def buffer_size(self, graph_id: str, kind: str = "behavior_summary") -> int:
        return len(self._buffers.get((str(graph_id), str(kind)), ()))

    # ------------------------------------------------------------------ #
    # internals                                                          #
    # ------------------------------------------------------------------ #

    async def _worker_loop(self) -> None:
        """Periodic flusher; wakes every WORKER_TICK seconds."""
        try:
            while self._running:
                await asyncio.sleep(self.WORKER_TICK)
                now = time.monotonic()
                for key in list(self._buffers.keys()):
                    buf = self._buffers.get(key) or []
                    last = self._last_flush_at.get(key, 0.0)
                    if buf and (now - last) >= self.SEND_INTERVAL:
                        # don't await here; isolate buckets
                        asyncio.create_task(self._flush_one(key))
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("MultiGraphUpdater worker crashed: %s", e)

    async def _flush_one(self, key: Tuple[str, str]) -> None:
        """Drain one (graph_id, kind) bucket and ship it to Zep."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            buf = self._buffers.get(key) or []
            if not buf:
                return
            drained = list(buf)
            self._buffers[key] = []
            self._last_flush_at[key] = time.monotonic()

        graph_id, kind = key
        # MiroFish merges all buffered episodes for one graph into a single
        # newline-joined payload to amortise RTT; preserve that behaviour.
        combined = "\n".join(ep.text for ep in drained)

        if self.client is None:
            logger.debug(
                "[dry-run] would write %d episode(s) to graph_id=%s kind=%s",
                len(drained), graph_id, kind,
            )
            self._total_sent += len(drained)
            return

        for attempt in range(self.MAX_RETRIES):
            try:
                # Zep SDK shape: client.graph.add(graph_id=..., type=..., data=...)
                # Some SDK forks expose ``add_episode`` or async variants;
                # try ``graph.add`` first, fall back gracefully.
                graph_api = getattr(self.client, "graph", None)
                if graph_api is None:
                    raise RuntimeError("zep client has no `.graph` attribute")
                add = getattr(graph_api, "add", None)
                if add is None:
                    raise RuntimeError("zep client.graph has no `.add`")
                result = add(graph_id=graph_id, type="text", data=combined)
                # support both sync + async SDKs
                if asyncio.iscoroutine(result):
                    await result
                self._total_sent += len(drained)
                logger.info(
                    "Zep write OK: graph_id=%s kind=%s episodes=%d",
                    graph_id, kind, len(drained),
                )
                return
            except Exception as e:  # noqa: BLE001
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(
                        "Zep write failed (attempt %d/%d) graph_id=%s: %s",
                        attempt + 1, self.MAX_RETRIES, graph_id, e,
                    )
                    await asyncio.sleep(self.RETRY_DELAY * (attempt + 1))
                else:
                    logger.error(
                        "Zep write failed after %d retries graph_id=%s: %s",
                        self.MAX_RETRIES, graph_id, e,
                    )
                    self._failed_count += 1
                    # MVP: drop the batch (LAYOUT N4)
                    return

    # ------------------------------------------------------------------ #
    # diagnostics                                                        #
    # ------------------------------------------------------------------ #

    def get_stats(self) -> Dict[str, Any]:
        return {
            "sim_id": self.sim_id,
            "running": self._running,
            "batch_size": self.BATCH_SIZE,
            "send_interval": self.SEND_INTERVAL,
            "total_enqueued": self._total_enqueued,
            "total_sent": self._total_sent,
            "failed": self._failed_count,
            "buffers": {f"{g}|{k}": len(v) for (g, k), v in self._buffers.items()},
        }


__all__ = ["MultiGraphUpdater", "BufferedEpisode"]
