# 2. 双段式异步 API 与后端引擎架构设计 (Architecture)

**文档目标**：深度结合现有的 Flask 路由、IPC 机制与 ScriptEngine，明确 Web 前端、Flask API、IPC Server 与 `WorldStep` (Tick) 之间的协同机制。

**架构参照**：严格遵循 `dev_logs/10_剧本引擎与事件注入_Script_Engine.md`（事件注入机制）与 `dev_logs/11_持久化与进程通信_Persistence_IPC.md`（数据库表结构与 IPC 流程）。

---

## 一、 系统整体架构图解

```text
[ Web 前端 (三屏 UI) ]
       |  (1) POST /api/simulation/<sim_id>/inject-event (携带玩家 Query)
       v
[ Flask API (app/api/simulation.py) ] 
       |  (2) 异步并发调用 DeepSeek-V4-Pro 生成 immediate_msg
       |  (3) 返回即时响应 (task_id, immediate_msg) 给前端
       |
       |  (4) 通过 IPC Client 发送 INJECT_SCRIPT_EVENT Command
       v
[ IPC Server (agent_world/ipc/server.py) ]
       |
       |  (5) 接收 Command，将 Query 作为 Script Event 注入，触发 WorldStep.run_one_tick()
       v
[ Agent World Engine (Tick 流转 - 核心后台逻辑) ]
       |  (6.1) ScriptEngine 触发：应用 DialogueInjectionEffect (玩家说话) 或 StateChangeEffect (路由切换)
       |  (6.2) 感知 (Perception)：NPC 收集环境信息和玩家 Query
       |  (6.3) 思考 (LLM 推理)：NPC 调用 DeepSeek-V4-Pro 决定下一步动作
       |  (6.4) 动作 (Action)：NPC 调用工具进行内部私聊 (RDC) 或直接回复 (F2F)
       |  (6.5) 循环判定：若有未处理私信，自动触发下一个 Tick
       v
[ WorldDB (持久化) ] <--- (7) 记录所有 Message (direct_message, overhear) 和 State

[ Web 前端 ]
       |  (8) GET /api/simulation/<sim_id>/action-result?task_id=... (轮询)
       v
[ Flask API ] ---> (9) 查询 WorldDB，返回最终结果 (public_messages, observer_messages)
```

---

## 二、 API 详细设计与前端交互

复用现有的 `POST /api/simulation/<sim_id>/inject-event` 接口作为发起交互的入口，并新增一个轮询接口。

### API 1：发起交互与获取极速反馈 (改造现有接口)
**Endpoint**: `POST /api/simulation/<sim_id>/inject-event`

**Request Body (JSON)**:
前端直接构造一个合法的 Script Event，利用 `BroadcastEventEffect` 将玩家的话作为系统广播注入到玩家当前所在的房间（Place）中。这样不仅房间内的所有 NPC 都能听到，而且消息会落库到 `WorldDB`，方便 API 2 轮询读取。

示例 (玩家在私人会议室对 Jensen 说话)：
```json
{
  "event": {
    "id": "task_9527",
    "trigger": {
      "type": "at_condition",
      "condition": "True"
    },
    "effect": {
      "type": "broadcast_event",
      "scope": "place",
      "place_id": "jensen_private_room",
      "message": "玩家说：黄总，我的底层算法能让显存消耗降低 80%。"
    }
  }
}
```

**Backend Logic (API 1 处理流程)**:
1. Flask 提取 `event.id` 作为 `task_id`。
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
  "simulation_id": "hbm_memory_war",
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

**Backend Logic**:
1. Flask 接收到轮询请求。
2. 查询 IPC Server 对应的 `task_id` 状态（是否为 `COMPLETED`）。
3. 如果未完成，返回 `{"status": "processing"}`。
4. 如果已完成，直接以**只读模式**查询 `WorldDB`：
   - 查询 `overhear` 表：获取 Tick 范围内产生的 F2F 消息（作为 `public_messages`）。
   - 查询 `direct_message` 表：获取 Tick 范围内产生的 RDC 私聊消息（作为 `observer_messages`）。
   - 查询 `group_event` 表：获取 Tick 范围内产生的群聊消息（作为 `group_messages`）。

**Response Body (JSON)**:
```json
{
  "success": true,
  "simulation_id": "hbm_memory_war",
  "data": {
    "status": "completed",
    "end_tick": 32,
    
    // 1. 主聊天框数据 (来自 overhear 表)
    "public_messages": [
      {
        "sender": "Jensen Hwang",
        "content": "年轻人，你知道上一个跟我这么说的人在哪吗？",
        "type": "F2F"
      }
    ],
    
    // 2. 上帝视角数据 (来自 direct_message 表)
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
    
    // 3. 群聊密谋数据 (来自 group_event 表)
    "group_messages": [
      {
        "tick": 32,
        "sender": "SK Hynix CEO",
        "group_id": 200,
        "content": "别慌，这小子肯定在吹牛，我们咬死 30% 涨价不松口！",
        "type": "GRP"
      }
    ],
    
    // 4. 状态面板数据 (由 Flask 层维护)
    "stats_update": {
      "vision": 25,
      "execution": 15,
      "trust": 10
    },
    
    // 5. 路由状态 (通知前端当前处于哪个 Phase)
    "current_phase": "Phase 2"
  }
}
```

---

## 三、 状态机与路由节点的后端实现逻辑 (基于 ScriptEngine)

在 `1_story_prototype.md` 中设计的动态路由节点，**不需要硬编码在 WorldStep 中**，而是通过现有的 **`ScriptEngine`** 完美实现。

**逻辑流 (以 Turn 10 路由为例)**：
1. Flask 层维护玩家的 Turn 计数和 Stats 数值。
2. 前端在发送第 10 次玩家输入时，Flask 判断满足进入 Phase 2 的条件。
3. Flask 通过 `inject-event` 接口，向世界额外注入一个 **`StateChangeEffect`**：
   ```json
   {
     "event": {
       "id": "route_phase2",
       "trigger": { "type": "at_condition", "condition": "True" },
       "effect": {
         "type": "state_change",
         "agent_id": 2,
         "new_state": "被玩家的狂言引起了兴趣，决定验证其底层技术的真实性。"
       }
     }
   }
   ```
4. 在下一个 Tick 的 Phase A (步骤 1-2)，`ScriptEngine` 捕获此 Effect，调用 `world.set_current_state()` 修改 Jensen 的状态。
5. 这种状态的更新直接影响下一个 Tick 中 DeepSeek-V4-Pro 的生成倾向，从而实现平滑的剧情路由。