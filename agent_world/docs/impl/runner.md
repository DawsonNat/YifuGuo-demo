# Runner 子进程实现文档

> 路径: `agent_world/runner/{run_agent_world_simulation.py, action_logger.py}`
> 对应 LAYOUT §: §2.I (IPC + Runner + Config)
> 上游依赖文档: `app_services.md` (`simulation_manager` 通过 `subprocess.Popen` 启动本入口), `ipc_layer.md` (server 端嵌入本进程), `config_layer.md` (从 `simulation_config.json` 加载)
> 下游依赖文档: 无 (调用 `agent_world/world/`、`agent_world/buses/`、`agent_world/pools/`、`agent_world/script/`、`agent_world/memory/`、`agent_world/persistence/` 等核心模块, 见各自模块文档)

## 1. 模块定位

子进程主入口。由 Flask 后端 `simulation_manager.start()` 通过 `subprocess.Popen` 启动, argv 传入 `simulation_id` 与 `simulation_dir`。本入口完成: (1) 加载 `simulation_config.json`; (2) 装配 `WorldState` + `MultiPoolPlatformManager` + `ScriptEngine` + `MultiGraphUpdater` 等; (3) 启动 IPC server (协程); (4) 主循环跑 `WorldStep` (含 micro-tick); (5) 每条 action 通过 `action_logger.append(...)` 写入 `actions.jsonl`, 让 Flask 进程的 `simulation_runner` tail 流式读取。

`action_logger.py` 是 actions.jsonl 写入工具, 独立提取以便单元测试。

输入: argv (`simulation_id`, `simulation_dir`); IPC 命令 (从 ipc_commands/ 读)。
输出: `world.db` / `pool_*.db` / `actions.jsonl` / Zep / IPC 响应文件。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 主入口骨架 | MiroFish | `backend/scripts/run_twitter_simulation.py` (L531-704) | PATTERN | 子进程启动 / argv 解析 / config 加载 / IPC server 装配 / 主循环骨架 |
| 双 env 模式 | MiroFish | `backend/scripts/run_parallel_simulation.py` (L1101-1490) | PATTERN | 多 Platform 装配链路 (Twitter + Reddit), 启发 `MultiPoolPlatformManager` 多池构造 |
| `actions.jsonl` 写入器 | MiroFish | `backend/scripts/run_parallel_simulation.py` 内联 (`action_logger`) | COPY + 加字段 | 提取到独立文件 `action_logger.py`; 加 5 字段 |
| `event_config.initial_posts` | MiroFish | `backend/scripts/run_parallel_simulation.py` (L1180-1207) | REPLACE | 由 ScriptEngine.loader 接管 (LAYOUT §2.E) |
| `asyncio.gather(twitter, reddit)` | MiroFish | `backend/scripts/run_parallel_simulation.py` (L1585-1589) | REPLACE | 改为单 `WorldStep` 循环 (LAYOUT §6.1 micro-tick) |

### 2.1 COPY/PATTERN/REPLACE 一览

| 文件 | 决定 | MiroFish 来源 | 改动要点 |
|---|---|---|---|
| `run_agent_world_simulation.py` | **REPLACE** | `run_twitter_simulation.py` L531-704 + `run_parallel_simulation.py` L1101-1490, L1585-1589 | main loop 替换为 `WorldStep` 循环 (micro-tick); 双 env 模式扩展为 N 池 |
| `action_logger.py` | **COPY + ADAPT** | `run_parallel_simulation.py` 内联 action_logger | 提取独立文件; 加 `place_id / channel_type / arrive_at / attempted_at / delivered` 5 字段 |

## 3. 关键改动 (相对 MiroFish)

### 3.1 `run_agent_world_simulation.py` (REPLACE)

#### 3.1.1 取代 MiroFish `run_parallel_simulation.py`

