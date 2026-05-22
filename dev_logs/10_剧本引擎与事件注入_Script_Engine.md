# 开发日志 10：剧本引擎与事件注入 (Script Engine)

**记录时间**：2026-05-22
**目标**：详细记录 `agent_world/script/` 目录下的机制。这是实现“动态路由”和“非写死剧情控制”的核心武器。

---

## 1. 剧本引擎：ScriptEngine (`script/engine.py`)
`ScriptEngine` 是一个轻量级的事件编排器。它在每个 Tick 的最开始（Phase A 步骤 1-2）执行：
1.  **`due_events`**：遍历所有已加载的事件，检查其 Trigger（触发器）是否满足条件。
2.  **`apply`**：对满足条件的事件，执行其 Effect（效果），并将记录写入 `script_event_log` 以供审计。

## 2. 触发器 (Triggers)
定义了事件在何时发生：
*   **`AtTimeTrigger` (`at_time.py`)**：在指定的绝对 Tick 时间点触发（如 `t=10`）。
*   **`AtConditionTrigger` (`at_condition.py`)**：当某个表达式为真时触发（支持 `simpleeval` 安全沙箱）。
*   **`OnActionTrigger` (`on_action.py`)**：当某个 Agent 执行了特定动作时触发（纯 Push 模式，由 Dispatcher 触发通知）。

## 3. 效果器 (Effects)
定义了事件发生后对世界产生的影响。**这是我们通过 API 干预世界的关键！**
*   **`DialogueInjectionEffect` (`dialogue_injection.py`)**：
    *   **作用**：替指定 Agent 注入一条对话记忆。
    *   **应用场景**：在 API 1 中，我们将玩家在前端输入的话，包装成这个 Effect 注入给 Jensen，让他“听”到玩家的声音。
*   **`StateChangeEffect` (`state_change.py`)**：
    *   **作用**：强制修改 Agent 的 `current_state`。
    *   **应用场景**：这就是我们的**路由节点控制器**。当满足进入下一阶段的条件时，注入此 Effect 改变 Jensen 的状态，从而引导大模型生成不同态度的对话。
*   **`BroadcastEventEffect` (`broadcast_event.py`)**：发送全局/地点的系统广播（如“突然停电了”）。
*   **`MoveEffect` (`move.py`)**：强制移动 Agent。
*   **`RelationChangeEffect` / `CapabilityChangeEffect`**：修改人际关系或能力。

## 4. 剧本加载器：ScriptLoader (`script/loader.py`)
负责将 YAML 格式的剧本文件解析、校验（基于 Pydantic），并实例化为具体的 Trigger 和 Effect 对象。它支持热重载（Hot Reload），允许在仿真运行期间增量追加新事件。