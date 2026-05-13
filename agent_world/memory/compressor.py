"""BehaviorCompressor (L3, v0.3 NEW).

Triggered by ActionDispatcher just BEFORE a MOVE is committed (and as a
fallback when an agent's raw segment overflows ``max_raw_actions``).
Reads the raw entries from ``SegmentStore``, asks Haiku for a 1-3 sentence
summary, then on success:

    1. Appends the summary as a system message into the agent's ChatMemory
       via the wired ``chat_memory_callback`` (ScriptEngine / runner is
       responsible for wiring it; if absent, this step is a no-op).
    2. Enqueues the summary as a Zep episode on ``agent_{id}`` graph via
       ``MultiGraphManager.for_agent(sim_id, agent_id).enqueue(...)``.
    3. Clears the agent's segment.

On Haiku failure / timeout (5s default) the segment is intentionally left
intact so the next MOVE re-runs the compression (LAYOUT v0.3 N4).

LAYOUT v0.3 references:
    * §2.F compressor row
    * §6.1 step 9 (round-end await of pending tasks) and step 10 (Zep flush)
    * §9.5.1 N4 (failure retains raw segment)
    * B5 UPDATE_STATE -- raw segment may include state_change entries

The LLM client is duck-typed: it must expose

    async_complete(model: str, messages: list[dict]) -> str

Anything matching that shape (an Anthropic SDK wrapper, a fake stub for
tests, ...) works. If ``llm_client`` is ``None`` the compressor degrades
to a no-op stub that simply returns: every ``on_move`` becomes a logged
warning and the segment is preserved (raw kept for next round).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("agent_world.memory.compressor")


# --------------------------------------------------------------------------- #
# Type aliases                                                                #
# --------------------------------------------------------------------------- #

# Translates one RawEntry-shaped dict to a single line of natural language.
TranslatorFn = Callable[[Dict[str, Any]], str]

# Append a system message to an agent's ChatMemory. Wired by ScriptEngine
# / runner via ``set_chat_memory_callback``; signature mirrors camel
# ``ChatAgent.update_memory(message, role)``.
ChatMemoryCallback = Callable[[int, str, str], Awaitable[None]]


# --------------------------------------------------------------------------- #
# Compressor                                                                  #
# --------------------------------------------------------------------------- #


class BehaviorCompressor:
    """Async behavioural-segment compressor.

    Args:
        segment_store: shared ``SegmentStore`` (per-agent raw buffer).
        manager: ``MultiGraphManager`` used to obtain the agent's
            ``MultiGraphUpdater`` for Zep enqueue.
        translator_fn: pure function turning a single RawEntry-like dict
            into one line of text. ``memory.translator.translate`` fits.
        llm_client: duck-typed Haiku client exposing
            ``async_complete(model, messages) -> str``. ``None`` means
            stub mode (compression is skipped, raw kept).
        model: Haiku model id; default per LAYOUT §2.F.
        sim_id: simulation id passed through to ``manager.for_agent``.
        timeout_seconds: per-call Haiku timeout (5.0s per task spec).
        summary_max_tokens: hint for the LLM client (caller may ignore).
    """

    DEFAULT_MODEL: str = "claude-haiku-4-5-20251001"
    DEFAULT_TIMEOUT: float = 5.0
    DEFAULT_MAX_RAW_ACTIONS: int = 30

    def __init__(
        self,
        segment_store: Any,
        manager: Any,
        translator_fn: TranslatorFn,
        llm_client: Any = None,
        model: str = DEFAULT_MODEL,
        *,
        sim_id: str = "default",
        timeout_seconds: float = DEFAULT_TIMEOUT,
        summary_max_tokens: int = 200,
    ) -> None:
        self.segment_store = segment_store
        self.manager = manager
        self.translator_fn = translator_fn
        self.llm_client = llm_client
        self.model = model
        self.sim_id = str(sim_id)
        self.timeout_seconds = float(timeout_seconds)
        self.summary_max_tokens = int(summary_max_tokens)

        # ScriptEngine / runner wires this so the compressor can append a
        # system message into the live ChatMemory of the agent. We accept
        # an async callable: ``cb(agent_id, message, role)``.
        self._chat_memory_cb: Optional[ChatMemoryCallback] = None

        # Per-agent re-entry lock + task registry.
        self._locks: Dict[int, asyncio.Lock] = {}
        self._pending: Dict[int, asyncio.Task] = {}

        if llm_client is None:
            logger.warning(
                "BehaviorCompressor initialised without llm_client -- "
                "running in stub mode (compression skipped, raw segment kept)."
            )

    # ------------------------------------------------------------------ #
    # wiring hooks                                                       #
    # ------------------------------------------------------------------ #

    def set_chat_memory_callback(self, cb: Optional[ChatMemoryCallback]) -> None:
        """Wire the agent-registry callback used to append the summary as
        a system message into the agent's ChatMemory (camel
        ``update_memory(message, role)``-compatible).

        Pass ``None`` to unwire.
        """
        self._chat_memory_cb = cb

    # ------------------------------------------------------------------ #
    # public hooks                                                       #
    # ------------------------------------------------------------------ #

    async def on_move(
        self,
        agent_id: int,
        old_place: Any,
        new_place: Any,
    ) -> None:
        """ActionDispatcher hook: fired pre-MOVE.

        Schedules ``_compress`` as a background task so the dispatcher's
        MOVE write path is NOT blocked by the Haiku call (LAYOUT §6.1).
        ``world_step`` step 9 should later ``await await_all_pending()``
        to ensure the next round's PerceptionBuilder reads a stable
        ChatMemory.
        """
        seg = self.segment_store.get(agent_id)
        if not seg:
            return
        task = asyncio.create_task(
            self._compress(agent_id, "move", old_place, new_place),
            name=f"BehaviorCompressor.on_move(agent={agent_id})",
        )
        self._pending[agent_id] = task

    async def force_compress_if_overflow(
        self,
        agent_id: int,
        max_raw_actions: int = DEFAULT_MAX_RAW_ACTIONS,
    ) -> None:
        """Bonus path: drain when the segment has grown past the threshold
        without a MOVE having fired (LAYOUT v0.3 N1 fallback).

        No-op if no overflow, or if a compression task for ``agent_id``
        is already in flight.
        """
        if self.segment_store.len(agent_id) < int(max_raw_actions):
            return
        existing = self._pending.get(agent_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._compress(agent_id, "threshold", None, None),
            name=f"BehaviorCompressor.threshold(agent={agent_id})",
        )
        self._pending[agent_id] = task

    async def await_all_pending(self) -> None:
        """Round-end barrier: wait for every in-flight compression task.

        Called by ``world_step`` step 9 (LAYOUT §6.1) so the next round's
        PerceptionBuilder sees an up-to-date ChatMemory.
        """
        tasks = [t for t in self._pending.values() if not t.done()]
        self._pending.clear()
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)

    def has_pending(self, agent_id: int) -> bool:
        t = self._pending.get(agent_id)
        return t is not None and not t.done()

    # ------------------------------------------------------------------ #
    # internals                                                          #
    # ------------------------------------------------------------------ #

    async def _compress(
        self,
        agent_id: int,
        trigger: str,
        old_place: Any,
        new_place: Any,
    ) -> None:
        lock = self._locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            seg = self.segment_store.get(agent_id)
            if not seg:
                return

            raw_log = self._raw_log(seg)
            prompt = self._build_prompt(raw_log, trigger, old_place, new_place)

            # ---- Haiku call (with timeout) ------------------------------ #
            if self.llm_client is None:
                logger.info(
                    "[stub] compressor would summarise %d raw entries for "
                    "agent=%s (trigger=%s); raw segment kept.",
                    len(seg), agent_id, trigger,
                )
                return  # N4: keep raw

            try:
                summary = await asyncio.wait_for(
                    self.llm_client.async_complete(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                    ),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "compressor Haiku timeout (>%ss) agent=%s trigger=%s "
                    "-- raw segment kept for next MOVE.",
                    self.timeout_seconds, agent_id, trigger,
                )
                return  # N4: keep raw
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "compressor Haiku failed agent=%s trigger=%s err=%s "
                    "-- raw segment kept for next MOVE.",
                    agent_id, trigger, e,
                )
                return  # N4: keep raw

            summary = (summary or "").strip()
            if not summary:
                logger.warning(
                    "compressor produced empty summary agent=%s -- raw kept.",
                    agent_id,
                )
                return

            # ---- Double-write: ChatMemory + Zep ------------------------- #
            try:
                await self._append_to_chat_memory(agent_id, summary)
                await self._enqueue_zep(agent_id, summary, trigger,
                                        old_place, new_place, seg)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "compressor write-side failed agent=%s err=%s "
                    "-- raw segment kept.",
                    agent_id, e,
                )
                return

            # Only clear on full success (LAYOUT N4).
            self.segment_store.clear(agent_id)
            logger.info(
                "compressor OK agent=%s trigger=%s entries=%d summary_len=%d",
                agent_id, trigger, len(seg), len(summary),
            )

    # -- raw log assembly -------------------------------------------------- #

    def _raw_log(self, seg: List[Any]) -> str:
        lines: List[str] = []
        for entry in seg:
            try:
                # Prefer pre-translated text if the caller stashed it on
                # ``payload['text']``; otherwise translate on the fly.
                payload = getattr(entry, "payload", None) or {}
                text = payload.get("text") if isinstance(payload, dict) else None
                if text:
                    lines.append(str(text))
                    continue
                action = dict(payload) if isinstance(payload, dict) else {}
                action.setdefault("t", getattr(entry, "t", None))
                action.setdefault("action_type", getattr(entry, "kind", None))
                lines.append(self.translator_fn(action))
            except Exception as e:  # noqa: BLE001 - never raise into _compress
                lines.append(f"(translate error: {e!s})")
        return "\n".join(lines)

    def _build_prompt(
        self,
        raw_log: str,
        trigger: str,
        old_place: Any,
        new_place: Any,
    ) -> str:
        if trigger == "move":
            edge = (
                f"Agent moved from {old_place} to {new_place}."
                if old_place is not None
                else f"Agent arrived at {new_place}."
            )
        else:
            edge = "Agent's behavior buffer reached its overflow threshold."

        return (
            "Summarize the following actions into 1-3 short sentences:\n\n"
            f"{edge}\n\nRaw events:\n{raw_log}\n\nSummary:"
        )

    # -- write side -------------------------------------------------------- #

    async def _append_to_chat_memory(self, agent_id: int, summary: str) -> None:
        cb = self._chat_memory_cb
        if cb is None:
            logger.debug(
                "no chat_memory_callback wired; skipping ChatMemory append "
                "for agent=%s", agent_id,
            )
            return
        await cb(agent_id, f"[behavior summary] {summary}", "system")

    async def _enqueue_zep(
        self,
        agent_id: int,
        summary: str,
        trigger: str,
        old_place: Any,
        new_place: Any,
        seg: List[Any],
    ) -> None:
        try:
            updater = await self.manager.for_agent(self.sim_id, agent_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "manager.for_agent failed agent=%s err=%s -- summary not "
                "enqueued to Zep.", agent_id, e,
            )
            raise

        t_start = getattr(seg[0], "t", None) if seg else None
        t_end = getattr(seg[-1], "t", None) if seg else None
        await updater.enqueue(
            graph_id=f"agent_{agent_id}",
            episode_text=summary,
            kind="behavior_summary",
            metadata={
                "trigger": trigger,
                "old_place": old_place,
                "new_place": new_place,
                "t_start": t_start,
                "t_end": t_end,
            },
        )


__all__ = ["BehaviorCompressor"]