MiroFish 的 `run_parallel_simulation.py` 是 "Twitter env + Reddit env 并行" 模式 (`asyncio.gather(twitter, reddit)`); Agent World 改为 **单一 `WorldStep` 循环**, 每轮内部调度 N 个 pool (`MultiPoolPlatformManager.update_all_rec_tables()`)。

#### 3.1.2 骨架借自 `run_twitter_simulation.py:531-704`

保留:
- argv 解析 + `simulation_dir` 解析
- `simulation_config.json` 加载
- 日志路径设置
- IPC server 装配 (现走 `agent_world/ipc/server.py`)
- `actions.jsonl` 路径与初始化
- 关闭信号 (SIGTERM / SIGINT) 处理

替换:
- `OASISClient + Twitter Platform` 单实例 → `MultiPoolPlatformManager.build(simulation_config)` 多池 (LAYOUT §2.D)
- 单 `agent_environment` → `WorldState + PerceptionBuilder` (LAYOUT §2.A)
- 单 `simulation.step()` → `WorldStep.run()` 循环 (LAYOUT §6.1 含 micro-tick 11 步)

#### 3.1.3 双 env 模式扩展为 N 池 (借鉴 `run_parallel_simulation.py:1101-1490`)

`MultiPoolPlatformManager.build()` 内部:
- 遍历 `simulation_config.world_config.coverage` + `agent_configs[*].location` 推断需要哪些 pool (place × feed)
- 每池: `PlatformFactory.build(pool_path, recsys_kind)` 创建独立 Channel + 独立 RecSys 实例 + 独立 SQLite
- 注册到 manager 的 `(place_id, feed) → Platform` 映射

#### 3.1.4 main loop 替换为 WorldStep 循环

LAYOUT §6.1 完整 11 步:

```
async def main_loop():
    world = await build_world_state(config)
    pools = await MultiPoolPlatformManager.build(config)
    script_engine = ScriptEngine.from_yaml(config.world_config.events)
    ipc_server = IPCServer(simulation_dir, world, script_engine)
    asyncio.create_task(ipc_server.run_forever())

    action_logger = ActionLogger(simulation_dir / "actions.jsonl")

    while not should_stop():
        await WorldStep.run(
            world=world,
            pools=pools,
            script_engine=script_engine,
            action_logger=action_logger,
        )
        # WorldStep 内部完成 micro-tick + script + sweep + dispatch + compressor + Zep flush + clock+1
```

每条 action 完成后 (micro-tick 内或 lockstep 阶段), `ActionDispatcher` 调 `action_logger.append(action_record)` 写 `actions.jsonl`。

### 3.2 `action_logger.py` (COPY + ADAPT)

#### 3.2.1 复用 MiroFish actions.jsonl 写入器

JSONL 格式 (一行一 action), append-only; flush 周期默认每 N=10 条或每 1s。

#### 3.2.2 加 5 字段 (LAYOUT §2.I `action_logger.py` 行)

每条 action record 在 MiroFish 字段基础上加 (v0.3):

| 字段 | 类型 | 含义 |
|---|---|---|
| `place_id` | `str \| None` | 发送时刻 actor 所在地点 (LAYOUT §3.2 direct_message DDL) |
| `channel_type` | `str \| None` | `"F2F" / "RDC" / "GRP"` (仅消息类 action 有值, FEED action 为 None) |
| `arrive_at` | `int \| None` | 消息到达 tick (B1.1; 仅 RDC/GRP 有值) |
| `attempted_at` | `int` | 调用 send 的 `world.t` (B9; 所有 action 都有) |
| `delivered` | `int \| None` | 0=失败 / 1=成功 / -1=已取消 (B6; 仅消息类 action 有值) |

注: FEED 类 action (CREATE_POST 等) 的这些字段大多为 None, 由 `simulation_runner.py` (Flask 侧) 解析时用 `dict.get(..., None)` 兜底兼容旧 demo (见 `app_services.md` §3.2)。

## 4. 核心逻辑

### 4.1 数据结构

