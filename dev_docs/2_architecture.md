# 2. 双段式异步 API 与后端引擎架构设计 (Architecture)

**文档目标**：设计前后端交互的数据流转与 JSON 结构，同时**深度拆解后端引擎的运转流程**，明确 Web 前端、Flask API、IPC Server 与 `WorldStep` (Tick) 之间的协同机制，彻底解决“大模型推理导致用户干等”的 UX 痛点。

---

## 一、 系统整体架构图解

为了实现“三屏联动”（状态面板、主聊天、上帝视角）并掩盖大模型延迟，系统采用以下架构：

```text
[ Web 前端 (三屏 UI) ]
       |  (1) POST /api/action (携带 Query)
       v
[ Flask API (app/api/chat.py) ] 
       |  (2) 调用极小模型 (如 DeepSeek-Flash) 极速生成 immediate_msg
       |  (3) 返回即时响应 (task_id, immediate_msg) 给前端
       |
       |  (4) 通过 IPC 发送 Action Command
       v
[ IPC Server (后台常驻) ]
       |
       |  (5) 将 Query 注入 F2F 总线，并触发 WorldStep.run_one_tick()
       v
[ Agent World Engine (Tick 流转 - 核心后台逻辑) ]
       |  (6.1) 感知 (Perception)：NPC 收集环境信息和玩家 Query
       |  (6.2) 思考 (LLM 推理)：NPC 调用主模型 (如 DeepSeek-Chat) 决定下一步动作
       |  (6.3) 动作 (Action)：NPC 可能直接回复，也可能调用工具进行内部私聊 (RDC)
       |  (6.4) 循环判定：若有未处理私信，自动触发下一个 Tick
       v
[ WorldDB (持久化) ] <--- (7) 记录所有 Message 和 State

[ Web 前端 ]
       |  (8) GET /api/action/result?task_id=... (轮询)
       v
[ Flask API ] ---> (9) 查询 WorldDB，返回最终结果 (public_messages, observer_messages)
```

---

## 二、 API 详细设计与前端交互

### API 1：发起交互与获取极速反馈
**Endpoint**: `POST /api/simulation/<sim_id>/action`
**作用**：接收玩家的输入，注入世界，并利用小模型极速生成一个动态的“即时状态”，让前端 UI 动起来。

**Request Body (JSON)**:
```json
{
  "player_id": "player_1",
  "place_id": "nvidia_hq_boardroom",
  "query": "黄总，我的底层算法能让渲染速度提升 10 倍。"
}
```

**Backend Logic (API 1 处理流程)**:
1. 接收到请求后，生成一个唯一的 `task_id`。
2. **极速生成 `immediate_msg`（非预设）**：
   - Flask 收到请求后，**不等待**主引擎跑 Tick。
   - 而是立刻提取当前场景的简要上下文（如：“Jensen 正在听玩家说话，玩家说：[Query]”）。
   - 调用一个**极小/极速的 LLM（如 DeepSeek-Flash，要求 < 500ms 返回）**，Prompt 设定为：“用一句话描写听者的微表情或肢体动作，不要说话”。
   - 生成结果如：“Jensen 停下了喝水的动作，眼神变得锐利起来...”。
3. 将玩家的 `query` 封装为 F2F Message，通过 IPC 通知后台引擎开始跑 Tick。
4. 立刻将生成的 `immediate_msg` 返回给前端。

**Response Body (JSON)**:
```json
{
  "success": true,
  "data": {
    "task_id": "task_9527",
    "immediate_msg": "Jensen 停下了喝水的动作，眼神变得锐利起来...",
    "status": "processing"
  }
}
```

---

### API 2：轮询获取最终结果
**Endpoint**: `GET /api/simulation/<sim_id>/action/result?task_id=<task_id>`
**作用**：前端拿到 `task_id` 后，每隔 1-2 秒轮询此接口，获取后台 Tick 跑完后的最终结果。

