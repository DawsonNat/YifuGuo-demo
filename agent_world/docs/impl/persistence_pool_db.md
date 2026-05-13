# persistence/pool_db.py 实现文档

> 路径: `agent_world/persistence/pool_db.py`
> 对应 LAYOUT §: §2.H PoolDB / §3.1 双层 DB / §3.3 13 张表 / §3.4 trace 归属 / §3.5 follow 双轨投影 / §9.3 / §9.4
> 上游依赖文档: `persistence_schema_pool.md`, `persistence_world_db.md`
> 下游依赖文档: 无 (pools/manager.py 与 pools/platform_factory.py 通过本模块装配每池 DB)

## 1. 模块定位

`pool_db.py` 是 **每个 pool_*.db 的访问层** —— 每个推荐池一个独立 SQLite 文件 (`pools/pool__<place>__<feed>.db`), 内含 fork 后剩余的 13 张 OASIS 表。本模块的核心职责:

1. **创建**: 调用 fork 后的 OASIS `database.create_db(pool_path)`, 让 OASIS 用自己的 schema 列表 (已剔除群聊三张) 跑 `executescript`。
2. **启动恢复 (A2 双轨投影)**: runner 启动时根据 `world.db.relation` **全量重建**所有 pool 的 `follow` 表 —— `DROP & INSERT` 模式, 不信任增量。
3. **on_change 钩子接收端**: 当 `RelationGraph.on_change` 触发 `mutual_follow / follower` 类边变更时, 立即向本池 `follow` 表写一行 (跨 DB 不原子)。
4. **新池加入 (A2/A3)**: agent 拿到 `account_<feed>` capability 时, 调 `sign_up()` 注册 `user` 行 + 补齐该 agent 在 world.db.relation 里的所有 mutual_follow / follower 投影。

- **输入**: 上层调用 (sign_up / on_change relation hook / 启动期 rebuild_follow); 来自 OASIS `Platform` 的高频 FEED 类写
- **输出**: pool 内 13 张表的物理状态; 启动期返回打开好的 SQLite connection 给 `PlatformFactory` 注入到 `Platform`

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `create_db()` 复用 | OASIS (fork) | `vendor/oasis/oasis/social_platform/database.py:84-201` | KEEP | fork 内删群聊三段 (L177-193) 后**直接调用** |
| `TABLE_NAMES` 常量 | OASIS (fork) | `database.py:42-59` | EDIT | fork 内删 `group / group_member / group_message` |
| `sign_up()` SQL 模板 | OASIS | `oasis/social_platform/platform.py:206-243` | PATTERN | fork 后 `user_id = agent_id` (A3); 不再依赖 +1 偏移 |
| follow 写入 SQL | OASIS | `platform.py:404-450` | PATTERN | 仅作 INSERT 模板参考; 这里由 RelationGraph.on_change 直接调, 不走 OASIS Platform.follow |
| recsys 重构后 RecSys 类 | OASIS (fork) | `oasis/social_platform/recsys.py` (重构) | PATTERN | fork 内 L38-61 模块全局 → 类成员; 每池一实例 (B1) |

## 3. 关键改动 (相对 OASIS database.py)

- **改动 1**: 不再使用 OASIS 默认的"data/social_media.db"全局路径; 每个 pool 显式传 `db_path = {simulation_dir}/pools/pool__{place}__{feed}.db`。
- **改动 2**: 13 张表 (vs OASIS 原 16) —— 群聊三张 (`chat_group / group_member / group_message`) 已搬到 `world.db`, fork 内 `database.py` 已删对应 `executescript` 段。
- **改动 3**: 新增**双轨 follow 投影**逻辑 —— OASIS 不存在的概念。LAYOUT §3.5 决议:
  - 启动期: `rebuild_follow_from_relation(world_db)` 对每 pool drop & insert
  - 运行时: `RelationGraph.on_change(src, dst, type, op)` 钩子直接调本模块写 follow
  - 失败容忍: 跨 DB 写非原子, 崩溃后重启全量重建兜底
- **改动 4**: 新增**懒注册** —— agent 不在所有池都有账号; 拿到 `account_<feed>` capability 时才 `sign_up`。 LAYOUT §9.4 已决, fork 内 `user.sql` 配套改 (PK 去 AUTOINCREMENT, agent_id UNIQUE NOT NULL, `user_id = agent_id` 由 schema 保证)。
- **改动 5**: 直接通信类 action (SPEAK_TO_LOCAL / SEND_MESSAGE / SEND_TO_GROUP) **不写**本池 trace (A5 决议) —— 本模块的 trace 仅承载 FEED 类 action 审计, 由 OASIS Platform 的 `_record_trace` 自动写。本模块**不**主动暴露 trace 写 API。

## 4. 核心逻辑

### 4.1 数据结构

