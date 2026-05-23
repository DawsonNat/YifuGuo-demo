# 1. 剧情原型与交互逻辑设计 (Story Prototype)

**文档目标**：将《HBM 显存价格保卫战》转化为可执行的 Multi-Agent 交互剧本，规划 20-25 个玩家 Turn 的状态机路由。

**配套文档**：API / Stats / 应用层实现见 `2_architecture.md`；Agent YAML 见 `3_prompt_management.md`。

**实现位置**：`agent_world/hbm_demo/`（新建应用包，**不修改** `agent_world/demo/` 与引擎核心）。

---

## 一、 场景与角色设定

### 1. 场景地点 (Places)
1.  **`nvidia_reception` (英伟达接待前台)**：玩家初始出生点。
2.  **`negotiation_room` (主谈判会议室)**：三大存储巨头逼宫英伟达的战场。（*注：氛围会在 Phase 3 发生突变*）
3.  **`jensen_private_room` (黄仁勋私人会议室)**：私密的技术验证空间。
4.  **`openai_hq` (OpenAI 总部)**：Sam Altman 所在的远程地点。

### 2. 出场角色 (Agents — 3 大阵营, 7 个实体 Agent)

**【玩家阵营】**
*   **玩家 (Player)**：无实体 Agent。Flask **`game_service`** 组装 `DialogueInjectionEffect`，由 Runner 注入当前 `place_id` 内 NPC；**`HbmAgent.update_memory()`** 负责让 LLM 看见玩家台词（见 `2_architecture.md` 1.2 节）。

**【英伟达阵营 (防守方)】**
1.  **接待前台 (Agent 1)**：位于 `nvidia_reception`。负责拦截与通报。
2.  **Jensen Hwang (Agent 2)**：初始位于 `negotiation_room`。被三大巨头逼迫，寻找破局点。
3.  **Tech VP (Agent 3)**：位于 `negotiation_room`。负责评估底层技术逻辑。

**【存储巨头阵营 (进攻方)】**
4.  **SK Hynix CEO (Agent 4)**：位于 `negotiation_room`。HBM 市场老大，态度最强硬。
5.  **Micron CEO (Agent 5)**：位于 `negotiation_room`。跟风涨价的华尔街商人。
6.  **Samsung CEO (Agent 6)**：位于 `negotiation_room`。老谋深算，随时准备背刺盟友。

**【第三方破局者】**
7.  **Sam Altman (Agent 7)**：位于 `openai_hq`。最大的算力买家，时刻关注技术突破。

---

## 二、 核心数值系统 (Stats)

由 Flask **`game_service`** 维护，每次玩家输入后调用 **DeepSeek-V4-Pro** 打分（细节见 `2_architecture.md` 第四节）：

*   **Vision (愿景值)**：画大饼、商业谈判能力。
*   **Execution (执行值)**：技术逻辑的严密性。
*   **Trust (信任值)**：英伟达阵营对你的信任度。
*   **Burnout (崩溃值)**：面对三大巨头施压时的抗压能力。

---

## 三、 动态路由机制与情节点设计 (20-25 Turns)

### Phase 1：前台的破局者 (Turn 1 - 4)
*   **剧情背景**：玩家来到前台。后台的 `negotiation_room` 里，三大巨头正在疯狂给 Jensen 施压。
*   **预期交互流**：
    *   **玩家**：「我要见黄仁勋，我的算法能把大模型推理的显存需求砍掉 80%。」
    *   **前台 (Agent 1)** 判定技术价值极高，调用 `send_message` (RDC) 给 Jensen 报信：「老板，前台有个辍学生说他的算法能把 HBM 需求砍掉 80%，您要见吗？」
    *   前台可对玩家 F2F 回复（如「请稍等，我通知黄总」），同时 RDC 内容进入上帝视角面板。
*   **【路由节点 A】 (Turn 4 结束时触发)**：
    *   *条件判定*：`Vision + Execution ≥ 15`（见架构文档）。
    *   *状态跃迁*：Flask 通过 IPC **`MOVE_AGENT`** 将 Jensen (Agent 2) 移到 `jensen_private_room`。前端 UI 提示：「前台带你穿过走廊，进入了一间私密会议室。Jensen 穿着皮衣推门而入。」进入 Phase 2；session 记录 **`phase2_start_tick`**。
    *   *玩家移动*：前端将 session 中 `place_id` 改为 `jensen_private_room`。
    *   *失败分支 (Bad End)*：未达标时 API 1 **在 inject 之前**判定，**立即**返回 `game_over` / `bad_reject`（`public_messages` 为 Flask stub，见 `2_architecture.md` API 1）；**无需** API 2、不消耗 Runner Tick。

### Phase 2：私密的技术审查与内心 OS (Turn 5 - 12)
*   **剧情背景**：Jensen 暂时离开主谈判桌，来听玩家的方案。
*   **预期交互流 (融入 UpdateState 功能)**：
    *   **Jensen** 态度急躁：「我只有 3 分钟，外面那群吸血鬼还在等我。你的算法凭什么省 80% 显存？」
    *   **玩家** 详细介绍技术（如：动态稀疏注意力、KV Cache 压缩）。
    *   **Jensen** 听完后，**先调用 `update_state`** 记录内心 OS，再 F2F 或 RDC 行动。
    *   **Jensen** 调用 `send_message` (RDC) 向 Tech VP (Agent 3) 求证逻辑。
    *   **Tech VP** 在会议室里通过 RDC 回复 Jensen：「如果他真的解决了哈希碰撞，理论上可行，这是个核武器！」
