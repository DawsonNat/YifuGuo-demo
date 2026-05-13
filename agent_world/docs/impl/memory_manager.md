# MultiGraphManager 实现文档

> 路径: `agent_world/memory/manager.py`
> 对应 LAYOUT §: §2.F MultiGraphManager
> 上游依赖文档: 无 (本模块是 memory 层入口注册中心)
> 下游依赖文档: `memory_updater.md` (manager 创建并持有 updater 实例)

## 1. 模块定位

`(sim_id, graph_id)` → `MultiGraphUpdater` 的索引层。负责按需创建 / 复用 updater 实例，确保同一仿真 + 同一 graph 全局只有一个 updater 在写。是 compressor / runner 在多仿真并存场景下的复用入口。

- 输入: 调用方传 `(sim_id, graph_id)`，要么取已存在的 updater，要么按需创建并 `start()`。
- 输出: 一个 `MultiGraphUpdater` 实例。
- 必须存在的理由: (a) 一个 Flask 进程可能同时监控多个仿真子进程，sim_id 隔离必要；(b) 三层 graph (agent / place / world) 数量可能上万，updater 不能每次现造；(c) flush_all 时需要枚举所有 updater 触发刷盘。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `ZepGraphMemoryManager` 类 + dict 索引 + lazy init | MiroFish | `backend/app/services/zep_graph_memory_updater.py:479-554` | PATTERN | 框架沿用 |
| `get_or_create_updater` 模式 | MiroFish | 同上 | EDIT | dict key 维度扩 |
| `flush_all` 全局收口 | MiroFish | 同上 | KEEP | 行为不变 |

## 3. 关键改动 (相对来源仓库)

- **改动 1**: dict key 从 `sim_id` 改为 `(sim_id, graph_id)` 二元组。MiroFish 一仿真只有一个 graph_id，本项目一仿真有 N 个 (agent_{id} / place_{id} / world)。
- **改动 2**: `get_or_create_updater(sim_id, graph_id)` 接受 graph_id 参数，内部决定是 lazy 创建一个新 updater 还是复用。
- **改动 3**: 与 LAYOUT §2.F 描述对齐——本模块本身不持有 buffer，buffer 在 updater 内；manager 只是注册表 + 生命周期管理。
- **改动 4**: 提供 `for_agent(sim_id, agent_id)` / `for_place(sim_id, place_id)` / `for_world(sim_id)` 三个语义糖，封装 `world_graphs` 模板字符串展开 (`agent_{id}` / `place_{id}`)，降低调用方拼字符串心智成本。
- **改动 5**: 进程关闭路径调 `shutdown_all()` 串行 stop 所有 updater，保证 buffer 不丢。

## 4. 核心逻辑

### 4.1 数据结构

```python
_updaters: Dict[Tuple[str, str], MultiGraphUpdater]
#                ^sim_id ^graph_id
_locks: Dict[str, asyncio.Lock]   # 按 sim_id 加锁，避免并发创建同一 (sim, graph) 两份
_zep_client: ZepClient
_world_graphs_cfg: WorldGraphsConfig   # per_agent_template / per_place_template / world graph_id
```

不变量:
- 同一 `(sim_id, graph_id)` 全进程唯一 updater 实例
- updater 一旦创建立即 `await updater.start()`
- `_updaters` 在 `shutdown_all()` 后清空

### 4.2 关键流程 / 算法

**get_or_create_updater:**

```
async def get_or_create_updater(sim_id, graph_id):
    key = (sim_id, graph_id)
    if key in _updaters:
        return _updaters[key]
    async with _locks[sim_id]:
        if key in _updaters:                # double-check
            return _updaters[key]
        u = MultiGraphUpdater(_zep_client, sim_id)
        await u.start()
        _updaters[key] = u
        return u
```

**语义糖:**

```
def for_agent(sim_id, agent_id) -> str:
    return _world_graphs_cfg.per_agent_template.format(id=agent_id)
# 调用方: updater = await mgr.for_agent_updater(sim_id, agent_id)
```

**flush_all (WorldStep §6.1 步骤 10):**

```
async def flush_all(sim_id):
    for (s, g), u in _updaters.items():
        if s == sim_id:
            await u.flush_all()
```

### 4.3 与其他模块的交互

- 上游调用方:
  - `BehaviorCompressor` 通过 manager 拿到目标 graph 的 updater
  - `runner/run_agent_world_simulation.py` 在主循环步骤 10 调 `flush_all(sim_id)`
  - 仿真进程退出 hook 调 `shutdown_all()`
- 下游被调方:
  - `MultiGraphUpdater.start / stop / flush_all`
- 共享状态:
  - 不直接读写 world.db / pool_*.db / Zep / ChatMemory；只持有 updater 引用

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class MultiGraphManager:
    def __init__(self, zep_client, world_graphs_cfg: WorldGraphsConfig): ...

    async def get_or_create_updater(
        self, sim_id: str, graph_id: str,
    ) -> MultiGraphUpdater: ...

    async def for_agent_updater(
        self, sim_id: str, agent_id: int,
    ) -> MultiGraphUpdater:
        """语义糖: graph_id = per_agent_template.format(id=agent_id)。"""

    async def for_place_updater(
        self, sim_id: str, place_id: str,
    ) -> MultiGraphUpdater: ...

    async def for_world_updater(
        self, sim_id: str,
    ) -> MultiGraphUpdater: ...

    async def flush_all(self, sim_id: str) -> None: ...

    async def shutdown_all(self) -> None: ...
```

### 5.2 IPC / Flask / SQL

- 不暴露 IPC / Flask 路由
- 不直接读写任何 SQL 表

## 6. 配置入口

来自 `simulation_config.json.world_graphs`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `world_graphs.world` | (必填) | world 层 graph_id |
| `world_graphs.per_agent_template` | `"agent_{id}"` | format 占位符固定 `{id}` |
| `world_graphs.per_place_template` | `"place_{id}"` | 同上 |

验证: 模板必须包含 `{id}`；世界 graph_id 不能与 agent/place 命名空间冲突。

## 7. 待决策 / 风险

- 100w agent 场景下 `_updaters` dict 规模 = agents + places + 1，单进程内存可承受但 flush_all 串行可能成为瓶颈 (LAYOUT N3 + #8 同 D 类讨论)。
- MVP 不做 LRU 淘汰；长跑仿真所有 graph 的 updater 永驻；后期可加冷热分层。
- 多 sim 共享同一 `MultiGraphManager` 实例 (整个 Flask 进程级)；sim 退出后 `shutdown_all` 必须显式调，否则 updater 后台 task 继续 sleep-loop。
