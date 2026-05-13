# Behavior Compressor 实现文档

> 路径: `agent_world/memory/compressor.py`
> 对应 LAYOUT §: §2.F compressor (v0.3 新增) / §6.1 步骤 9 / §9.5.1 N4
> 上游依赖文档: `memory_segment.md` (compressor drain segment), `memory_translator.md` (拼 prompt 用 translator 输出), `memory_manager.md` (compressor 通过 manager 找 updater), `memory_updater.md` (Zep 入队接口)
> 下游依赖文档: 无 (本模块是 v0.3 行为压缩链的最末环之一)

## 1. 模块定位

行为级压缩的执行者。当 agent 发生 MOVE (或段长触顶) 时，把该 agent segment 中的 raw 行为流交给 Haiku 异步生成 1-3 句摘要；摘要落两处:

1. ChatMemory 追加一条 system message 替换 raw 段 (清掉 ChatMemory 中对应 raw entries)
2. Zep `graph_{agent}` 入队一条 episode (经 MultiGraphUpdater)

raw action **不**从 trace / direct_message 删；那两张表仍是审计真相 (LAYOUT §2.F compressor 改动 5)。

- 输入: ActionDispatcher 在 MOVE 路由前 hook `on_move(agent_id, old_place_id, new_place_id)`；或 segment 阈值回调 `on_threshold_hit(agent_id)`
- 输出: 副作用——ChatMemory 多一条 summary、Zep 多一条 episode、segment 变空
- 必须存在的理由: (a) raw 行为流污染 ChatMemory 上下文，必须周期性蒸馏；(b) MOVE 是天然的"行为段"边界，语义自洽；(c) 摘要进入 Zep 可被未来跨 agent / 跨 place 检索复用。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| —— | —— | —— | NEW | 全新写，无直接来源 |
| 设计灵感: Haiku 异步摘要 | (内部经验) | —— | PATTERN | claude-haiku-4-5-20251001 调用 |
| 设计灵感: MiroFish enqueue → Zep | MiroFish | `zep_graph_memory_updater.py` | PATTERN | 经 updater 间接落 Zep |
| ChatMemory append 接口 | camel | `camel.memories.ChatHistoryMemory.write_records` | KEEP | 直接调，不改其内部 |

## 3. 关键改动 (相对来源仓库)

- **改动 1 (全新写)**: 无来源；为 v0.3 行为级压缩机制设计。
- **改动 2 (异步不阻塞 dispatch)**: ActionDispatcher 在 MOVE 路由前 `asyncio.create_task(compressor.on_move(...))`；MOVE 真正写 `world.db.agent_location` 不等待摘要完成。轮末步骤 9 显式 `await` 一遍尚未完成的任务以保证 ChatMemory 在下轮 PerceptionBuilder 读取之前已就绪 (LAYOUT §6.1 步骤 9)。
- **改动 3 (失败回退保留 raw)**: Haiku 调用失败 / 超时时**不**清 segment、**不**清 ChatMemory raw、**不**入队 Zep；下次 MOVE 重试 (LAYOUT N4)。MVP 不做指数退避。
- **改动 4 (双路径触发)**: MOVE hook + segment 阈值兜底；两路径走同一 `_compress(agent_id)` 内部函数，加 per-agent 重入锁防并发。
- **改动 5 (写两处)**: ChatMemory append + Zep enqueue 两处都成功才算成功；任一失败保留 raw。
- **改动 6 (raw 不删 trace / direct_message)**: 仅清 segment + ChatMemory 内对应 raw entries；trace / direct_message 永久保留。

## 4. 核心逻辑

### 4.1 数据结构

```python
class CompressionTask:
    agent_id: int
    trigger: Literal["move", "threshold"]
    old_place_id: str | None
    new_place_id: str | None
    started_at: float

# 重入锁
_locks: Dict[int, asyncio.Lock]    # 每 agent 一把

# pending tasks 注册表 (轮末 await 用)
_pending: Dict[int, asyncio.Task]

# 模型 client
_haiku_client: AnthropicClient
_haiku_model: str = "claude-haiku-4-5-20251001"

# 配置
_summary_sentences: str = "1-3"
_haiku_timeout_seconds: float = 15.0
```