- **PoolDB 实例**: 持有 `pool_id: str` (`pool__earth__reddit` 形式)、`db_path: str`、`conn: sqlite3.Connection`、`feed: str` (twitter/reddit/...)、`place_id: str`、回写 `world_db: WorldDB` 引用 (用于启动期 rebuild)。
- **MultiPoolDB 注册表** (在 `pools/manager.py` 中): `Dict[(place_id, feed), PoolDB]`; 本模块只是单池抽象, 多池由 manager 编排。
- **不变量**:
  - `user.user_id == user.agent_id` (fork 后 schema 保证, A3)
  - `follow` 表中每条 (follower_id, followee_id) 在 `world.db.relation` 里**必有**对应 `mutual_follow` 或 `follower` 类边 (启动期 rebuild + on_change 同步保证)
  - 群聊三表**不存在**于 pool DB (fork 内 database.py 不会创建)

### 4.2 关键流程 / 算法

**(a) 创建池**
```
PoolDB.create(simulation_dir, place_id, feed) -> PoolDB:
    db_path = f"{simulation_dir}/pools/pool__{place_id}__{feed}.db"
    os.makedirs(dirname(db_path), exist_ok=True)
    # 调 fork 后的 OASIS create_db (已删群聊三段)
    conn, cur = oasis.social_platform.database.create_db(db_path)
    return PoolDB(pool_id=..., db_path=..., conn=conn, ...)
```

**(b) 启动期 follow 全量重建 (A2)**
```
rebuild_follow_from_relation(world_db: WorldDB):
    pool_users = {row.user_id for row in conn.execute("SELECT user_id FROM user")}
    conn.execute("DELETE FROM follow")              # drop
    rels = world_db.fetch_active_relations(t=0)
    for r in rels:
        if r.relation_type not in ('mutual_follow', 'follower'): continue
        if r.src not in pool_users or r.dst not in pool_users: continue
        if r.relation_type == 'mutual_follow':
            insert_follow(r.src, r.dst); insert_follow(r.dst, r.src)
        else:                                      # follower (有向)
            insert_follow(r.src, r.dst)
    conn.commit()
```
注: runner 启动时对**所有** pool 跑一遍, 多花数秒换 crash recovery 简单 (LAYOUT §3.5)。

**(c) 运行时 on_change 投影**
```
on_relation_change(src, dst, rtype, op):       # 由 RelationGraph 调
    if rtype not in ('mutual_follow', 'follower'): return
    if src not in pool_users: return                 # agent 在本池无账号则跳过
    if op == 'create':
        if rtype == 'mutual_follow':
            insert_follow(src, dst); insert_follow(dst, src)
        else:
            insert_follow(src, dst)
    elif op == 'break':
        if rtype == 'mutual_follow':
            delete_follow(src, dst); delete_follow(dst, src)
        else:
            delete_follow(src, dst)
```
跨 DB 不原子: world.db.relation 已 commit, pool 写失败仅 log warn; 下一次启动 rebuild 修复。

**(d) 新池加入 (capability 触发)**
```
on_account_granted(agent_id: int, t: int):
    # 1. 注册 user 行 (sign_up)
    conn.execute("INSERT INTO user (user_id, agent_id, ...) VALUES (?, ?, ...)",
                 (agent_id, agent_id, ...))         # A3: user_id = agent_id
    # 2. 补齐 follow 投影
    rels = world_db.fetch_active_relations(t)
    for r in rels:
        if r.relation_type not in ('mutual_follow', 'follower'): continue
        # agent_id 作为 src 或 dst 都要补 (mutual 双写)
        ...
    conn.commit()
```

**(e) FEED 类写入路径** (本模块**不参与**)
```
agent.create_post → OASIS Platform.create_post → conn.execute("INSERT INTO post ...")
                                                + _record_trace → conn.execute("INSERT INTO trace ...")
```
本模块仅持有 conn, 不拦截 FEED 写。

### 4.3 与其他模块的交互

- **上游调用方**:
  - `pools/manager.py` (MultiPoolPlatformManager) — 启动期 `for each pool: PoolDB.create() + rebuild_follow_from_relation`
  - `pools/platform_factory.py` — 拿到 `PoolDB.conn` 注入到 `Platform` 实例 + 注入新建的 `RecSys` 实例
  - `world/relation_graph.py` — `on_change` 钩子调 `on_relation_change`
  - `world/capability_table.py` — `on_grant(account_<feed>)` 钩子调 `on_account_granted`
  - `runner/run_agent_world_simulation.py` — 启动期协调 rebuild_follow
- **下游被调方**:
  - `vendor/oasis/oasis/social_platform/database.py:create_db` (fork 后)
  - `WorldDB.fetch_active_relations` (启动期 rebuild)
  - `sqlite3` 标准库
