# 开发日志 05：Dev Docs 编写规划与详细大纲

**记录时间**：2026-05-22

在充分理解了“玩家 Turn vs 后台 Tick”的差异，以及 Web 端三屏联动的 UI/UX 目标后，现对 `dev_docs/` 目录下的 4 份核心文档进行最终的详细规划。这 4 份文档将作为今晚 10 点进度对焦的核心交付物，并指导后续的代码开发。

---

## 规划一：`1_story_prototype.md`（剧情原型与路由设计）
**核心目标**：将《Dropout in Silicon Valley》第五章转化为可执行的交互剧本，规划 25-50 个玩家 Turn 的状态机路由，并强制植入“NPC 互相影响记忆”的 Feature。

**详细内容大纲**：
1.  **场景设定**：
    *   **地点**：NVIDIA 硅谷总部，顶层全玻璃会议室。
    *   **出场角色**：玩家（19岁辍学生）、Jensen Hwang（NPC 1，掌握算力大权）、Tech VP（NPC 2，负责技术背调）。
2.  **核心数值系统**：
    *   定义 Vision（愿景）、Execution（执行）、Trust（信任）、Burnout（崩溃）在当前场景的初始值及增减逻辑。
3.  **状态机与路由节点 (25-50 玩家 Turns)**：
    *   **Phase 1：破冰与狂言 (Turn 1-10)**
        *   *触发条件*：游戏开始。
        *   *剧情表现*：Jensen 态度傲慢，玩家需通过输入硬核技术词汇提升 Vision 值。
        *   *路由分支*：若 Turn 10 结束时 Vision < 阈值 -> 触发 Bad End（保安驱逐）；若达标 -> 进入 Phase 2。
    *   **Phase 2：技术审查与核心 Feature 触发 (Turn 11-25)**
        *   *触发条件*：Vision 达标，玩家抛出核心技术点。
        *   *后台 Tick 表现*：Jensen 不直接回复玩家，而是通过 RDC 通道私聊 Tech VP 验证技术可行性。Tech VP 给出正面评价。
    *   **Phase 3：记忆生效与态度反转 (Turn 26-40)**
        *   *触发条件*：Tech VP 的评价进入 Jensen 的 Memory。
        *   *剧情表现*：爽点爆发。Jensen 态度 180 度反转，提出 500 张 H100 的算力支持，但附带对赌协议。
    *   **Phase 4：结局结算 (Turn 41-50)**
        *   *触发条件*：玩家接受或拒绝协议。
        *   *剧情表现*：根据最终数值给出 Demo 评级。

---

## 规划二：`2_architecture.md`（双段式异步 API 架构设计）
**核心目标**：设计前后端交互的 JSON 数据结构，明确 Web 前端、Flask API、IPC Server 与 WorldStep (Tick) 之间的数据流转，彻底解决“用户干等”的痛点。

**详细内容大纲**：
1.  **系统架构图解**：
    *   描述三屏联动 UI（状态面板、主聊天、上帝视角）如何与后端 API 交互。
    *   阐明“1 个玩家 Turn 触发 N 个后台 Tick”的引擎运转逻辑。
2.  **API 1：发起交互 (`POST /api/action`)**
    *   *请求体 (JSON)*：包含 `player_id`、`query`（玩家输入）、当前 `state`。
    *   *后端逻辑*：将 Query 注入 F2F 总线 -> 生成唯一 `task_id` -> 异步唤醒 `WorldStep` 开始跑 Tick。
    *   *响应体 (即时)*：返回 `task_id` 和 `immediate_msg`（用于前端立刻显示的动作描写，如“Jensen 皱了皱眉...”）。
3.  **API 2：轮询结果 (`GET /api/action/result?task_id=...`)**
    *   *后端逻辑*：检查后台 Tick 是否跑到了“有 NPC 对玩家说话”或“达到最大静默 Tick 数”的状态。
    *   *响应体 (完成时)*：
        *   `public_messages`：NPC 对玩家说的话（更新主聊天框）。
        *   `observer_messages`：Tick 流转期间 NPC 之间的私聊/内心 OS（更新上帝视角）。
        *   `stats_update`：玩家数值的变化。

---

## 规划三：`3_prompt_management.md`（角色设定与 Prompt 管理）
**核心目标**：将 Markdown 剧本中的人物性格翻译为 `agent_world` 引擎可读的四段式系统提示词（B5 规范）。

**详细内容大纲**：
1.  **Jensen Hwang 的 Prompt 配置**：
    *   *Soul (灵魂内核)*：穿皮衣的商业暴君，极度聪明，没耐心，寻找能消耗海量 GPU 的杀手级应用。
    *   *Long-term Goal (长期目标)*：榨干玩家价值或将其赶走。
    *   *Current State (当前状态)*：坐在主位，喝水，看表。
    *   *Behavior Hint (场景行为规则)*：说话简短、压迫感强。**强制规则**：遇到不懂的技术细节，必须使用 `send_message` 工具向 Tech VP 求证，禁止自行编造。
2.  **Tech VP 的 Prompt 配置**：
    *   *Soul*：极客，只看代码不听故事。
    *   *Behavior Hint*：待在自己的办公室。收到 Jensen 的私信后，必须给出客观、硬核的技术评价。

---

## 规划四：`4_test_scripts.md`（测试与验证脚本）
**核心目标**：提供在前端 UI 就绪前，后端能够独立闭环测试双段式 API 和 Tick 流转的脚本。

**详细内容大纲**：
1.  **测试环境准备**：说明如何启动 Flask 服务和 IPC Server。
2.  **`test_async_api.py` 脚本逻辑**：
    *   使用 Python `requests` 库。
    *   **步骤 1**：POST `/api/action` 发送测试 Query，断言是否能在 1 秒内拿到 `task_id` 和即时反馈。
    *   **步骤 2**：编写 `while` 循环，每隔 1 秒 GET `/api/action/result`。
    *   **步骤 3**：断言最终返回的 JSON 中是否包含了预期的 `public_messages` 和 `observer_messages`（验证 NPC 内部交互是否成功触发）。
