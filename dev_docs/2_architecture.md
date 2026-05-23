# 2. 双段式异步 API 与后端架构设计 (Architecture)

**文档目标**：在**不修改 `agent_world` 引擎核心**的前提下，说明 Web 前端、Flask 应用层、IPC、`WorldStep` 与 `WorldDB` 如何协同，使 HBM Demo 可直接开发。

**应用代码目录（新建，不动 `demo/`）**：

```text
agent_world/hbm_demo/
  hbm_scenario.yaml      # 场景配置（内容见 3_prompt_management.md）
  hbm_agent.py           # HbmAgent：LLM 决策 + update_memory + 工具参数适配
  run_hbm.py             # Runner 子进程：WorldStep 循环 + IPC + ScriptEngine
  game_service.py        # Flask 侧：Stats、路由、WorldDB 只读查询、id→name 映射
  broadcast_helper.py    # Runner 内系统广播（替代 BroadcastEventEffect）
  ipc_helper.py          # Flask 侧 IPC 封装（batch inject / tick_count / broadcast）
  routes.py              # Flask Blueprint（API 1 / API 2），在 create_app 注册
```

**引擎参照**：`dev_logs/10_剧本引擎与事件注入_Script_Engine.md`、`dev_logs/11_持久化与进程通信_Persistence_IPC.md`。  
**群聊正文**：查 `direct_message` 且 `channel_type='GRP'`（`attempted_at` 为 world tick）；**不查** `group_event`（仅存 join/leave/kick）。

---

## 〇、 进程与数据目录约定

Flask 与 Runner **必须读写同一份** `world.db`：

| 配置项 | 约定值 |
|--------|--------|
| `simulation_id` | `hbm_memory_war` |
| `sim_dir` | 默认 `agent_world/hbm_demo/sim/hbm_memory_war/`（环境变量 `HBM_SIM_DIR` 可覆盖） |
| `world.db` | `{sim_dir}/world.db` |
| IPC 目录 | `{sim_dir}/ipc_commands/`、`{sim_dir}/ipc_responses/` |
| Tick 同步文件 | `{sim_dir}/env_status.json` — **由 `run_hbm.py` 写入**（见下文） |

`env_status.json` 格式（Runner 每 Tick 更新）：

```json
{
  "status": "running",
  "current_tick": 42,
  "timestamp": "2026-05-23T14:00:00Z"
}
```

**注意**：`IPCServer.start/stop` 也会写 `env_status.json`，但**不含** `current_tick`。`run_hbm.py` 应在每次写 tick 时 **merge 保留** `current_tick`，或 monkey-patch `_update_env_status`，避免 Flask 读到缺失 tick 的 JSON。

**Runner 启动**：

```bash
python -m agent_world.hbm_demo.run_hbm \
  --config agent_world/hbm_demo/hbm_scenario.yaml \
  --sim-dir agent_world/hbm_demo/sim/hbm_memory_war/
```

**Flask 启动**（同一 `sim_dir`）：

```bash
HBM_SIM_DIR=agent_world/hbm_demo/sim/hbm_memory_war/ \
FLASK_APP=agent_world.app:create_app flask run --port 5000
```

---

## 一、 引擎能力 vs 应用层职责

以下基于**当前引擎源码**（只读使用，不修改 `agent_world/world/*`、`script/effects/*` 等核心）。

### 1.1 引擎已提供（可直接 import）

| 模块 | 能力 | 典型入口 |
|------|------|----------|
| **WorldStep** | 11 步 Tick 管线 | `await world_step.run_one_tick()` |
| **WorldDB** | F2F/RDC/GRP 落库与查询 | `insert_message`, `fetch_f2f_history_at`, `agents_at` |
| **ScriptEngine** | YAML 加载、按 tick 触发 Effect | `load_from_yaml`, `due_events`, `apply`, `notify_agent` |
| **ScriptLoader** | 校验并实例化单条 Event | `ScriptLoader.load_dict({"events":[event]}, existing_ids=...)` |
| **IPC** | 文件 IPC 协议 | `SimulationIPCClient`, `IPCServer` + `register_handler` |
| **ActionDispatcher** | 工具分发 | `send_message(target)`, `relation_change(dst, op=remove)` 等 |
| **Flask 壳** | 已有 stub 路由 | `/api/simulation/simulations/<id>/inject-event` 等 |

