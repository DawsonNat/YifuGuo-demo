"""``WorldStep`` — the L5 micro-tick orchestrator (LAYOUT §6.1).

Each call to :meth:`WorldStep.run_one_tick` advances ``world.t`` by one
global tick by walking the LAYOUT §6.1 11-step pipeline:

    Phase A (lockstep, in order):
      1. ``script_engine.due_events(world, t)``
      2. ``script_engine.apply(events, world, t)``
      3. ``await pool_manager.update_all()``
      4. ``active = scheduler.pick_active(world, t)``  (None → all agents)
      5. ``await grp_bus.sweep_undelivered(t)``  # B6 persistent queue
      6. group_by_place + ``random.Random(t).shuffle`` per group  # B1 seed

    Phase B (asyncio.gather; place-level concurrency, in-place serial):
      7. ``await asyncio.gather(*[run_place(p, agents) ...])``

    Phase C (lockstep, in order):
      8. (no-op: RDC/GRP ``delivered=0`` already written in step 5+7)
      9. ``await dispatcher.commit_pending_moves(t)``  # compressor.on_move + move
     10. ``manager.flush_all(sim_id)``                 # Zep flush
     11. ``clock.advance(1)``                          # t += 1

MVP design rules:

* Every optional dependency tolerates ``None``.
* Every numbered step is wrapped in ``try/except`` so one failure logs and
  continues; ``world.t`` always advances in step 11 so the sim keeps moving.
* Action shape duck-typed: an Action object (``.type``+``.kwargs``), a dict
  (``{'type','kwargs'}``), or a CAMEL ``response.info['tool_calls']``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


class WorldStep:
    """Central micro-tick orchestrator (LAYOUT §6.1)."""

    def __init__(
        self,
        world_state: Any,
        perception_builder: Any,
        dispatcher: Any,
        script_engine: Any = None,
        pool_manager: Any = None,
        f2f_bus: Any = None,
        rdc_bus: Any = None,
        grp_bus: Any = None,
        segment_store: Any = None,
        compressor: Any = None,
        world_db: Any = None,
        scheduler: Any = None,
        sim_id: Optional[str] = None,
    ) -> None:
        self.world = world_state
        self.perception = perception_builder
        self.dispatcher = dispatcher
        self.script_engine = script_engine
        self.pool_manager = pool_manager
        self.f2f_bus = f2f_bus
        self.rdc_bus = rdc_bus
        self.grp_bus = grp_bus
        self.segments = segment_store
        self.compressor = compressor
        self.world_db = world_db
        self.scheduler = scheduler
        self.sim_id = sim_id

    # ------------------------------------------------------------------ #
    # public entry points                                                #
    # ------------------------------------------------------------------ #

    async def run_one_tick(self) -> Dict[str, Any]:
        """Execute one full 11-step LAYOUT §6.1 pipeline."""
        t = self._current_t()
        report: Dict[str, Any] = {"t": t, "failures": []}

        # ----- Phase A: lockstep prelude --------------------------------- #
        # 1+2: script triggers/effects.
        due_events: List[Any] = []
        if self.script_engine is not None:
            try:
                due_events = list(
                    await _maybe_await(
                        self.script_engine.due_events(self.world, t)
                    ) or []
                )
            except Exception as e:  # noqa: BLE001
                self._record(report, "step1_due_events", e)
            if due_events:
                try:
                    await _maybe_await(
                        self.script_engine.apply(due_events, self.world, t)
                    )
                except Exception as e:  # noqa: BLE001
                    self._record(report, "step2_script_apply", e)

        # 3: pools.update_all (asyncio.gather rec tables internally).
        if self.pool_manager is not None:
            fn = (
                getattr(self.pool_manager, "update_all", None)
                or getattr(self.pool_manager, "update_all_rec_tables", None)
            )
            if fn is not None:
                try:
                    await _maybe_await(fn())
                except Exception as e:  # noqa: BLE001
                    self._record(report, "step3_update_all", e)

        # 4: scheduler.pick_active (or all agents).
        try:
            active_ids = self._pick_active(t)
        except Exception as e:  # noqa: BLE001
            self._record(report, "step4_pick_active", e)
            active_ids = []

        # 5: B6 persistent queue redeliver.
        if self.grp_bus is not None:
            sweep = getattr(self.grp_bus, "sweep_undelivered", None)
            if sweep is not None:
                try:
                    await _maybe_await(sweep(t))
                except Exception as e:  # noqa: BLE001
                    self._record(report, "step5_sweep", e)

        # 6: bucket by place + per-group shuffle (seed = world.t).
        try:
            groups = self._group_and_shuffle(active_ids, t)
        except Exception as e:  # noqa: BLE001
            self._record(report, "step6_group_by_place", e)
            groups = {}

        report["active"] = len(active_ids)
        report["places"] = len(groups)

        # ----- Phase B: micro-tick (place-level gather) ----------------- #
        if groups:
            try:
                results = await asyncio.gather(
                    *[
                        self._run_place(p, agents, t)
                        for p, agents in groups.items()
                    ],
                    return_exceptions=True,
                )
                for place_id, result in zip(groups.keys(), results):
                    if isinstance(result, Exception):
                        logger.error(
                            "step7 run_place(%s) raised: %s", place_id, result
                        )
                        report["failures"].append(
                            ("step7_run_place", place_id, str(result))
                        )
            except Exception as e:  # noqa: BLE001
                self._record(report, "step7_micro_tick", e)

        # ----- Phase C: lockstep post-pass ------------------------------ #
        # 8: no-op (RDC/GRP delivered=0 already in world.db.direct_message).

        # 9: commit pending moves (dispatcher fires compressor.on_move).
        if self.dispatcher is not None:
            commit = getattr(self.dispatcher, "commit_pending_moves", None)
            if commit is not None:
                try:
                    await _maybe_await(commit(t))
                except Exception as e:  # noqa: BLE001
                    self._record(report, "step9_commit_moves", e)

        # 9.5: round-end barrier — wait for any in-flight Haiku compress tasks
        #      so step 10's Zep flush sees the resulting episodes.  LAYOUT
        #      §6.1 step 9 (memory_compressor.md §6).
        if self.compressor is not None:
            await_all = getattr(self.compressor, "await_all_pending", None)
            if await_all is not None:
                try:
                    await _maybe_await(await_all())
                except Exception as e:  # noqa: BLE001
                    self._record(report, "step9b_compressor_barrier", e)

        # 10: Zep flush_all (best-effort; skip if no sim_id).
        try:
            await self._flush_zep()
        except Exception as e:  # noqa: BLE001
            self._record(report, "step10_zep_flush", e)

        # 11: clock advance always runs, even after errors above.
        try:
            self._advance_clock()
        except Exception as e:  # noqa: BLE001
            self._record(report, "step11_advance", e)

        report["next_t"] = self._current_t()
        return report

    async def run_loop(self, num_ticks: int) -> List[Dict[str, Any]]:
        """Call :meth:`run_one_tick` ``num_ticks`` times sequentially."""
        if num_ticks < 0:
            raise ValueError("run_loop: num_ticks must be non-negative")
        return [await self.run_one_tick() for _ in range(int(num_ticks))]

    # ------------------------------------------------------------------ #
    # phase helpers                                                      #
    # ------------------------------------------------------------------ #

    def _pick_active(self, t: int) -> List[int]:
        """Step 4 — agents active this tick (Scheduler or B7 fallback).

        LAYOUT B7 / step.py contract: ``scheduler is None`` → all agents.
        A scheduler that returns ``[]`` keeps the empty list (intentional).
        """
        if self.scheduler is not None:
            picked = self.scheduler.pick_active(self.world, t)
            if picked is None:  # explicit "no opinion" → fallback below
                pass
            else:
                return [
                    aid
                    for aid in (_coerce_agent_id(a) for a in picked)
                    if aid is not None
                ]
        agents = getattr(self.world, "agents", None) or {}
        out: List[int] = []
        for key, agent in agents.items():
            if not _is_active(agent):
                continue
            aid = _coerce_agent_id(key) or _coerce_agent_id(agent)
            if aid is not None:
                out.append(aid)
        return out

    def _group_and_shuffle(
        self, agent_ids: Iterable[int], t: int
    ) -> Dict[str, List[int]]:
        """Bucket by ``L_t`` then shuffle each bucket with ``Random(t)``."""
        groups: Dict[str, List[int]] = {}
        for aid in agent_ids:
            place = self._location_of(aid)
            if place is None:
                continue
            groups.setdefault(place, []).append(aid)
        rng = random.Random(int(t))
        for bucket in groups.values():
            rng.shuffle(bucket)
        return groups

    async def _run_place(
        self, place_id: str, agent_ids: List[int], t: int
    ) -> None:
        """Step 7 inner loop — strict serial decisions within one place.

        The agent's ``perform_action_by_llm`` calls ``perception_builder.build``
        internally (it owns the prompt construction); we just supply ``world``
        and ``t``.  After the decision the dispatcher fans out actions, and
        we advance ``last_message_seen_at`` so the next tick's perception
        only re-emits messages with arrive_at > the just-seen tick.
        """
        for agent_id in agent_ids:
            agent = self._resolve_agent(agent_id)
            if agent is None:
                continue
            # 7.a LLM decision (perception built inside agent.perform_action_by_llm;
            #     B4 retry handled there).
            decision = await self._decide(agent, agent_id, t)
            # Even on decision failure, advance last_message_seen_at so the
            # already-rendered messages don't re-appear next tick.
            try:
                if hasattr(agent, "last_message_seen_at"):
                    agent.last_message_seen_at = int(t)
            except Exception:  # noqa: BLE001
                pass
            if decision is None or isinstance(decision, Exception):
                continue
            # 7.b dispatch each parsed action.
            for atype, akwargs in _extract_actions(decision):
                try:
                    await self.dispatcher.dispatch(
                        agent_id, atype, t, **(akwargs or {})
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "dispatch(agent=%s, action=%s) failed: %s",
                        agent_id, atype, e,
                    )

    async def _decide(self, agent: Any, agent_id: int, t: int) -> Any:
        try:
            return await agent.perform_action_by_llm(self.world, t)
        except TypeError:
            try:
                return await agent.perform_action_by_llm()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "agent %s perform_action_by_llm failed: %s", agent_id, e
                )
                return None
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "agent %s perform_action_by_llm failed: %s", agent_id, e
            )
            return None

    # ------------------------------------------------------------------ #
    # Zep flush + clock                                                  #
    # ------------------------------------------------------------------ #

    async def _flush_zep(self) -> None:
        """Step 10 — flush MultiGraphUpdater ledgers, if configured."""
        manager = self._memory_manager()
        if manager is None or self.sim_id is None:
            return
        flush_all = getattr(manager, "flush_all", None)
        if flush_all is None:
            return
        await _maybe_await(flush_all(self.sim_id))

    def _memory_manager(self) -> Optional[Any]:
        mem = getattr(self.world, "memory", None)
        if mem is not None:
            mgr = getattr(mem, "manager", None) or getattr(
                mem, "multi_graph_updater", None
            )
            if mgr is not None:
                return mgr
        if self.compressor is not None:
            return getattr(self.compressor, "manager", None)
        return None

    def _current_t(self) -> int:
        clock = getattr(self.world, "clock", None)
        if clock is not None and hasattr(clock, "t"):
            return int(clock.t)
        return int(getattr(self.world, "t", 0))

    def _advance_clock(self) -> None:
        clock = getattr(self.world, "clock", None)
        if clock is not None and hasattr(clock, "advance"):
            clock.advance(1)
            return
        if hasattr(self.world, "t"):
            try:
                self.world.t = int(getattr(self.world, "t", 0)) + 1
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # agent resolution                                                   #
    # ------------------------------------------------------------------ #

    def _location_of(self, agent_id: int) -> Optional[str]:
        loc = getattr(self.world, "location_of", None)
        if callable(loc):
            try:
                return loc(agent_id)
            except Exception:  # noqa: BLE001
                pass
        places = getattr(self.world, "places", None)
        if places is not None:
            try:
                return places.L_t(agent_id)
            except Exception:  # noqa: BLE001
                return None
        return None

    def _resolve_agent(self, agent_id: int) -> Optional[Any]:
        agents = getattr(self.world, "agents", None) or {}
        a = agents.get(int(agent_id))
        if a is not None:
            return a
        for k, v in agents.items():
            if (
                _coerce_agent_id(k) == agent_id
                or _coerce_agent_id(v) == agent_id
            ):
                return v
        return None

    # ------------------------------------------------------------------ #

    @staticmethod
    def _record(report: Dict[str, Any], label: str, e: Exception) -> None:
        logger.error("%s failed: %s", label, e)
        report["failures"].append((label, str(e)))


# --------------------------------------------------------------------------- #
# module-level helpers                                                        #
# --------------------------------------------------------------------------- #


def _is_active(agent: Any) -> bool:
    """B7 profile-based gate.  Default: active when no level is declared."""
    prof = getattr(agent, "profile", None)
    for src in (prof, agent):
        if src is None:
            continue
        level = getattr(src, "activity_level", None)
        if level is None and isinstance(src, dict):
            level = src.get("activity_level")
        if level is not None:
            try:
                return float(level) > 0.0
            except (TypeError, ValueError):
                return bool(level)
    return True


def _coerce_agent_id(agent: Any) -> Optional[int]:
    if agent is None:
        return None
    if isinstance(agent, int):
        return agent
    for attr in ("agent_id", "social_agent_id", "id", "user_id"):
        v = getattr(agent, attr, None)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    try:
        return int(agent)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _extract_actions(decision: Any) -> List[Tuple[Any, Dict[str, Any]]]:
    """Coerce ``perform_action_by_llm`` output to ``[(type, kwargs), ...]``."""
    if decision is None:
        return []
    if isinstance(decision, list):
        out: List[Tuple[Any, Dict[str, Any]]] = []
        for item in decision:
            out.extend(_extract_actions(item))
        return out
    # CAMEL response with tool_calls.
    info = getattr(decision, "info", None)
    if isinstance(info, dict) and "tool_calls" in info:
        out = []
        for tc in info.get("tool_calls") or []:
            name = getattr(tc, "tool_name", None) or getattr(tc, "name", None)
            args = (
                getattr(tc, "args", None)
                or getattr(tc, "arguments", None)
                or {}
            )
            if not isinstance(args, dict):
                args = dict(args) if hasattr(args, "items") else {}
            if name is not None:
                out.append((name, args))
        return out
    # Action-like dataclass.
    atype = (
        getattr(decision, "type", None)
        or getattr(decision, "action_type", None)
        or getattr(decision, "tool_name", None)
    )
    akwargs = (
        getattr(decision, "kwargs", None)
        or getattr(decision, "args", None)
        or getattr(decision, "arguments", None)
        or {}
    )
    if not isinstance(akwargs, dict):
        try:
            akwargs = dict(akwargs)
        except Exception:  # noqa: BLE001
            akwargs = {}
    if atype is not None:
        return [(atype, akwargs)]
    # dict shape.
    if isinstance(decision, dict):
        atype = decision.get("type") or decision.get("action_type")
        akwargs = decision.get("kwargs") or decision.get("args") or {}
        if atype is not None and isinstance(akwargs, dict):
            return [(atype, akwargs)]
    return []


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = ["WorldStep"]
