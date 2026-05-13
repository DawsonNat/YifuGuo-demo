# Script Triggers 实现文档

> 路径: `agent_world/script/triggers/*.py` (一文件一类: `at_time.py`, `at_condition.py`, `on_action.py`, `on_duration.py`)
> 对应 LAYOUT §: §2.E triggers/ / §10.1 (metaclass 继承自动注册) / §10.7 TriggerRegistrar
> 上游依赖文档: `script_registrars.md`
> 下游依赖文档: `script_engine.md` (engine 调 fires / notify), `script_loader.md` (YAML→实例)

## 1. 模块定位

`triggers/` 目录下的 4 个 trigger 子类是剧本"何时触发" 的判定单元。每个子类:

- 继承 `TriggerBase` (script_registrars.md), 通过 metaclass 自动注册到 `TriggerRegistrar`。
- 实现 `fires(world, t) -> bool`: 在 ScriptEngine 主循环步骤 1 被批量调用。
- (可选) 实现 `notify(actor_id, action_type, payload)`: OnAction 类用, 由 ActionDispatcher hook 触发后置 fired flag。

输入: WorldState 引用 + 当前 `t` (+ OnAction: ActionDispatcher 通知)。
输出: 布尔值 `fires`, 表示本 tick 应该执行配套 effect。

为什么必须存在: 剧本的"触发条件" 是 LAYOUT §2.E 中明确划分的扩展层之一 (LAYOUT §10.7); 4 种触发模式覆盖了 MVP 期所有典型用例 (定时/状态条件/动作钩子/时间窗口)。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 安全表达式求值 | `simpleeval` (PyPI) | — | KEEP | AtCondition 必须用; 严禁裸 `eval` (LAYOUT §2.E 明确) |
| OASIS step 时序参考 | OASIS | `env.py:136-198` | PATTERN | 决定 trigger.fires 在 step 步骤 1 跑 (LLM gather 之前) |
| ManualAction 注入模式 | OASIS | `env_action.py` | PATTERN | OnAction 借其"系统侧 hook 监听 dispatcher"的思路 |

## 3. 关键改动 (相对来源仓库)

- 全新写。设计灵感来自 LAYOUT §2.E 的 4 类触发模式划分。
- AtCondition 强制走 `simpleeval.SimpleEval`; LAYOUT 明确禁用裸 `eval`。
- OnAction 不轮询, 由 ActionDispatcher 主动 push (`engine.on_action_dispatched` → `trigger.notify`); `fires` 仅检查 `_pending_fire` flag。
- OnDuration 不持有内部状态机, 纯函数式判定 `start_t <= world.t < start_t + duration_ticks`。

## 4. 核心逻辑

### 4.1 数据结构

每个子类持有的字段 (即 `__init__` 参数, 也是 YAML schema 来源):

| 子类 | 文件 | 字段 |
|---|---|---|
| `AtTimeTrigger` | `triggers/at_time.py` | `t: int` |
| `AtConditionTrigger` | `triggers/at_condition.py` | `expr: str` (simpleeval 表达式) |
| `OnActionTrigger` | `triggers/on_action.py` | `actor_id: int`, `action_type: str`, 可选 `match: dict[str, Any]` (payload 字段过滤); 内部 `_pending_fire: bool = False` |
| `OnDurationTrigger` | `triggers/on_duration.py` | `start_t: int`, `duration_ticks: int` |

不变量:
- AtTime / OnDuration 是无状态判定; 同一 `(t, world)` 多次调用 `fires` 结果相同 (幂等)。
- OnAction 的 `_pending_fire` 由 `notify` 置 True, 由 ScriptEngine 在 effect.apply 完成后置回 False (或直接从 `events` 字典移除事件后, 该实例随之 GC)。
- AtCondition 表达式可读取 `world` 子树 (限 simpleeval names = `{world: WorldStateView}`); 不允许写入。

### 4.2 关键流程 / 算法

**AtTimeTrigger.fires(world, t)**:
```
return t >= self.t                                # 准时或迟到都触发, 但 ScriptEngine 加载期会拦截过期事件
```
注: 实际只在 `t == self.t` 那一轮触发一次, 因为 ScriptEngine 触发后会从 events 字典移除该事件 (event_id 进 applied_events)。

**AtConditionTrigger.fires(world, t)**:
```
evaluator = SimpleEval(
    names={"world": WorldStateView(world), "t": t},
    operators=DEFAULT_OPERATORS,
    functions={"len": len, ...},                  # 白名单
)
try:
    return bool(evaluator.eval(self.expr))
except Exception as e:
    log.warning(f"AtCondition eval failed: {self.expr} → {e}")
    return False
```
`WorldStateView` 是只读 facade (限制可访问字段, 防剧本作者误改世界态)。

**OnActionTrigger.notify(actor_id, action_type, payload)** (由 ScriptEngine.on_action_dispatched 调):
```
if actor_id != self.actor_id or action_type != self.action_type:
    return
if self.match and not all(payload.get(k) == v for k, v in self.match.items()):
    return
self._pending_fire = True
```

**OnActionTrigger.fires(world, t)**:
```
return self._pending_fire                         # 由 ScriptEngine 在下一轮 due_events 中读
```

