# MultiPoolPlatformManager 实现文档

> 路径: `agent_world/pools/manager.py`
> 对应 LAYOUT §: §2.D MultiPoolPlatformManager / §6.1 步骤 3 / §3.3 / §3.5
> 上游依赖文档: `pools_platform_factory.md`, `fork_oasis_recsys.md`
> 下游依赖文档: 无

## 1. 模块定位

`MultiPoolPlatformManager` 是 Agent World 多池推荐子系统的总编排器。系统中可能并存多个 OASIS Platform 实例 (例如 `pool__earth__twitter`, `pool__earth__reddit`, `pool__moon__lunar_net`), 每个 Platform 各拥有独立的 `Channel` / `pool_*.db` / `RecSys` 实例。Manager 负责:

- **生命周期**: 启动期按 `world_config` 装配出 N 个 Platform; 关停时统一关闭 Channel + flush DB。
- **每轮编排**: WorldStep 步骤 3 调用 `update_all_rec_tables()`, Manager 用 `asyncio.gather` 并发调用各池 `Platform.update_rec_table()`。
- **路由查询**: 给 PerceptionBuilder / ActionDispatcher 提供 `platform_for(place_id, feed_type)` 与 `feeds_at(place_id)`, 与 `ConnectivityResolver.φ_FEED` 协作完成 "哪个地点能访问哪些 feed" 的解析。
- **关系投影管控**: 新池加入 / 启动恢复阶段触发 `PlatformFactory` 重建 follow 表 (具体 SQL 在 PoolDB 层, 但调度时机在 Manager)。

输入: `world_config.places` 中各地点的 `feeds`、`coverage`、`account_<feed>` capability 表; `world.db.relation` 投影源。
输出: 一组热的 OASIS Platform 句柄, 以及若干批量并发协程。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 装配链路 | OASIS | `oasis/environment/make.py` 全文件 | PATTERN | 单池装配模式拆解后按池循环 |
| step.update_rec 调用顺序 | OASIS | `oasis/environment/env.py:71-116` | PATTERN | 仅借 "先 update_rec → 收 LLM → 推进时间" 的顺序; 多池版改为 `asyncio.gather` |
| Channel 派发 | OASIS | `oasis/social_platform/channel.py` | KEEP | 每池一 Channel 实例, 不共享 |
| Platform 内部循环 | OASIS | `oasis/social_platform/platform.py:148` 反射 dispatch | PATTERN | Manager 不直接调 dispatch; 由 ActionDispatcher 拿到 Platform 后再走 Channel |
| 全部本模块代码 | — | — | NEW | OASIS `make.py` 单池, Agent World 多池, 主体重写 |

## 3. 关键改动 (相对来源仓库)

- 把 OASIS `make.py` 中"一个 EnvAction → 一个 Platform"的 1:1 装配改成 1:N: 一个 `world_config` 派生 N 个 `(place_id, feed_type)` 池条目, 逐条调 `PlatformFactory.build()`。
- 删除 OASIS `OasisEnv.step` 中的 update_rec / LLM gather / time+1 三件事的捆绑: Manager 只负责 update_rec; LLM gather 由 `WorldStep` 主循环负责; 时间推进由全局 `Clock` 推进。
- 新增 `feeds_at(place_id)` 与 `platform_for(place_id, feed_type)` 两个 lookup API, 与 `ConnectivityResolver.φ_FEED` 配合; OASIS 原代码无地点维度。
- 新增 `rebuild_follow_for_pool(pool_key)` 调度入口: 启动 / 新池加入时由 RelationGraph 投影驱动。

## 4. 核心逻辑

### 4.1 数据结构

