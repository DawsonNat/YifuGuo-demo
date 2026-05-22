# 开发日志 07：核心引擎与状态管理 (World Engine & State)

**记录时间**：2026-05-22
**目标**：详细记录 `agent_world/world/` 目录下的核心引擎机制，这是整个仿真世界的心脏。

---

## 1. 核心调度器：WorldStep (`world/step.py`)
`WorldStep` 是整个仿真引擎的微观调度器（Micro-tick Orchestrator）。每次调用 `run_one_tick()`，它会严格按照 11 步流水线（Phase A -> Phase B -> Phase C）推进世界时间：

*   **Phase A (Lockstep 同步阶段)**：
    1.  调用 `script_engine.due_events` 获取当前 Tick 触发的剧本事件。
    2.  调用 `script_engine.apply` 执行这些事件（如注入对话、改变状态）。
    3.  更新外部 Agent 池（Pools）。
    4.  调度器挑选本轮活跃的 Agent。
    5.  清理未送达的群聊消息队列。
    6.  按地点（Place）对 Agent 进行分组打乱。
*   **Phase B (Async 并发阶段)**：
    7.  **核心执行**：并发执行每个地点的 `run_place`。Agent 会构建感知（Perception） -> 思考（LLM） -> 执行动作（ActionDispatcher）。
*   **Phase C (Lockstep 结算阶段)**：
    8.  （空操作占位）
    9.  调用 `dispatcher.commit_pending_moves` 结算所有挂起的移动请求（MOVE）。
    10. 调用 `manager.flush_all` 将记忆刷入 Zep 向量库。
    11. `clock.advance(1)` 推进全局时间 `t += 1`。

## 2. 世界状态总线：WorldState (`world/state.py`)
`WorldState` 是一个“句柄（Handle）”，它本身不存大量数据，而是聚合了所有的子系统引用：
*   持有的引用：`PlaceStore` (地点), `RelationGraph` (关系), `CapabilityTable` (能力), `MultiPoolPlatformManager` (外部池)。
*   **自有核心数据**：`agents` 字典。每个 `AgentRuntime` 实例持有三个关键的动态字段（用于 B5 提示词）：
    *   `soul`：性格内核。
    *   `long_term_goal`：长期目标。
    *   `current_state`：当前状态/心情。**这个字段是动态路由的核心，可被 `UPDATE_STATE` 动作或 `StateChangeEffect` 剧本直接无锁修改。**

## 3. 感知构建器：PerceptionBuilder (`world/perception.py`)
在每个 Tick，Agent 思考前，必须先“看”世界。`PerceptionBuilder` 负责收集：
1.  **物理环境**：当前地点、地点属性、同地点的其他 Agent。
2.  **社交网络**：通讯录（Contacts）。
3.  **信息流**：收到的私信（RDC）、群聊消息（GRP）、面对面听到的对话（F2F/Overhear）。
4.  **记忆与通知**：从 Zep 检索到的相关记忆（Memories），以及剧本系统强塞进来的通知（Scripted Notifications）。
这些信息会被组装成一段 Prompt，交给大模型进行推理。

## 4. 物理与社交拓扑
*   **Clock (`clock.py`)**：维护全局单调递增的整型时间 `t`。
*   **PlaceStore (`place_store.py`)**：管理所有的地点（Place）以及 Agent 当前所在的地点映射（`L_t`）。
*   **RelationGraph (`relation_graph.py`)**：管理 Agent 之间的人际关系（如 `spouse`, `lover`），支持对称和互斥关系。
*   **ConnectivityResolver (`connectivity.py`)**：判断两个 Agent 之间是否能通信（如 RDC 私聊需要有信号能力且有关系）。