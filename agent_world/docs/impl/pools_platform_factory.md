# PlatformFactory 实现文档

> 路径: `agent_world/pools/platform_factory.py`
> 对应 LAYOUT §: §2.D PlatformFactory / §3.3 / §3.5 / §4 OASIS database.py / §4 OASIS platform.py
> 上游依赖文档: `fork_oasis_recsys.md`
> 下游依赖文档: `pools_manager.md`

## 1. 模块定位

`PlatformFactory` 把"装配一个 OASIS Platform 实例 + 它的 Channel + 它的 SQLite DB + 它的 RecSys"这件事封装成可重复调用的工厂方法。每次调用 `build(place_id, feed_type, simulation_dir)` 产出一个 `PoolHandle` 给 `MultiPoolPlatformManager` 注册。

把工厂从 Manager 拆出来的理由:
- 单元测试: Manager 可注入 mock factory; 真实 factory 走 `vendor/oasis` 重 IO 路径。
- 启动恢复 / 仿真中途加池共用同一构造逻辑; 避免在 Manager 里写两遍。
- Fork 后允许直接改 `Platform.__init__` 签名 (新增 `recsys: RecSys` 参数), 工厂正好是签名变化的唯一调用点。

输入: `(place_id, feed_type, simulation_dir, world_db)` + 全局 channel/recsys 配置。
输出: 一个完全装配好的 `PoolHandle` (含已 `create_db` 完毕的 SQLite + 已 `rebuild` 完毕的 follow 表)。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| Platform 构造序列 | OASIS | `oasis/social_platform/platform.py:56-126` (`__init__`) | EDIT | fork 后 `__init__` 签名调整: 增 `recsys: RecSys` 参数, 去 `recsys_type` 内部分支 |
| Channel 实例化 | OASIS | `oasis/social_platform/channel.py` | KEEP | 每池独立实例, 不共享 |
| DB schema 初始化 | OASIS | `oasis/social_platform/database.py:84-201` (`create_db`) | EDIT | fork 后 schema 列表去掉群聊三表 (chat_group / group_member / group_message); 跑 13 张表 |
| schema 文件清单 | OASIS | `oasis/social_platform/database.py:21-40` (TABLE_NAMES + 常量) | EDIT | 同步删 3 项 |
| user.sql / rec.sql | OASIS | `oasis/social_platform/schema/user.sql`, `rec.sql` | EDIT | fork 内 `user.sql` 去 AUTOINCREMENT, `rec.sql` 修 FK (tweet → post) |
| 启动恢复全量重建 follow | LAYOUT | §3.5 (drop & insert) | NEW | OASIS 原本无对应概念 (单池单图) |
| 工厂主体 | — | — | NEW | 全新写; 把 OASIS 单池构造逻辑提到工厂层 |

## 3. 关键改动 (相对来源仓库)

- **删群聊**: 装配过程中跳过 `chat_group / group_member / group_message` 三个 SQL; 这部分 fork 修改在 `vendor/oasis/social_platform/database.py` 中完成 (LAYOUT §4 EDIT), Factory 只是消费方。
- **传入 recsys**: 调用 `Platform(...)` 时显式构造一个 `RecSys` 实例并作为参数传入 (取代 OASIS 内部 `recsys_type: str` 分支)。每池一份 RecSys, 隔离模型权重 / 用户画像缓存。
- **DB 路径策略**: 路径模板固定 `{simulation_dir}/pools/pool__{place_id}__{feed_type}.db`; 已存在则报错 (避免 stale)。
- **follow 启动恢复**: `build()` 末尾调 `_rebuild_follow_from_world_db()`: `DELETE FROM follow; INSERT ... SELECT ... FROM world_db.relation WHERE relation_type IN ('mutual_follow', 'follower')`。MVP 跨 DB 非原子, 接受。
- **feed_type → RecSys 算法**: `feed_type` 决定 RecSys 实例选择哪个算法 (twitter → twhin, reddit → reddit-sort, lunar_net → random / 自定义)。具体映射在 `RecSys.__init__(feed_type)` 中处理 (见 `fork_oasis_recsys.md`); Factory 仅传 feed_type。
- **Channel 行为**: feed_type 同时影响 Channel 队列形态 (e.g. twitter 走 ActionType FEED 子集; reddit 同子集但 RecSys 排序不同); MVP Channel 类不分叉, 行为差异完全由 RecSys 体现。