```
PoolKey = Tuple[str, str]                # (place_id, feed_type)

class PoolHandle:
    key: PoolKey
    platform: Platform                   # forked OASIS Platform 实例
    channel: Channel                     # OASIS Channel, 一池一实例
    recsys: RecSys                       # forked OASIS RecSys 类实例
    db_path: Path                        # pool__<place>__<feed>.db 绝对路径
    feed_type: str                       # 'twitter' | 'reddit' | 'lunar_net' | ...
    place_ids: Set[str]                  # 该池可被哪些地点访问 (来自 world_config.places)

class MultiPoolPlatformManager:
    pools: Dict[PoolKey, PoolHandle]
    feeds_index: Dict[str, List[PoolHandle]]    # place_id → 可见 feed 列表反向索引
    factory: PlatformFactory                    # 注入式
    world_db: WorldDB                           # 投影 follow 表的源
```

不变量:
- 每个 PoolKey 唯一; `feeds_index[p]` 必为 `pools.values()` 子集。
- 仿真生命周期内, `PoolHandle.platform` 引用稳定 (不重建); 池内部状态变化通过 `RecSys` 与 `Channel` 完成。
- `place_ids` 集合在启动后不可变 (剧本 `PlaceMutation` 改 attrs 不改 feed 拓扑; 如需改, P7 阶段再讨论)。

### 4.2 关键流程 / 算法

**启动装配 (build)**:
```
build(world_config, world_db, simulation_dir):
    pools = {}
    feeds_index = defaultdict(list)
    for (place_id, feed_type) in iter_feed_pairs(world_config):
        key = (place_id, feed_type)        # 注意: 多 place 共享一池时 key 退化为 (canonical_place, feed)
        if key already built: 复用 handle
        else:
            handle = factory.build(place_id, feed_type, simulation_dir)
            pools[key] = handle
        feeds_index[place_id].append(handle)
    # 启动恢复: 全量重建 follow
    for handle in pools.values():
        rebuild_follow_for_pool(handle, world_db)   # 见 §3.5 LAYOUT
    return self
```

**每轮 update_rec**:
```
async def update_all_rec_tables():
    # WorldStep 步骤 3 调用; 与 §6.1 步骤 3 对齐
    await asyncio.gather(*[
        h.platform.update_rec_table()        # OASIS Platform 内部走 RecSys.rec_sys_*()
        for h in pools.values()
    ])
```
失败语义: 单池失败不应阻塞其它池; 用 `asyncio.gather(..., return_exceptions=True)` 收集异常并写 warn (不抛, 让 WorldStep 继续推进时间)。

**routing**:
```
def platform_for(place_id: str, feed_type: str) -> Platform | None:
    handle = pools.get((place_id, feed_type)) or pools.get((canonical(place_id), feed_type))
    return handle.platform if handle else None

def feeds_at(place_id: str) -> List[FeedBrief]:
    return [h.brief for h in feeds_index.get(place_id, [])]
```
`FeedBrief` 是给 PerceptionBuilder 用的轻量 DTO (含 feed_type / pool_key / display_name); 不暴露 Platform 句柄给 LLM 层。

**新池加入 (P7 准备, MVP 不实现)**:
- 当 RelationGraph 投影到一个 pool 但 pool 尚未热实例化时, 触发 `factory.build` + `rebuild_follow_for_pool`。
- MVP 只在启动期处理; 仿真中途新增池由 P7 完善。

### 4.3 与其他模块的交互

- **上游调用方**:
  - `WorldStep` → `update_all_rec_tables()` (步骤 3)
  - `PerceptionBuilder` → `feeds_at(place_id)` (构建 `obs.feeds`)
  - `ActionDispatcher` → `platform_for(place_id, feed_type).channel.write_to_receive_queue(...)` (CREATE_POST / LIKE / FOLLOW 等 FEED 类 action 路由)
  - `agents/dynamic_tools.py` → `feeds_at(...)` 决定哪些 FEED 工具暴露给 LLM
- **下游被调方**:
  - `PlatformFactory.build()` (装配)
  - `Platform.update_rec_table()` (每轮)
  - `WorldDB.fetch_relations_for_pool(pool_key)` (启动恢复)
