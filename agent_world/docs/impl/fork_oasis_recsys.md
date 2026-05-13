# Fork: OASIS recsys.py 重构为 RecSys 类

> 路径: `vendor/oasis/oasis/social_platform/recsys.py` (fork 内 EDIT)
> 对应 LAYOUT §: §4 OASIS recsys.py 行 / §2.D 注释 / §3.3 user 表 / §9.6 E
> 上游依赖文档: 无
> 下游依赖文档: `pools_platform_factory.md`, `pools_manager.md`

## 1. 模块定位

`recsys.py` 在 OASIS 原版中是一个"模块全局变量 + 4 个顶层函数"的 procedural 文件: `model / twhin_tokenizer / twhin_model / user_previous_post_all / user_previous_post / user_profiles / t_items / u_items / date_score` 等张量 / 缓存全部挂在模块级别, `rec_sys_random / rec_sys_reddit / rec_sys_twitter / rec_sys_twhin` 都直接读写这些全局。

这种结构在单池场景能跑, 但 Agent World 是**多池并存**: 每个 OASIS Platform 实例都需要自己的"用户画像 + 帖子缓存 + recsys 状态"。模块全局意味着多池间状态串台, 必须重构为类。

本 fork 把 recsys.py 重构为 `RecSys` 类:
- 把全局变量重构为成员变量 (每池一份)
- 把 4 个 `rec_sys_*` 函数变成 method
- 修 L54-58 注释及代码: 不再依赖 OASIS 旧的 `user_id = agent_id + 1` 注册顺序假设 (与 `user.sql` 去 AUTOINCREMENT 同步)
- 提供给 `PlatformFactory` 实例化, 由 `Platform.__init__` 显式注入 (Platform 的对应签名修改放在 `fork_oasis_platform.md`, 本文不写)

输入: `(feed_type, model 配置, max_rec_post_len, ...)` 实例化参数; 调用期 `(post_table, user_table, trace_table, ...)` 等 Platform 透传的 DB 视图。
输出: `pool_*.db.rec` 表的全表 upsert (由 Platform.update_rec_table 在调用 method 后落库, RecSys 本身不直接写 DB, 只算分)。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 模块全局变量 (~9 个) | OASIS | `vendor/oasis/oasis/social_platform/recsys.py:38-61` | EDIT | 重构为类成员; 行号是 OASIS 原版本, 实际行可能因 fork 略漂移 |
| `rec_sys_random` | OASIS | `recsys.py` 顶层函数之一 | EDIT | 改为 `RecSys.rec_sys_random` method |
| `rec_sys_reddit` | OASIS | `recsys.py` 顶层函数之一 | EDIT | 同上 |
| `rec_sys_twitter` | OASIS | `recsys.py` 顶层函数之一 | EDIT | 同上 |
| `rec_sys_twhin` | OASIS | `recsys.py` 顶层函数之一 | EDIT | 同上; 内部依赖 `twhin_tokenizer / twhin_model / t_items / u_items` 全部移成员化 |
| user_id ↔ agent_id 注册偏移 | OASIS | `recsys.py:54-58` 注释 | EDIT | 注释 + 代码全删, 与 `user.sql` 去 AUTOINCREMENT 配套 (LAYOUT §3.3 / §9.4) |
| TWHIN 嵌入工具 | OASIS | `oasis/social_platform/process_recsys_posts.py:24-71` | KEEP | 不动, 仍按函数调用; RecSys method 内部 import |

## 3. 关键改动 (相对来源仓库)

- **改动 1 (全局变量类成员化)**: L38-61 的以下 9 个全局变量全部重构为 `RecSys` 类的实例属性:
  - `model` (sentence-transformer / openai embedding 切换)
  - `twhin_tokenizer`, `twhin_model` (TWHIN BERT)
  - `user_previous_post_all`, `user_previous_post` (用户历史 post 缓存)
  - `user_profiles` (用户画像 dict)
  - `t_items`, `u_items` (TWHIN item / user 嵌入张量)
  - `date_score` (时间衰减权重缓存)

  注意: TWHIN 大模型权重 (~500MB) 不应每池独立加载; 在类内用 `_class_level_cache: Dict[str, Any]` (类变量) 缓存权重句柄, 实例只持有引用。`user_profiles / t_items / u_items` 等用户态缓存按池独立 (实例属性)。

- **改动 2 (函数 → method)**: 4 个 `rec_sys_*` 函数变 method:
  - `rec_sys_random(self, post_table, trace_table, max_rec_post_len) -> List[Tuple[user_id, post_id, score]]`
  - `rec_sys_reddit(self, post_table, trace_table, max_rec_post_len) -> ...`
  - `rec_sys_twitter(self, post_table, trace_table, follow_table, max_rec_post_len) -> ...`
  - `rec_sys_twhin(self, post_table, user_table, trace_table, max_rec_post_len) -> ...`

  签名保持原参数顺序, 仅前置 `self`。

