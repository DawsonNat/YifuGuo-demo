# WorldStep 实现文档

> 路径: `agent_world/world/step.py`
> 对应 LAYOUT §: §2.A WorldStep（表行 199）/ §6.1 一轮 micro-tick 主循环（行 466-525）/ §6.2（一个动作）/ §6.3（一个感知）
> 上游依赖文档: `world_state.md`（聚合根）, `world_clock.md`（单全局 `t`）, `place_store.md`（按 `L_t` 分组活跃 agent）, `dispatcher.md`（路由 6 类 action）, `perception.md`（PerceptionBuilder.build）, `script_engine.md`（due_events / apply / pending_for）, `memory_compressor.md`（`BehaviorCompressor.on_move`）, `memory_segment.md`（max_raw_actions 兜底）, `pools_manager.md`（`update_all_rec_tables`）, `buses_face_to_face.md` / `buses_remote_message.md` / `buses_group_message.md`（DeliveryQueue.sweep_undelivered）, `scheduler.md`（profile-based + 剧本白名单）, `persistence_world_db.md`（direct_message / overhear / group_event 写入）
> 下游依赖文档: `runner_run_agent_world_simulation.md`（runner 主循环调 `WorldStep.run_one_tick`）, `app/services/simulation_runner.md`（Web 监控 actions.jsonl）

## 1. 模块定位

`WorldStep` 是仿真器的"心脏"。每次 `await world_step.run_one_tick()` 推进一个 `world.t`，按 LAYOUT §6.1 描述的 11 步流水线编排：轮初 lockstep 准备（剧本、推荐、调度、持久队列重投、按地点分组）→ micro-tick（地点间 `asyncio.gather` 并行 + 地点内 shuffle 串行）→ 轮末 lockstep 结算（pending_moves + BehaviorCompressor.on_move + Zep flush + `t += 1`）。

- **输入**：`WorldState`（持有所有子段）+ 当前 `t`
- **输出**：副作用——写入 `world.db.{direct_message, overhear, group_*, script_event_log, relation, capability, agent_location}` + `pool_*.db.{post, like, rec, trace, ...}` + Zep enqueue + `world.t += 1`

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| step 主循环骨架（先 update_rec → gather LLM → time+1） | OASIS | `vendor/oasis/oasis/environment/env.py:136-198`（`OasisEnv.step`） | PATTERN | 仅作为节奏参考；fork 内文件可保留也可删，本项目用全新写的 WorldStep 取代 |
| `asyncio.gather` 并发 LLM 决策模式 | OASIS | `vendor/oasis/oasis/environment/env.py:170-185` | PATTERN | 改为按 place 分组的二级 gather：地点间 gather + 地点内 for-loop |
| update_rec 并发模式 | OASIS | `vendor/oasis/oasis/environment/env.py:140-152`（`update_rec_table`） | EDIT（fork 内已重构） | 原 OASIS 单 platform；本项目调 `pools.update_all_rec_tables()` 内部 `asyncio.gather` |
| `event_config.initial_posts` 注入剧本 | MiroFish | `ref/MiroFish/.../run_parallel_simulation.py:1180-1207` | PATTERN | 微缩剧本范式扩展为完整 ScriptEngine 前导步（步骤 1-2） |
| 双 env `asyncio.gather(twitter, reddit)` 并行模式 | MiroFish | `ref/MiroFish/.../run_parallel_simulation.py:1585-1589` | PATTERN | 多池调度灵感来源；本项目集中到 `MultiPoolPlatformManager.update_all` |
| Scheduler profile-based 活跃度（B7） | MiroFish | profile-based 活跃度（默认沿用） | KEEP | 剧本可注入"强制激活白名单"覆盖（LAYOUT §9.5 #B7） |

## 3. 关键改动 (相对 OASIS `OasisEnv.step`)

