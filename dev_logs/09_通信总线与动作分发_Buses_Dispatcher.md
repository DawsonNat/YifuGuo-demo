# 开发日志 09：通信总线与动作分发 (Buses & Dispatcher)

**记录时间**：2026-05-22
**目标**：详细记录 `agent_world/buses/` 和 `agent_world/world/dispatcher.py` 的机制，它们负责 Agent 之间的消息传递和动作执行。

---

## 1. 动作分发器：ActionDispatcher (`world/dispatcher.py`)
这是 LLM Tool Call 到底层子系统的**单一路由层**。当大模型决定执行某个动作时，请求会进入 `dispatch()` 方法，并被路由到 6 类后端：
1.  **FaceToFaceBus**：处理 `speak_to_local` 动作。
2.  **RemoteMessageBus**：处理 `send_message` 动作（私聊）。
3.  **GroupMessageBus**：处理群聊动作。
4.  **WorldState (MOVE 队列)**：处理 `request_move`，将其放入 `pending_moves` 队列，在 Tick 结尾统一结算。
5.  **WorldState (直写)**：处理 `update_state`，**绕过所有 Bus**，直接修改内存中的 `world.agents[a].current_state`。
6.  **外部 Pools**：处理点赞、发帖等社交媒体动作。

## 2. 通信总线 (Buses)
总线负责处理消息的投递、延迟和可见性：
*   **FaceToFaceBus (`face_to_face.py`)**：
    *   **特性**：同地点零延迟广播。
    *   **机制**：写入 `overhear` 表。同地点的所有 Agent 在下一个 Tick 都能“听”到这句话。
*   **RemoteMessageBus (`remote_message.py`)**：
    *   **特性**：跨地点点对点私聊（RDC），带有网络延迟。
    *   **机制**：写入 `direct_message` 表。需要通过 `ConnectivityResolver` 校验双方是否有信号、是否有关系。消息的 `arrive_at` 会加上延迟时间（如 1 个 Tick），接收方只有在时间到达后才能看到。
*   **GroupMessageBus (`group_message.py`)**：
    *   **特性**：群聊广播。
    *   **机制**：写入 `group_event` 表。支持持久化队列，如果接收方当前无信号，消息会暂存，直到有信号时再投递。