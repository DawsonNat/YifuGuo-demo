# 1. 剧情原型与交互逻辑设计 (Story Prototype)

**文档目标**：将全新的《HBM 显存价格保卫战》转化为可执行的 Multi-Agent 交互剧本，规划 20-25 个玩家 Turn 的状态机路由，并深度融合“多地点移动 (Move)”、“NPC 跨房间私聊 (RDC)”以及“阵营对抗”的核心 Feature。

**架构参照**：严格遵循 `dev_logs/07-11` 号底层架构白皮书。

---

## 一、 场景与角色设定

### 1. 场景地点 (Places)
1.  **`nvidia_reception` (英伟达接待前台)**：玩家的初始出生点。
2.  **`negotiation_room` (主谈判会议室)**：三大存储巨头逼宫英伟达的战场。
3.  **`jensen_private_room` (黄仁勋私人会议室)**：私密的技术验证空间。

### 2. 出场角色 (Agents - 3大阵营, 6个实体Agent)

**【玩家阵营】**
*   **玩家 (Player)**：无实体 Agent。通过 API 向所在房间的 NPC 注入对话。掌握着能大幅降低 AI 显存消耗的革命性压缩算法。

**【英伟达阵营 (防守方)】**
1.  **接待前台 (Agent 1)**：位于 `nvidia_reception`。负责接待玩家，并通过 RDC 向黄仁勋通报高价值信息。
2.  **Jensen Hwang (Agent 2)**：位于 `negotiation_room`。正因 HBM 涨价被三大巨头围攻，处于劣势。
3.  **Tech VP (Agent 3)**：位于 `negotiation_room`。协助 Jensen 谈判，负责评估底层技术。

**【存储巨头阵营 (进攻方)】**
4.  **SK Hynix CEO (Agent 4)**：位于 `negotiation_room`。HBM 市场老大，态度最强硬，主导涨价。
5.  **Micron CEO (Agent 5)**：位于 `negotiation_room`。跟风涨价，看重短期利润。
6.  **Samsung CEO (Agent 6)**：位于 `negotiation_room`。老谋深算，试图在涨价中抢占份额。

---

## 二、 核心数值系统 (Stats)

由 Flask Web 层维护，每次玩家输入后调用大模型打分累加：
*   **Vision (愿景值)**：画大饼、商业谈判能力。
*   **Execution (执行值)**：技术逻辑的严密性。
*   **Trust (信任值)**：英伟达阵营对你的信任度。
*   **Burnout (崩溃值)**：面对三大巨头施压时的抗压能力（若过高则谈判崩盘）。

---

## 三、 动态路由机制与情节点设计 (20-25 Turns)

### Phase 1：前台的破局者 (Turn 1 - 4)
*   **初始物理分布**：玩家与前台(Agent 1)在 `nvidia_reception`；其余 5 人在 `negotiation_room` 激烈争吵。
*   **动态交互**：
    *   玩家向**前台**抛出自己的革命性显存压缩算法。
    *   前台的 Prompt 强制规则：一旦听到颠覆性技术，必须立刻使用 `send_message` (RDC) 给正在开会的 Jensen 发私信（例如：“老板，前台有个疯子说他的算法能把 HBM 需求砍掉 80%”）。
    *   与此同时，后台的 `negotiation_room` 里，三大巨头正在疯狂给 Jensen 施压要求涨价（上帝视角展示）。
*   **【路由节点 A】 (Turn 4 结束时触发)**：
    *   *条件判定*：Flask 检查 `Vision` + `Execution` 是否达标。
    *   *状态跃迁*：若达标，Flask 注入 **`MoveEffect`**，将 Jensen (Agent 2) 从会议室瞬间移动到 `jensen_private_room`。同时前端 UI 提示玩家被前台带入了私人会议室。进入 Phase 2。

### Phase 2：私密的技术审查 (Turn 5 - 12)
*   **当前物理分布**：玩家与 Jensen 在 `jensen_private_room`；VP 与三大巨头仍在 `negotiation_room` 僵持。
*   **动态交互**：
    *   Jensen 态度急躁：“我只有 3 分钟，外面那群吸血鬼还在等我。你的算法是什么？”
    *   玩家介绍技术细节。
    *   Jensen 听完后，调用 `send_message` (RDC) 向仍在会议室里的 Tech VP (Agent 3) 求证：“这小子说他解决了显存带宽瓶颈，逻辑成立吗？”
    *   Tech VP 在会议室里一边应付三大巨头，一边通过 RDC 回复 Jensen：“如果他用了XXX哈希，理论上可行，这是个核武器！”
*   **【路由节点 B】 (Turn 12 结束时触发)**：
    *   *条件判定*：Tech VP 给出正面评价，且玩家 `Execution` 达标。
    *   *状态跃迁*：Flask 注入 **`StateChangeEffect`**（Jensen 状态变为“极度兴奋，找到了反击的武器”），并注入 **`MoveEffect`** 将 Jensen 移回 `negotiation_room`（玩家在剧情上跟进）。进入 Phase 3。

### Phase 3：舌战群儒，绝地反击 (Turn 13 - 20) 【全场高潮】
*   **当前物理分布**：所有人（玩家 + 5 个 Agent）齐聚 `negotiation_room`。
*   **动态交互 (6 方混战)**：
    *   Jensen 带着玩家入场，向三大巨头摊牌：“我们不需要买那么多 HBM 了，这位年轻人有新的解决方案。”
    *   **阵营对抗**：SK Hynix、Micron、Samsung 会疯狂质疑玩家的技术（“这不可能！”“PPT 骗局！”）。
    *   玩家需要通过输入进行反驳。Jensen 和 Tech VP 会在此时**主动附和并支援玩家**。
*   **【路由节点 C】 (Turn 20 结束时触发)**：
    *   *条件判定*：玩家成功顶住压力（`Burnout` 未爆表，`Vision` 足够高）。
    *   *状态跃迁*：三大巨头态度软化，放弃涨价。Flask 注入 **`MoveEffect`**，将三大巨头 (Agent 4, 5, 6) 移动到 `nvidia_reception`（灰溜溜地离开）。进入 Phase 4。

### Phase 4：胜利的果实 (Turn 21 - 25)
*   **当前物理分布**：只剩下玩家、Jensen、Tech VP 在 `negotiation_room`。
*   **动态交互**：
    *   Jensen 恢复了从容，对玩家大加赞赏。
    *   抛出终极选择：“年轻人，你拯救了 NVIDIA 的利润率。现在，我给你两个选择：1. 加入我们，做首席科学家；2. 自己开公司，我给你 1 亿美金的种子轮投资。”
*   **【终极路由节点 D】 (Turn 25 结束)**：
    *   根据玩家的选择和最终的 `Trust` 数值，生成结局画面。游戏圆满结束。