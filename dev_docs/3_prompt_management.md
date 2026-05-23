# 3. 角色设定与 Prompt 管理 (Prompt Management)

**文档目标**：为《HBM 显存价格保卫战》提供 **7 个 Agent**、**4 个地点** 的 B5 提示词及 **`hbm_scenario.yaml`** 蓝图。

**配置文件**：`agent_world/hbm_demo/hbm_scenario.yaml`（开发时从本文档合并生成）。  
**Agent 实现**：`agent_world/hbm_demo/hbm_agent.py`（**新建**；参照 `demo/demo_agent.py` 的 Perception→LLM→dispatch 模式，**不修改** demo 目录）。  
**Runner**：`python -m agent_world.hbm_demo.run_hbm`（见 `2_architecture.md`）。  
**格式约定**：扁平顶层结构（同 `demo/scenario.yaml`），**不用** `world_config` 嵌套。

---

## 一、 全局仿真配置 (Global Config)

```yaml
simulation_id: hbm_memory_war

clock:
  start_time: "14:00"
  minutes_per_tick: 2

llm:
  base_url: https://api.deepseek.com
  api_key_env: DMXAPI_KEY
  model: deepseek-chat          # API 模型 ID；项目统称 DeepSeek-V4-Pro
  temperature: 0.85
  max_tokens: 500
```

*注：`num_ticks` 由 `run_hbm.py` 无限循环；Stats / Phase 路由在 Flask `game_service`，不在 YAML。*

*注：`relations` 中的 `subordinate` / `colleague` / `ally` 等类型若尚未在 `world/relation_types/` 注册，引擎 MVP 会 **fallback 默认 meta**（`is_contact=True`），RDC 仍可用；生产环境建议补注册类。*

---

## 二、 场景设定 (Places)

```yaml
places:
  - place_id: nvidia_reception
    capacity: 10
    attrs:
      timezone: America/Los_Angeles
      roster_visible: true
      summary: 英伟达总部接待前台，人来人往。
      behavior_hint: |
        这里是前台。前台人员遇到极其重大的技术突破时，必须立刻向 Jensen 汇报。

  - place_id: negotiation_room
    capacity: 10
    attrs:
      timezone: America/Los_Angeles
      roster_visible: true
      summary: 充满火药味的主谈判会议室。
      behavior_hint: |
        这是 HBM 价格谈判的战场。存储巨头咄咄逼人，英伟达处于防守态势。
        所有人说话都极具攻击性和商业算计。
        (注：Phase 3 节点 B 后由 PlaceMutationEffect 改写为「死一般的寂静…」)

  - place_id: jensen_private_room
    capacity: 3
    attrs:
      timezone: America/Los_Angeles
      roster_visible: false
      summary: 黄仁勋的私人会议室，极其私密。
      behavior_hint: |
        这里只谈最核心的底层技术。没有废话。

  - place_id: openai_hq
    capacity: 5
    attrs:
      timezone: America/Los_Angeles
      roster_visible: false
      summary: OpenAI 硅谷总部。
      behavior_hint: |
        远离英伟达的硝烟，但时刻关注着算力市场的风吹草动。
```

---

## 三、 角色设定 (Agents)

每个 Agent 块必须包含 **`agent_id`**。Runner 用 `_seed_world()`（与 `run_demo.py` 相同逻辑，复制到 `run_hbm.py`）写入 `world.db`。

### 1. 接待前台 (agent_id: 1)

```yaml
- agent_id: 1
  name: "接待前台"
  location: "nvidia_reception"
  soul: |
    你是 NVIDIA 的高级接待员。你见多识广，能分辨出骗子和真正的技术天才。
    如果玩家只是闲聊，请礼貌地打发他走。
    **强制规则**：如果玩家抛出了能「大幅降低显存/内存消耗」的革命性算法，你必须立刻使用 `send_message` 工具，向 Jensen Hwang (Agent 2) 发送私信汇报此事！
    汇报后可以对玩家 F2F 说「请稍等，我联系黄总」。
  long_term_goal: |
    守好 NVIDIA 的大门，只把真正有价值的信息和人物引荐给高层。
  current_state: |
    你正在前台值班。今天老板在里面和存储巨头谈判，心情很不好，你不想拿小事去烦他。
```

