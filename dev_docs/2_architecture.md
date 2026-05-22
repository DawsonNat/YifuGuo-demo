# 2. 双段式异步 API 架构设计 (Architecture)

**文档目标**：设计前后端交互的数据流转与 JSON 结构，明确 Web 前端、Flask API、IPC Server 与 `WorldStep` (Tick) 之间的协同机制，彻底解决“大模型推理导致用户干等”的 UX 痛点。

---

## 一、 系统整体架构图解

为了实现“三屏联动”（状态面板、主聊天、上帝视角）并掩盖大模型延迟，系统采用以下架构：

```text
[ Web 前端 (三屏 UI) ]
       |  (1) POST /api/action (携带 Query)
       v
[ Flask API (app/api/chat.py) ] ---> (2) 返回即时响应 (task_id, immediate_msg)
       |
       |  (3) 通过 IPC 发送 Action Command
       v
[ IPC Server (后台常驻) ]
       |
       |  (4) 注入 F2F 消息，并触发 WorldStep.run_one_tick()
       v
[ Agent World Engine (Tick 流转) ]
       |  (5) Agent 感知 -> LLM 推理 -> 内部私聊 (RDC) -> 状态更新
       v
[ WorldDB (持久化) ] <--- (6) 记录所有 Message 和 State

[ Web 前端 ]
       |  (7) GET /api/action/result?task_id=... (轮询)
       v
[ Flask API ] ---> (8) 查询 WorldDB，返回最终结果 (public_messages, observer_messages)
```

**核心设计理念**：
*   **1 个玩家 Turn 触发 N 个后台 Tick**：前端发送一次请求，后台引擎可能会跑好几个 Tick（直到 NPC 开口说话或达到最大静默次数）。
*   **解耦**：Flask Web 服务只负责接收请求和查询数据库，绝不阻塞等待大模型推理。所有繁重的 LLM 调用都在后台 IPC 进程的 Tick 循环中完成。

---

## 二、 API 详细设计

### API 1：发起交互与获取即时反馈
**Endpoint**: `POST /api/simulation/<sim_id>/action`
**作用**：接收玩家的输入，注入世界，并立刻返回一个让前端 UI 动起来的“即时状态”。

**Request Body (JSON)**:
```json
{
  "player_id": "player_1",
  "place_id": "nvidia_hq_boardroom",
  "query": "黄总，我的底层算法能让渲染速度提升 10 倍。"
}
```

**Backend Logic**:
1. 接收到请求后，生成一个唯一的 `task_id`。
2. 将玩家的 `query` 封装为一个 F2F（面对面）Message 注入到 `WorldDB` 中。
3. 通过 IPC 通知后台引擎：“玩家说话了，开始跑 Tick 吧”。
4. **立刻**返回响应（耗时 < 1秒）。

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
*(注：`immediate_msg` 可以是预设的，也可以用极小模型/规则引擎极速生成，用于前端主聊天框的占位显示)*

---

### API 2：轮询获取最终结果
**Endpoint**: `GET /api/simulation/<sim_id>/action/result?task_id=<task_id>`
**作用**：前端拿到 `task_id` 后，每隔 1-2 秒轮询此接口，获取后台 Tick 跑完后的最终结果。

**Backend Logic**:
1. 检查该 `task_id` 对应的 Tick 循环是否结束（结束条件：NPC 对玩家发出了 F2F 消息，或者连续跑了 3 个 Tick NPC 依然保持沉默）。
2. 如果未结束，返回 `{"status": "processing"}`。
3. 如果已结束，从 `WorldDB` 中查出这段时间内发生的所有事情，分类打包返回。

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

## 三、 状态机与路由节点的后端实现逻辑

为了实现 `1_story_prototype.md` 中规划的路由节点（如 Turn 10 判定），后端需要在 `WorldStep` 的 Tick 循环中加入**状态检查钩子 (State Check Hooks)**。

**逻辑流**：
1. 每次 `WorldStep.run_one_tick()` 结束时。
2. 检查当前的 `player_turn_count`。
3. 如果 `player_turn_count == 10`：
   - 读取当前的 `stats_update.vision`。
   - 如果 `< 20`，触发 Bad End 事件。
   - 如果 `>= 20`，调用 `WorldState.update_agent_prompt(agent_id="jensen", new_goal="验证他的技术")`。
4. 这种状态的更新会直接影响下一个 Tick 中 LLM 的生成倾向，从而实现平滑的剧情路由。