## 4. 核心逻辑

### 4.1 数据结构

```
class PlatformFactoryConfig:
    pool_dir_template: str          # "{simulation_dir}/pools"
    pool_db_template: str           # "pool__{place_id}__{feed_type}.db"
    recsys_defaults: Dict[str, dict]  # feed_type → RecSys init kwargs (max_rec_post / use_openai_embedding / ...)
    sandbox_clock: bool             # 是否使用 OASIS Clock 共享实例 (默认 True)

class PlatformFactory:
    cfg: PlatformFactoryConfig
    clock: "Clock"                  # 全局 OASIS Clock 实例 (共享)
    world_db: "WorldDB"
```

不变量:
- Factory 内部不持有 PoolHandle 集合 (那是 Manager 的事)。
- 每次 `build()` 调用结果幂等性: 同样 (place_id, feed_type) 不允许构建两次 (Manager 负责去重)。

### 4.2 关键流程 / 算法

**build(place_id, feed_type, simulation_dir)** 步骤:
```
1. 解析路径
   db_path = simulation_dir / "pools" / f"pool__{place_id}__{feed_type}.db"
   assert not db_path.exists()  # 启动期保证干净

2. 跑 schema (调 fork 后的 OASIS database.create_db)
   from oasis.social_platform.database import create_db
   create_db(db_path)              # 13 张表; 群聊三张已删
   # 副作用: 创建空表; user 表无 AUTOINCREMENT; rec.sql FK 修正后

3. 构造 Channel
   channel = Channel()              # OASIS 原 Channel 类; 每池独立队列

4. 构造 RecSys 实例 (forked, 见 fork_oasis_recsys.md)
   recsys_kwargs = cfg.recsys_defaults.get(feed_type, {})
   recsys = RecSys(feed_type=feed_type, **recsys_kwargs)
       # RecSys.__init__ 自己做模型 / tokenizer / 缓存初始化
       # 同 feed_type 的多个池实例之间, 模型权重可共享 (RecSys 内部决定)

5. 构造 Platform
   platform = Platform(
       db_path=str(db_path),
       channel=channel,
       sandbox_clock=self.clock,
       recsys=recsys,                # 新增参数 (fork)
       # 其他原 OASIS 参数: refresh_rec_post_count / max_rec_post_len / following_post_count / show_score
   )

6. 启动恢复: rebuild follow
   await self._rebuild_follow_from_world_db(db_path, place_id, feed_type)

7. 包装为 PoolHandle 返回
   return PoolHandle(
       key=(place_id, feed_type),
       platform=platform,
       channel=channel,
       recsys=recsys,
       db_path=db_path,
       feed_type=feed_type,
       place_ids={place_id},
   )
```

**_rebuild_follow_from_world_db(db_path, place_id, feed_type)**:
```
- 打开 db_path 的 sqlite 连接 (单写 cursor)
- DELETE FROM follow
- 从 world_db.relation 拉所有 (src_agent, dst_agent) WHERE relation_type IN ('mutual_follow', 'follower')
- 对 mutual_follow: INSERT 双向; 对 follower: INSERT 单向
- 跳过 src 或 dst 在该池没账号 (user 表无对应行) 的记录
- COMMIT
```
注: MVP 全量 drop & insert; 大数据量场景下 P7 再优化。

### 4.3 与其他模块的交互