- **ActionRecord** (dict, 写入 actions.jsonl 的一行):
  ```
  {
    "t": int,                        # world.t
    "agent_id": int,
    "action_type": str,              # ActionType enum value
    "args": dict,                    # action 参数
    "result": "ok" | "fail" | "deferred",
    "error": str | None,             # 失败原因 (B4 retry 后仍失败 / B9 silent)
    # v0.3 新加 (LAYOUT §2.I)
    "place_id": str | None,
    "channel_type": "F2F" | "RDC" | "GRP" | None,
    "arrive_at": int | None,
    "attempted_at": int,
    "delivered": -1 | 0 | 1 | None
  }
  ```
- **ActionLogger** (内存): 文件句柄 + 缓冲列表 + flush 锁。
- **WorldStep 主循环状态**: 由 `WorldState` 持有 (此处不重复定义, 见 LAYOUT §2.A)。

### 4.2 关键流程

```
启动 (子进程):
  parse argv -> simulation_id, simulation_dir
  load simulation_config.json
  set up logging
  build WorldState (load world.db schema, places, coverage, ...)
  build MultiPoolPlatformManager (N pools)
  build ScriptEngine (load events from world_config.events)
  build MultiGraphUpdater (Zep)
  start IPCServer (asyncio task)
  init ActionLogger (open actions.jsonl in append mode)

主循环:
  while world.t < max_ticks and not stop_signal:
    await WorldStep.run(...)
        # 11 步 (LAYOUT §6.1):
        # 1. ScriptEngine.due_events
        # 2. apply effects
        # 3. Pools.update_all_rec_tables  (asyncio.gather)
        # 4. Scheduler.pick_active
        # 5. DeliveryQueue.sweep_undelivered
        # 6. group_by_place + shuffle(seed=t)
        # 7. await asyncio.gather(*[run_place(...)])  # micro-tick
        # 8. (no-op, 已在 7 内)
        # 9. for a, move in pending_moves: BehaviorCompressor.on_move + places.move
        # 10. ZepUpdater.flush
        # 11. world.t += 1
    # 每条 action 在 ActionDispatcher 内:
    #   action_logger.append(record)
  # 关闭
  await ZepUpdater.flush_remaining()
  action_logger.close()
  ipc_server.stop()
```

### 4.3 与其他模块的交互

- **上游调用方**:
  - `app/services/simulation_manager.py` 通过 `subprocess.Popen` 启动本入口
  - `app/services/simulation_ipc.py` (client 端) 通过文件 IPC 发命令到本进程的 `IPCServer`
- **下游被调方**:
  - `agent_world/world/{state, step, place_store, relation_graph, capability_table, connectivity, perception, dispatcher, clock}.py`
  - `agent_world/buses/{face_to_face, remote_message, group_message}.py`
  - `agent_world/pools/{manager, platform_factory}.py`
  - `agent_world/script/{engine, loader, triggers/, effects/}.py`
  - `agent_world/memory/{updater, manager, translator, retrieval, segment, compressor}.py`
  - `agent_world/persistence/{world_db, pool_db}.py`
  - `agent_world/ipc/server.py`
- **共享状态**:
  - 写: `{simulation_dir}/world.db` (12 表), `{simulation_dir}/pools/*.db` (N×13 表), `{simulation_dir}/actions.jsonl`, `{simulation_dir}/ipc_responses/`
  - 读: `{simulation_dir}/simulation_config.json`, `{simulation_dir}/ipc_commands/`
  - Zep: 三层 graph (`graph_{agent}`, `place_{place}`, `world`), 仅由 `agent_world/memory/compressor.py` 触发写

## 5. 暴露 API

### 5.1 公开 class / function 签名 (伪代码)