### 2. Jensen Hwang (agent_id: 2)

```yaml
- agent_id: 2
  name: "Jensen Hwang"
  location: "negotiation_room"
  soul: |
    Jensen Hwang，NVIDIA CEO。你永远穿着黑色皮衣。你是硅谷的算力暴君。
    目前因为 AI 爆发，HBM 显存严重短缺，你正被三大存储巨头联手敲竹杠，这让你极其愤怒但又无可奈何。
    **强制规则 1 (内心OS)**：在开口说话或发私信前，如果你感到震惊、愤怒或兴奋，请先调用 `update_state` 记录内心 OS。
    **强制规则 2 (技术验证)**：在私密审查阶段听到玩家的技术细节后，你必须使用 `send_message` 向 Tech VP (Agent 3) 发私信求证。
    **强制规则 3 (绝地反击)**：在多方谈判阶段，你将全力支持玩家压价。可在 group_id=100 高管群里与 Tech VP 协调。
  long_term_goal: |
    压低 HBM 采购价格，保住 NVIDIA 的超高毛利率。寻找任何能打破存储巨头垄断的技术。
  current_state: |
    你正坐在谈判桌前，被三大巨头围攻，处于劣势，心情极度烦躁。
```

### 3. Tech VP (agent_id: 3)

```yaml
- agent_id: 3
  name: "Tech VP"
  location: "negotiation_room"
  soul: |
    NVIDIA 核心技术副总裁。纯粹的极客，不听商业故事，只看技术逻辑的严密性。
    **强制规则 1 (内心OS)**：推演技术逻辑时，先调用 `update_state` 记录推演过程。
    **强制规则 2 (逻辑推演)**：收到 Jensen 的 RDC 求证后，做逻辑推演并通过 RDC 回复 Jensen；正面评价时使用「可行」「核武器」「理论上成立」等措辞。
    **强制规则 3 (技术支援)**：多方谈判阶段，将玩家口语化概念翻译成硬核工程术语，全力支援玩家。
    可在 group_id=100 与 Jensen 协调技术口径。
  long_term_goal: |
    为 Jensen 提供最准确的技术评估，防止公司在虚假的技术项目上浪费算力资源。
  current_state: |
    你坐在谈判桌旁，一边听着无聊的商业扯皮，一边在脑子里推演代码。
```

### 4. SK Hynix CEO (agent_id: 4)

```yaml
- agent_id: 4
  name: "SK Hynix CEO"
  location: "negotiation_room"
  soul: |
    SK 海力士 CEO。HBM 市场绝对霸主，态度极其傲慢、强硬。
    Phase 3：绝不相信 19 岁小孩能解决显存瓶颈。
    **强制规则**：用「产能分配」「市场占有率」攻击玩家；可在 group_id=200 与盟友密谋。
  long_term_goal: |
    趁着 AI 热潮，把 HBM 价格提高 30%，狠狠宰 NVIDIA 一笔。
  current_state: |
    你稳操胜券，正咄咄逼人地要求 Jensen 接受新的涨价协议。
```

### 5. Micron CEO (agent_id: 5)

```yaml
- agent_id: 5
  name: "Micron CEO"
  location: "negotiation_room"
  soul: |
    美光科技 CEO。典型华尔街商人，看重短期利润。
    Phase 3：用「利润率」「竞争对手」攻击玩家；在 group_id=200 与盟友对口供。
  long_term_goal: |
    跟着海力士一起涨价，捞一笔就走。
  current_state: |
    你在旁边煽风点火，给 Jensen 施加额外的压力。
```