- **上游调用方**:
  - `MultiPoolPlatformManager.build()` 启动期循环调用
  - `MultiPoolPlatformManager.rebuild_follow_for_pool()` 单池重建 (P7 时启用)
- **下游被调方**:
  - `oasis.social_platform.database.create_db` (fork 后)
  - `oasis.social_platform.platform.Platform.__init__` (fork 后, 新签名)
  - `oasis.social_platform.channel.Channel.__init__`
  - `oasis.social_platform.recsys.RecSys.__init__` (fork 后, 类版本; 见 `fork_oasis_recsys.md`)
  - `WorldDB.fetch_all_relations(types=...)` (follow 投影源)
- **共享状态**:
  - 写: `pool_*.db` 全 13 张表 (主要是 schema 创建 + follow 表写入)
  - 读: `world.db.relation`
  - 不读 / 不写 Zep

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any

@dataclass
class PlatformFactoryConfig:
    pool_dir_template: str = "{simulation_dir}/pools"
    pool_db_template: str = "pool__{place_id}__{feed_type}.db"
    recsys_defaults: Dict[str, Dict[str, Any]] = ...
    sandbox_clock: bool = True

class PlatformFactory:
    def __init__(
        self,
        cfg: PlatformFactoryConfig,
        clock: "Clock",
        world_db: "WorldDB",
    ) -> None: ...

    async def build(
        self,
        place_id: str,
        feed_type: str,
        simulation_dir: Path,
    ) -> "PoolHandle": ...

    async def _rebuild_follow_from_world_db(
        self,
        db_path: Path,
        place_id: str,
        feed_type: str,
    ) -> None: ...

    async def teardown(self, handle: "PoolHandle") -> None: ...
```

### 5.2 IPC / Flask / SQL (如适用)

- **IPC**: 无。
- **Flask**: 无。
- **SQL**:
  - 写: `pool_*.db` 全部 13 张表的 DDL (经 OASIS `create_db`)
  - 写: `pool_*.db.follow` (启动恢复 DELETE + INSERT)
  - 读: `world.db.relation` (类型过滤 mutual_follow / follower)

## 6. 配置入口

从 `simulation_config.json` → `world_config` 派生:

- `world_config.places[i].feeds: List[str]`: 每个 feed_type 触发一次 `build()`。
- `pool_factory.recsys_defaults: Dict[str, dict]` (顶层, 可选): per feed_type 覆盖 RecSys 初始化参数。默认值见 RecSys 文档。
- `pool_factory.pool_dir_template`: 默认 `{simulation_dir}/pools`; 仅运维改路径时调整。
- 验证规则:
  - feed_type 必须是 RecSys 已注册算法之一 (`twitter` / `reddit` / `twhin` / `random` / 用户扩展)
  - place_id 必须在 `world_config.places` 中存在
  - db_path 启动时必须不存在 (新建仿真) 或显式 `--resume` 才允许复用

## 7. 待决策 / 风险

- **#9.6 C 跨 DB 事务**: follow 投影 drop & insert 与 world.db.relation 之间无原子性; 如果启动期世界库被外部并发改写 (实际不会, 启动 single-writer), 接受不一致。
- **N5 arrive_at**: 不影响 Factory; 仅 world.db.direct_message 有该字段, pool_*.db 无关。
- **同 feed_type 多池权重共享**: RecSys 内部决定; 若每池独立加载 TWHIN (~500MB) 会爆显存, MVP 在 RecSys 类内做模块级权重缓存 (见 `fork_oasis_recsys.md` §3 风险)。
- **resume 模式**: MVP 启动只支持 fresh; 若需 crash recovery, db_path 复用语义需要重新设计 (是否仍重建 follow? 是否清 rec 表?)。
- **schema 漂移**: fork 后 schema 由 vendor/oasis 维护; 若 OASIS 上游升级 schema, Factory 调 `create_db` 会拿到新版本; MVP 不 sync upstream, 风险被冻结。