**注意**：引擎**没有** `ScriptEngine.inject_event()`；热注入与 `run_agent_world_simulation._wire_ipc_handlers` 相同——`ScriptLoader.load_dict` 后写入 `script_engine.events_by_id`，再在 `run_one_tick` 里由 `due_events` + `apply` 执行。

### 1.2 引擎缺口 — 由应用层补齐（不改引擎）

| 缺口 | 应用层做法 |
|------|------------|
| `DialogueInjectionEffect` 需要 `agent.update_memory()` | **`HbmAgent.update_memory()`** 把玩家台词写入内存，并在 `_observation_to_text` 中展示 |
| `BroadcastEventEffect` 与 `WorldDB.insert_message` API 不一致 | **不用该 Effect**；`broadcast_helper.py` 在 Runner 内调用 `WorldDB.insert_message(channel_type='RDC', ...)` |
| `PlaceMutationEffect` 仅改内存 attrs | 接受 MVP：节点 B 的 `behavior_hint` **进程内有效**；或 Turn 12 用 IPC `RELOAD_SCRIPTS` 重载带新 hint 的 YAML |
| `MoveEffect` 不传 `world.t` | 路由 Move 优先用 IPC **`MOVE_AGENT`**（`place_store.move(..., world=, t=)`） |
| `relation_change` LLM 用 `target/break`，Dispatcher 用 `dst/remove` | **`HbmAgent` dispatch 前做参数映射** |
| `DemoAgent` 不渲染 `scripted_notification` | **`HbmAgent` 在 user prompt 中输出 `obs.scripted_notification`** |
| Runner 无 LLM Agent 循环 | **`run_hbm.py` 参照 `demo/run_demo.py` 注册 7 个 `HbmAgent`** |
| IPC inject 后不跑 Tick | **`run_hbm.py` 的 inject handler 内循环 `run_one_tick` 3~8 次** |
| `env_status.json` 无 `current_tick` | **`run_hbm.py` 每 Tick 写入 `{status, current_tick, timestamp}`** |
| Flask `simulation.py` 为 stub | **`hbm_demo/routes.py` 实现 API 1/2**；底层用 `SimulationIPCClient` + 只读 `WorldDB` |

### 1.3 不使用的引擎路径

| 不用 | 原因 |
|------|------|
| `agent_world/demo/run_demo.py` 直接作为主入口 | 无 IPC、无 ScriptEngine；仅作**接线参考** |
| `run_agent_world_simulation.py` 单独作为主入口 | 默认**不注册 LLM Agent**，Tick 空转；仅作 **IPC handler 参考** |
| `BroadcastEventEffect` | 落库 API 与 schema 不一致 |
| 修改 `dispatcher.py` / `broadcast_event.py` 等 | 超出本项目范围 |

---

## 二、 系统整体架构

```text
[ Web 前端 ]
       |  POST /api/hbm/.../player-turn          (API 1，hbm_demo/routes.py)
       |  GET  /api/hbm/.../action-result        (API 2)
       v
[ Flask — game_service.py + ipc_helper.py ]
       |  Stats 打分（DeepSeek-V4-Pro，Flask session）
       |  组装 Script Event JSON
       |  ipc_helper.send_inject_batch（单次 IPC：events + 可选 broadcast + tick_count）
       |  send_move_agent（路由 Move）
       |  只读 WorldDB + env_status.json
       v
[ run_hbm.py 子进程 ]
       IPCServer（扩展 inject handler：broadcast → load_dict×N → run_one_tick×N）
       ScriptEngine + WorldStep + 7×HbmAgent
       写 world.db、env_status.json
```

**玩家输入**：Flask 组装 `DialogueInjectionEffect`（`trigger.expr: "True"`），目标为 `WorldDB.agents_at(place_id)` 返回的 agent_id 列表。**Phase 3 默认使用批量 inject**（见第七节），单次 IPC 传入 `events: [...]`，避免 6 次 inject × 6 tick 的指数级 Tick 推进。

**系统广播（AMD 快讯）——进程边界**：

Flask 与 Runner 是**不同进程**，Flask **不能** `import broadcast_helper` 直接写库（`attempted_at` 会与 Runner 的 `world_state.clock.t` 错位，Agent 感知失效）。

正确调用链：

