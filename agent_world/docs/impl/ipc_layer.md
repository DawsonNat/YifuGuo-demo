# IPC 层实现文档

> 路径: `agent_world/ipc/{commands.py, client.py, server.py}`
> 对应 LAYOUT §: §2.I (IPC + Runner + Config) + §7.2 (IPC 命令清单)
> 上游依赖文档: `app_services.md` (client 端调用方), `app_api.md` (HTTP 路由层间接触发)
> 下游依赖文档: `runner.md` (server 端 handler 的真实业务执行)

## 1. 模块定位

文件 IPC 层。Flask 后端进程 (Web Server) 与 仿真子进程 (`run_agent_world_simulation.py`) 之间通过文件系统目录 (`ipc_commands/` 与 `ipc_responses/`) 交换 JSON 命令与响应。本层定义命令类型枚举、客户端发送方法、服务端 handler 调度。Agent World 几乎照搬 MiroFish 的文件 IPC 协议 (`backend/app/services/simulation_ipc.py`), 仅在 `commands.py` 加 4 个新命令枚举, 并在 client / server 两端各加 4 个 `send_xxx()` 与 4 个 handler。

输入 (client 端): 命令类型 + payload。
输出 (server 端): handler 异步消费 + 写响应文件。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `commands.py` (CommandType enum) | MiroFish | `backend/app/services/simulation_ipc.py` (L25-29) | ADAPT | 加 4 项: `INJECT_SCRIPT_EVENT / RELOAD_SCRIPTS / LIST_PLACES / MOVE_AGENT` |
| `client.py` | MiroFish | `backend/app/services/simulation_ipc.py` (L147-415, client 部分) | COPY + 加 4 send_xxx | 文件 IPC 写命令 + 等响应骨架不动 |
| `server.py` | MiroFish | `backend/app/services/simulation_ipc.py` (L147-415, server 部分; runner 侧 handler 在 run_parallel_simulation.py 内联) | COPY + 加 4 handler | poll `ipc_commands/` 目录 + dispatch handler |
| 文件 IPC 目录约定 | MiroFish | `simulation_dir/ipc_commands/` + `ipc_responses/` | COPY | 命令与响应文件名 = `<seq>.json` |

### 2.1 COPY/ADAPT 一览

| 文件 | 决定 | MiroFish 行号 | 改动要点 |
|---|---|---|---|
| `commands.py` | **ADAPT** | L25-29 | `CommandType` enum 加 4 项 |
| `client.py` | **COPY + ADAPT** | L147-415 (client 半边) | 文件写入 / 序列号 / 等响应骨架不动; 加 4 个 `send_xxx()` 方法 |
| `server.py` | **COPY + ADAPT** | L147-415 (server 半边) | 命令 poll / dispatch / 响应写入骨架不动; 加 4 个 handler |

注: MiroFish 的 IPC 实现把 client 与 server 同放在 `simulation_ipc.py` 内; Agent World 拆开成 `client.py` 与 `server.py` 两个文件, 让 Web 进程只 import `client.py`、runner 进程只 import `server.py`, 减少误用。`commands.py` 单独拆出来给两侧共享。

## 3. 关键改动 (相对 MiroFish)

### 3.1 `commands.py` — CommandType enum 加 4 项

LAYOUT §7.2:

```python
class CommandType(str, Enum):
    # MiroFish 原 (KEEP)
    INTERVIEW = "INTERVIEW"
    BATCH_INTERVIEW = "BATCH_INTERVIEW"
    CLOSE_ENV = "CLOSE_ENV"
    # Agent World 新增 (LAYOUT §7.2)
    INJECT_SCRIPT_EVENT = "INJECT_SCRIPT_EVENT"
    RELOAD_SCRIPTS = "RELOAD_SCRIPTS"          # C2: 增量追加 scripts.yaml
    LIST_PLACES = "LIST_PLACES"
    MOVE_AGENT = "MOVE_AGENT"
```

### 3.2 `client.py` — 加 4 个 `send_xxx()`

每个命令一对 method: `send_xxx(...) -> seq` + `wait_xxx_response(seq, timeout) -> dict`; 也可统一用通用 `wait_response(seq, timeout)`。

| 方法 | payload 字段 | 响应字段 |
|---|---|---|
| `send_inject_script_event(sim_id, event)` | `{"event": {"id", "trigger", "effect"}}` | `{"ok": bool, "event_id": str}` |
| `send_reload_scripts(sim_id, scripts_path)` | `{"scripts_path": str}` | `{"ok": bool, "added_event_ids": [str], "skipped_expired": [str]}` |
| `send_list_places(sim_id)` | `{}` | `{"places": [{"place_id", "attrs"}], "agent_locations": {agent_id: place_id}}` |
| `send_move_agent(sim_id, agent_id, place_id)` | `{"agent_id": int, "place_id": str}` | `{"ok": bool, "old_place": str, "new_place": str}` |

