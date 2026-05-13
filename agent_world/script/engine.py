"""``ScriptEngine`` —— 剧本驱动器 (L3).

LAYOUT §2.E / impl/script_engine.md:

* WorldStep 主循环步骤 1-2: ``due_events(world, t)`` → ``apply(due, world, t)``.
* IPC ``RELOAD_SCRIPTS`` / 启动期: ``load_from_yaml(path)`` 增量追加新事件 (C2).
* PerceptionBuilder: ``pending_for(agent_id)`` 拉取本轮的 scripted_notification.
* ActionDispatcher: ``notify_action(actor_id, action_type, payload)`` 推送 OnAction.

设计原则:

* engine 仅 *orchestrate* trigger / effect; 不解析 YAML (loader 的活), 不写
  side effect (effect 的活), 不过滤 schema (registrar 的活).
* ``loaded_event_ids`` ⊇ ``applied_events``; 任一 id 进入 ``applied_events``
  之后, 即使 reload 重读也跳过.
* ``effect.apply`` 抛错 *记日志, 不冒泡*; 该 id 留在 ``events_by_id`` 里, 下
  一轮重试 (除非用户改 YAML 把它删掉).
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from agent_world.script._registrars import EffectBase, TriggerBase
from agent_world.script.loader import LoadedEvent, ScriptLoader

if TYPE_CHECKING:  # pragma: no cover
    from agent_world.script.triggers.at_time import AtTimeTrigger  # noqa: F401

log = logging.getLogger(__name__)


class ScriptEngine:
    """Orchestrates trigger evaluation + effect application.

    Constructor stays loose on purpose: the engine merely wires references
    that *triggers* / *effects* might want to reach. The canonical world
    handle is ``world_db`` (for ``script_event_log`` writes); the rest are
    pass-through references that effects can introspect via the ``world``
    object passed to :meth:`apply`.
    """

    def __init__(
        self,
        world_db: Any = None,
        places: Any = None,
        relations: Any = None,
        caps: Any = None,
        agents: Any = None,
        segment_store: Any = None,
    ) -> None:
        self.world_db = world_db
        self.places = places
        self.relations = relations
        self.caps = caps
        self.agents = agents
        self.segment_store = segment_store

        # Core state ---------------------------------------------------
        self.loaded_event_ids: Set[str] = set()
        self.applied_events: Set[str] = set()
        self.events_by_id: Dict[str, LoadedEvent] = {}
        self._pending_notifications: Dict[Any, List[Any]] = {}
        # OnAction reverse index for fast notify() fan-out.
        self._on_action_index: Dict[tuple, List[str]] = {}

    # ------------------------------------------------------------------
    # YAML / IPC entry
    # ------------------------------------------------------------------

    async def load_from_yaml(
        self,
        path: str,
        *,
        world: Any = None,
    ) -> Dict[str, int]:
        """Load (or reload) a script YAML; new events are appended.

        Implements LAYOUT C2 reload semantics:

        * already-loaded ids: skip (no rebind).
        * already-applied ids: skip (never re-fire).
        * past-due ``AtTimeTrigger`` (``trigger.t <= world.t``): warn + skip.

        Returns a small stats dict for IPC ack.
        """

        result = ScriptLoader.load(path, existing_ids=self.loaded_event_ids)
        new_events = result.events
        ignored = list(result.ignored_ids)

        loaded = 0
        skipped_applied = 0
        skipped_expired = 0
        now_t = self._world_t(world)

        for ev in new_events:
            if ev.id in self.applied_events:
                skipped_applied += 1
                continue
            if self._is_past_due(ev.trigger, now_t):
                log.warning(
                    "ScriptEngine: event %r is past-due (trigger.t<=t=%s); skipping",
                    ev.id, now_t,
                )
                skipped_expired += 1
                continue

            self.events_by_id[ev.id] = ev
            self.loaded_event_ids.add(ev.id)
            loaded += 1

            # Reverse-index OnAction triggers if they expose actor_id /
            # action_type fields (duck-typed; trigger may not be OnAction).
            actor_id = getattr(ev.trigger, "actor_id", None)
            action_type = getattr(ev.trigger, "action_type", None)
            if action_type is not None:
                self._on_action_index.setdefault(
                    (actor_id, str(action_type)), []
                ).append(ev.id)

        return {
            "loaded": loaded,
            "skipped_already_loaded": len(ignored),
            "skipped_applied": skipped_applied,
            "skipped_expired": skipped_expired,
        }

    # ------------------------------------------------------------------
    # WorldStep hooks
    # ------------------------------------------------------------------

    async def due_events(self, world: Any, t: int) -> List[LoadedEvent]:
        """Return events whose trigger fires at tick ``t``.

        ``trigger.fires`` may be sync or async (duck-typed); errors during
        evaluation are logged and that trigger is skipped for this tick.
        """
        due: List[LoadedEvent] = []
        for ev_id, ev in list(self.events_by_id.items()):
            if ev_id in self.applied_events:
                # Defensive: should already be popped, but guard anyway.
                continue
            try:
                fired = ev.trigger.fires(world, t)
                if inspect.isawaitable(fired):
                    fired = await fired
            except Exception as e:  # pragma: no cover - defensive
                log.error(
                    "ScriptEngine: trigger.fires(%s) raised: %s", ev_id, e
                )
                continue
            if fired:
                due.append(ev)
        return due

    async def apply(
        self,
        events: List[LoadedEvent],
        world: Any,
        t: int,
    ) -> None:
        """Apply each due event's effect; record into ``script_event_log``.

        Effects are applied sequentially; one failing effect does **not**
        abort the rest (LAYOUT §3 cross-DB non-atomic posture). On success
        the event id is moved from ``events_by_id`` to ``applied_events``
        and a row is appended to ``world.db.script_event_log``.
        """
        for ev in events:
            try:
                await ev.effect.apply(world)
            except Exception as e:
                log.error(
                    "ScriptEngine: effect.apply(%s) failed: %s", ev.id, e
                )
                # Leave event in events_by_id so a future tick can retry.
                continue

            # Persist a log row (best-effort; failure here doesn't roll back
            # the effect — log + move on, matching LAYOUT non-atomicity).
            await self._append_log(ev, t)

            self.applied_events.add(ev.id)
            self.events_by_id.pop(ev.id, None)
            # Cleanup reverse index entries (best-effort).
            for key, ids in list(self._on_action_index.items()):
                if ev.id in ids:
                    ids.remove(ev.id)
                    if not ids:
                        self._on_action_index.pop(key, None)

    # ------------------------------------------------------------------
    # PerceptionBuilder + Dispatcher integration
    # ------------------------------------------------------------------

    def pending_for(self, agent_id: Any) -> List[Any]:
        """Pop & return scripted notifications queued for ``agent_id``.

        Notifications are one-shot per tick: PerceptionBuilder reads them
        once into ``Observation.scripted_notification`` and they are gone.
        """
        return self._pending_notifications.pop(agent_id, [])

    def notify_agent(self, agent_id: Any, payload: Any) -> None:
        """Effect-side helper: queue a scripted notification for an agent.

        Used by Broadcast / DialogueInjection effects to surface text into
        the next perception build.
        """
        self._pending_notifications.setdefault(agent_id, []).append(payload)

    def notify_action(
        self,
        agent_id: Any,
        action_type: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Forward a dispatched action to all matching OnAction triggers.

        Looks up ``(actor_id, action_type)`` and ``(None, action_type)`` so
        triggers that left ``actor_id`` unset still receive the notify.
        """
        payload = payload or {}
        action_type = str(action_type)
        keys = ((agent_id, action_type), (None, action_type))
        seen: Set[str] = set()
        for key in keys:
            for ev_id in list(self._on_action_index.get(key, [])):
                if ev_id in seen:
                    continue
                seen.add(ev_id)
                ev = self.events_by_id.get(ev_id)
                if ev is None:
                    continue
                try:
                    ev.trigger.notify(
                        actor_id=agent_id,
                        action_type=action_type,
                        payload=payload,
                    )
                except Exception as e:  # pragma: no cover - defensive
                    log.error(
                        "ScriptEngine: trigger.notify(%s) raised: %s",
                        ev_id, e,
                    )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _world_t(self, world: Any) -> int:
        if world is None:
            return 0
        t = getattr(world, "t", None)
        if t is None:
            clock = getattr(world, "clock", None)
            t = getattr(clock, "t", 0) if clock is not None else 0
        try:
            return int(t)
        except (TypeError, ValueError):
            return 0

    def _is_past_due(self, trigger: TriggerBase, now_t: int) -> bool:
        # Only AtTime-style triggers carry an absolute ``t`` field. We
        # duck-type rather than import to avoid coupling to a specific
        # trigger class hierarchy.
        target = getattr(trigger, "t", None)
        if target is None:
            return False
        try:
            return int(target) <= int(now_t) and now_t > 0
        except (TypeError, ValueError):
            return False

    async def _append_log(self, ev: LoadedEvent, t: int) -> None:
        if self.world_db is None:
            return
        # Prefer the spec'd async helper; fall back to ``insert_event``
        # alias defined on WorldDB. Either way, errors are logged not raised.
        fn = getattr(self.world_db, "append_script_event", None) or getattr(
            self.world_db, "insert_event", None
        )
        if fn is None:
            return
        try:
            summary = ""
            try:
                summary = ev.effect.summary()
            except Exception:  # pragma: no cover - defensive
                summary = type(ev.effect).__name__
            res = fn(ev.id, int(t), summary)
            if inspect.isawaitable(res):
                await res
        except Exception as e:  # pragma: no cover - defensive
            log.error(
                "ScriptEngine: append_script_event(%s) failed: %s",
                ev.id, e,
            )


__all__ = ["ScriptEngine"]