不变量:
- 同一 agent 同时只有一个 `_compress` 在跑；后到的 hook 等前一个完成或直接 skip (取决于 trigger)
- 失败时 segment 不清；下次 MOVE 自然重试
- 成功时 segment 一定空、ChatMemory 一定有 summary、Zep 一定 enqueue

### 4.2 关键流程 / 算法

**on_move (ActionDispatcher 在 MOVE 路由前 hook):**

```
async def on_move(agent_id, old_place_id, new_place_id):
    task = asyncio.create_task(_compress(agent_id, "move", old_place_id, new_place_id))
    _pending[agent_id] = task
```

**on_threshold_hit (segment 注册的回调):**

```
async def on_threshold_hit(agent_id):
    if agent_id in _pending and not _pending[agent_id].done():
        return                # 已在跑，跳过
    task = asyncio.create_task(_compress(agent_id, "threshold", None, None))
    _pending[agent_id] = task
```

**_compress (核心):**

```
async def _compress(agent_id, trigger, old, new):
    async with _locks[agent_id]:
        seg = segment_store.peek(agent_id)
        if not seg:
            return
        prompt = _build_prompt(seg, agent_id, old, new)
        try:
            summary = await asyncio.wait_for(
                _haiku_client.complete(prompt, model=_haiku_model,
                                        max_tokens=200),
                timeout=_haiku_timeout_seconds,
            )
        except (TimeoutError, HaikuError) as e:
            log.warn("compressor failed, keep raw", e); return    # N4: 不清

        # 双写
        try:
            await chat_memory.append_system(agent_id,
                f"[summary t={seg[0].t}-{seg[-1].t}] " + summary)
            await chat_memory.drop_raw_range(agent_id,
                from_t=seg[0].t, to_t=seg[-1].t)
            await manager.for_agent_updater(sim_id, agent_id).enqueue(
                graph_id=f"agent_{agent_id}",
                kind="behavior_summary",
                text=summary,
                metadata={"t_start": seg[0].t, "t_end": seg[-1].t,
                          "trigger": trigger,
                          "old_place": old, "new_place": new},
            )
        except Exception as e:
            log.warn("compressor write failed, keep raw", e); return

        segment_store.drain(agent_id)         # 仅成功路径才清
```

**_build_prompt:**

```
def _build_prompt(seg, agent_id, old, new):
    raw_log = "\n".join(e.text for e in seg)
    trigger_line = (f"Agent moved from {old} to {new}." if old else
                    f"Agent reached behavior threshold ({len(seg)} events).")
    return f"""You summarize an agent's behavior segment in {_summary_sentences} sentences.
{trigger_line}

Raw events:
{raw_log}

Summary:"""
```

**轮末 await (LAYOUT §6.1 步骤 9):**

```
async def await_all_pending():
    tasks = list(_pending.values())
    _pending.clear()
    await asyncio.gather(*tasks, return_exceptions=True)
```

### 4.3 与其他模块的交互

- 上游调用方:
  - `world/dispatcher.py` ActionDispatcher 在 MOVE 路由前 `asyncio.create_task(compressor.on_move(...))`
  - `memory/segment.py` SegmentStore 在 max_raw_actions 触发时调 `compressor.on_threshold_hit(...)`
  - `runner/run_agent_world_simulation.py` WorldStep 步骤 9 调 `await_all_pending()`
- 下游被调方:
  - `memory/segment.py` `peek` / `drain`
  - `memory/translator.py` (间接：通过 segment 中已译好的 RawEntry.text)
  - `memory/manager.py` `for_agent_updater(sim_id, agent_id)`
  - `memory/updater.py` `enqueue(graph_id, kind, text, metadata)`
  - camel `ChatMemory.append_system / drop_raw_range` (注：drop_raw_range 是本项目对 camel ChatMemory 加的薄包装，按时间窗清 raw 记录)
  - Anthropic SDK Haiku client
