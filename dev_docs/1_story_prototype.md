# 1. 剧情原型与交互逻辑设计 (Story Prototype)

**文档目标**：将《HBM 显存价格保卫战》转化为可执行的 Multi-Agent 交互剧本，规划 20-25 个玩家 Turn 的状态机路由，并深度融合“多地点移动 (Move)”、“跨房间私聊 (RDC)”、“系统广播 (Broadcast)”、“场景突变 (PlaceMutation)”、“关系破裂 (RelationChange)”以及“内心 OS (UpdateState)”等全套底层引擎高级功能。

---

## 一、 场景与角色设定

### 1. 场景地点 (Places)
1.  **`nvidia_reception` (英伟达接待前台)**：玩家初始出生点。
2.  **`negotiation_room` (主谈判会议室)**：三大存储巨头逼宫英伟达的战场。（*注：氛围会在 Phase 3 发生突变*）
3.  **`jensen_private_room` (黄仁勋私人会议室)**：私密的技术验证空间。

### 2. 出场角色 (Agents - 3大阵营, 6个实体Agent)

**【玩家阵营】**
*   **玩家 (Player)**：无实体 Agent。通过 API 注入对话。掌握着革命性压缩算法的 19 岁辍学生。

**【英伟达阵营 (防守方)】**
1.  **接待前台 (Agent 1)**：位于 `nvidia_reception`。负责拦截与通报。
2.  **Jensen Hwang (Agent 2)**：位于 `negotiation_room`。被三大巨头逼迫，寻找破局点。
3.  **Tech VP (Agent 3)**：位于 `negotiation_room`。负责评估底层技术逻辑。

**【存储巨头阵营 (进攻方)】**
4.  **SK Hynix CEO (Agent 4)**：位于 `negotiation_room`。HBM 市场老大，态度最强硬。
5.  **Micron CEO (Agent 5)**：位于 `negotiation_room`。跟风涨价的华尔街商人。
6.  **Samsung CEO (Agent 6)**：位于 `negotiation_room`。老谋深算，随时准备背刺盟友。

**【第三方破局者】**
7.  **Sam Altman (Agent 7)**：OpenAI CEO。不在现场（位于 `openai_hq`）。他作为最大的算力买家，时刻关注着底层技术的突破。

---

## 二、 核心数值系统 (Stats)

由 Flask Web 层维护，每次玩家输入后调用大模型打分累加：
*   **Vision (愿景值)**：画大饼、商业谈判能力。
*   **Execution (执行值)**：技术逻辑的严密性。
*   **Trust (信任值)**：英伟达阵营对你的信任度。
*   **Burnout (崩溃值)**：面对三大巨头施压时的抗压能力。

---

## 三、 动态路由机制与情节点设计 (20-25 Turns)

### Phase 1：前台的破局者 (Turn 1 - 4)
*   **剧情背景**：玩家来到前台。后台的 `negotiation_room` 里，三大巨头正在疯狂给 Jensen 施压。
*   **预期交互流**：
    *   **玩家**：“我要见黄仁勋，我的算法能把大模型推理的显存需求砍掉 80%。”
    *   **前台 (Agent 1)** 判定技术价值极高，调用 `send_message` (RDC) 给 Jensen 报信：“老板，前台有个辍学生说他的算法能把 HBM 需求砍掉 80%，您要见吗？”
*   **【路由节点 A】 (Turn 4 结束时触发)**：
    *   *条件判定*：Flask 检查 `Vision` + `Execution` 是否达标。
    *   *状态跃迁*：若达标，Flask 注入 **`MoveEffect`**，将 Jensen (Agent 2) 瞬间移动到 `jensen_private_room`。同时前端 UI 提示：“前台带你穿过走廊，进入了一间私密会议室。Jensen 穿着皮衣推门而入。” 进入 Phase 2。
    *   *失败分支*：若未达标，前台回复：“保安，把他轰出去。” 触发 Bad End。

### Phase 2：私密的技术审查与内心 OS (Turn 5 - 12)
*   **剧情背景**：Jensen 暂时离开主谈判桌，来听玩家的方案。
*   **预期交互流 (融入 UpdateState 功能)**：
    *   **Jensen** 态度急躁：“我只有 3 分钟，外面那群吸血鬼还在等我。你的算法凭什么省 80% 显存？”
    *   **玩家** 详细介绍技术（如：动态稀疏注意力、KV Cache 压缩）。
    *   **Jensen** 听完后，**先调用 `update_state` 工具修改内心 OS**：“这小子的想法太疯狂了，但我必须掩饰住激动，不能让他看出我急需这个技术。”（*上帝视角展示，极大地增加拟真感*）。
    *   随后，**Jensen** 调用 `send_message` (RDC) 向 Tech VP (Agent 3) 求证逻辑：“这小子说他用动态稀疏激活解决了 KV Cache 瓶颈，逻辑成立吗？”
    *   **Tech VP** 在会议室里通过 RDC 回复 Jensen：“如果他真的解决了哈希碰撞，理论上可行，这是个核武器！”
