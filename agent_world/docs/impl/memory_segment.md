# Behavior Segment 实现文档

> 路径: `agent_world/memory/segment.py`
> 对应 LAYOUT §: §2.F segment (v0.3 新增) / §6.1 步骤 9 / §9.5.1 N1
> 上游依赖文档: `memory_translator.md` (append 时调 translator 把 action 转成单行)
> 下游依赖文档: `memory_compressor.md` (compressor 读 segment 后清空)

## 1. 模块定位

每 agent 一份滑动 raw 缓冲，记录"自上一次行为压缩 (MOVE 触发) 之后所有 action / 事件"。它是 BehaviorCompressor 的输入源，也是行为级压缩这个 v0.3 新机制的中枢数据结构。

- 输入: ActionDispatcher / 各 Bus 在每条 action 落库**之后**调 `append(agent_id, kind, payload, t)`
- 输出: compressor 读 `drain(agent_id)` 把当前段拿走 (清空)；retrieval / 其他模块**不读**本结构 (raw 信息真相在 trace / direct_message)
- 必须存在的理由: (a) ChatMemory 的 raw entry 太碎，行为压缩需要"段"概念；(b) trace / direct_message 是审计真相不可清，需要独立的可清空缓冲；(c) `max_raw_actions` 兜底机制需要按 agent 计数。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| —— | —— | —— | NEW | 全新写，无直接来源 |
| 设计灵感: ChatMemory 滑动窗口 | camel | `camel.memories.ChatHistoryMemory` | PATTERN | 不复用其内部结构；本模块独立 |
| 设计灵感: MiroFish per-action buffer | MiroFish | `zep_graph_memory_updater.py:202-477` | PATTERN | 借"按主体分桶"思路 |

## 3. 关键改动 (相对来源仓库)

- **改动 1 (全新写)**: 无来源，全新模块；为 v0.3 行为级压缩机制设计。
- **改动 2 (与 ChatMemory 解耦)**: segment 是 raw 缓冲，camel ChatMemory 只看到 raw entries 或 summary——见 LAYOUT §9.6 H 决议。本模块不动 camel.ChatMemory 内部结构。
- **改动 3 (与 trace 解耦)**: raw action 不从 trace / direct_message 删；这俩仍是审计真相 (LAYOUT §2.F compressor 改动 5)。segment 的 drain 仅清自身缓冲。
- **改动 4 (max_raw_actions 兜底)**: 当 agent 长时间不 MOVE 时，segment 长度达到阈值强制触发 compressor (LAYOUT §2.F compressor 兜底)。具体触发逻辑由 compressor 监听本模块的 `on_threshold_hit` 回调实现，本模块只负责检测与广播。

## 4. 核心逻辑

### 4.1 数据结构

```python
@dataclass(frozen=True)
class RawEntry:
    t: int                        # world.t 当时
    kind: str                     # 'action' | 'incoming_message' | 'overhear' |
                                  # 'relation_change' | 'capability_change' |
                                  # 'state_change' | 'group_event'
    text: str                     # translator.translate(...) 已译好的单行
    metadata: dict                # 原始结构化字段 (供 compressor 二次使用)

# 主存储
_segments: Dict[int, List[RawEntry]]      # agent_id → 当前段

# 阈值监听
_threshold_callbacks: List[Callable[[int], Awaitable[None]]]
#                          ↑ 收到 agent_id，由 compressor 注册
```

不变量:
- 每 agent 至多一段；调 `drain(agent_id)` 后该 agent 的 list 为空
- 段内顺序 = 落库 (append) 顺序 = action 实际发生顺序 (微秒级)
- `len(_segments[a]) <= max_raw_actions + ε`；超过 max_raw_actions 时立即触发回调，回调内 compressor 在 epsilon 时间窗内 drain
- segment 不持久化；仿真进程 crash 后该段丢失 (与 trace 真相互补，可重建)

### 4.2 关键流程 / 算法

**append (ActionDispatcher / Bus 调):**

