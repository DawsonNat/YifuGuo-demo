# OASIS Platform fork 实现文档

> 路径: `vendor/oasis/oasis/social_platform/platform.py`
> 对应 LAYOUT §: §4 OASIS Copy/Adapt/Skip 总表 / §2.D PlatformFactory / §1 顶层目录树（A1 群聊删除）
> 上游依赖文档: `fork_oasis_typing.md`, `fork_oasis_database.md`, `fork_oasis_recsys.md`（外部）, `fork_oasis_schema.md`
> 下游依赖文档: `fork_oasis_agent.md`, `fork_oasis_agent_action.md`, `fork_oasis_agents_generator.md`

## 1. 模块定位
OASIS `Platform` 是单池仿真的中枢：持有一个 SQLite 连接、一个 `Channel`、一个推荐子系统，分发 ActionType → handler method（`platform.py:148` 的反射 dispatch）。Agent World 把它**降级**为"单池数据写入器 + recsys 持有者"——多池由上层 `MultiPoolPlatformManager` 编排（§2.D）；群聊路由全部上提到 `GroupMessageBus`（A1）；recsys 模块全局变量重构为 `RecSys` 类后由外部注入。

输入：每池一个 `Channel`、一个 `RecSys` 实例、一个 db 路径；FEED 类 ActionType 通过 `Channel.read_from_send_queue` 进来。
输出：写 `pool_*.db.{post,like,follow,comment,trace,...}`（13 张表，不含群聊三张）；通过 `Channel.write_to_receive_queue` 把响应回发给 agent。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `Platform.__init__` 装配 | OASIS | `oasis/social_platform/platform.py:56-126` | EDIT | 加 `recsys: RecSys` 参数；删 `recsys_type` 内部分支 |
| dispatch 反射循环 | OASIS | `oasis/social_platform/platform.py:148` 附近 | KEEP | 反射调度结构保留 |
| `_record_trace` | OASIS | `oasis/social_platform/platform_utils.py:188-217` | KEEP | 仍由 Platform 内部调用 |
| `send_to_group` 等五个群聊 method | OASIS | `oasis/social_platform/platform.py:1448-1495` 邻近段 | DELETE | 5 个 method 整段删除（A1） |
| FEED 类 method（CREATE_POST 等 12 个） | OASIS | `oasis/social_platform/platform.py` 全文 | KEEP | 12 个 FEED 类 handler 不动 |

## 3. 关键改动 (相对来源仓库)

- **改动 1（构造签名）**：`__init__` 增 `recsys: RecSys` 形参；去掉原 `recsys_type` 字符串分支与模块级全局变量初始化（B1 配套，详见 `fork_oasis_recsys.md`）。Platform 不再"自带" recsys，由 `PlatformFactory` 每池构造一个 RecSys 实例传入。
- **改动 2（删群聊 method）**：删除 5 个 method `send_to_group / create_group / join_group / leave_group / listen_from_group`（A1）。删除后 ActionDispatcher 路由这 4 个 ActionType 时**绕过 Platform**，走 `GroupMessageBus`（详见 §2.C）。`LISTEN_FROM_GROUP` 在 `typing.py` 整个删枚举，故 method 也无需保留 stub。
- **改动 3（多实例化）**：原 OASIS 通过 `make.py` 单例化 Platform；fork 后 `make.py` 整个 DELETE（§4 表），多池构造由 `PlatformFactory.build(pool_path, channel, recsys, …)` 完成。Platform 自身不感知"池"概念，行为不变。
- **改动 4（schema 联动）**：因为 group 三张表已从 OASIS schema 删除（`fork_oasis_schema.md`），Platform 任何 `executescript` / `_record_trace` 调用都不会再触碰 group 表。需在 `__init__` 内部确认 schema 装载列表对齐 `database.py` 修改后的 13 张表清单。
- **改动 5（_record_trace 不变）**：Platform 内 FEED handler 调用 `_record_trace` 仍写 `pool_*.db.trace`；不写 world.db（A5：直接通信类不写 pool trace）。

## 4. 核心逻辑

### 4.1 数据结构

`Platform` 实例字段（fork 后）：
- `channel: Channel`（每池一份）
- `recsys: RecSys`（B1 重构后类实例；持原 `model / twhin_model / u_items / ...` 作为成员）
- `db_path: str`、`db: sqlite3.Connection`（每池一份）
- `pool_id: str`（新加，仅供 trace 与日志辨识，不参与业务逻辑）
- 其余原有 in-memory cache（如 `user_id_to_agent_id`）保留

