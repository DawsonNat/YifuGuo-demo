# MultiGraphUpdater 实现文档

> 路径: `agent_world/memory/updater.py`
> 对应 LAYOUT §: §2.F MultiGraphUpdater / §6.1 步骤 10
> 上游依赖文档: `memory_compressor.md` (compressor 入队), `memory_manager.md` (manager 持有 updater 实例)
> 下游依赖文档: 无 (本模块是 Zep 写入终点)

## 1. 模块定位

把 BehaviorCompressor 产出的"行为摘要 episode"批量、异步、按 graph 维度聚合后写入 Zep。它是 Agent World 三层记忆 (`agent_{id}` / `place_{id}` / `world`) 的**唯一 Zep 写入闸门**。

- 输入: compressor 调 `enqueue(graph_id, kind, payload)` 投递一条 episode；payload 是 translator 拼好的自然语言文本。
- 输出: 后台 worker 周期性把同一 `(graph_id, kind)` 桶的若干条 episode 调 Zep SDK 一次性提交。
- 必须存在的理由: (a) Zep 单条 episode 写入有 RTT 延迟，需 batch；(b) 三层 graph 同时写入，buffer 必须按 graph 隔离，避免互相阻塞；(c) compressor 可能在 micro-tick 紧密调用，必须解耦同步主循环。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| Worker 主循环 + buffer flush | MiroFish | `backend/app/services/zep_graph_memory_updater.py:202-477` | PATTERN | 整体架构沿用，键改造 |
| `_platform_buffers` dict | MiroFish | `zep_graph_memory_updater.py:252-255` | EDIT | key 改 `(graph_id, kind)` |
| `BATCH_SIZE` / `SEND_INTERVAL` 常量 | MiroFish | 同上 | KEEP | 数值不动 |
| Zep SDK `add_episode` 调用 | MiroFish | 同上 | KEEP | 原样调用 |

## 3. 关键改动 (相对来源仓库)

- **改动 1**: `_platform_buffers` 的 key 从 `platform_name` 改为 `(graph_id, kind)`。原 MiroFish 单 graph 模式下区分 platform (twitter / reddit)；本项目改为按 `graph_id` 分桶，每个 `(graph_id, kind)` 一个独立 buffer + 独立 lock。
- **改动 2**: 写入触发不再是"每条 action 都进 buffer"，而是 **compressor 完成行为摘要后才 enqueue**。translator 不再直接写 Zep。每次 enqueue 的 payload 是一段 1-3 句的摘要文本，体积可观。
- **改动 3**: `kind` 字段保留 (例：`"behavior_summary"`、`"system_event"`)，作为 Zep `data_type` 透传，方便后期检索时 filter。
- **改动 4**: 删除 MiroFish 中"按 action_type 翻译"的内联逻辑——这部分搬到 `translator.py`。本模块只接收已译好的文本。
- **改动 5**: `flush_all()` 在 WorldStep §6.1 步骤 10 调用，确保本轮 compressor 入队的内容在轮末统一刷盘。

## 4. 核心逻辑

### 4.1 数据结构

```python
# 主缓冲表
_platform_buffers: Dict[Tuple[str, str], List[BufferedEpisode]]
#                       ^graph_id ^kind
_buffer_locks: Dict[Tuple[str, str], asyncio.Lock]
_last_flush_at: Dict[Tuple[str, str], float]      # monotonic 时间戳

@dataclass
class BufferedEpisode:
    graph_id: str
    kind: str                # 'behavior_summary' | 'system_event' | ...
    text: str                # translator 已译好的自然语言
    metadata: dict           # {agent_id, t, place_id, ...} 透传
    enqueued_at: float
```

不变量:
- `BATCH_SIZE = 20`，`SEND_INTERVAL = 5.0` 秒 (沿用 MiroFish)
- 同一 `(graph_id, kind)` 的 flush 串行 (per-key lock)，不同 key 并发 flush
- buffer 内顺序保留 enqueue 顺序，Zep 写入按 enqueue 顺序

### 4.2 关键流程 / 算法

**enqueue 路径 (compressor 调):**