*   **【路由节点 B】 (Turn 12 结束时触发)**：
    *   *条件判定*：Tech VP 给出正面评价，且玩家 `Execution` 达标。
    *   *状态跃迁*：Tech VP 给出正面评价后，Flask 注入 **`MoveEffect`** 将 Jensen 移回 `negotiation_room`。前端 UI 提示：“Jensen 眼神狂热，一把拉开门，‘跟我来，我们去给那群吸血鬼一点颜色看看！’”
    *   **【场景突变触发】**：同时，Flask 注入 **`PlaceMutationEffect`**，将 `negotiation_room` 的 `behavior_hint` 从“充满火药味”瞬间修改为“死一般的寂静，所有人都被 Jensen 带来的底牌震撼了，说话变得小心翼翼”。进入 Phase 3。

### Phase 3：舌战群儒与背刺大戏 (Turn 13 - 20) 【全场高潮】
*   **剧情背景**：所有人齐聚 `negotiation_room`。场景氛围已被突变。
*   **预期交互流 (融入 Broadcast 与 RelationChange 功能)**：
    *   **Jensen** 霸气开场：“各位，我们不需要接受 30% 的涨价了。这位年轻人有新的解决方案。”
    *   三大巨头开始在群聊（`group_id: 200`）中对口供：`[群聊: 存储巨头联盟] 海力士 -> 群: "别慌，这小子肯定在吹牛，我们咬死 30% 涨价不松口！"`
    *   **SK Hynix (Agent 4)** 攻击产能：“纯属扯淡！没有我们的高带宽，你的算法连跑都跑不起来！”
    *   **Micron (Agent 5)** 攻击商业：“PPT 骗局，如果你信他，我们明天的产能就全给 Google 和 AMD。”
    *   **Samsung (Agent 6)** 攻击生态：“年轻人，算法再好，没有我们的 2.5D 高级封装，你也做不出芯片。”
    *   **玩家** 需要逐一驳斥。**Tech VP** 会用硬核术语帮玩家圆场；**Jensen** 会用玩家的技术去压价。
    *   **【系统广播触发】**：在谈判最焦灼时，Flask 突然注入 **`BroadcastEventEffect`**：“*会议室的彭博社终端机突然弹出快讯：AMD 宣布下一代 MI400 芯片将采用全新自研显存架构...*”
    *   **【Sam Altman 搅局触发】**：紧接着，Flask 注入事件，让远在 `openai_hq` 的 **Sam Altman (Agent 7)** 通过 RDC 私聊 Jensen：“*Jensen，听说有个做稀疏注意力的小孩在你那里？别急着拒绝，我们 OpenAI 很有兴趣。*”
    *   **【关系破裂触发】**：突发新闻加上 Sam Altman 的抢人举动，彻底击溃了存储联盟的心理防线。**Samsung CEO (Agent 6)** 见势不妙，**主动调用 `relation_change` 工具**，解除与 SK Hynix (Agent 4) 的“盟友”关系，并在谈判桌上当场倒戈。
    *   **Jensen** 收到 Sam 的私信后，产生强烈的危机感，态度从“利用玩家压价”转变为“必须立刻签下独家协议”。
*   **【路由节点 C】 (Turn 20 结束时触发)**：
    *   *条件判定*：玩家成功顶住压力（`Burnout` 未爆表，`Vision` 足够高）。
    *   *状态跃迁*：存储联盟瓦解。Flask 注入 **`MoveEffect`**，将三大巨头移到 `nvidia_reception`（灰溜溜地离开）。前端 UI 提示：“三大巨头面色铁青地收拾文件离开了会议室。” 进入 Phase 4。

### Phase 4：胜利的果实 (Turn 21 - 25)
*   **剧情背景**：只剩下玩家、Jensen、Tech VP 在 `negotiation_room`。
*   **预期交互流**：
    *   **Jensen** 恢复了从容，对玩家大加赞赏：“你拯救了 NVIDIA 几百亿的利润率。”
    *   **Jensen** 抛出终极选择：“现在，我给你两个选择：1. 加入 NVIDIA 做首席科学家，我给你 5000 万美金研发预算；2. 自己开公司，我给你 1 亿美金种子轮，但你的算法必须由 NVIDIA 独家买断 5 年。”
    *   **玩家** 进行最后的讨价还价。
*   **【终极路由节点 D】 (Turn 25 结束)**：
    *   根据玩家的选择和最终的 `Trust` 数值，生成结局画面。游戏圆满结束。