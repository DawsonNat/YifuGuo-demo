# persistence/schema/pool/*.sql 实现文档

> 路径: `agent_world/persistence/schema/pool/*.sql` (13 个 DDL 文件)
> 对应 LAYOUT §: §3.3 pool_*.db 13 张表 / §3.4 trace 归属 / §3.5 follow 双轨 / §4 OASIS Copy/Adapt/Skip / §9.4
> 上游依赖文档: 无
> 下游依赖文档: `persistence_pool_db.md`

## 1. 模块定位

本目录是 **每个 `pool_*.db` 推荐池 SQLite 的 DDL**, 13 张表全部从 OASIS `vendor/oasis/oasis/social_platform/schema/` **直接复用** (fork 内已删群聊三张)。每池一个独立物理 DB 文件, 但 schema 共享同一份 DDL。

注意: **本目录可以是符号链接** —— LAYOUT §1 目录树展示的是逻辑位置, 物理上 `agent_world/persistence/schema/pool/` 可以直接 import / symlink 到 `vendor/oasis/oasis/social_platform/schema/`。MVP 推荐**不做物理拷贝**, 直接由 `pool_db.py` 通过 fork 后 OASIS 的 `database.create_db()` 间接使用 (该函数自带 schema 文件路径解析逻辑, 见 `vendor/oasis/oasis/social_platform/database.py:77-81`)。

输入: 无 (DDL 是声明)。输出: 13 个 `CREATE TABLE` (fork 内 schema 文件原样)。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| user.sql | OASIS (fork) | `oasis/social_platform/schema/user.sql:1-11` | EDIT (fork 内改) | PRIMARY KEY 去 AUTOINCREMENT; agent_id UNIQUE NOT NULL (A3) |
| post.sql | OASIS | `oasis/social_platform/schema/post.sql:1-16` | KEEP | 原样 |
| follow.sql | OASIS | `oasis/social_platform/schema/follow.sql:1-9` | KEEP | 原样 (双轨投影写入逻辑见 `persistence_pool_db.md`) |
| like.sql | OASIS | `oasis/social_platform/schema/like.sql:1-9` | KEEP | 原样 (FK 表名 `tweet` 在 fork 内已修为 `post`) |
| dislike.sql | OASIS | `oasis/social_platform/schema/dislike.sql:1-9` | KEEP | 同上 |
| comment.sql | OASIS | `oasis/social_platform/schema/comment.sql:1-12` | KEEP | 原样 |
| comment_like.sql | OASIS | `oasis/social_platform/schema/comment_like.sql:1-9` | KEEP | 原样 |
| comment_dislike.sql | OASIS | `oasis/social_platform/schema/comment_dislike.sql:1-9` | KEEP | 原样 |
| mute.sql | OASIS | `oasis/social_platform/schema/mute.sql:1-9` | KEEP | 原样 |
| report.sql | OASIS | `oasis/social_platform/schema/report.sql:1-10` | KEEP | 原样 (可选, 非默认场景不用) |
| trace.sql | OASIS | `oasis/social_platform/schema/trace.sql:1-9` | KEEP | 原样; 仅承载 FEED 类 action 审计 (A5) |
| rec.sql | OASIS (fork) | `oasis/social_platform/schema/rec.sql:1-8` | EDIT (fork 内改) | FK `tweet` → `post` |
| product.sql | OASIS | `oasis/social_platform/schema/product.sql:1-5` | KEEP | 原样, 可选 |
| chat_group.sql | OASIS | (原 schema) | **SKIP / DELETE** | 已搬到 `agent_world/persistence/schema/world/chat_group.sql` (A1) |
| group_member.sql | OASIS | (原 schema) | **SKIP / DELETE** | 同上 |
| group_message.sql | OASIS | (原 schema) | **SKIP / DELETE** | 同上 |

## 3. 关键改动 (相对 OASIS)

- **改动 1 (A3, fork 内)**: `user.sql` —— `user_id INTEGER PRIMARY KEY AUTOINCREMENT` 改为 `user_id INTEGER PRIMARY KEY`; `agent_id INTEGER` 改为 `agent_id INTEGER UNIQUE NOT NULL`。理由: 让 `user_id = agent_id` 由 schema 约束保证, 消除 sign_up 需显式传 user_id 与 AUTOINCREMENT 的暧昧 (LAYOUT §3.3 + §9.4)。
- **改动 2 (fork 内)**: `rec.sql` —— FK 引用从 `tweet(post_id)` 改为 `post(post_id)` (OASIS 原 typo)。原文件第 7 行 `FOREIGN KEY(post_id) REFERENCES tweet(post_id)` → `FOREIGN KEY(post_id) REFERENCES post(post_id)`。`like.sql` / `dislike.sql` 内同样的 typo (第 8 行 `REFERENCES tweet(post_id)`) 也在 fork 内修为 `post(post_id)`。
- **改动 3 (A1, fork 内)**: 删除三个 schema 文件 + 配套 `database.py:21-40` 常量 + `database.py:177-193` 三段 `executescript`。`chat_group / group_member / group_message` 完全离开 pool DB, 搬到 `agent_world/persistence/schema/world/`。
- **改动 4**: 不新增任何字段 —— pool DB 维持 OASIS 的"推荐池视角" (user / post / follow / like / dislike / comment / mute / report / rec / trace), 不引入 world.db 概念 (place/coverage/relation/capability)。