**OnDurationTrigger.fires(world, t)**:
```
return self.start_t <= t < self.start_t + self.duration_ticks
```
注: 与 AtTime 不同, OnDuration 设计上"持续多个 tick 都触发"。但 ScriptEngine 触发一次后即 applied, 实践上等价于"在 `[start_t, start_t+duration)` 区间内任意一轮 due_events 命中即触发一次"。如需"区间内每轮触发一次", 需要 effect 自带循环逻辑 (MVP 不支持)。

### 4.3 与其他模块的交互

- **上游调用方**:
  - `ScriptEngine.due_events(world, t)`: 遍历 `engine.events.values()` 调每个 `bundle.trigger.fires(world, t)`
  - `ScriptEngine.on_action_dispatched(actor_id, action_type, payload)`: 调 `OnActionTrigger.notify(...)`
  - `ScriptLoader.load`: 从 YAML 实例化各子类
- **下游被调方**:
  - `simpleeval.SimpleEval` (AtCondition)
  - `WorldStateView` (`agent_world/world/state.py` 的只读视图; 暂未独立成模块, 由 state.py 导出)
- **共享状态**:
  - 仅 OnActionTrigger 有内部状态 `_pending_fire`
  - 不直接读写 world.db / pool_*.db / Zep
  - AtCondition 通过 `WorldStateView` 间接读 WorldState 内存态

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
from typing import Any, Dict, Optional
from agent_world.script._registrars import TriggerBase

class AtTimeTrigger(TriggerBase):
    """Fire when world.t reaches the configured tick.

    Args:
        t: Target tick (world.t).
    """
    def __init__(self, *, t: int) -> None: ...
    def fires(self, world: "WorldState", t: int) -> bool: ...


class AtConditionTrigger(TriggerBase):
    """Fire when a simpleeval expression evaluates truthy.

    Args:
        expr: simpleeval expression. `world` and `t` are available as names.
    """
    def __init__(self, *, expr: str) -> None: ...
    def fires(self, world: "WorldState", t: int) -> bool: ...


class OnActionTrigger(TriggerBase):
    """Fire after a specific agent dispatches a specific action.

    Args:
        actor_id: Agent ID to watch.
        action_type: ActionType enum name (e.g. "SPEAK_TO_LOCAL").
        match: Optional payload field filter (all key-values must match).
    """
    def __init__(
        self,
        *,
        actor_id: int,
        action_type: str,
        match: Optional[Dict[str, Any]] = None,
    ) -> None: ...
    def notify(
        self,
        actor_id: int,
        action_type: str,
        payload: Dict[str, Any],
    ) -> None: ...
    def fires(self, world: "WorldState", t: int) -> bool: ...


class OnDurationTrigger(TriggerBase):
    """Fire while world.t is within [start_t, start_t + duration_ticks).

    Args:
        start_t: Window start tick (inclusive).
        duration_ticks: Window length.
    """
    def __init__(self, *, start_t: int, duration_ticks: int) -> None: ...
    def fires(self, world: "WorldState", t: int) -> bool: ...
```

### 5.2 IPC / Flask / SQL

无。本目录下子类不直接产出 IPC/Flask/SQL; 由 ScriptEngine 调度后通过 effect 间接写库。

## 6. 配置入口

每个 trigger 子类的 `__init__` 签名 + Google docstring `Args:` 段即 conscribe Tier 1 schema (LAYOUT §10.3)。

YAML 形态:
```yaml
trigger: { type: at_time, t: 10 }
trigger: { type: at_condition, expr: "world.places.count_at('bar_anchor') >= 3" }
trigger: { type: on_action, actor_id: 5, action_type: "SPEAK_TO_LOCAL", match: { content_contains: "hello" } }
trigger: { type: on_duration, start_t: 100, duration_ticks: 20 }
```

`type` 字段是 conscribe 的 `discriminator_field`; 由 `strip_suffixes=["Trigger"]` + CamelCase→snake_case 映射: `AtTimeTrigger` → `"at_time"`, `OnActionTrigger` → `"on_action"`, etc.

无运行时 simulation_config 直接读取; 全部经 `world_config.events[*].trigger` 子树进入。

## 7. 待决策 / 风险

- **simpleeval 函数白名单深度**: AtCondition 默认仅放 `len`; 如剧本作者要求 `min/max/sum`, MVP 按需扩。但严禁开放 `__import__` / `getattr` 链。
- **WorldStateView 暴露面**: 当前未独立模块。MVP 阶段建议仅暴露 `world.places`, `world.relations`, `world.capabilities` 三个只读 facade; 不暴露 `world.db` 防剧本作者直接 SQL。
- **OnAction 的"一次性触发"语义**: 当前 fired 后事件即从 `events` 字典移除; 如剧本要求"alice 每次说话都触发某 effect", 必须用多个 event_id (不同 id) 配多个 OnAction trigger, 或后期加 `repeat: true` 字段 (MVP 不做)。
- **本模块自暴露的风险**:
  - OnDuration 的"区间触发一次" 语义可能与用户直觉不符 (用户可能期待"每 tick 都触发"); 文档需明确说明。
  - simpleeval 表达式无类型检查; YAML 写错 (如 `world.places.count_at` 拼错) 只在 fires 调用时报 warning, 不在加载期拒绝。后期可加"启动期 dry-run 求值" 兜底。