```text
Flask game_service
  → ipc_helper.send_inject_batch(broadcast={place_id, message}, events=[...], tick_count=6)
  → IPC INJECT_SCRIPT_EVENT
  → run_hbm.handle_inject：
       1) broadcast_helper.broadcast_place(world_db, place_id, message, t=world_state.t)
       2) ScriptLoader.load_dict 加载 events
       3) run_one_tick × tick_count
```

**不**注入 `BroadcastEventEffect`；**不**在 Flask 进程写 `world.db`。

---

## 三、 API 详细设计

路由前缀建议 **`/api/hbm/simulations/<sim_id>/`**（与引擎 stub 的 `/api/simulation/...` 并存，避免改引擎路由文件）。

### API 1：发起交互（player-turn）

**Endpoint**: `POST /api/hbm/simulations/<sim_id>/player-turn`

**Request**：

```json
{
  "player_text": "我的算法能把显存消耗降低 80%。",
  "place_id": "nvidia_reception",
  "phase": "Phase 1",
  "player_turn": 1
}
```

**Flask / game_service 流程**（顺序有依赖，勿颠倒）：

1. 读 `{sim_dir}/env_status.json` → `start_tick`；生成 `task_id`，session 存 `{ start_tick, place_id, phase, player_turn, phase2_start_tick?, stats, ... }`。
2. **Stats 打分**：调用 DeepSeek-V4-Pro，解析 JSON delta 并累加 session 中的 Vision/Execution/Trust/Burnout（详见第四节）。
3. **路由预判（Turn 4 Bad End）**：若 `player_turn == 4` 且 `vision + execution < 15`，**跳过 inject**，直接返回 `game_over` / `bad_reject`（`public_messages` 为 Flask **stub 文案**，见下方说明）；**不进入 API 2**。
4. **immediate_msg**：调用 DeepSeek 生成一行动态描写；可与步骤 5 并行，≤1s 超时则占位句 + 仍 `processing`。
5. **组装 inject 列表**：`WorldDB.agents_at(place_id)`（Flask 只读连接）；为每个 agent_id 生成一条 `DialogueInjectionEffect` event。
6. **Turn 16 剧本事件**：若 `player_turn == 16` **且** session `phase == "Phase 3"`，payload 追加 `broadcast: {...}` + Sam DialogueInjection（agent 7）。
7. **单次 IPC**：`ipc_helper.send_inject_batch(events=[...], broadcast=..., tick_count=6)` → Runner handler 内 `broadcast_helper`（若有）→ `load_dict` → `run_one_tick` **3~8 次** → 更新 `env_status.json`。
8. **路由副作用**（`player_turn` ∈ {4, 12, 20, 25} 且条件满足）：IPC `send_move_agent` / inject `PlaceMutationEffect`；节点 A 通过后写入 `phase2_start_tick = env_status.current_tick`。
9. **Turn 25 结局**：inject 仍执行（让 Jensen 做最后一轮反应），但 API 1 **直接返回** `status: "completed"` + `ending_id`（见第四节节点 D）；**无需** API 2 轮询。

**Turn 4 Bad End 的 `public_messages`**：MVP 由 Flask **硬编码 stub**（不 inject、不跑 Tick），前端直接展示；可选后续改为 inject 前台 F2F 后再判 Bad End。

**DialogueInjection 模板**：

```json
{
  "event": {
    "id": "task_<uuid>_agent_<id>",
    "trigger": { "type": "at_condition", "expr": "True" },
    "effect": {
      "type": "dialogue_injection",
      "agent_id": 1,
      "text": "玩家说：我的算法能把显存消耗降低 80%。"
    }
  }
}
```

**Response（正常）**：

```json
{
  "success": true,
  "data": {
    "task_id": "task_9527",
    "immediate_msg": "前台接待员微微挑眉…",
    "status": "processing"
  }
}
```

**Response（Bad End，Turn 4）**：

```json
{
  "success": true,
  "data": {
    "status": "game_over",
    "ending_id": "bad_reject",
    "public_messages": [
      { "sender": "接待前台", "content": "保安，请这位先生离开。", "type": "F2F" }
    ]
  }
}
```

**Response（Turn 25 结局，跳过 API 2）**：

```json
{
  "success": true,
  "data": {
    "status": "completed",
    "ending_id": "ending_join_nvidia",
    "stats_update": { "vision": 35, "execution": 28, "trust": 45, "burnout": 42 },
    "current_phase": "Phase 4"
  }
}
```

---

### API 2：轮询结果（action-result）