不变量：
- 同一进程内，每个 `Platform` 实例的 `db_path` 与 `channel` 一一绑定，不与其他池共享。
- `recsys` 持有的所有可变状态都是该池私有；与其他池的 RecSys 实例完全隔离（B1 决议）。

### 4.2 关键流程 / 算法

主循环（保持 OASIS 原结构）：

```
loop:
  message = await self.channel.read_from_send_queue()
  action_type, args, agent_id = message
  if action_type in {CREATE_GROUP, JOIN_GROUP, LEAVE_GROUP, SEND_TO_GROUP}:
      # 不应到达这里——ActionDispatcher 已直接路由到 GroupMessageBus
      log.warning("group action reached Platform; should be Bus-routed"); continue
  handler = getattr(self, action_type.value)   # 反射
  result = await handler(**args, agent_id=agent_id)
  await self.channel.write_to_receive_queue((message_id, agent_id, result))
```

`update_rec_table()`：保持 OASIS 原行为，调 `self.recsys.rec_sys_*`（B1 类化后从模块函数变成类 method）。

### 4.3 与其他模块的交互

- 上游调用方：`MultiPoolPlatformManager` 在 `update_all_rec_tables()` 中并发触发；`SocialAgent.perform_action_by_llm` 通过 `platform_manager.dispatch(action_type, …)` 间接驱动（FEED 类 → Channel → 本 Platform 的 main loop）。
- 下游被调方：`Channel.{read_from_send_queue, write_to_receive_queue}`、`self.recsys.rec_sys_*`、`platform_utils._record_trace`、`pool_*.db` 直接 SQL。
- 共享状态：写 `pool_*.db` 全部 13 张表（`user/post/follow/like/dislike/comment/comment_like/comment_dislike/mute/report/rec/trace/product`）；不写 world.db。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class Platform:
    def __init__(
        self,
        db_path: str,
        channel: Channel,
        recsys: RecSys,                    # 新增（B1）
        pool_id: str,                      # 新增（标识池）
        sandbox_clock: Clock,              # OASIS 原参（KEEP）
        start_time: int = 0,
        recsys_topk: int = 5,
        ...                                # 其余 OASIS 原参原样
    ) -> None: ...

    async def running(self) -> None: ...   # 主消息循环，KEEP
    async def update_rec_table(self) -> None: ...  # 调 self.recsys.rec_sys_*

    # 12 个 FEED 类 handler KEEP：create_post / repost / quote_post /
    # like_post / dislike_post / create_comment / like_comment / dislike_comment /
    # follow / unfollow / mute / unmute …
    # 5 个群聊 handler DELETE：send_to_group / create_group / join_group / leave_group / listen_from_group
```

### 5.2 IPC / Flask / SQL

- IPC：无（Platform 不直接暴露 IPC；通过 `MultiPoolPlatformManager` 间接）
- SQL 输入/输出：每池独立 `pool_*.db`（13 张表，`fork_oasis_schema.md` 明确清单）

## 6. 配置入口

`simulation_config.json` 中相关字段（继承 MiroFish 二层配置）：
- `twitter_config.recsys_topk` / `reddit_config.recsys_topk`：传入 `Platform.__init__`
- `twitter_config.start_time` / `reddit_config.start_time`：传入 `Platform.__init__`
- `world_config.places[*]` 间接决定 `feeds_at(place)` → 哪几个 pool 需要构造 Platform

PlatformFactory 读这些字段后构造每池实例（详见 §2.D PlatformFactory 的设计；本文件不重复）。

## 7. 待决策 / 风险

- 9.5 #8 100w agent scale：单 Platform 主循环串行处理消息，可能成为该池热点；需配合 batch dispatch（D 类讨论）。
- 9.6 G：world.db 单写者锁与 pool_*.db 多 Platform 并发写之间无原子性（已接受，§3.5）。
- N5：`arrive_at` 字段仅 world.db 用，pool_*.db.trace 不受影响——Platform 写 trace 时不需要管 arrive_at 列。
- 隐含风险：删除 5 个 group method 后，若 ActionDispatcher 错路由 group ActionType 到 Channel，会触发 `getattr` AttributeError；需 ActionDispatcher 端测试 + Platform main loop 加 warning 兜底（已在 §4.2 写入）。