- **共享状态**:
  - 写: 本池 13 张表 (主要是 `user / follow`; FEED 类表由 OASIS Platform 直接写)
  - 读: `world.db.relation` (跨 DB 读, 仅启动期 + on_change 时点)
  - 不读不写 Zep

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class PoolDB:
    def __init__(self, pool_id: str, place_id: str, feed: str,
                 db_path: str, conn: sqlite3.Connection,
                 world_db: "WorldDB") -> None: ...

    @classmethod
    def create(cls, simulation_dir: str, place_id: str, feed: str,
               world_db: "WorldDB") -> "PoolDB": ...
        # 调 fork OASIS create_db, 返回打开好的实例

    def close(self) -> None: ...
    @property
    def conn(self) -> sqlite3.Connection: ...     # 给 PlatformFactory 注入 Platform

    # ---- 双轨 follow 投影 (A2) ----
    async def rebuild_follow_from_relation(self) -> int: ...
        # drop & insert; 返回 inserted 行数
    async def on_relation_change(self, src: int, dst: int,
                                 rtype: str, op: str) -> None: ...
        # rtype ∈ {'mutual_follow', 'follower', ...}; 仅前两类投影
        # op ∈ {'create', 'break'}

    # ---- 懒注册 (A3 + B-account_capability) ----
    async def on_account_granted(self, agent_id: int, t: int) -> None: ...
        # sign_up + 补齐 follow 投影

    async def sign_up(self, agent_id: int, user_name: str, name: str,
                      bio: str, created_at: int) -> None: ...
        # 直接 INSERT INTO user (user_id=agent_id, ...)

    # ---- 工具 / 查询 ----
    def list_user_ids(self) -> set[int]: ...      # 缓存到 self._user_ids
    def has_user(self, agent_id: int) -> bool: ...

    # ---- 内部 ----
    def _insert_follow(self, src: int, dst: int) -> None: ...
    def _delete_follow(self, src: int, dst: int) -> None: ...
```

`pools/manager.py` 顶层使用样例 (示意, 不在本模块):
```python
class MultiPoolPlatformManager:
    pools: dict[tuple[str, str], PoolDB]
    async def boot(self, simulation_dir, world_db, places_feeds):
        for (p, f) in places_feeds:
            db = PoolDB.create(simulation_dir, p, f, world_db)
            await db.rebuild_follow_from_relation()
            self.pools[(p, f)] = db
```

### 5.2 IPC / Flask / SQL

- **IPC**: 无
- **Flask**: 无直接路由; `report_agent` 跨多 pool 联合查询 trace
- **SQL 输入**: `agent_world/persistence/schema/pool/*.sql` 13 个 (即 fork 后 OASIS schema 目录, 通过 fork 内 `database.py:create_db` 间接读)
- **SQL 输出**:
  - 主写: `user / follow`
  - 间接 (经 Platform): `post / like / dislike / comment / comment_like / comment_dislike / mute / report / rec / trace / product`
- **跨 DB 读**: `WorldDB.fetch_active_relations` (启动期 + on_change 触发时)

## 6. 配置入口

- `simulation_dir`: 决定每池 DB 路径
- `world_config.places[*].feeds: list[str]` (隐式) — manager 决定为哪些 (place, feed) 组合建池
- `agent_configs[*].capabilities`: 启动期初始 `account_<feed>` capability 决定首批 sign_up 名单
- 默认: 与 fork OASIS 一致 (无 WAL); SQLite `synchronous=NORMAL`

## 7. 待决策 / 风险

- **跨 DB 原子性 (LAYOUT §9.6.C)**: world.db.relation 与 pool.follow 双写非原子, 已接受。崩溃恢复靠启动 rebuild。监控点: 增加 metrics 统计每次启动 rebuild 修正了多少行 (偏离 = 上次 crash 损失估算)。
- **A3 user_id 偏移**: fork 内 `recsys.py:54-58` 残留的 `user_id = agent_id + 1` 假设必须在 fork 内一并修掉 (LAYOUT §4 OASIS recsys.py 行已列改动); 否则启动 rebuild 写 follow 时与 recsys 读 user 表对不上。本模块假设 fork 内已修。
- **on_relation_change 调用频次**: 大规模剧本批量改关系时, 串行写每池可能慢; 后期可批量化 (`executemany`) 但 MVP 不做。
- **新池加入时机**: 当前设计在 `CapabilityTable.on_grant` 钩子触发; 若 capability 撤销 (`account_<feed>` revoke) 是否要 `DELETE FROM user` + 级联清 post? MVP 暂不实现 revoke 路径 —— `account_<feed>` capability 默认单调增。
- **群聊三表迁移残留**: fork 内 `database.py` 删除群聊三段需要严格 grep 检查 (LAYOUT §4 fork 汇总变更); 本模块假设 fork 已干净, 否则会重复创建 world.db 已有的表名。
- **多 pool sweep_undelivered**: 群聊重投发生在 world.db, 不涉及 pool_*.db; 本模块无关。