**Endpoint**: `GET /api/hbm/simulations/<sim_id>/action-result?task_id=<id>&place_id=<id>`

**读取**：

- `current_tick` ← `env_status.json`（若缺少 `current_tick` 字段，视为 Runner 未就绪，返回 `processing`）
- 消息 ← 只读 `world.db`（Flask 独立连接；见 §6.2.1 超时与重试，引擎当前未启用 WAL）
- **RDC 结束条件白名单** ← session 中的 **`phase`**（非 `player_turn` 推导）

**结束条件**（满足任一 → `completed`）：

1. `current_tick >= start_tick + 3` **且** 存在与本回合**相关**的新活动：
   - **F2F**：`place_id = 当前玩家房间` 且 `attempted_at > start_tick`
   - **RDC（收窄）**：`attempted_at > start_tick` 且 `(sender_id, recipient_id)` 属于当前 Phase 的**关注列表**（见下表），避免其他房间 NPC 私聊误触发完成
   - **GRP**：`attempted_at > start_tick` 且 `group_id ∈ {100, 200}`
2. `current_tick >= start_tick + 8`（超时兜底）

| Phase | RDC 关注（`(sender_id, recipient_id)` 任一方向匹配即可） |
|-------|----------------------------------------------------------|
| 1 | `(1, 2)` — 前台报 Jensen |
| 2 | `(2, 3)`, `(3, 2)` — Jensen ↔ Tech VP |
| 3 | `(2, 3)`, `(3, 2)`；`(4, 2)`, `(5, 2)`, `(6, 2)` — CEO 攻击/谈判；`(4, 5)`, `(4, 6)`, `(5, 6)` — 存储联盟密谋 RDC；`(7, 2)` — Sam 搅局 |
| 4 | `(2, 3)`, `(3, 2)` — 收尾协调；F2F 为主 |

**数据查询**：

| 字段 | 实现 |
|------|------|
| `public_messages` | `fetch_f2f_history_at(place_id, current_tick, start_tick)` + **game_service 用 scenario 映射 sender_id→name** |
| `observer_messages` | `SELECT * FROM direct_message WHERE channel_type='RDC' AND attempted_at > start_tick`（Phase 2 节点 B 判定用 **phase2_start_tick**，见第四节） |
| `group_messages` | `direct_message WHERE channel_type='GRP' AND attempted_at > start_tick` |

**Response 字段**：`status`, `end_tick`, `public_messages`, `observer_messages`, `group_messages`, `stats_update`, `current_phase`。

---

## 四、 Stats 与路由

Stats 由 **Flask `game_service`** 维护（Flask session 或 SQLite session 表），引擎 WorldState **不含** Vision/Execution/Trust/Burnout。

### 4.1 初始值

| 字段 | 初始值 | 说明 |
|------|--------|------|
| `vision` | 0 | 画大饼、商业谈判 |
| `execution` | 0 | 技术逻辑严密性 |
| `trust` | 10 | 英伟达阵营对玩家的基础信任 |
| `burnout` | 0 | 抗压；越高越接近崩溃 |

### 4.2 每 Turn 打分（DeepSeek-V4-Pro）

**调用时机**：API 1 步骤 2，**在 inject 之前**（Stats 仅依赖 `player_text` + session 上下文）。

**System Prompt 要点**：你是游戏裁判；根据玩家本回合发言与当前 Phase，输出四维 delta；Burnout 在 Phase 3 对攻击性发言加重惩罚。

**Request 上下文**：`player_text`、`phase`、`player_turn`、当前四维累计值。

**Response JSON schema**（严格解析，缺字段视为 0）：

```json
{
  "vision_delta": 0,
  "execution_delta": 0,
  "trust_delta": 0,
  "burnout_delta": 0,
  "reason": "一句话理由"
}
```

**累加规则**：`stats[field] += delta`；`burnout`  clamp 到 `[0, 100]`，其余 clamp 到 `[0, 999]`。

**Burnout 增量参考**（写入 Stats Prompt，非硬编码）：

| Phase | 典型增量 |
|-------|----------|
| 1–2 | 0~+2（正常对话） |
| 3 | 被 CEO 羞辱/威胁时 +3~+8；成功反击 -1~+1 |
| 4 | 0~+2 |

**Trust 增量参考**：Phase 2 技术阐述 +1~+4；Phase 4 合理讨价还价 +2~+5；明显欺骗 -5~+0。

### 4.3 路由 Turn 与条件

