# 2. 双段式异步 API 与后端引擎架构设计 (Architecture)

**文档目标**：设计前后端交互的数据流转与 JSON 结构，同时**深度结合现有的 Flask 路由、IPC 机制与 ScriptEngine**，明确 Web 前端、Flask API、IPC Server 与 `WorldStep` (Tick) 之间的协同机制。

---

## 一、 系统整体架构图解

```text
[ Web 前端 (三屏 UI) ]
       |  (1) POST /api/simulation/<sim_id>/inject-event (携带玩家 Query)
       v
[ Flask API (app/api/simulation.py) ] 
       |  (2) 异步调用 DeepSeek-V4-Pro 生成 immediate_msg
       |  (3) 返回即时响应 (task_id, immediate_msg) 给前端
       |
       |  (4) 通过 IPC Client 发送 INJECT_SCRIPT_EVENT Command
       v
[ IPC Server (agent_world/ipc/server.py) ]
       |
       |  (5) 接收 Command，将 Query 作为 Script Event 注入，触发 WorldStep.run_one_tick()
       v
[ Agent World Engine (Tick 流转 - 核心后台逻辑) ]
       |  (6.1) ScriptEngine 触发：应用注入的 DialogueInjectionEffect / StateChangeEffect
       |  (6.2) 感知 (Perception)：NPC 收集环境信息和玩家 Query
       |  (6.3) 思考 (LLM 推理)：NPC 调用 DeepSeek-V4-Pro 决定下一步动作
       |  (6.4) 动作 (Action)：NPC 调用工具进行内部私聊 (RDC) 或直接回复
       |  (6.5) 循环判定：若有未处理私信，自动触发下一个 Tick
       v
[ WorldDB (持久化) ] <--- (7) 记录所有 Message 和 State

[ Web 前端 ]
       |  (8) GET /api/simulation/<sim_id>/action-result?task_id=... (轮询)
       v
[ Flask API ] ---> (9) 查询 WorldDB，返回最终结果 (public_messages, observer_messages)
```

---

## 二、 API 详细设计与前端交互

**重大修正**：为了复用现有代码结构，我们不新建 `/api/action` 路由，而是**改造并复用现有的 `POST /api/simulation/<sim_id>/inject-event` 接口**作为发起交互的入口，并新增一个轮询接口。

### API 1：发起交互与获取极速反馈 (改造现有接口)
**Endpoint**: `POST /api/simulation/<sim_id>/inject-event`
**作用**：接收玩家的输入，注入世界，并利用统一的 DeepSeek 模型生成一个动态的“即时状态”，让前端 UI 动起来。

**Request Body (JSON)**:
```json
{
  "event": {
    "id": "task_9527",
    "trigger": {
      "type": "at_time",
      "t": 0 
    },
    "effect": {
      "type": "dialogue_injection",
      "agent_id": 3,
      "text": "玩家说：黄总，我的底层算法能让渲染速度提升 10 倍。"
    }
  }
}
```
*(注：这里利用了现有的 `ScriptEngine` 和 `DialogueInjectionEffect` 将玩家的话注入给目标 NPC)*

**Backend Logic (API 1 处理流程)**:
1. Flask 接收到请求，提取 `event.id` 作为 `task_id`。
2. **生成 `immediate_msg`（单模型并发调用）**：
   - Flask **不等待** IPC Server 跑 Tick。
   - 提取简要上下文，**异步调用 DeepSeek-V4-Pro**。
   - Prompt 限制：“根据玩家的话，用一句话描写听者（Jensen）的微表情或肢体动作，不要让他开口说话。要求极速响应。”
   - 得到结果：“Jensen 停下了喝水的动作，眼神变得锐利起来...”。
3. 通过 `simulation_ipc.client` 发送 `CommandType.INJECT_SCRIPT_EVENT` 到后台 IPC Server。
4. 将生成的 `immediate_msg` 返回给前端。

**Response Body (JSON)**:
```json
{
  "success": true,
  "simulation_id": "shedog_husband",
  "data": {
    "task_id": "task_9527",
    "immediate_msg": "Jensen 停下了喝水的动作，眼神变得锐利起来...",
    "status": "processing"
  }
}
```

---

### API 2：轮询获取最终结果 (新增接口)
**Endpoint**: `GET /api/simulation/<sim_id>/action-result?task_id=<task_id>`
**作用**：前端拿到 `task_id` 后，每隔 1-2 秒轮询此接口，获取后台 Tick 跑完后的最终结果。

**Backend Logic**:
1. Flask 接收到轮询请求。
2. 查询 IPC Server 对应的 `task_id` 状态（是否为 `COMPLETED`）。
3. 如果未完成，返回 `{"status": "processing"}`。
4. 如果已完成，直接以**只读模式**查询 `WorldDB`（复用类似 `world_state` 接口的逻辑），提取该 `task_id` 触发的 Tick 范围内产生的所有 Message 和状态更新。

**Response Body (JSON)**:
```json
{
  "success": true,
  "simulation_id": "shedog_husband",
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

## 三、 状态机与路由节点的后端实现逻辑 (基于 ScriptEngine)

在 `1_story_prototype.md` 中我们设计了基于 Turn 的动态路由节点。在现有的 `agent_world` 框架中，这**不需要硬编码在 WorldStep 中**，而是可以通过现有的 **`ScriptEngine`** 完美实现。

**逻辑流 (以 Turn 10 路由为例)**：
1. 前端在发送第 10 次玩家输入时，除了发送 `DialogueInjectionEffect`，还会附带一个状态检查的逻辑（或者由后端在 `inject-event` 时拦截判定）。
2. 如果满足进入 Phase 2 的条件，后端通过 `inject-event` 接口，向世界注入一个 **`StateChangeEffect`**：
   ```json
   {
     "type": "state_change",
     "agent_id": 3,
     "new_state": "被玩家的技术折服，决定验证其真实性"
   }
   ```
3. 在下一个 Tick 的 Phase A (步骤 1-2)，`ScriptEngine` 会捕获这个 Effect，并调用 `world.set_current_state(agent_id=3, new_state=...)`。
4. 这种状态的更新会直接影响下一个 Tick 中 LLM 的生成倾向，从而实现平滑的剧情路由。