- **改动 1**：`await asyncio.gather(*[p.update_rec_table() for p in active_pools])` 取代 OASIS 单 `self.platform.update_rec_table()`（LAYOUT §2.A 表行 199）。
- **改动 2**：在 OASIS 步骤前加 **ScriptEngine 前导**（步骤 1-2：`due_events` + `apply`），取代原 `ManualAction` 的临时注入式调用（OASIS `env_action.py`）。
- **改动 3**：在 OASIS 步骤后加 **DeliveryQueue 中插步**（步骤 5：`sweep_undelivered`，重投 B6 群聊持久队列）+ **轮末 BehaviorCompressor + ZepUpdater 后置步**（步骤 9-10）。
- **改动 4**：核心改动——**active 按 `L_t` 分组**，每组一个 coroutine 串行决策；地点内顺序按 `random.Random(world.t).shuffle(agents)`（LAYOUT §6.1 步骤 6-7）。MOVE/RDC/GRP/FEED **不参与 micro-tick**（lockstep，本轮发下轮收）。
- **改动 5**：`micro-tick` 内每个 agent 决策前重读 `direct_message`（步骤 7 内的 SELECT），F2F 因 `arrive_at=t, delivered=1` 立刻被同地点下一个决策者读到；RDC 因 `arrive_at>=t+1` 仍要等到下一个 `world.t` 才被读取（LAYOUT §6.1 行 521-522）。
- **改动 6**：MVP **不引入** `max_serial_per_place`（LAYOUT v0.3 B1 决议）；同地点 N 个 agent 严格全部串行决策。
- **改动 7**：B4 retry 仅在 parse_error / arg_missing 时允许 ≤1 次，错误仅在该次 retry 的 prompt **临时附加，不持久化**（LAYOUT §6.1 步骤 7 + §9.5 #4）。

## 4. 核心逻辑

### 4.1 数据结构

WorldStep 自身几乎无状态，只持：

- `world: WorldState`（聚合根引用）
- `scheduler: Scheduler`（profile-based 活跃度，LAYOUT §9.5 #B7）
- `delivery_queue: DeliveryQueue`（B6 持久队列 sweep 实现）
- `tick_metrics: TickMetrics`（可选；记录每步耗时、active_agent 数等，用于 P0 卡口验收）

### 4.2 关键流程 / 算法 — micro-tick 11 步主循环（LAYOUT §6.1）

**整体语义**：一轮 `world.t` 内，**地点间并行**、**地点内串行**。同一地点的 agent 按 shuffle 序逐个决策；F2F 发言被同地点的下一个决策者立刻看到。跨地点的 RDC/GRP/FEED/MOVE **不进入 micro-tick**，全部 lockstep（本轮发、下轮收 / 下轮生效）。