**路由 Turn**：4 / 12 / 20 / 25（与 `1_story_prototype.md` 一致）。

| 节点 | 条件 | 副作用 |
|------|------|--------|
| A → Phase 2 | Turn 4：`vision + execution ≥ 15`；否则 **Bad End**（步骤 3 早退） | IPC `MOVE_AGENT` agent 2 → `jensen_private_room`；session `phase=Phase 2`，`place_id=jensen_private_room`，**`phase2_start_tick=current_tick`** |
| B → Phase 3 | Turn 12：`execution ≥ 20` 且 Phase 2 至今存在 **Tech VP(3)→Jensen(2)** 正面 RDC | `MOVE_AGENT` 2 → `negotiation_room` + `PlaceMutationEffect`；session `phase=Phase 3` |
| C → Phase 4 | Turn 20：`burnout < 80` 且 `vision ≥ 30` | 三次 `MOVE_AGENT`：agent 4/5/6 → `nvidia_reception`；session `phase=Phase 4` |
| D 结局 | Turn 25：见下表 | API 1 返回 `completed` + `ending_id`，**跳过 API 2** |

**节点 B 正面 RDC 判定**：

- 查询：`direct_message WHERE channel_type='RDC' AND sender_id=3 AND recipient_id=2 AND attempted_at >= phase2_start_tick`
- **关键词命中**（content 包含任一即可）：`可行`、`核武器`、`理论上成立`、`理论上可行`、`成立`
- 或：DeepSeek 二分类（`positive` / `negative`），MVP 优先关键词

**节点 D 结局判定**（Turn 25，inject 之后）：

1. DeepSeek 分类 `player_text` 意图：`join_nvidia` | `seed_round` | `ambiguous`
2. 映射规则：

| ending_id | 条件 |
|-----------|------|
| `ending_join_nvidia` | `trust ≥ 40` 且意图 `join_nvidia` |
| `ending_seed_round` | `trust ≥ 25` 且意图 `seed_round` |
| `ending_cold_deal` | 其余（信任不足或意图模糊） |

**Move 执行**：对 Jensen / CEO 等使用 IPC `send_move_agent`（应用层），不用有缺陷的 `MoveEffect` 落库。

**节点 B PlaceMutation**：inject `PlaceMutationEffect`（内存 attrs）；`HbmAgent` 的 Perception 读 `place.attrs.behavior_hint` 即可在进程内生效。

---

## 五、 路由与剧本 Event 示例

`trigger` 均为 `{ "type": "at_condition", "expr": "True" }`。Event `id` **必须每回合唯一**（含 `task_id` 前缀），避免 `ScriptLoader` 重复 id 拒绝加载。

### 节点 A — Move Jensen（应用层用 IPC MOVE_AGENT）

```json
POST IPC MOVE_AGENT: { "agent_id": 2, "place_id": "jensen_private_room" }
```

### 节点 B — Move + PlaceMutation

```json
POST IPC MOVE_AGENT: { "agent_id": 2, "place_id": "negotiation_room" }
```

```json
{
  "event": {
    "id": "route_b_mutate_<task_id>",
    "trigger": { "type": "at_condition", "expr": "True" },
    "effect": {
      "type": "place_mutation",
      "place_id": "negotiation_room",
      "attrs_patch": {
        "behavior_hint": "死一般的寂静，所有人都被 Jensen 带来的底牌震撼了…"
      }
    }
  }
}
```

### 节点 C — 逐 CEO 移出（三次 MOVE_AGENT）

`agent_id` 4、5、6 → `nvidia_reception`。

### Turn 16 — AMD + Sam（单次 IPC batch）

Flask 调用 `ipc_helper.send_inject_batch`，payload 示例：

```json
{
  "broadcast": {
    "place_id": "negotiation_room",
    "message": "彭博终端快讯：AMD 宣布下一代 MI400 将采用全新自研显存架构…"
  },
  "events": [
    {
      "id": "task_<uuid>_agent_2",
      "trigger": { "type": "at_condition", "expr": "True" },
      "effect": { "type": "dialogue_injection", "agent_id": 2, "text": "玩家说：…" }
    },
    {
      "id": "phase3_sam_nudge_<task_id>",
      "trigger": { "type": "at_condition", "expr": "True" },
      "effect": {
        "type": "dialogue_injection",
        "agent_id": 7,
        "text": "系统指令：OpenAI 对稀疏注意力算法极度感兴趣，请立刻 RDC 私信 Jensen，暗示愿意高价截胡。"
      }
    }
  ],
  "tick_count": 6
}
```