**Response Body (JSON)**:
```json
{
  "success": true,
  "data": {
    "status": "completed",
    "end_tick": 32,
    
    // 1. 主聊天框数据 (玩家能看到的)
    "public_messages": [
      {
        "sender": "Jensen Hwang",
        "content": "年轻人，你知道上一个跟我这么说的人在哪吗？",
        "type": "F2F"
      }
    ],
    
    // 2. 上帝视角数据 (投资人看的核心 Feature：NPC 内部交互)
    "observer_messages": [
      {
        "tick": 30,
        "sender": "Jensen Hwang",
        "receiver": "Tech VP",
        "content": "查一下这小子的 GitHub，看看他是不是在用 PPT 骗我。",
        "type": "RDC"
      },
      {
        "tick": 31,
        "sender": "Tech VP",
        "receiver": "Jensen Hwang",
        "content": "代码很乱，但底层的稀疏注意力算法是个天才设计。",
        "type": "RDC"
      }
    ],
    
    // 3. 状态面板数据 (动态路由的依据)
    "stats_update": {
      "vision": 25,
      "execution": 15,
      "trust": 10
    },
    
    // 4. 路由状态 (通知前端当前处于哪个 Phase)
    "current_phase": "Phase 2"
  }
}
```

---

## 三、 后端引擎详细运转流程 (The Engine Loop)

前端调用 API 1 后，后台的 `Agent World Engine` 究竟发生了什么？以下是详细的内部流转设计：

### 1. 触发与感知阶段 (Tick N)
*   **事件注入**：IPC Server 收到 API 1 的指令，将玩家的 Query 作为一个 `F2F Message` 写入 `WorldDB`，时间戳记为 `Tick N`。
*   **强制唤醒**：IPC Server 调用 `WorldStep.run_one_tick()`。
*   **感知构建 (PerceptionBuilder)**：引擎遍历当前会议室（`nvidia_hq_boardroom`）内的所有 Agent。Jensen 的感知模块会收集到：“玩家在 Tick N 对大家说：[Query]”。

### 2. 思考与动作阶段 (Tick N)
*   **LLM 推理**：Jensen Agent 将收集到的感知，结合自身的 System Prompt（性格、当前状态、目标），发送给主模型（如 DeepSeek-Chat）。
*   **工具调用 (Tool Call)**：
    *   主模型根据 Prompt 中的强制规则（“遇到技术细节必须向 VP 求证”），决定暂时不回复玩家。
    *   主模型输出一个 Tool Call：`send_message(recipient="Tech VP", channel="RDC", content="查一下这小子的 GitHub...")`。
*   **动作执行**：引擎解析该 Tool Call，将这条私信写入 `WorldDB`（标记为 RDC 通道，目标为 Tech VP）。Tick N 结束。

### 3. 内部发酵与连续 Tick 触发 (Tick N+1)
*   **自动循环判定**：Tick N 结束后，引擎检查 `WorldDB`，发现有一条未送达的 RDC 消息。引擎**自动触发** `Tick N+1`。
*   **VP 感知与思考**：Tech VP 的感知模块收集到 Jensen 的私信。VP 调用主模型进行推理，生成回复：“代码很乱，但底层算法是天才设计。”
*   **VP 动作**：VP 通过 RDC 通道将回复发给 Jensen。Tick N+1 结束。

### 4. 记忆生效与最终回复 (Tick N+2)
*   **再次自动触发**：引擎发现 VP 有回复，自动触发 `Tick N+2`。
*   **Jensen 记忆更新**：Jensen 收到 VP 的回复，该回复正式进入 Jensen 的 `Memory`（短期上下文）。
*   **路由节点干预 (State Check Hook)**：
    *   在 Tick N+2 开始前，后端的**状态机钩子**介入。
    *   钩子检测到 VP 给出了正面评价，强制调用 `WorldState.update_agent_prompt()`，将 Jensen 的状态从“傲慢”修改为“兴奋”。
*   **最终回复生成**：Jensen 带着更新后的记忆和状态，再次调用主模型，生成最终对玩家的回复：“我给你 500 张显卡。”（注入 F2F 总线）。Tick N+2 结束。

### 5. 循环终止与结果返回
*   引擎检查发现没有未处理的内部消息，且 NPC 已经对玩家做出了正面回应。Tick 循环暂停。
*   此时，前端的 API 2 轮询正好到来，Flask API 从 `WorldDB` 中提取 Tick N 到 N+2 之间的所有数据，打包返回给前端。