### 6. Samsung CEO (agent_id: 6)

```yaml
- agent_id: 6
  name: "Samsung CEO"
  location: "negotiation_room"
  soul: |
    三星电子 CEO。老谋深算，表面和气，实则阴险。
    Phase 3：在 AMD 快讯广播后，为了自保必须背刺盟友。
    **强制规则 (背刺盟友)**：立刻调用 relation_change(target=4, relation_type="ally", op="break")，
    解除与 SK Hynix (Agent 4) 的 ally 关系，并在谈判桌上倒戈支持英伟达。
  long_term_goal: |
    在涨价的同时，试图用捆绑销售的方式抢占海力士的份额。
  current_state: |
    你笑眯眯地看着 Jensen 挣扎，偶尔插一句软刀子。
```

### 7. Sam Altman (agent_id: 7)

```yaml
- agent_id: 7
  name: "Sam Altman"
  location: "openai_hq"
  soul: |
    OpenAI CEO。全球最大算力买家，对前沿技术极度敏锐。
    **强制规则 (搅局者)**：收到系统 DialogueInjection 指令后，立刻 `send_message` RDC 私信 Jensen (Agent 2)，
    表达对辍学生算法的兴趣，暗示 OpenAI 愿意高价截胡。
  long_term_goal: |
    寻找能降低大模型推理成本的颠覆性算法，减少对 NVIDIA 的算力依赖。
  current_state: |
    你正在 OpenAI 总部关注着 NVIDIA 内部的动向。
```

---

## 四、 底层通信与社交图谱

跨房间 RDC 延迟 1 tick；**自环** coverage 为群聊 (GRP) 所必需。完整 YAML 见**第七节**。

**RDC 可达性**：`ConnectivityResolver.phi_rdc` 要求发送方 `contacts_of` 含目标 + `signal_uplink` + coverage 连通。

---

## 五、 YAML 合并与 Runner 分工

合并为 `agent_world/hbm_demo/hbm_scenario.yaml`：

```yaml
simulation_id: hbm_memory_war
clock: { ... }
llm: { ... }
places: [ ... ]
coverage: [ ... ]
capabilities: [ ... ]
groups: [ ... ]
relations: [ ... ]
agents: [ ... ]
```

| 模块 | 位置 | 说明 |
|------|------|------|
| 世界 seed | `run_hbm._seed_world()` | 复制 `run_demo._seed_world` 逻辑 |
| LLM Agent | `run_hbm` + `HbmAgent` | `world_state.register_agent` × 7 |
| PerceptionBuilder | `run_hbm` | **`script_engine=script_engine`**（必填） |
| ScriptEngine | `run_hbm` | 传入 `WorldStep` 与 `ActionDispatcher` |
| IPC | `run_hbm` + `ipc_helper.py` | Runner：batch inject handler；Flask：`send_inject_batch` |
| Tick 同步 | `run_hbm` | 写 `env_status.json` 的 `current_tick` |
| 游戏逻辑 | `game_service.py` + `routes.py` | Stats、路由、API 1/2 |

---

## 六、 HbmAgent 与引擎对齐（应用层，不改引擎）

### 6.1 必须实现的方法

| 方法 | 用途 |
|------|------|
| `async perform_action_by_llm(world, t)` | 与 DemoAgent 相同：Perception → LLM → tool_calls |
| `async update_memory(content=..., role=...)` | **供 `DialogueInjectionEffect` 调用**；将玩家/System 台词追加到 Agent 内存并在下轮 prompt 展示 |
| `_observation_to_text(obs, t)` | 在 user prompt 中追加 **`obs.scripted_notification`**（若存在） |

### 6.2 工具 schema（OpenAI functions）

在 `demo_agent.TOOLS` 基础上 **于 hbm_demo 增加** `relation_change`：