## 4. 13 个 DDL 完整定义 (KEEP / EDIT 标注)

### 4.1 `user.sql` (EDIT, A3)

```sql
CREATE TABLE user (
    user_id          INTEGER PRIMARY KEY,         -- 改: 去 AUTOINCREMENT
    agent_id         INTEGER UNIQUE NOT NULL,     -- 改: 加 UNIQUE NOT NULL
    user_name        TEXT,
    name             TEXT,
    bio              TEXT,
    created_at       DATETIME,
    num_followings   INTEGER DEFAULT 0,
    num_followers    INTEGER DEFAULT 0
);
```
约束: `user_id = agent_id` 由 sign_up 写入侧保证 (`PoolDB.sign_up(agent_id=N)` → `INSERT INTO user (user_id, agent_id, ...) VALUES (N, N, ...)`)。

### 4.2 `post.sql` (KEEP)

```sql
CREATE TABLE post (
    post_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER,
    original_post_id INTEGER,                     -- NULL = 原创
    content          TEXT DEFAULT '',
    quote_content    TEXT,                        -- NULL = 原创或 repost
    created_at       DATETIME,
    num_likes        INTEGER DEFAULT 0,
    num_dislikes     INTEGER DEFAULT 0,
    num_shares       INTEGER DEFAULT 0,           -- = repost + quote
    num_reports      INTEGER DEFAULT 0,
    FOREIGN KEY(user_id) REFERENCES user(user_id),
    FOREIGN KEY(original_post_id) REFERENCES post(post_id)
);
```

### 4.3 `follow.sql` (KEEP)

```sql
CREATE TABLE follow (
    follow_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    follower_id   INTEGER,
    followee_id   INTEGER,
    created_at    DATETIME,
    FOREIGN KEY(follower_id) REFERENCES user(user_id),
    FOREIGN KEY(followee_id) REFERENCES user(user_id)
);
```
**写入路径** (LAYOUT §3.5): 启动期 `PoolDB.rebuild_follow_from_relation(world_db)` drop & insert; 运行时 `RelationGraph.on_change(... mutual_follow|follower ...)` 钩子同步写。OASIS recsys (twhin / twitter) 读本表为推荐输入。

### 4.4 `like.sql` (KEEP, fork 内 FK 已修)

```sql
CREATE TABLE like (
    like_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    post_id     INTEGER,
    created_at  DATETIME,
    FOREIGN KEY(user_id) REFERENCES user(user_id),
    FOREIGN KEY(post_id) REFERENCES post(post_id)   -- fork 内: tweet → post
);
```

### 4.5 `dislike.sql` (KEEP, fork 内 FK 已修)

```sql
CREATE TABLE dislike (
    dislike_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    post_id     INTEGER,
    created_at  DATETIME,
    FOREIGN KEY(user_id) REFERENCES user(user_id),
    FOREIGN KEY(post_id) REFERENCES post(post_id)   -- fork 内: tweet → post
);
```

### 4.6 `comment.sql` (KEEP)

```sql
CREATE TABLE comment (
    comment_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id       INTEGER,
    user_id       INTEGER,
    content       TEXT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    num_likes     INTEGER DEFAULT 0,
    num_dislikes  INTEGER DEFAULT 0,
    FOREIGN KEY(post_id) REFERENCES post(post_id),
    FOREIGN KEY(user_id) REFERENCES user(user_id)
);
```

### 4.7 `comment_like.sql` (KEEP)

```sql
CREATE TABLE comment_like (
    comment_like_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER,
    comment_id       INTEGER,
    created_at       DATETIME,
    FOREIGN KEY(user_id) REFERENCES user(user_id),
    FOREIGN KEY(comment_id) REFERENCES comment(comment_id)
);
```

### 4.8 `comment_dislike.sql` (KEEP)

```sql
CREATE TABLE comment_dislike (
    comment_dislike_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER,
    comment_id          INTEGER,
    created_at          DATETIME,
    FOREIGN KEY(user_id) REFERENCES user(user_id),
    FOREIGN KEY(comment_id) REFERENCES comment(comment_id)
);
```

### 4.9 `mute.sql` (KEEP)

```sql
CREATE TABLE mute (
    mute_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    muter_id   INTEGER,
    mutee_id   INTEGER,
    created_at DATETIME,
    FOREIGN KEY(muter_id) REFERENCES user(user_id),
    FOREIGN KEY(mutee_id) REFERENCES user(user_id)
);
```

### 4.10 `report.sql` (KEEP, 可选)

```sql
CREATE TABLE report (
    report_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER,
    post_id        INTEGER,
    report_reason  TEXT,
    created_at     DATETIME,
    FOREIGN KEY(user_id) REFERENCES user(user_id),
    FOREIGN KEY(post_id) REFERENCES post(post_id)
);
```
非默认场景不用; LAYOUT §3.3 标注 `report_post` action 可选。

