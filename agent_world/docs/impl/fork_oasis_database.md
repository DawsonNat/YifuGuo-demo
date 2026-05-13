# OASIS database.py fork 实现文档

> 路径: `vendor/oasis/oasis/social_platform/database.py`
> 对应 LAYOUT §: §4 OASIS 总表（database.py 行）/ §3.3 pool_*.db 13 张表 / A1 群聊三表迁移
> 上游依赖文档: `fork_oasis_schema.md`
> 下游依赖文档: `fork_oasis_platform.md`（被 Platform.__init__ 调用）

## 1. 模块定位
OASIS `database.py` 是 pool 级 SQLite 的"建库 / schema 装载"工具：维护一张 schema 文件路径常量列表（每个 .sql 一项）+ `TABLE_NAMES` 常量 + `create_db(path)` 函数（用 `executescript` 把所有 .sql 跑一遍）。Agent World 在 fork 内**减表**：删除群聊三张表的 schema 路径常量，让任何调用 `create_db()` 创建的 pool DB 都不再带 chat_group / group_member / group_message（A1 配套）。

输入：调用者传入 db 路径。
输出：建立 / 打开一个 13 张表（fork 后剩余）的 SQLite；返回 `sqlite3.Connection`。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `create_db(path)` 函数 | OASIS | `oasis/social_platform/database.py:84-201` | KEEP | 函数体直接保留；只是输入的 schema 列表减少 |
| schema 文件路径常量列表 | OASIS | `oasis/social_platform/database.py:21-40` | EDIT | 删除 GROUP_SCHEMA_SQL / GROUP_MEMBER_SCHEMA_SQL / GROUP_MESSAGE_SCHEMA_SQL 三个常量 |
| `TABLE_NAMES` 常量 | OASIS | `oasis/social_platform/database.py`（紧邻 schema 列表） | EDIT | 同步删除 'chat_group', 'group_member', 'group_message' 三项 |
| `executescript` 装载循环 | OASIS | `database.py:create_db` 内部 | KEEP | 循环体不动；只是迭代列表变短 |

## 3. 关键改动 (相对来源仓库)

- **改动 1（schema 路径列表）**：删除三个模块级常量（路径指向已被 `fork_oasis_schema.md` 删除的三个 .sql 文件）。删除后该列表保留 13 项：user / post / follow / like / dislike / comment / comment_like / comment_dislike / mute / report / rec / trace / product。
- **改动 2（TABLE_NAMES 同步）**：`TABLE_NAMES` 列表也减 3 项（与 schema 列表保持长度一致）；OASIS 内部任何依赖 TABLE_NAMES 的代码（如 `for tbl in TABLE_NAMES: ...`）会自动跳过群聊表，无需额外改动。
- **改动 3（create_db 函数体不动）**：`create_db(path: str) -> sqlite3.Connection` 主体保留——它读 schema 列表 + 用 `executescript` 装载。删表纯粹通过减少输入实现。
- **改动 4（与 PlatformFactory 协作）**：`PlatformFactory.build(pool_path, …)` 每池调一次 `create_db(pool_path)`；fork 后建出来的 pool DB 严格 13 张表。
- **改动 5（用户表语义连锁）**：因为 `fork_oasis_schema.md` 改了 `user.sql`（去 AUTOINCREMENT、加 `agent_id UNIQUE NOT NULL`），`create_db` 装载时会沿用新 schema；`recsys.py:54-58` "+1 偏移" 假设需在 `fork_oasis_recsys.md`（外部）同步修正——本文件不展开。

## 4. 核心逻辑

### 4.1 数据结构

模块级常量：
- `USER_SCHEMA_SQL: str`（路径）
- `POST_SCHEMA_SQL: str`
- `FOLLOW_SCHEMA_SQL: str`
- `LIKE_SCHEMA_SQL: str`
- `DISLIKE_SCHEMA_SQL: str`
- `COMMENT_SCHEMA_SQL: str`
- `COMMENT_LIKE_SCHEMA_SQL: str`
- `COMMENT_DISLIKE_SCHEMA_SQL: str`
- `MUTE_SCHEMA_SQL: str`
- `REPORT_SCHEMA_SQL: str`
- `REC_SCHEMA_SQL: str`
- `TRACE_SCHEMA_SQL: str`
- `PRODUCT_SCHEMA_SQL: str`
- ~~`GROUP_SCHEMA_SQL`~~（DELETE）
- ~~`GROUP_MEMBER_SCHEMA_SQL`~~（DELETE）
- ~~`GROUP_MESSAGE_SCHEMA_SQL`~~（DELETE）

`TABLE_NAMES: List[str]`：13 项（去掉 'chat_group' / 'group_member' / 'group_message'）。

不变量：
- `len(SCHEMA_PATH_LIST) == len(TABLE_NAMES) == 13`
- 每个 .sql 文件路径在 `vendor/oasis/oasis/social_platform/schema/` 下真实存在（删除后由 `fork_oasis_schema.md` 维护）。

### 4.2 关键流程 / 算法

```
create_db(path):
  conn = sqlite3.connect(path)
  for sql_path in [USER_SCHEMA_SQL, POST_SCHEMA_SQL, ..., PRODUCT_SCHEMA_SQL]:   # 13 项
    with open(sql_path) as f:
      conn.executescript(f.read())
  conn.commit()
  return conn
```

被调点：
- `PlatformFactory.build()` per-pool 一次。
- 启动恢复期 `RelationGraph.on_change` 不调 `create_db`（pool DB 已存在）；只是写 follow 投影。

### 4.3 与其他模块的交互

- 上游调用方：`PlatformFactory.build(pool_path)`；可能还有 OASIS 测试代码（fork 后跑通）。
- 下游被调方：`sqlite3.connect`；磁盘 I/O 读 13 个 .sql 文件。
- 共享状态：写 pool_*.db 文件（每池一份）；不读 / 写 world.db。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
# database.py 顶层
USER_SCHEMA_SQL: str
POST_SCHEMA_SQL: str
FOLLOW_SCHEMA_SQL: str
LIKE_SCHEMA_SQL: str
DISLIKE_SCHEMA_SQL: str
COMMENT_SCHEMA_SQL: str
COMMENT_LIKE_SCHEMA_SQL: str
COMMENT_DISLIKE_SCHEMA_SQL: str
MUTE_SCHEMA_SQL: str
REPORT_SCHEMA_SQL: str
REC_SCHEMA_SQL: str
TRACE_SCHEMA_SQL: str
PRODUCT_SCHEMA_SQL: str
TABLE_NAMES: list[str]   # 13 项

def create_db(path: str) -> sqlite3.Connection: ...
```

### 5.2 IPC / Flask / SQL

- SQL 输入：13 个 `.sql` 文件（全部位于 `vendor/oasis/oasis/social_platform/schema/`，由 `fork_oasis_schema.md` 维护）。
- SQL 输出：建立 13 张表的 pool_*.db。

## 6. 配置入口

无配置依赖。schema 路径常量在编译期就确定。

## 7. 待决策 / 风险

- 隐含风险：若有人在 OASIS 测试代码里 hardcode `len(TABLE_NAMES) == 16` 之类断言，会因为减 3 而失败；fork 期需 grep 全仓库一次性修复。
- 隐含风险：`agent_world/persistence/pool_db.py` 是 `create_db` 的薄包装；只要本文件 fork 改动正确，pool_db.py 无需额外感知群聊表迁移。
- 9.6 G 单写者锁与本模块无关：`create_db` 仅启动期建表，不参与运行时并发写。
