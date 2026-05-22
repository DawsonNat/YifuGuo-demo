# 1. 剧情原型与交互逻辑设计 (Story Prototype)

**文档目标**：将《Dropout in Silicon Valley》第五章转化为可执行的 Multi-Agent 交互剧本，规划 25-50 个玩家 Turn 的状态机路由，并强制植入“NPC 互相影响记忆”的核心 Feature。

**架构参照**：严格遵循 `dev_logs/08_智能体与记忆管理_Agent_Memory.md` 中关于 Agent 字段的定义，以及 `dev_logs/10_剧本引擎与事件注入_Script_Engine.md` 中关于状态修改的机制。

---

## 一、 场景与角色设定

### 1. 场景背景
*   **地点**：NVIDIA 硅谷总部，顶层全玻璃会议室（`nvidia_hq_boardroom`）。
*   **时间**：下午 4:00，阳光刺眼。

### 2. 出场角色 (Agents)
1.  **玩家 (Player)**：19 岁辍学生，RAMUS 创始人。（在引擎中，玩家没有实体 Agent，而是通过 `DialogueInjectionEffect` 向 NPC 注入声音）。
2.  **Jensen Hwang (NPC 1)**：NVIDIA CEO。掌握算力生杀大权。
3.  **Tech VP (NPC 2)**：NVIDIA 核心技术副总裁。极客，只看代码不听故事。目前在自己的独立办公室（`tech_vp_office`）。

---

## 二、 核心数值系统 (Stats)

*注：为了保持底层 `agent_world` 引擎的纯洁性，以下数值由 **Flask Web 应用层** 维护，不写入底层引擎。Flask 每次收到玩家 Query 时，会调用大模型对玩家的发言进行打分，并累加到 Session 中。*

*   **Vision (愿景值)**：玩家讲故事、画大饼的能力。
*   **Execution (执行值)**：玩家展现出的工程能力和底层技术硬实力。
*   **Trust (信任值)**：Jensen 和 VP 对玩家的信任度。

---

## 三、 动态路由机制与情节点设计 (The Dynamic Routing System)

**核心机制**：剧情非写死。对话由 LLM 实时生成。路由节点通过 Flask 层向底层引擎注入 `StateChangeEffect`，修改 Agent 的 `current_state`（B5 提示词的第三段），从而动态引导 LLM 的生成倾向。

### Phase 1：破冰与狂言 (Turn 1 - 10)
*   **当前状态 (`current_state`)**：Jensen 的初始状态为 `"极度不耐烦，频频看表，想要快速打发走眼前的年轻人。"`
*   **动态交互**：玩家自由输入。Jensen 会生成极具压迫感和质疑的回复。
*   **【路由节点 A】 (Turn 10 结束时触发检查)**：
    *   *条件判定*：Flask 层检查玩家的 `Vision` 数值。
    *   *路由分支 1 (Bad End)*：若数值未达标，Flask 注入广播事件（保安驱逐），Demo 结束。
    *   *路由分支 2 (推进)*：若数值达标，Flask 通过 IPC 注入 `StateChangeEffect`，将 Jensen 的 `current_state` 修改为 `"被玩家的狂言引起了兴趣，决定验证其底层技术的真实性。"`，进入 Phase 2。

### Phase 2：技术审查与后台博弈 (Turn 11 - 25) 【核心 Feature 展示区】
*   **当前状态 (`current_state`)**：Jensen 开始认真对待，但保持怀疑。
*   **动态交互与核心 Feature**：
    *   Jensen 的 Prompt 中有一条强制行为规则（Behavior Hint）：**“遇到关键技术主张时，必须使用 `send_message` 工具向 Tech VP 求证。”**
    *   Jensen 会根据玩家的发言，调用工具通过 RDC 通道向 Tech VP 发送私信。
    *   Tech VP 收到私信后，调用大模型生成回复，并通过 RDC 发回给 Jensen。
*   **【路由节点 B】 (Turn 25 结束时，或 Tech VP 回复后触发)**：
    *   *条件判定*：Flask 轮询发现 Tech VP 给出了正面评价（该评价已通过 `PerceptionBuilder` 进入 Jensen 的记忆）。
    *   *路由分支*：Flask 注入 `StateChangeEffect`，将 Jensen 的 `current_state` 修改为 `"极度兴奋，确认了技术的颠覆性，决定不惜代价拿下这个项目。"`，进入 Phase 3。

### Phase 3：记忆生效与态度反转 (Turn 26 - 40) 【爽点爆发区】
*   **当前状态 (`current_state`)**：基于 Tech VP 的背书和更新后的状态，Jensen 的态度发生 **180 度大反转**。
*   **动态交互**：Jensen 主动抛出橄榄枝（如提供 500 张 H100），附带严苛的对赌协议。玩家自由讨价还价。
*   **【路由节点 C】 (Turn 40 结束时触发检查)**：
    *   *条件判定*：检查是否达成初步口头协议，进入结算谈判。

### Phase 4：结局结算 (Turn 41 - 50)
*   **动态交互**：对赌协议细节拉扯。
*   **【终极路由节点 D】 (Turn 50 结束时)**：
    *   *条件判定*：根据最终的 `Execution` 和 `Trust` 数值。
    *   *结局分发*：生成最终投资条款（Term Sheet），展示 Demo 评级。