```
def append(agent_id, kind, payload, t, ctx):
    text = translator.translate_event(kind, ctx | {'t': t, ...})
    entry = RawEntry(t=t, kind=kind, text=text, metadata=payload)
    _segments[agent_id].append(entry)
    if len(_segments[agent_id]) >= _max_raw_actions:
        for cb in _threshold_callbacks:
            asyncio.create_task(cb(agent_id))    # 不阻塞
```

**drain (compressor 调):**

```
def drain(agent_id) -> List[RawEntry]:
    seg = _segments.pop(agent_id, [])
    _segments[agent_id] = []
    return seg
```

**peek (debug / report 用):**

```
def peek(agent_id) -> List[RawEntry]:
    return list(_segments[agent_id])     # 不清空
```

**回调注册 (compressor 启动时调):**

```
def register_threshold_callback(cb):
    _threshold_callbacks.append(cb)
```

### 4.3 与其他模块的交互

- 上游调用方 (写):
  - `world/dispatcher.py` ActionDispatcher 在每条 action 路由完成后调 `segment.append(agent, 'action', ...)`
  - `buses/face_to_face.py` 在 overhear / incoming_message 写 world.db 之后调 `segment.append(target_agent, 'incoming_message', ...)`
  - `buses/remote_message.py` 同上 (delivered=1 才 append；delivered=0 不进 segment——B9)
  - `buses/group_message.py` 同上 + group_event 也 append
  - `world/relation_graph.py` on_change 钩子调 `segment.append(src, 'relation_change', ...)`
  - `world/capability_table.py` on_change 钩子调 `segment.append(agent, 'capability_change', ...)`
  - `world/state.py` UPDATE_STATE 落地后调 `segment.append(agent, 'state_change', ...)`
- 下游被调方 (读):
  - `memory/compressor.py` `BehaviorCompressor.on_move` / `on_threshold_hit` 调 `drain`
  - 调 `translator.translate_event(...)` 生成 text
- 共享状态:
  - 不读写 world.db / pool_*.db / Zep / ChatMemory
  - 仅持有内存 `Dict[agent_id, List[RawEntry]]`

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
@dataclass(frozen=True)
class RawEntry:
    t: int
    kind: str
    text: str
    metadata: dict

class SegmentStore:
    def __init__(self, max_raw_actions: int = 30): ...

    def append(
        self,
        agent_id: int,
        kind: str,
        payload: dict,
        t: int,
        ctx: dict | None = None,
    ) -> None: ...

    def drain(self, agent_id: int) -> List[RawEntry]: ...

    def peek(self, agent_id: int) -> List[RawEntry]: ...

    def length(self, agent_id: int) -> int: ...

    def register_threshold_callback(
        self,
        cb: Callable[[int], Awaitable[None]],
    ) -> None: ...

    def clear(self, agent_id: int) -> None: ...
```

### 5.2 IPC / Flask / SQL

- 不暴露 IPC / Flask 路由
- 不读写任何 SQL 表 (segment 是纯内存缓冲)

## 6. 配置入口

来自 `simulation_config.json.memory_config`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `memory_config.compressor.max_raw_actions` | 30 | 段长达到该阈值强制触发压缩 (与 compressor 共享配置) |
| `memory_config.segment.kinds_to_track` | (full set) | 可选：限制只追踪部分 kind (P5 调试用，MVP 全开) |

## 7. 待决策 / 风险

- (LAYOUT N1) 被踢出群、剧本强制传送等"被动行为边界"是否触发压缩？MVP 默认沿用 MOVE 触发；如出现需要再加显式 `END_BEHAVIOR` action / 在 segment 中加显式分段标记。
- 进程 crash 时段内 raw 丢失：trace / direct_message 仍是真相，可由 report_agent 重建——但本轮的 ChatMemory 摘要会缺。MVP 接受。
- 100w agent 时 `_segments` dict 体积约 100w × 平均段长；按平均段长 10 估算 1000w RawEntry，单条 metadata dict 约 200B → 约 2GB。需要 D 类讨论降阈值或冷热分层。
- `register_threshold_callback` 的 cb 异步触发，compressor 必须在 cb 内自己加锁避免重入 (同 agent 短时间内连击两次阈值)。