```
========== 阶段 A: 轮初准备（lockstep, 严格顺序）==========

# 步骤 1-2: ScriptEngine 前导（驱动剧本）
1. due = world.script.due_events(world, t)
   # 读 world.db.script_event_log 中已加载未触发且 trigger 命中的事件
2. for evt in due:
       await world.script.apply(evt, world)
   # 写 world.db.{place,relation,capability,agent_location,script_event_log}
   # 包含 StateChangeEffect → 直接写 world.agents[a].current_state（B5）

# 步骤 3: 多池推荐刷新（地点间并行）
3. await world.pools.update_all_rec_tables()
   # 内部 asyncio.gather(*[p.update_rec_table() for p in active_pools])
   # 写 pool_*.db.rec（每池全表删建）

# 步骤 4: 调度——决定本轮哪些 agent 活跃（B7）
4. active_agents = scheduler.pick_active(world, t)
   # MiroFish profile-based + 剧本"强制激活白名单"覆盖

# 步骤 5: 持久队列重投（B6）
5. delivery_queue.sweep_undelivered(world, t)
   # UPDATE direct_message
   # SET delivered=1, arrive_at=t
   # WHERE delivered=0 AND group_id IS NOT NULL AND recipient 现可达

# 步骤 6: 按地点分组 + 同地点 shuffle（决策顺序确定性）
6. groups = group_by_place(active_agents, world.places.L_t)
   # Dict[place_id, List[Agent]]
   for p, agents in groups.items():
       random.Random(world.t).shuffle(agents)   # seed=world.t 保证可复现

========== 阶段 B: micro-tick（地点间并行 / 地点内串行）==========

# 步骤 7: 地点间 gather；每个 place 一个 coroutine 串行执行所有 agent
7. await asyncio.gather(*[run_place(p, agents) for p, agents in groups.items()])

   async def run_place(p, agents):
       for a in agents:                                         # 严格串行
           # ---- 7.a 派生 Observation（LAYOUT §6.3）----
           obs = await PerceptionBuilder.build(a, world, t)
             读取来源:
               world.db.{agent_location, relation, capability}
               world.db.direct_message  WHERE recipient=a AND delivered=1
                                          AND arrive_at <= t
                                          AND arrive_at > a.last_message_seen_at
               world.db.direct_message  WHERE sender=a AND delivered=0
                                          AND attempted_at == t-1   # B9 失败 1 轮透传
               world.db.overhear        WHERE overhearer=a AND attempted_at >= t-1
               world.db.group_event     WHERE agent_in_group AND occurred_at == t-1   # B6 1 轮透传
               pool_*.db.{rec,post}     via OASIS Platform.refresh()
               Zep graph_{a} / place_{p} / world  via MultiGraphRetriever

           # ---- 7.b 拼 4 段 system prompt（B5）----
           system_prompt = "\n\n".join([
               f"# Soul\n{a.soul}",
               f"# Long-term Goal\n{a.long_term_goal}",
               f"# Current State\n{a.current_state}",
               f"# Place Behavior Rule\n{obs.location_attrs.behavior_hint or '(none)'}",
           ])

           # ---- 7.c LLM 决策 ----
           action, retry = None, 0
           while True:
               try:
                   action = await a.astep(system_prompt, obs)
                   break
               except (ParseError, ArgMissing) as e:
                   retry += 1
                   if retry > 1: break          # B4 retry≤1
                   system_prompt += f"\n\n# Retry hint\n{e}"   # 临时附加，不持久化

           # ---- 7.d 路由 ----
           if action is None or action.illegal:
               # silent: 仅写失败记录（B4+B9）
               INSERT direct_message(... delivered=0, attempted_at=t)  # 若是 send 类
           else:
               await ActionDispatcher.dispatch(a, action, world, t)
               # 详细路由表见下方"动作路由"小节

           # ---- 7.e segment 追加（compressor raw 输入）----
           world.memory.segment.append(a.id, RawEntry(t=t, kind=action.type, payload=action))

           # ---- 7.f max_raw_actions 兜底（即使没 MOVE 也强制压缩）----
           if len(world.memory.segment[a.id]) >= memory_config.max_raw_actions:
               asyncio.create_task(world.memory.compressor.on_threshold(a.id))

   # ====== 动作路由（ActionDispatcher.dispatch 内部，LAYOUT §6.1 步骤 7 + §6.2）======
   # SPEAK_TO_LOCAL → FaceToFaceBus
   #     → world.db.direct_message(channel='F2F', arrive_at=t, delivered=1)
   #     → world.db.overhear（同地点旁观）
   #     ↑ 立刻可见：地点内下一个 agent 的 obs 自动包含
   #
   # UPDATE_STATE  → 直接改 world.agents[a].current_state（不走 Bus，无锁单 owner 写）
   #
   # SEND_MESSAGE  → RemoteMessageBus（lockstep）
   #     → φ_RDC 校验
   #     → 失败: world.db.direct_message(channel='RDC', delivered=0, arrive_at=t, attempted_at=t)
   #     → 成功: world.db.direct_message(channel='RDC', delivered=1, arrive_at=t+delay, attempted_at=t)
   #     ↑ 不立即可见；下轮 PerceptionBuilder 按 arrive_at<=t' 拉取
   #
   # SEND_TO_GROUP → GroupMessageBus（lockstep）
   #     → world.db.group_message + world.db.direct_message(channel='GRP', ...)
   #
   # CREATE/JOIN/LEAVE_GROUP → GroupMessageBus
   #     → world.db.{chat_group, group_member, group_event}
   #     → LEAVE/KICK 同时 DELETE direct_message WHERE recipient=? AND group_id=? AND delivered=0（B6 清未读）
   #
   # CREATE_POST 等 FEED → world.pools.platform_for(p,f).Channel
   #     → pool_*.db.{post, like, trace, ...}（lockstep）
   #
   # REQUEST_MOVE  → ScriptEngine.审批 → world.queue_move(a, new_place_id)
   #     ↑ 推迟到步骤 9 统一结算；MOVE 不在 micro-tick 内生效

========== 阶段 C: 轮末结算（lockstep, 严格顺序）==========

# 步骤 8: micro-tick 结束后，所有 RDC/GRP delivered=0 已由步骤 5 + 步骤 7 写入
8. # no-op；不再单独 flush（step 5 的 sweep + step 7 的 INSERT 已覆盖）

# 步骤 9: pending_moves 串行结算 + BehaviorCompressor.on_move
9. for a, new_place in world.pending_moves.items():
       old_place = world.places.L_t[a]
       # 容量检查（capacity=1 场景：先到先得；后到者失败进 B9 silent）
       if not world.places.has_capacity(new_place):
           continue   # 失败 silent，下轮 obs.recent_failed_attempts 透传
       # 关键 hook: MOVE 真正写入之前触发摘要
       asyncio.create_task(
           world.memory.compressor.on_move(a, old_place, new_place)
       )
       # → 异步: 读 segment[a] → Haiku 摘要 → ChatMemory append + 清 raw + Zep enqueue
       world.places.move(a, new_place)   # 写 world.db.agent_location
   world.pending_moves.clear()

# 步骤 10: Zep flush（仅刷新 compressor 入队的 episode）
10. await world.memory.multi_graph_updater.flush()
    # 写 Zep agent_{a} / place_{p} / world

# 步骤 11: 推进时间
11. world.clock.advance()    # world.t += 1
```