- **改动 3 (`__init__` 接受 feed_type 选择算法)**: 新增 `__init__(self, feed_type: str, *, max_rec_post_len: int = 100, use_openai_embedding: bool = False, twhin_model_path: str | None = None, ...)`。`feed_type` 决定 `update()` 默认 dispatch 到哪个 `rec_sys_*` method:
  - `'random'` → `rec_sys_random`
  - `'reddit'` → `rec_sys_reddit`
  - `'twitter'` / `'lunar_net'` (默认 twitter 算法) → `rec_sys_twitter`
  - `'twhin'` → `rec_sys_twhin`

- **改动 4 (修 L54-58 注释及代码)**: 删除 "user_id = agent_id + 1 (sign_up 顺序产生的偏移)" 假设。fork 同步改 `user.sql` 让 `user_id = agent_id` 由 schema 保证 (LAYOUT §3.3 / §9.4)。recsys 内部所有 `user_id - 1` / `agent_id + 1` 类偏移代码删除; 直接用 `agent_id == user_id`。
  - 注释段全删, 改成 `# user_id == agent_id (enforced by user.sql PRIMARY KEY without AUTOINCREMENT, see LAYOUT §3.3)`

- **改动 5 (TWHIN 权重共享)**: `twhin_tokenizer` / `twhin_model` 走类级缓存; 第一个 RecSys 实例加载, 后续实例共享 (避免显存爆炸):
  ```
  _twhin_singleton: Tuple[tokenizer, model] | None = None  (类变量)
  __init__ 中: if feed_type == 'twhin' and not _twhin_singleton: 加载并赋值
  ```

- **改动 6 (Platform 接 RecSys)**: Platform.__init__ 签名增加 `recsys: RecSys` 参数, Platform 内部去掉 `recsys_type: str` 分支, 改为 `await self.recsys.update(...)`。**Platform 文件改动详见 `fork_oasis_platform.md`**, 本文档**只写 recsys 文件本身改动**。

## 4. 核心逻辑

### 4.1 数据结构

```
class RecSys:
    # ----- 实例属性 (per-pool) -----
    feed_type: str
    max_rec_post_len: int
    use_openai_embedding: bool

    # 用户画像 / 历史缓存 (按池独立)
    user_profiles: Dict[int, dict]                 # user_id (= agent_id) → profile
    user_previous_post: Dict[int, List[int]]       # user_id → 最近 N 条 post_id
    user_previous_post_all: Dict[int, List[int]]   # user_id → 全部 post_id

    # 嵌入缓存 (按池独立)
    t_items: torch.Tensor | None                   # post 嵌入
    u_items: torch.Tensor | None                   # user 嵌入
    date_score: Dict[int, float]                   # post_id → 时间衰减分

    # ----- 类级缓存 (跨池共享) -----
    _twhin_singleton: ClassVar[Optional[Tuple[Any, Any]]] = None     # (tokenizer, model)
    _embedding_model_singleton: ClassVar[Optional[Any]] = None        # sentence-transformer
```

不变量:
- `user_id == agent_id` 永远成立 (由 fork 后 user.sql + sign_up 逻辑共同保证)
- `update()` method 是无副作用的 (只算分, 不写 DB); Platform 拿返回值后写 `pool_*.db.rec`
- 类级 singleton 一旦加载不卸载 (整个仿真进程生命周期内常驻)

### 4.2 关键流程 / 算法

**初始化**:
```
__init__(feed_type, max_rec_post_len=100, use_openai_embedding=False, twhin_model_path=None):
    self.feed_type = feed_type
    self.max_rec_post_len = max_rec_post_len
    self.user_profiles = {}
    self.user_previous_post = {}
    self.user_previous_post_all = {}
    self.t_items = None
    self.u_items = None
    self.date_score = {}

    if feed_type in ('twitter', 'twhin'):
        # 懒加载 sentence-transformer; 类级 singleton
        if RecSys._embedding_model_singleton is None and use_openai_embedding is False:
            RecSys._embedding_model_singleton = load_sentence_transformer()

    if feed_type == 'twhin':
        if RecSys._twhin_singleton is None:
            RecSys._twhin_singleton = load_twhin(twhin_model_path)
```

**update (Platform 每轮调用入口)**:
```
async def update(self, post_table, user_table, trace_table, follow_table) -> List[Tuple[int,int,float]]:
    if self.feed_type == 'random':
        return await self.rec_sys_random(post_table, trace_table, self.max_rec_post_len)
    elif self.feed_type == 'reddit':
        return await self.rec_sys_reddit(post_table, trace_table, self.max_rec_post_len)
    elif self.feed_type in ('twitter', 'lunar_net'):
        return await self.rec_sys_twitter(post_table, trace_table, follow_table, self.max_rec_post_len)
    elif self.feed_type == 'twhin':
        return await self.rec_sys_twhin(post_table, user_table, trace_table, self.max_rec_post_len)
    else:
        raise ValueError(f"unknown feed_type: {self.feed_type}")
```

**rec_sys_***: 4 个 method 体直接搬 OASIS 顶层函数, 把所有模块全局引用改为 `self.xxx`。算法本身不动 (random / reddit-sort / twitter-follow-graph / twhin-embedding-cosine)。