*   **【路由节点 B】 (Turn 12 结束时触发)**：
    *   *条件判定*：`Execution ≥ 20`，且 **自 Phase 2 开始**（`phase2_start_tick` 至今）存在 Tech VP→Jensen 的**正面 RDC**（见 `2_architecture.md` 第四节；**不是**仅本 Turn 的 API 2 窗口）。
    *   *状态跃迁*：IPC **`MOVE_AGENT`** 将 Jensen 移回 `negotiation_room`；inject **`PlaceMutationEffect`** 改写谈判室 `behavior_hint`（进程内有效）。进入 Phase 3；前端 `place_id` 改为 `negotiation_room`。
    *   *未达标分支*：无正面 RDC 则**不触发**节点 B，**Phase 仍为 2**（`player_turn` 继续递增，但 session `phase` / `place_id` 不变，仍 inject Agent 2）。

### Phase 3：舌战群儒与背刺大戏 (Turn 13 - 20) 【全场高潮】
*   **剧情背景**：所有人齐聚 `negotiation_room`。场景氛围已被突变。
*   **预期交互流**：
    *   **Jensen** 霸气开场，并在 **NVIDIA 高管群 (group_id: 100)** 里让 Tech VP 准备技术数据支援玩家。
    *   存储巨头在 **群聊 (group_id: 200)** 中对口供密谋压价。
    *   三大 CEO 轮番 F2F 攻击玩家；**Tech VP** 用硬核术语帮玩家圆场；**Jensen** 压价。
    *   **【系统广播】**（Turn 16）：Flask 经 **`ipc_helper.send_inject_batch`** 把 `broadcast` 字段发给 Runner；Runner 内 **`broadcast_helper.broadcast_place()`** 写库（**不用** `BroadcastEventEffect`，Flask 进程不得直接写 `world.db`）。
    *   **【Sam Altman 搅局】**（Turn 16，紧接广播后）：inject `DialogueInjectionEffect` → Agent 7（JSON 见 `2_architecture.md` 第五节）。
    *   **【关系破裂】**：**Samsung CEO (Agent 6)** 调用 `relation_change`；**`HbmAgent`** 将 LLM 参数 `target/break` 映射为 Dispatcher 的 `dst/remove`（见 `3_prompt_management.md` 第六节）。
    *   **Jensen** 收到 Sam 私信后危机感上升，转向「必须立刻签独家协议」。
*   **【路由节点 C】 (Turn 20 结束时触发)**：
    *   *条件判定*：`Burnout < 80` 且 `Vision ≥ 30`。
    *   *状态跃迁*：三次 IPC **`MOVE_AGENT`**，将 Agent 4/5/6 移到 `nvidia_reception`。进入 Phase 4。
    *   *未达标分支*：继续 Phase 3 舌战（`phase` 不变；`player_turn` 仍可递增到 21+，见 Phase/Turn 解耦说明）。

### Phase 4：胜利的果实 (Turn 21 - 25)
*   **剧情背景**：玩家、Jensen、Tech VP 留在 `negotiation_room`。
*   **预期交互流**：
    *   **Jensen** 大加赞赏，抛出加入 NVIDIA 或拿种子轮两个选择。
    *   **玩家** 最后讨价还价。
*   **【终极路由节点 D】 (Turn 25 结束)**：
    *   本 Turn 仍 inject 玩家台词并跑 Tick（Jensen 最后一轮反应），但 API 1 **直接返回** `completed` + `ending_id`，**无需** API 2 轮询。
    *   根据 DeepSeek 意图分类 + `Trust` 生成结局（规则见 `2_architecture.md` 第四节 4.3）：
        *   `ending_join_nvidia` — `trust ≥ 40` 且倾向加入
        *   `ending_seed_round` — `trust ≥ 25` 且倾向融资
        *   `ending_cold_deal` — 信任不足或意图模糊

---

## 四、 Phase / place_id 速查

| Phase | Turn（剧情规划） | 玩家 `place_id` | DialogueInjection 目标（`WorldDB.agents_at`） | inject 方式 |
|-------|------------------|-----------------|-----------------------------------------------|-------------|
| 1 | 1–4 | `nvidia_reception` | Agent 1 | 单条 event |
| 2 | 5–12（可延长，见节点 B 未达标） | `jensen_private_room` | Agent 2 | 单条 event |
| 3 | 13–20（可延长，见节点 C 未达标） | `negotiation_room` | Agent 2, 3, 4, 5, 6 | **batch** `events[]` |
| 4 | 21–25 | `negotiation_room` | Agent 2, 3 | batch `events[]` |

*Turn 列为剧情规划区间；**实际 Phase 以 session 为准**，路由未触发时可超出区间仍停留在上一 Phase。*

**Phase 与引擎**：引擎无 `phase` 字段；Phase 仅存在于 Flask session 与 Prompt 文案。**`player_turn` 是单调递增计数（1–25），与 Phase 解耦**——路由节点未触发时 Phase 不变（例如 Turn 12 未过节点 B，Turn 13 仍属 Phase 2，`place_id` 仍为 `jensen_private_room`，inject 目标仍为 Agent 2）。进入新 Phase 时更新 session 的 `phase` / `place_id`，并在 Stats Prompt 中传入当前 Phase 字符串。

---

## 五、 上帝视角展示范围

| 类型 | API 2 字段 | 说明 |
|------|------------|------|
| F2F | `public_messages` | 玩家当前房间 transcript |
| RDC | `observer_messages` | 含前台报信、Jensen↔Tech VP、Sam 搅局等 |
| GRP | `group_messages` | group 100 / 200 |
| 内心 OS | *扩展* | `update_state` 写入 Agent `current_state`；可选在 API 2 增加 `state_updates`（Flask 查 segment 或 Agent 快照），MVP 可仅展示 RDC/F2F |