**三类并发分离总结**（B1 决议）：

| 类别 | 涉及步骤 | 并发模式 | 何时生效 |
|---|---|---|---|
| 地点内串行 | 步骤 7 内 `for a in agents` | 严格顺序，shuffle seed=`world.t` | F2F 立即可见 |
| 地点间并行 | 步骤 7 顶层 `asyncio.gather` + 步骤 3 `pools.update_all_rec_tables` | 多 coroutine | 跨地点互不干扰 |
| Lockstep（本轮发下轮收） | 步骤 1-2（剧本）/ 5（sweep）/ 8（RDC/GRP 已落库）/ 9（MOVE）/ 10（Zep）/ 11（t++） | 串行执行 | 本轮发下轮 PerceptionBuilder 可见 |

**P0 卡口验证**（LAYOUT §8 P0 卡口检查）：
- 同地点 A 说话 → B 在同一 micro-step 决策时 `obs.incoming_messages` 包含 A 的发言（验证地点内串行可见性）
- shuffle seed=`world.t`：同样的 t 多次跑顺序一致；t 变化顺序变化（验证可复现）
- arrive_at：F2F 写入 `arrive_at=t`；下一个同地点 agent SELECT `arrive_at<=t` 命中

### 4.3 与其他模块的交互

- **上游调用方**：
  - `runner/run_agent_world_simulation.py`（主进程 `while True: await world_step.run_one_tick()`）
- **下游被调方**（按步骤顺序）：
  1. `ScriptEngine.due_events / apply`
  2. `MultiPoolPlatformManager.update_all_rec_tables`
  3. `Scheduler.pick_active`
  4. `DeliveryQueue.sweep_undelivered`
  5. `PerceptionBuilder.build` × N
  6. `SocialAgent.astep`（fork 内 `vendor/oasis/oasis/social_agent/agent.py:127` perform_action_by_llm）
  7. `ActionDispatcher.dispatch` → 6 类路由（FaceToFaceBus / RemoteMessageBus / GroupMessageBus / Pools / ScriptEngine / WorldState UPDATE_STATE）
  8. `BehaviorCompressor.on_move / on_threshold`
  9. `MultiGraphUpdater.flush`
  10. `Clock.advance`
- **共享状态**：
  - 读：`world.db` 12 张表 + `pool_*.db` 13 张表 + Zep 三层 graph
  - 写：每步在不同表上写入；持 `world.delivery_lock` 保护 world.db 直接通信类表（B8 单写者）

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
import asyncio
import random
from typing import Dict, List

class WorldStep:
    def __init__(
        self,
        world: "WorldState",
        scheduler: "Scheduler",
        delivery_queue: "DeliveryQueue",
        retry_policy: "RetryPolicy",
    ) -> None: ...

    async def run_one_tick(self) -> "TickReport":
        """执行一次完整 11 步主循环；副作用即结果。"""
        ...

    # 内部步骤（拆出便于测试）
    async def _phase_a_lockstep_pre(self, t: int) -> List[int]: ...   # 步骤 1-6, 返回 active
    async def _phase_b_micro_tick(
        self, groups: Dict[str, List["SocialAgent"]], t: int
    ) -> None: ...                                                     # 步骤 7
    async def _phase_c_lockstep_post(self, t: int) -> None: ...        # 步骤 8-11

    async def _run_place(
        self, place_id: str, agents: List["SocialAgent"], t: int
    ) -> None: ...                                                     # 步骤 7 内

    @staticmethod
    def _shuffle_in_place(agents: list, seed: int) -> None:
        random.Random(seed).shuffle(agents)