### 4.3 与其他模块的交互

- **上游调用方**:
  - `vendor/oasis/oasis/social_platform/platform.py` 内 `update_rec_table()` (fork 后改为 `await self.recsys.update(...)`)
  - `agent_world/pools/platform_factory.py` 实例化阶段 (`RecSys(feed_type=...)`)
- **下游被调方**:
  - `vendor/oasis/oasis/social_platform/process_recsys_posts.py` (TWHIN 嵌入工具, KEEP 不动)
  - 第三方: `sentence-transformers`, `transformers` (TWHIN BERT)
- **共享状态**:
  - 读: 由 Platform 透传的 post / user / follow / trace 表视图 (RecSys 不直接连 DB)
  - 写: 无 (返回值由 Platform 写 `pool_*.db.rec`)
  - Zep: 无关
  - 类级缓存: TWHIN 模型 + sentence-transformer 在跨池实例间共享

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
from typing import Any, ClassVar, Dict, List, Optional, Tuple

class RecSys:
    _twhin_singleton: ClassVar[Optional[Tuple[Any, Any]]] = None
    _embedding_model_singleton: ClassVar[Optional[Any]] = None

    def __init__(
        self,
        feed_type: str,
        *,
        max_rec_post_len: int = 100,
        use_openai_embedding: bool = False,
        twhin_model_path: Optional[str] = None,
    ) -> None: ...

    async def update(
        self,
        post_table: Any,
        user_table: Any,
        trace_table: Any,
        follow_table: Any,
    ) -> List[Tuple[int, int, float]]: ...

    async def rec_sys_random(
        self, post_table: Any, trace_table: Any, max_rec_post_len: int
    ) -> List[Tuple[int, int, float]]: ...

    async def rec_sys_reddit(
        self, post_table: Any, trace_table: Any, max_rec_post_len: int
    ) -> List[Tuple[int, int, float]]: ...

    async def rec_sys_twitter(
        self,
        post_table: Any,
        trace_table: Any,
        follow_table: Any,
        max_rec_post_len: int,
    ) -> List[Tuple[int, int, float]]: ...

    async def rec_sys_twhin(
        self,
        post_table: Any,
        user_table: Any,
        trace_table: Any,
        max_rec_post_len: int,
    ) -> List[Tuple[int, int, float]]: ...
```

### 5.2 IPC / Flask / SQL (如适用)

- **IPC**: 无。
- **Flask**: 无。
- **SQL**: RecSys 不直接 SQL; Platform 把 `post / user / follow / trace` 视图作为参数传入。但内部依赖的语义假设:
  - `user.user_id == user.agent_id` (fork 后 user.sql 保证, LAYOUT §3.3)
  - `rec.post_id` FK 指向 `post.post_id` (fork 后 rec.sql 修复, LAYOUT §3.3)
  - 不依赖 `chat_group / group_member / group_message` (已删, LAYOUT §3.3)

## 6. 配置入口

从 `simulation_config.json` → `pool_factory.recsys_defaults[feed_type]` 读取每个 feed 的 RecSys 初始化参数:

```yaml
pool_factory:
  recsys_defaults:
    twitter:
      max_rec_post_len: 100
      use_openai_embedding: false
    reddit:
      max_rec_post_len: 50
    twhin:
      max_rec_post_len: 100
      twhin_model_path: "Twitter/twhin-bert-base"
    lunar_net:
      max_rec_post_len: 30
```

默认值: `max_rec_post_len=100` / `use_openai_embedding=False` / `twhin_model_path` 缺省时走 huggingface hub 默认。
验证规则: `feed_type` 必须在 4 个内置算法中 (`random / reddit / twitter / twhin`); `max_rec_post_len > 0`。

## 7. 待决策 / 风险

- **#9.6 E (已决)**: 模块全局 → 类成员化, 本文档完成。
- **TWHIN 显存压力**: 类级 singleton 缓存仅在所有池都 `feed_type='twhin'` 时才共享; 异构池场景 (一池 twitter / 一池 twhin) 仍只加载一次, 但 OOM 风险随 `lunar_net + twhin` 同时启用上升。MVP 接受。
- **类级 singleton 测试隔离**: 单元测试需要在 `setUp` 中清空 `_twhin_singleton` / `_embedding_model_singleton`; 否则跨测试污染。提供 `RecSys._reset_class_cache()` 测试 hook。
- **OASIS 上游 sync**: MVP 不追 OASIS 上游; 若上游 recsys.py 算法升级, fork 需手动 merge。冻结风险已知。
- **N5 (LAYOUT §9.5.1)**: `arrive_at` 字段不影响 RecSys (只在 world.db.direct_message)。
- **`feed_type='lunar_net'` 默认走 twitter 算法**: 临时映射, 待 LunarNet 场景细化时单独写一个 `rec_sys_lunar_net` method。
- **OASIS recsys.py 实际行号漂移**: LAYOUT 标的 L38-61 / L54-58 是参考行号; fork 实际改时以 grep 模块级 `model = ` / `twhin_` / `user_previous_post` 等关键字定位为准。