- **共享状态**:
  - 读 `world.db.relation` (启动恢复)
  - 间接经 Platform 读写 `pool_*.db` 全 13 张表
  - 不直接读写 Zep

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
from typing import Dict, List, Set, Tuple, Optional
from pathlib import Path

PoolKey = Tuple[str, str]   # (place_id, feed_type)

class FeedBrief:
    feed_type: str
    pool_key: PoolKey
    display_name: str

class PoolHandle:
    key: PoolKey
    platform: "Platform"            # forked OASIS
    channel: "Channel"
    recsys: "RecSys"
    db_path: Path
    feed_type: str
    place_ids: Set[str]
    @property
    def brief(self) -> FeedBrief: ...

class MultiPoolPlatformManager:
    def __init__(
        self,
        factory: "PlatformFactory",
        world_db: "WorldDB",
    ) -> None: ...

    @classmethod
    async def build(
        cls,
        world_config: "WorldConfig",
        world_db: "WorldDB",
        simulation_dir: Path,
        factory: "PlatformFactory",
    ) -> "MultiPoolPlatformManager": ...

    async def update_all_rec_tables(self) -> None: ...

    def platform_for(self, place_id: str, feed_type: str) -> Optional["Platform"]: ...

    def feeds_at(self, place_id: str) -> List[FeedBrief]: ...

    def all_pools(self) -> List[PoolHandle]: ...

    async def rebuild_follow_for_pool(self, pool_key: PoolKey) -> None: ...

    async def shutdown(self) -> None: ...
```

### 5.2 IPC / Flask / SQL (如适用)

- **IPC**: 无直接命令; UI 的 `LIST_PLACES` 经 Manager 间接拿 `feeds_at`。
- **Flask**: 无新路由; `GET /simulations/<id>/world-state` 内部拼装 `feeds_at` 结果。
- **SQL**:
  - 读 `world.db.relation` (启动恢复, 全表扫描)
  - 写 `pool_*.db.follow` (启动恢复 drop & insert; 由 Factory 实际执行, Manager 仅调度)
  - 间接读写 `pool_*.db.rec` (经 Platform.update_rec_table)

## 6. 配置入口

从 `simulation_config.json` → `world_config.places[].feeds: List[str]` 与 `world_config.coverage` 派生池清单。

- `world_config.places[i].feeds`: 该地点暴露的 feed 列表 (e.g. `["twitter"]` / `["reddit", "lunar_net"]`)
- `world_config.places[i].pool_canonical: str | None`: 可选, 若多 place 共享同一物理池, 指定 canonical place_id (默认每个 place 独立池)
- 路径模板: `{simulation_dir}/pools/pool__{place_id}__{feed_type}.db`
- 默认值: 若 `place.feeds` 缺省则视为 `[]` (该地点无 feed); 验证规则: 任一 feed_type 必须能在 `RecSys` 类的注册算法中找到 (twitter / reddit / twhin / random)。

## 7. 待决策 / 风险

- **N5 (LAYOUT §9.5.1)**: `arrive_at` 字段仅 world.db.direct_message 有; pool_*.db.trace 不受影响, Manager 无须感知。
- **#8 (LAYOUT §9.5)**: 100w agent scale 下 `feeds_index` 反向索引内存压力; MVP 接受。
- **P7 跨池镜像 / 新池加入**: 仿真中途授予 capability 触发新池热装配的语义, 与 RelationGraph.on_change 的并发顺序需在 P7 复审。
- **update_rec 单池失败处理**: 当前默认 warn 不抛; 是否需要熔断 / 重试由后续运营压测决定。
- **多 place 共享池**: `pool_canonical` 字段尚未在 LAYOUT 显式规定, 默认每地点独立池; 若运营场景出现 "同城多酒吧共享一个 reddit 池", 需要扩展 `place_ids: Set[str]` 语义。