Runner 内 `broadcast_helper.broadcast_place` **落库规范**（对每个 `place_store.agents_at(place_id)` 的 recipient）：

```python
await world_db.insert_message(
    sender_id=-1,
    recipient_id=agent_id,
    group_id=None,
    channel_type="RDC",
    content=message,
    place_id=place_id,
    attempted_at=t,
    arrive_at=t,
    delivered=1,
)
```

`delivered=1` 且 `arrive_at=t` 保证下一 Tick `PerceptionBuilder.fetch_arrived_for` 可见；**必须在 Runner 进程**用 `world_state.clock.t` 作为 `t`。

---

## 六、 应用层实现清单（hbm_demo）

| # | 模块 | 任务 |
|---|------|------|
| 1 | `hbm_agent.py` | `update_memory`；渲染 `scripted_notification`；`relation_change` 参数 `target→dst`, `break→remove` |
| 2 | `run_hbm.py` | 见 **6.1 启动清单**；inject handler 支持 `broadcast` + `events[]` + `tick_count` |
| 3 | `broadcast_helper.py` | Runner 内 `broadcast_place(world_db, place_id, message, t)`，按上文落库规范 |
| 4 | `ipc_helper.py` | Flask 侧 `send_inject_batch` / `send_move_agent` 封装（扩展 payload，不改 `ipc/commands.py`） |
| 5 | `game_service.py` | Stats、路由、Phase session、id→name、API 2 查询与结束条件 |
| 6 | `routes.py` | API 1 `player-turn`、API 2 `action-result`；注册 Blueprint |
| 7 | `hbm_scenario.yaml` | 自 `3_prompt_management.md` 生成 |
| 8 | `app/__init__.py` | 注册 `hbm_bp`（**仅一行注册**，不改引擎逻辑） |

### 6.1 `run_hbm.py` 启动清单（必读）

参照 `demo/run_demo.py` 的 `_build_kernel` + `_seed_world`，并叠加 `run_agent_world_simulation._wire_ipc_handlers` 的 IPC 注册；**以下项缺一不可**：

| 步骤 | 要求 |
|------|------|
| 世界 seed | `_seed_world()` 写入 places / agents / relations / groups / coverage |
| Agent 注册 | 创建 7×`HbmAgent` 后 **`world_state.register_agent(aid, agent)`**（`DialogueInjectionEffect` 读 `world.agents`） |
| PerceptionBuilder | **`script_engine=script_engine`**（非 `None`；否则 `pending_for` / 剧本通知不可用） |
| WorldStep | 传入 `script_engine`；`ActionDispatcher` 同样传入 `script_engine` |
| **Tick 推进** | **见 §6.2 推荐方案**：无后台空转主循环；仅 inject handler（及路由后的补充 inject）推进 tick；Runner 常驻 IPCServer，回合间 world 冻结 |
| inject handler | **覆盖**参考实现「只 load_dict、不跑 Tick」的行为；支持 batch `events[]` + `broadcast` |
| IPC MOVE_AGENT | 注册 handler，内部 `place_store.move(..., world=world_state, t=world_state.t)` |

**注意**：`run_agent_world_simulation` 的 inject handler **不跑 Tick**；HBM 必须在 `run_hbm` 中扩展，否则 API 2 永远等不到 Agent 活动。

### 6.2 Tick 并发与实现注意事项（开发必读）

#### Tick 并发模型（**最重要**）

`WorldStep` **没有**全局 tick 锁。若「后台持续 `run_one_tick`」与 inject handler 内再跑 3–8 tick **并发**执行，`clock.advance` 会被双重调用，Agent 状态与 `world.db` 会错乱。

**推荐实现（回合制 Demo，默认采用）**：

```text
无后台空转主循环。
仅 inject handler（及必要时 MOVE 后的单次 inject）推进 tick。
Runner 进程常驻 IPCServer；回合之间 world 冻结在上一 tick。
```

`handle_inject` 末尾的 tick 循环即**唯一** tick 推进入口；`MOVE_AGENT` 仅改位置，不 advance tick。节点 B 的 `PlaceMutationEffect` 在 `MOVE_AGENT` 之后用**第二次** `send_inject_batch`（单 event + `tick_count`）注入并跑 tick（见下文 §6.2.4）。