- 共享状态:
  - 读: `memory/segment.py` 内存 dict
  - 写: ChatMemory (per-agent，camel.ChatHistoryMemory 实例)；Zep `agent_{id}` graph (经 updater)
  - 不读写 world.db / pool_*.db / trace / direct_message

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class BehaviorCompressor:
    def __init__(
        self,
        sim_id: str,
        segment_store: SegmentStore,
        manager: MultiGraphManager,
        chat_memory_provider: Callable[[int], ChatMemory],
        haiku_client,
        model: str = "claude-haiku-4-5-20251001",
        summary_sentences: str = "1-3",
        timeout_seconds: float = 15.0,
    ): ...

    async def on_move(
        self,
        agent_id: int,
        old_place_id: str | None,
        new_place_id: str,
    ) -> None: ...

    async def on_threshold_hit(self, agent_id: int) -> None: ...

    async def await_all_pending(self) -> None:
        """WorldStep 步骤 9 调用：阻塞至所有 pending compress task 完成。"""

    def has_pending(self, agent_id: int) -> bool: ...
```

### 5.2 IPC / Flask / SQL

- 不暴露 IPC / Flask 路由
- 不直接写 SQL；间接经 ChatMemory (内存) + Zep (经 updater)
- 关键写入路径:
  - **ChatMemory** (camel per-agent 实例): `append_system(summary)` + `drop_raw_range(t_start, t_end)`
  - **Zep `graph_{agent_id}`**: 经 `MultiGraphUpdater.enqueue(graph_id=f"agent_{a}", kind="behavior_summary", ...)`
  - **`world.db.direct_message`**: **不写**、**不删** (审计真相)
  - **pool_*.db.trace**: **不写**、**不删** (审计真相)

## 6. 配置入口

来自 `simulation_config.json.memory_config.compressor`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `memory_config.compressor.enabled` | true | false 时所有 hook 变 no-op |
| `memory_config.compressor.model` | `"claude-haiku-4-5-20251001"` | Haiku 模型 ID |
| `memory_config.compressor.max_raw_actions` | 30 | 段长兜底阈值 (与 segment 共享) |
| `memory_config.compressor.summary_sentences` | `"1-3"` | prompt 中嵌入的句数约束 |
| `memory_config.compressor.timeout_seconds` | 15.0 | Haiku 单次调用超时 |
| `memory_config.compressor.haiku_max_tokens` | 200 | 输出 token 上限 |

验证: enabled=true 时必须能拿到 Anthropic API key (从环境变量或 config)。

## 7. 待决策 / 风险

- (LAYOUT N4) Haiku 摘要失败仅保留 raw + log warn，不指数退避；如失败率高，P5 后期再加退避。
- (LAYOUT N1) MOVE 之外的"被动行为边界" (被踢群、剧本强制传送) 当前不主动触发；max_raw_actions 兜底是唯一安全网。如剧本传送频繁但不 MOVE，可能漏压。
- (LAYOUT N2) UPDATE_STATE 滥用治理：current_state 频繁切换 → segment 内大量 state_change 进 raw → 摘要质量下降。MVP 仅靠 prompt 引导。
- ChatMemory `drop_raw_range` 是本项目薄包装，camel 原 API 不支持按时间窗删；需在 P5 阶段确认 camel.ChatHistoryMemory 内部表是否暴露可写接口或改用 fork。
- 同 agent 短时间内连续两次 MOVE 可能并发触发 `_compress`；per-agent 锁兜底，但第二次 MOVE 时 segment 已是新段，逻辑正常。
- 跨 sim_id 共用 compressor 需谨慎：本类构造时绑定单 sim_id；多 sim 场景由调用方各建一份。
