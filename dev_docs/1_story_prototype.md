# 1. 剧情原型与交互逻辑设计 (Story Prototype)

**文档目标**：将《HBM 显存价格保卫战》转化为可执行的 Multi-Agent 交互剧本，规划 20-25 个玩家 Turn 的状态机路由，并深度融合“多地点移动 (Move)”、“NPC 跨房间私聊 (RDC)”以及“阵营对抗”的核心 Feature。本文件是后续代码开发的**唯一故事依据**。

---

## 一、 场景与角色设定

### 1. 场景地点 (Places)
1.  **`nvidia_reception` (英伟达接待前台)**：玩家初始出生点。人来人往，只有前台接待员。
2.  **`negotiation_room` (主谈判会议室)**：三大存储巨头逼宫英伟达的战场。充满火药味。
3.  **`jensen_private_room` (黄仁勋私人会议室)**：私密的技术验证空间。绝对安静。

### 2. 出场角色 (Agents - 3大阵营, 6个实体Agent)

**【玩家阵营】**
*   **玩家 (Player)**：无实体 Agent。通过 API 注入对话。19 岁辍学生，掌握着能通过“动态稀疏激活”与“显存按需分配”大幅降低 AI 显存消耗的革命性压缩算法。

**【英伟达阵营 (防守方)】**
1.  **接待前台 (Agent 1)**：位于 `nvidia_reception`。负责拦截普通访客，但对颠覆性技术极其敏感。
2.  **Jensen Hwang (Agent 2)**：位于 `negotiation_room`。被三大巨头逼迫接受 HBM 涨价 30% 的协议，极度烦躁。
3.  **Tech VP (Agent 3)**：位于 `negotiation_room`。协助 Jensen 谈判，负责评估底层技术。

**【存储巨头阵营 (进攻方)】**
4.  **SK Hynix CEO (Agent 4)**：位于 `negotiation_room`。HBM 市场老大，用“产能分配”作为武器。
5.  **Micron CEO (Agent 5)**：位于 `negotiation_room`。华尔街做派，用“利润率”和“转投 AMD”作为武器。
6.  **Samsung CEO (Agent 6)**：位于 `negotiation_room`。笑面虎，用“2.5D 封装良率”作为武器。

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
*   **剧情背景**：玩家来到前台。后台的 `negotiation_room` 里，三大巨头正在疯狂给 Jensen 施压。
*   **预期交互流 (Expected Flow)**：
    *   玩家：“我要见黄仁勋，我的算法能把大模型推理的显存需求砍掉 80%。”
    *   前台 (Agent 1) 判定技术价值极高，调用 `send_message` (RDC) 给 Jensen：“老板，前台有个辍学生说他的算法能把 HBM 需求砍掉 80%，您要见吗？”
*   **【路由节点 A】 (Turn 4 结束时触发)**：
    *   *条件判定*：Flask 检查 `Vision` + `Execution` 是否达标。
    *   *状态跃迁*：若达标，Flask 注入 **`MoveEffect`**，将 Jensen (Agent 2) 从会议室瞬间移动到 `jensen_private_room`。同时前端 UI 提示：“前台带你穿过走廊，进入了一间私密会议室。Jensen 穿着皮衣推门而入。” 进入 Phase 2。

### Phase 2：私密的技术审查 (Turn 5 - 12)
*   **剧情背景**：Jensen 暂时离开主谈判桌，来听玩家的方案。
*   **预期交互流 (Expected Flow)**：
    *   Jensen 态度急躁：“我只有 3 分钟，外面那群吸血鬼还在等我。你的算法凭什么省 80% 显存？”
    *   玩家详细介绍技术（如：动态稀疏注意力、KV Cache 压缩）。
    *   Jensen 听完，调用 `send_message` (RDC) 向仍在会议室里的 Tech VP (Agent 3) 求证：“这小子说他用动态稀疏激活解决了 KV Cache 瓶颈，逻辑成立吗？”
    *   Tech VP (Agent 3) 在会议室里通过 RDC 回复 Jensen：“如果他真的解决了哈希碰撞，理论上可行，这是个核武器！”
*   **【路由节点 B】 (Turn 12 结束时触发)**：
    *   *条件判定*：Tech VP 给出正面评价，且玩家 `Execution` 达标。
    *   *状态跃迁*：Flask 注入 **`StateChangeEffect`**（Jensen 状态变为“极度兴奋，找到了反击的武器”），并注入 **`MoveEffect`** 将 Jensen 和玩家一起移动到 `negotiation_room`。前端 UI 提示：“Jensen 眼神狂热，一把拉开门，‘跟我来，我们去给那群吸血鬼一点颜色看看！’” 进入 Phase 3。

### Phase 3：舌战群儒，绝地反击 (Turn 13 - 20) 【全场高潮】
*   **剧情背景**：所有人（玩家 + 5 个 Agent）齐聚 `negotiation_room`。6 方混战开始。
*   **预期交互流 (Expected Flow)**：
    *   Jensen 霸气开场：“各位，我们不需要接受 30% 的涨价了。这位年轻人有新的解决方案。”
    *   **阵营内部群聊密谋 (核心 Feature 展示)**：
        *   在上帝视角中，玩家会看到三大巨头在他们的内部群聊（`group_id: 200`）中对口供。例如：`[群聊: 存储巨头联盟] 海力士 -> 群: "别慌，这小子肯定在吹牛，我们咬死 30% 涨价不松口！"`
        *   同时，Jensen 也会在英伟达高管群（`group_id: 100`）中向 Tech VP 下达战术指令。
    *   **SK Hynix (Agent 4)** 攻击产能：“纯属扯淡！没有我们的高带宽，你的算法连跑都跑不起来！”
    *   **Micron (Agent 5)** 攻击商业：“PPT 骗局，如果你信他，我们明天的产能就全给 Google 和 AMD。”
    *   **Samsung (Agent 6)** 攻击生态：“年轻人，算法再好，没有我们的 2.5D 高级封装，你也做不出芯片。”
    *   **玩家反击**：玩家需要逐一驳斥。
    *   **英伟达助攻**：Tech VP 会用硬核术语帮玩家圆场；Jensen 会用玩家的技术去压价。
*   **【路由节点 C】 (Turn 20 结束时触发)**：
    *   *条件判定*：玩家成功顶住压力（`Burnout` 未爆表，`Vision` 足够高）。
    *   *状态跃迁*：三大巨头态度软化，放弃涨价。Flask 注入 **`MoveEffect`**，将三大巨头 (Agent 4, 5, 6) 移动到 `nvidia_reception`（灰溜溜地离开）。前端 UI 提示：“三大巨头面色铁青地收拾文件离开了会议室。” 进入 Phase 4。

### Phase 4：胜利的果实 (Turn 21 - 25)
*   **剧情背景**：只剩下玩家、Jensen、Tech VP 在 `negotiation_room`。
*   **预期交互流 (Expected Flow)**：
    *   Jensen 恢复了从容，对玩家大加赞赏：“你拯救了 NVIDIA 几百亿的利润率。”
    *   抛出终极选择：“现在，我给你两个选择：1. 加入 NVIDIA 做首席科学家，我给你 5000 万美金研发预算；2. 自己开公司，我给你 1 亿美金种子轮，但你的算法必须由 NVIDIA 独家买断 5 年。”
    *   玩家进行最后的讨价还价。
*   **【终极路由节点 D】 (Turn 25 结束)**：
    *   根据玩家的选择和最终的 `Trust` 数值，生成结局画面。游戏圆满结束。