### 3.3 `server.py` — 加 4 个 handler

handler 注册表 (`Dict[CommandType, Callable]`) 增 4 项, runner 侧 dispatch 调用真实业务:

| 命令 | handler 委派给 |
|---|---|
| `INJECT_SCRIPT_EVENT` | `ScriptEngine.inject_event(event)` (`agent_world/script/engine.py`) |
| `RELOAD_SCRIPTS` | `ScriptEngine.reload_from_yaml(path)` — 增量追加, 返回 `added / skipped_expired` |
| `LIST_PLACES` | 读 `world.db.{place, agent_location}` 直接序列化 |
| `MOVE_AGENT` | `WorldState.places.force_move(agent_id, place_id)` — 触发 `BehaviorCompressor.on_move` (LAYOUT §2.A ActionDispatcher) |

### 3.4 文件 IPC 协议 (目录与文件)

LAYOUT §1 顶层目录树 + §3.1 SQLite 物理布局:

```
{simulation_dir}/
├── ipc_commands/      # client 写, server 读后删
│   └── <seq>.json     # {"seq": int, "type": CommandType, "payload": {...}}
├── ipc_responses/     # server 写, client 读后删
│   └── <seq>.json     # {"seq": int, "ok": bool, "data": {...} | "error": str}
└── ...
```

- **seq**: 严格单调递增, client 端持久化 (内存计数器即可, sim 重启重置)
- **轮询周期**: server 端默认 0.1s poll 一次 `ipc_commands/`; client 端默认 0.05s poll 一次 `ipc_responses/`
- **超时**: client 等响应默认 30s, 超时返回 `{"ok": false, "error": "timeout"}`
- **原子写**: 写文件用 `tempfile.NamedTemporaryFile` + `os.rename` (POSIX 原子) 避免 server 读到半文件
- **不可重入**: 同一 seq 的命令文件不会被同时处理 (server 读到即删除)

## 4. 核心逻辑

### 4.1 数据结构

- **CommandType** (str Enum): 命令类型枚举, MiroFish 3 项 + Agent World 4 项 = 7 项。
- **Command 文件**: JSON `{"seq": int, "type": str, "payload": dict}`。
- **Response 文件**: JSON `{"seq": int, "ok": bool, "data": dict, "error": str | None}`。
- **PendingResponses** (内存): `Dict[int, asyncio.Future]`, 异步等待响应。

### 4.2 关键流程

```
Client 端 (Flask 进程):
  send_xxx(sim_id, **kwargs):
    seq = next_seq()
    cmd = {"seq": seq, "type": CommandType.XXX, "payload": kwargs}
    atomic_write(f"{ipc_commands_dir}/{seq}.json", cmd)
    return seq

  wait_response(seq, timeout):
    while elapsed < timeout:
      if exists(f"{ipc_responses_dir}/{seq}.json"):
        resp = read_and_remove(...)
        return resp
      sleep(0.05)
    raise TimeoutError

Server 端 (Runner 子进程, 协程):
  loop():
    for path in glob(f"{ipc_commands_dir}/*.json"):
      cmd = read_and_remove(path)
      asyncio.create_task(handle(cmd))   # 不阻塞主 WorldStep 循环

  handle(cmd):
    handler = registry[cmd.type]
    try:
      data = await handler(cmd.payload)
      resp = {"seq": cmd.seq, "ok": True, "data": data, "error": None}
    except Exception as e:
      resp = {"seq": cmd.seq, "ok": False, "data": None, "error": str(e)}
    atomic_write(f"{ipc_responses_dir}/{cmd.seq}.json", resp)
```

### 4.3 与其他模块的交互

- **上游调用方** (client): `app/services/simulation_ipc.py` (Web 进程的 service shim 层透传到本 client) — 见 `app_services.md` §3.3。
- **下游被调方** (server handler): `agent_world/script/engine.py`、`agent_world/world/{state.py, place_store.py}`、`agent_world/persistence/world_db.py`。
- **共享状态**:
  - 文件: `{simulation_dir}/ipc_commands/`, `{simulation_dir}/ipc_responses/` (双向)
  - DB: server 端 `LIST_PLACES` 直读 `world.db.{place, agent_location}`; `MOVE_AGENT` / `INJECT_SCRIPT_EVENT` / `RELOAD_SCRIPTS` 间接通过 ScriptEngine + WorldState 写 `world.db`

## 5. 暴露 API

### 5.1 公开 class / function 签名 (伪代码)