```
enqueue(graph_id, kind, text, metadata):
  key = (graph_id, kind)
  async with _buffer_locks[key]:
      _platform_buffers[key].append(BufferedEpisode(...))
      if len(buffer) >= BATCH_SIZE:
          await _flush_one(key)         # 满 batch 立刻刷
```

**worker 路径 (后台 task):**

```
async def _worker_loop():
  while not stopped:
      await asyncio.sleep(0.5)
      now = monotonic()
      for key, buf in list(_platform_buffers.items()):
          if buf and now - _last_flush_at[key] >= SEND_INTERVAL:
              asyncio.create_task(_flush_one(key))
```

**_flush_one(key):**

```
async with _buffer_locks[key]:
    drained = buffer[:]
    buffer.clear()
for ep in drained:
    await zep_client.graph.add(
        graph_id=ep.graph_id,
        type='message',
        data=ep.text,
        ...
    )
_last_flush_at[key] = monotonic()
```

**flush_all() (轮末调):**

```
for key in _platform_buffers:
    await _flush_one(key)
```

### 4.3 与其他模块的交互

- 上游调用方:
  - `BehaviorCompressor.on_move` → `updater.enqueue(graph_id=f"agent_{a}", kind="behavior_summary", text=..., metadata=...)`
  - 可选 `ScriptEngine` → `updater.enqueue(graph_id="world", kind="system_event", ...)` (后期扩展，MVP 不强求)
- 下游被调方:
  - `zep_client.graph.add(...)` (Zep SDK)
- 共享状态:
  - 写: Zep 三层 graph (`agent_{id}` / `place_{id}` / `world`)
  - 读: 无 (检索由 `retrieval.py` 负责)
  - 不读写 world.db / pool_*.db / ChatMemory

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class MultiGraphUpdater:
    BATCH_SIZE: int = 20
    SEND_INTERVAL: float = 5.0

    def __init__(self, zep_client, sim_id: str): ...

    async def start(self) -> None:
        """启动后台 worker task。"""

    async def stop(self) -> None:
        """停止 worker，flush_all 后退出。"""

    async def enqueue(
        self,
        graph_id: str,
        kind: str,
        text: str,
        metadata: dict | None = None,
    ) -> None:
        """compressor / 其他生产者调用，往 (graph_id, kind) buffer 投递一条 episode。"""

    async def flush_all(self) -> None:
        """WorldStep §6.1 步骤 10 调用；同步把所有 buffer 写空。"""

    def buffer_size(self, graph_id: str, kind: str) -> int: ...
```

### 5.2 IPC / Flask / SQL

- 不暴露 IPC / Flask 路由
- 不直接写 SQL；只调 Zep SDK

## 6. 配置入口

来自 `simulation_config.json.memory_config`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `memory_config.updater.batch_size` | 20 | 单桶满 batch 立刻 flush |
| `memory_config.updater.send_interval_seconds` | 5.0 | 后台 worker 周期 |
| `memory_config.updater.enabled` | true | 关闭后 enqueue 变成 no-op (用于 dry-run) |
| `world_graphs.world` | `world_graph_id` | world 层 graph_id 字面量 |
| `world_graphs.per_agent_template` | `"agent_{id}"` | 三层命名约定 |
| `world_graphs.per_place_template` | `"place_{id}"` | 同上 |

验证: enabled=true 但 zep_client 未注入时启动报错。

## 7. 待决策 / 风险

- (LAYOUT N4) Haiku 摘要失败时 compressor 不 enqueue，本模块无须处理；但若 enqueue 后 Zep SDK 失败，MVP 仅记 log + drop，不重试 (与 N4 一致)。
- (LAYOUT N5) `arrive_at` 字段仅 world.db.direct_message 用，本模块写 Zep 时不携带——不构成兼容问题。
- 多 `(graph_id, kind)` 数 = O(agents) + O(places) + 1 (world)；100w agent 时 buffer dict 体积是 D 类未决项，与 §9.5 #8 同一议题。
- BATCH_SIZE / SEND_INTERVAL 沿用 MiroFish 数值未在本项目实测；P5 阶段验证后回填默认值。