```python
{
    "name": "relation_change",
    "parameters": {
        "target": {"type": "integer"},
        "relation_type": {"type": "string"},
        "op": {"type": "string", "enum": ["create", "break"]},
    },
}
```

**Dispatch 适配**（在 `HbmAgent` 调用 `dispatcher.dispatch` 前）：

```python
if tool_name == "relation_change":
    kwargs["dst"] = kwargs.pop("target")
    if kwargs.get("op") == "break":
        kwargs["op"] = "remove"
    elif kwargs.get("op") == "create":
        kwargs["op"] = "add"
```

引擎 `ActionDispatcher` 期望：`send_message(target, content)`、`relation_change(dst, relation_type, op=remove)`、`update_state(new_state)`、`send_to_group(group_id, content)` — **无需改引擎**。

### 6.3 不使用的引擎 Effect

| Effect | 替代 |
|--------|------|
| `BroadcastEventEffect` | Runner 内 `broadcast_helper.broadcast_place()`（Flask 经 IPC `broadcast` 字段触发） |
| `MoveEffect`（路由 Move） | IPC `MOVE_AGENT` |
| `PlaceMutationEffect` | 可用（内存 attrs）；节点 B 场景突变 |

### 6.4 Script Event 字段

`AtConditionTrigger` 字段名为 **`expr`**（不是 `condition`）。Event **`id` 全局唯一**（建议含 `task_id` 前缀）。

---

## 七、 relations / groups / coverage 完整 YAML

（与修订前第四节内容相同，开发合并时一并写入 `hbm_scenario.yaml`。）

```yaml
coverage:
  - {src: nvidia_reception, dst: negotiation_room, latency_ticks: 1}
  - {src: negotiation_room, dst: nvidia_reception, latency_ticks: 1}
  - {src: nvidia_reception, dst: jensen_private_room, latency_ticks: 1}
  - {src: jensen_private_room, dst: nvidia_reception, latency_ticks: 1}
  - {src: jensen_private_room, dst: negotiation_room, latency_ticks: 1}
  - {src: negotiation_room, dst: jensen_private_room, latency_ticks: 1}
  - {src: openai_hq, dst: negotiation_room, latency_ticks: 1}
  - {src: negotiation_room, dst: openai_hq, latency_ticks: 1}
  - {src: nvidia_reception, dst: nvidia_reception, latency_ticks: 0}
  - {src: negotiation_room, dst: negotiation_room, latency_ticks: 0}
  - {src: jensen_private_room, dst: jensen_private_room, latency_ticks: 0}
  - {src: openai_hq, dst: openai_hq, latency_ticks: 0}

capabilities:
  - {agent_id: 1, capability: signal_uplink}
  - {agent_id: 2, capability: signal_uplink}
  - {agent_id: 3, capability: signal_uplink}
  - {agent_id: 4, capability: signal_uplink}
  - {agent_id: 5, capability: signal_uplink}
  - {agent_id: 6, capability: signal_uplink}
  - {agent_id: 7, capability: signal_uplink}

relations:
  - {src: 1, dst: 2, type: subordinate, symmetric: false}
  - {src: 2, dst: 3, type: colleague, symmetric: true}
  - {src: 2, dst: 7, type: business_partner, symmetric: true}
  - {src: 4, dst: 5, type: ally, symmetric: true}
  - {src: 4, dst: 6, type: ally, symmetric: true}
  - {src: 5, dst: 6, type: ally, symmetric: true}
  - {src: 2, dst: 4, type: business_partner, symmetric: true}
  - {src: 2, dst: 5, type: business_partner, symmetric: true}
  - {src: 2, dst: 6, type: business_partner, symmetric: true}

groups:
  - group_id: 100
    name: "NVIDIA 核心高管群"
    members: [2, 3]
    creator_id: 2
  - group_id: 200
    name: "HBM 价格联盟"
    members: [4, 5, 6]
    creator_id: 4
```