```python
# commands.py
from enum import Enum

class CommandType(str, Enum):
    INTERVIEW = "INTERVIEW"
    BATCH_INTERVIEW = "BATCH_INTERVIEW"
    CLOSE_ENV = "CLOSE_ENV"
    INJECT_SCRIPT_EVENT = "INJECT_SCRIPT_EVENT"
    RELOAD_SCRIPTS = "RELOAD_SCRIPTS"
    LIST_PLACES = "LIST_PLACES"
    MOVE_AGENT = "MOVE_AGENT"

# client.py
class IPCClient:
    def __init__(self, simulation_dir: str): ...

    # 沿 MiroFish (COPY)
    def send_interview(self, agent_id: int, prompt: str) -> int: ...
    def send_batch_interview(self, agent_ids: list[int], prompt: str) -> int: ...
    def send_close_env(self) -> int: ...

    # Agent World 新增 (4 项)
    def send_inject_script_event(self, event: dict) -> int: ...
    def send_reload_scripts(self, scripts_path: str) -> int: ...
    def send_list_places(self) -> int: ...
    def send_move_agent(self, agent_id: int, place_id: str) -> int: ...

    def wait_response(self, seq: int, timeout: float = 30.0) -> dict: ...

# server.py
class IPCServer:
    def __init__(self, simulation_dir: str, world_state, script_engine): ...

    def register_handler(self, cmd_type: CommandType, handler: Callable) -> None: ...

    async def run_forever(self) -> None: ...   # 主 poll 循环

    # handler 签名
    async def handle_inject_script_event(self, payload: dict) -> dict: ...
    async def handle_reload_scripts(self, payload: dict) -> dict: ...
    async def handle_list_places(self, payload: dict) -> dict: ...
    async def handle_move_agent(self, payload: dict) -> dict: ...
```

### 5.2 IPC / Flask / SQL

#### IPC 命令 schema

| 命令 | payload | 响应 data |
|---|---|---|
| `INTERVIEW` (MiroFish) | `{"agent_id": int, "prompt": str}` | `{"response": str}` |
| `BATCH_INTERVIEW` (MiroFish) | `{"agent_ids": [int], "prompt": str}` | `{"responses": {agent_id: str}}` |
| `CLOSE_ENV` (MiroFish) | `{}` | `{}` |
| `INJECT_SCRIPT_EVENT` (NEW) | `{"event": {"id": str, "trigger": {...}, "effect": {...}}}` | `{"event_id": str}` |
| `RELOAD_SCRIPTS` (NEW, C2) | `{"scripts_path": str}` | `{"added_event_ids": [str], "skipped_expired": [str]}` |
| `LIST_PLACES` (NEW) | `{}` | `{"places": [{"place_id", "attrs"}], "agent_locations": {agent_id: place_id}}` |
| `MOVE_AGENT` (NEW) | `{"agent_id": int, "place_id": str}` | `{"old_place": str, "new_place": str}` |

#### SQL 输入 / 输出表

- `LIST_PLACES` handler: SELECT `world.db.place`, `world.db.agent_location`
- `MOVE_AGENT` handler: 通过 `WorldState.places.force_move()` → 触发 `agent_world/memory/compressor.py:on_move` (LAYOUT §2.A) → 写 `world.db.agent_location`
- `INJECT_SCRIPT_EVENT` handler: `ScriptEngine.inject_event` → 在适当 tick 写 `world.db.script_event_log`
- `RELOAD_SCRIPTS` handler: `ScriptEngine.reload_from_yaml` → 不直接写 DB, 仅更新内存中 `loaded_event_ids`

## 6. 配置入口

从 `simulation_config.json` 不读字段 (本层无配置)。从环境 / 运行参数:

- `simulation_dir`: 由 runner / Flask 进程双方在启动时各自获得 (Flask 进程通过 `simulation_manager.start()` 创建, runner 进程从 argv 接收)
- `IPC_POLL_INTERVAL_SERVER` (默认 0.1s)
- `IPC_POLL_INTERVAL_CLIENT` (默认 0.05s)
- `IPC_DEFAULT_TIMEOUT` (默认 30s)

默认值由常量给出, 不需 `simulation_config.json` 中显式声明。

## 7. 待决策 / 风险

- **响应文件清理**: client 读取响应后立即删除; 若 client 崩溃, 残留文件由 runner 启动 / 关闭时统一清理。MVP 不实现重试 / 幂等性。
- **大 payload**: `BATCH_INTERVIEW` 响应可能很大 (N agents × 长文本); MVP 接受单文件 5MB 上限, 超过分块由调用方处理。
- **并发命令**: client 可同时 in-flight 多个命令 (不同 seq); server 端 handler 并发执行 (`asyncio.create_task`), 各自独立。但 `MOVE_AGENT` / `INJECT_SCRIPT_EVENT` 等会写 `world.db`, 走 `world.db` 单写者 `asyncio.Lock` (LAYOUT v0.3 B8) 保证一致性。
- **C2 RELOAD_SCRIPTS**: 路径必须在 `simulation_dir` 下, 防止任意文件读取 (server handler 入口校验)。
- **跨平台**: 文件 IPC 依赖 POSIX `os.rename` 原子性; Windows 上 `rename` 在目标存在时报错, 需要先删再 rename — MVP 仅 macOS/Linux 测试, Windows 兼容 D 类。