**备选（不推荐 MVP）**：若坚持后台主循环，须加全局 `asyncio.Lock` 包裹**所有** `run_one_tick` 调用（主循环与 inject handler 共用同一把锁）。

#### 6.2.1 SQLite 并发读

API 2 说明中「WAL 模式」为理想态；当前引擎 `WorldDB` **未**启用 WAL。Flask 开第二个连接轮询 `world.db` 时，可能与 Runner 写库偶发 `database is locked`。

**实现建议**（`game_service.py` / Flask 侧只读 `WorldDB`）：

- `sqlite3.connect(path, timeout=5.0, check_same_thread=False)` + 读失败指数退避重试；或
- API 2 优先在 IPC inject **返回后**再读库（此时 Runner 写锁压力最低）。

#### 6.2.2 Session 为权威来源

API 1 请求体中的 `place_id` / `phase` **不得**直接作为 inject 与路由依据。以 Flask **session** 为准；路由节点通过后由 `game_service` 更新 session。前端传值仅作校验/展示，避免节点 A 通过后仍向 `nvidia_reception` inject。

| 字段 | 权威来源 | 更新时机 |
|------|----------|----------|
| `phase` | session | 节点 A/B/C 通过后 |
| `place_id` | session | 同上（与 phase 同步） |
| `phase2_start_tick` | session | 节点 A 通过后写入 `env_status.current_tick` |
| inject 目标 | `WorldDB.agents_at(session.place_id)` | 每 Turn 读取 session |

#### 6.2.3 次要映射与顺序

- **`observer_messages`**：`sender_id == -1`（系统广播）映射显示名为 **「彭博终端」** 或 **「系统」**（`game_service` id→name 表）。
- **节点 B 顺序**：① IPC `MOVE_AGENT`（Jensen → `negotiation_room`）→ ② `send_inject_batch` 单条 `PlaceMutationEffect` + `tick_count` → ③ 更新 session `phase=Phase 3`。

---

## 七、 IPC 与应用层 helper 参考

### 7.1 Flask 侧 `ipc_helper.py`

引擎 `SimulationIPCClient.send_inject_script_event` 仅传 `{event}`，无 `tick_count` / batch / broadcast。在 **hbm_demo** 新增 thin wrapper，调用 `SimulationIPCClient.send_command`，**不修改** `ipc/commands.py`：

```python
from agent_world.app.services.simulation_ipc import SimulationIPCClient
from agent_world.ipc.commands import CommandType

def send_inject_batch(
    client: SimulationIPCClient,
    *,
    events: list[dict],
    tick_count: int = 6,
    broadcast: dict | None = None,
    timeout: float = 60.0,
):
    payload = {"events": events, "tick_count": tick_count}
    if broadcast:
        payload["broadcast"] = broadcast
    resp = client.send_command(
        CommandType.INJECT_SCRIPT_EVENT, payload, timeout=timeout
    )
    return resp
```

兼容旧 payload：handler 若收到单个 `event`（无 `events`），视为 `[event]`。

### 7.2 Runner 侧 `handle_inject`（`run_hbm.py`）

```python
async def handle_inject(payload):
    start_tick = world_state.clock.t

    bc = payload.get("broadcast")
    if bc:
        await broadcast_helper.broadcast_place(
            world_db, bc["place_id"], bc["message"], t=world_state.clock.t
        )

    events = payload.get("events") or []
    if payload.get("event"):
        events = [payload["event"]]

    if events:
        result = ScriptLoader.load_dict(
            {"events": events}, existing_ids=script_engine.loaded_event_ids
        )
        for ev in result.events:
            script_engine.events_by_id[ev.id] = ev
            script_engine.loaded_event_ids.add(ev.id)

    n = int(payload.get("tick_count", 6))
    for _ in range(max(3, min(n, 8))):
        await world_step.run_one_tick()
        write_env_status(sim_dir, world_state.clock.t)

    return {
        "start_tick": start_tick,
        "end_tick": world_state.clock.t,
        "world_t": world_state.clock.t,
    }
```

### 7.3 Phase 3 批量 inject 建议

Phase 3 每 Turn 对 Agent 2/3/4/5/6 各一条 DialogueInjection，**必须**合并为单次 `send_inject_batch(events=[...])`，否则 6×6 tick 会导致单回合 Tick 推进过多、API 2 行为异常。