### 4.11 `trace.sql` (KEEP)

```sql
CREATE TABLE trace (
    user_id     INTEGER,
    created_at  DATETIME,
    action      TEXT,
    info        TEXT,
    PRIMARY KEY(user_id, created_at, action, info),
    FOREIGN KEY(user_id) REFERENCES user(user_id)
);
```
**仅** FEED 类 action 写入 (A5 决议): CREATE_POST / REPOST / QUOTE_POST / LIKE_POST / DISLIKE_POST / CREATE_COMMENT / LIKE_COMMENT / DISLIKE_COMMENT / FOLLOW / UNFOLLOW / MUTE / UNMUTE。**不写**直接通信类 (SPEAK_TO_LOCAL / SEND_MESSAGE / SEND_TO_GROUP) —— 这些只进 `world.db.direct_message`。`report_agent` 跨 DB 联合查询需 (LAYOUT §3.4)。

### 4.12 `rec.sql` (EDIT, fork 内 FK 已修)

```sql
CREATE TABLE rec (
    user_id  INTEGER,
    post_id  INTEGER,
    PRIMARY KEY(user_id, post_id),
    FOREIGN KEY(user_id) REFERENCES user(user_id),
    FOREIGN KEY(post_id) REFERENCES post(post_id)   -- fork 内: tweet → post
);
```
**写入**: 每轮 `Pools.update_all_rec_tables()` 全表 `DELETE FROM rec` 再批量 INSERT。**读取**: OASIS `Platform.refresh()` 对每 agent 查 rec 取候选 post。

### 4.13 `product.sql` (KEEP, 可选)

```sql
CREATE TABLE product (
    product_id    INTEGER PRIMARY KEY,
    product_name  TEXT,
    sales         INTEGER DEFAULT 0
);
```
非默认场景不用; LAYOUT §3.3 标注可省。

### 4.14 SKIP 列表 (已搬到 world)

| 文件 | 原路径 | 新路径 |
|---|---|---|
| chat_group.sql | `oasis/social_platform/schema/chat_group.sql` (fork 内已删) | `agent_world/persistence/schema/world/chat_group.sql` |
| group_member.sql | `oasis/social_platform/schema/group_member.sql` (fork 内已删) | `agent_world/persistence/schema/world/group_member.sql` |
| group_message.sql | `oasis/social_platform/schema/group_message.sql` (fork 内已删) | `agent_world/persistence/schema/world/group_message.sql` |

fork 内 `database.py` 配套删除:
- `database.py:38-40`: 三个 SCHEMA_SQL 路径常量
- `database.py:42-59` `TABLE_NAMES`: 删 `group / group_member / group_message`
- `database.py:177-193`: 三段 `executescript` 调用

## 5. 暴露 API

### 5.1 公开 class / function 签名

无 (DDL 文件不暴露 API)。`pool_db.py` 通过 fork 后 OASIS `create_db(pool_path)` 间接读这些 .sql 文件。

### 5.2 IPC / Flask / SQL

- **SQL 输出**: 13 张表 (无新增索引; OASIS 原 schema 也无 index 声明)
- 加载顺序由 fork 后 OASIS `database.py:84-201` 决定: `user → post → follow → mute → like → dislike → report → trace → rec → comment → comment_like → comment_dislike → product`

## 6. 配置入口

无 (DDL 是静态)。每池物理文件路径由 `pools/manager.py` 根据 `world_config.places[*].feeds` 推导。

## 7. 待决策 / 风险

- **fork 内 grep 验证**: 群聊三个 SCHEMA_SQL 常量、TABLE_NAMES 三个值、三段 `executescript` 必须严格删除; 否则 `create_db` 会因找不到文件抛错。LAYOUT §4 fork 汇总变更已列 checklist。
- **`like.sql` / `dislike.sql` 的 FK 名 typo**: 原 OASIS 第 8 行 `REFERENCES tweet(post_id)`, fork 内一并修为 `post(post_id)`; 与 `rec.sql` 改动同时进行 (LAYOUT §3.3 末"已修正"列表第 2 条 + §4 OASIS schema 行第 3 项)。
- **trace 主键唯一性**: PRIMARY KEY 是 (user_id, created_at, action, info) 四元组 —— OASIS 原设计若同一 agent 同一秒做两次同样的 action 内容相同会 PK 冲突; MVP 不改 (沿用 OASIS 行为)。后续如发现冲突频繁, 加一个自增 id 或换毫秒精度。
- **product.sql / report.sql 是否启用**: MVP 推荐都建表 (开销极小); 是否真有数据由场景决定。
- **多 pool 复用同一份 schema**: 物理上每池一个 .db 文件, 但 13 个 .sql 是共享读取的; pool_db.py 需保证不串改 schema 文件 (静态文件天然安全)。
- **A2 启动期 rebuild 性能**: pool 数 × 平均 follow 边数; 100w agent + 多池场景下 rebuild 时间需压测 (LAYOUT §9.5.1.N3 同) —— schema 层无能为力, 见 `persistence_pool_db.md` §7。