```

### 5.2 IPC / Flask / SQL

- 不直接暴露 IPC（IPC handler 只会调 `world.script.inject_event` / `world.places.move` 等子段方法，不跳过 WorldStep 直接干预流水线）。
- SQL 输入 / 输出：见 §4.2 每步注释；本模块自身不写裸 SQL，全部委托给子模块。

## 6. 配置入口

从 `simulation_config.json`（LAYOUT §7.1）：
- `time_config.{tick_interval, total_ticks}` → runner 决定何时 stop；WorldStep 自身不读
- `channel_config.default_delays.{F2F, RDC, GRP}` → 由 Bus 读取（dispatcher 路由后落库时使用）
- `channel_config.group_message.redeliver_undelivered: bool` → DeliveryQueue.sweep 是否启用
- `channel_config.failed_attempt_ttl_ticks: 1` → PerceptionBuilder 读，确认 1 轮透传
- `channel_config.group_event_ttl_ticks: 1` → 同上
- `memory_config.compressor.{enabled, model, max_raw_actions, summary_sentences}` → BehaviorCompressor 读
- `memory_config.retry_policy.{parse_error_max_retry, arg_missing_max_retry, other_max_retry}` → 步骤 7 retry 控制

**默认值**：
- `redeliver_undelivered = true`（B6 默认开启）
- `parse_error_max_retry = arg_missing_max_retry = 1`，`other_max_retry = 0`（B4 silent）
- `max_raw_actions = 30`（兜底压缩阈值）

**验证规则**：
- `total_ticks >= 1`
- `default_delays.F2F == 0`（不允许 F2F 有延迟，破坏 micro-tick 即时可见性）
- `default_delays.RDC >= 1`（RDC 必须至少 1 tick 延迟，否则 micro-tick 内会跨地点中插）

## 7. 待决策 / 风险

- **#8 / N3**（LAYOUT §9.5 / §9.5.1）：100w agent scale + DeliveryQueue.sweep_undelivered 性能。MVP 单写者 + 全表 sweep；需要建索引 `(delivered, recipient_id, group_id)` + 后期分桶。D 类讨论。
- **#G**（LAYOUT §9.6）：`world.delivery_lock` 单写者锁是否成为瓶颈——已决（v0.3 B8）：MVP 接受，性能瓶颈在 LLM 推理而非 DB；后期再做读写分离评估。
- **MOVE 与 micro-tick 的语义边界**（LAYOUT §6.1 行 524）：两人同时 REQUEST_MOVE 到 capacity=1 地点，按 pending_moves 列表顺序串行处理；后到者 silent。MVP 接受，需在 P0 测试覆盖。
- **N1**（LAYOUT §9.5.1）：MOVE 之外的"被动行为边界"（被踢出群、剧本强制传送）是否触发 BehaviorCompressor。MVP 默认沿用 MOVE 触发；如有需要再加显式 `END_BEHAVIOR` action。
- **风险**：步骤 7 内 `asyncio.gather` 任一 coroutine 抛异常会取消其他地点；需要包一层 `return_exceptions=True` 并在 TickReport 中收集失败地点而不直接中断仿真。
- **风险**：步骤 9 的 `BehaviorCompressor.on_move` 用 `asyncio.create_task` 异步触发，不阻塞 dispatch；若 Haiku 接口抖动堆积大量未完成 task，可能导致内存增长。N4 决议：失败保留 raw 不清 ChatMemory，下次 MOVE 重试；MVP 不做指数退避。
- **风险**：retry 仅在 parse_error / arg_missing 时进行；其他 illegal（coverage / capability / target 不存在）走 silent，但本步骤 7 的实现需要 ActionDispatcher 把 illegal 类型清晰区分回传——是 dispatcher.md 与本文件之间的接口契约要点。