```python
# run_agent_world_simulation.py
async def main(simulation_id: str, simulation_dir: str) -> int:
    """子进程主入口; 返回 exit code"""
    ...

if __name__ == "__main__":
    import sys
    sim_id = sys.argv[1]
    sim_dir = sys.argv[2]
    sys.exit(asyncio.run(main(sim_id, sim_dir)))

# action_logger.py
class ActionLogger:
    def __init__(self, path: str, flush_interval: float = 1.0, batch_size: int = 10): ...
    def append(self, record: dict) -> None: ...   # 缓冲 + 按需 flush
    def flush(self) -> None: ...
    def close(self) -> None: ...
```

### 5.2 IPC / Flask / SQL

- **IPC**: 本进程内嵌 `agent_world/ipc/server.py` 的 `IPCServer`; 不直接暴露 IPC 接口给外部, 全部通过 `simulation_dir/ipc_commands|responses/` 文件目录。
- **SQL 写入** (经过各下游模块):
  - `world.db.{place, coverage, agent_location, relation, capability, direct_message, overhear, script_event_log, chat_group, group_member, group_message, group_event}` — 12 张表全部由本进程写
  - `pool_*.db.{user, post, follow, like, dislike, comment, comment_like, comment_dislike, mute, report, rec, trace, product}` — 每池 13 张表 (LAYOUT §3.3)
- **文件输出**:
  - `actions.jsonl` (append-only)
  - `world.db` (单写者, `asyncio.Lock`, LAYOUT §9.6 G)
- **Zep 写入**: 仅 `BehaviorCompressor.on_move` 触发, 经 `MultiGraphUpdater` flush 落 Zep

## 6. 配置入口

从 `simulation_config.json` 读取 (LAYOUT §7.1):

- `simulation_id / project_id / graph_id` — 透传
- `world_graphs.{world, per_agent_template, per_place_template}` — 给 `MultiGraphUpdater`
- `time_config.{max_ticks, ...}` — 主循环退出条件
- `agent_configs[*]` — 启动 agent + 6 字段 (location/relations/capabilities/soul/long_term_goal/current_state)
- `event_config` (MiroFish 兼容) + `world_config.events` (新) — 给 `ScriptEngine.loader`
- `world_config.{places, coverage}` — 给 `WorldState` + `ConnectivityResolver`
- `channel_config.default_delays.{F2F,RDC,GRP}` + `channel_config.failed_attempt_ttl_ticks` + `channel_config.group_event_ttl_ticks` — 给 Bus 层 + PerceptionBuilder
- `channel_config.group_message.redeliver_undelivered` — 给 `DeliveryQueue.sweep_undelivered`
- `memory_config.compressor.{enabled, model, max_raw_actions, summary_sentences}` — 给 `BehaviorCompressor`
- `memory_config.retry_policy.{parse_error_max_retry, arg_missing_max_retry, other_max_retry}` — 给 `ActionDispatcher`
- `twitter_config / reddit_config` — 兼容旧 demo (经 `MultiPoolPlatformManager` 投影到对应 pool)

默认值 / 验证: 由 `agent_world/config/world_config.py` Pydantic schema 校验 (LAYOUT §2.I, 见 `config_layer.md`)。

## 7. 待决策 / 风险

- LAYOUT §9.6 G: `world.db` 单写者 `asyncio.Lock` 性能瓶颈 — MVP 接受, 性能瓶颈在 LLM 推理而非 DB; 后期再做读写分离评估。
- LAYOUT §9.5 #8 + N3: 100w agent scale + DeliveryQueue.sweep 性能 — D 类讨论。
- 子进程崩溃恢复: 当前 MVP 不实现断点续跑; 重启即从 `world.t = 0` 重新开始。后期可加 checkpoint (将 WorldState 序列化到 `world.db.checkpoint` 表)。
- Zep flush 失败: `MultiGraphUpdater.flush_remaining()` 在关闭时如失败, 仅打 warn 日志, 不阻塞退出 (LAYOUT §9.5.1 N4)。
- argv 兼容: MiroFish `run_twitter_simulation.py` 接受多个命名参数 (`--config`, `--sim-dir`); Agent World 简化为 2 个位置参数 + 可选 `--config-path`, 与 `simulation_manager.py` 协议一致。
