# Flask 后端 Services 实现文档

> 路径: `agent_world/app/services/*`
> 对应 LAYOUT §: §2.B 整表
> 上游依赖文档: `app_api.md`, `ipc_layer.md`, `runner.md`, `config_layer.md`
> 下游依赖文档: 无 (业务最外层)

## 1. 模块定位

`app/services/` 是 Flask 后端的业务服务层, 位于 `app/api/*` 路由与 `agent_world/{world,buses,pools,memory,script,...}` 仿真核心之间。每个服务封装一个独立责任 (子进程生命周期、IPC 通道、配置生成、profile 生成、Zep 工具、复盘报告), 让 API 层只做"参数校验 + 调用 service"。Agent World 几乎照搬 MiroFish 的 services, 只对涉及世界级数据 (places / relations / capabilities / direct_message / world.db) 的部分做 ADAPT。

输入: API 层调用、子进程 IPC 响应、actions.jsonl tail。
输出: 子进程进程对象、IPC 命令文件、agent profile (CSV/JSON)、simulation_config.json、复盘报告文本。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `simulation_manager.py` | MiroFish | `backend/app/services/simulation_manager.py` (L313-480) | COPY | 整片复用; 仅改 `script_path` |
| `simulation_runner.py` | MiroFish | `backend/app/services/simulation_runner.py` (L482-563) | COPY | 监控线程 + actions.jsonl tail |
| `simulation_ipc.py` | MiroFish | `backend/app/services/simulation_ipc.py` (L25-415) | ADAPT | enum + send_xxx + handler 各加 4 项 |
| `simulation_config_generator.py` | MiroFish | `backend/app/services/simulation_config_generator.py` (L1-198, dataclass L150-174) | ADAPT | 顶层加 `world_config / channel_config / memory_config` |
| `oasis_profile_generator.py` | MiroFish | `backend/app/services/oasis_profile_generator.py` (L30-140 dataclass; L1070-1119 Twitter CSV; L1146-1193 Reddit JSON) | ADAPT | profile 加 6 字段 + CSV/JSON 导出加列 + prompt 加生成指令 |
| `report_agent.py` | MiroFish | `backend/app/services/report_agent.py` 全 | ADAPT | 加世界级表查询 + 跨 DB 复盘 |
| `zep_tools.py` | MiroFish | `backend/app/services/zep_tools.py` (L1237-1270 quick_search; L45-54 to_text) | COPY | 已支持 graph_id 参数 |
| `zep_entity_reader.py` | MiroFish | `backend/app/services/zep_entity_reader.py` 全 | COPY | 已支持 graph_id 参数 |
| `zep_graph_memory_updater.py` | MiroFish | `backend/app/services/zep_graph_memory_updater.py` (L35-199, L202-477, L479-554) | ADAPT | 移到 `agent_world/memory/{updater.py, manager.py, translator.py}` (见 LAYOUT §2.F); services 层不再保留 |
| `ontology_generator.py` | MiroFish | `backend/app/services/ontology_generator.py` | SKIP | 域建模无关 |
| `text_processor.py` | MiroFish | `backend/app/services/text_processor.py` | SKIP | 通用文本工具, 价值低 |
| `graph_builder.py` | MiroFish | `backend/app/services/graph_builder.py` | SKIP | 批量建图基础设施, 暂不需要 |

### 2.1 COPY/ADAPT/SKIP 一览

| 服务 | 决定 | MiroFish 行号 | 改动要点 |
|---|---|---|---|
| `simulation_manager.py` | **COPY** | L313-480 | `script_path` 改指 `runner/run_agent_world_simulation.py` |
| `simulation_runner.py` | **COPY** | L482-563 | 监听新 IPC 响应类型 (4 个新命令的 ack) |
| `simulation_ipc.py` | **ADAPT** | L25-415 | 加 4 命令枚举 (`INJECT_SCRIPT_EVENT / RELOAD_SCRIPTS / LIST_PLACES / MOVE_AGENT`) + 对应 `send_xxx()` + handler |
| `simulation_config_generator.py` | **ADAPT** | L1-198 | 顶层 dataclass 加 `world_config / channel_config / memory_config` 三个 key |
| `oasis_profile_generator.py` | **ADAPT** | L30-1193 | `OasisAgentProfile` 加 6 字段 (`location/relations/capabilities/soul/long_term_goal/current_state`); CSV (Twitter L1070-1119) / JSON (Reddit L1146-1193) 导出加列; prompt 加生成指令 |
| `report_agent.py` | **ADAPT** | 全 | 加世界级表查询 (relation/capability 变迁统计) + 跨 DB 复盘 (`world.db.{direct_message, script_event_log} ∪ pool_*.db.trace`) |
| `zep_tools.py` | **COPY** | L1237-1270 | 原样, 已接受 `graph_id` |
| `zep_entity_reader.py` | **COPY** | 全 | 原样, 已接受 `graph_id` |
| `ontology_generator.py` | **SKIP** | — | 与 Agent World 无直接关系 |
| `text_processor.py` | **SKIP** | — | 通用文本工具 |
| `graph_builder.py` | **SKIP** | — | 批量建图基础设施 |

## 3. 关键改动 (相对 MiroFish)

### 3.1 `simulation_manager.py` (COPY)

- `_build_subprocess_command()` 中 `script_path` 由 `backend/scripts/run_twitter_simulation.py` 改指 `agent_world/runner/run_agent_world_simulation.py` (绝对路径或相对 `agent_world/` 包路径)。
- 子进程环境变量、`simulation_dir` 布局、`ipc_commands/` 与 `ipc_responses/` 目录约定全部不动。
- 进程生命周期 (`start / stop / status / wait_until_ready`) 逻辑不动。

### 3.2 `simulation_runner.py` (COPY)

- 监控线程 + `actions.jsonl` tail 流式读取整片复用 (L482-563)。
- 因 `action_logger.py` (LAYOUT §2.I, 见 `runner.md`) 在每条 action 上多写 `place_id / channel_type / arrive_at / attempted_at / delivered`, 解析逻辑要把这些字段透传给 UI; 字段不存在时 fallback `None` 兼容旧 demo。
- 新 IPC 响应类型 (4 项) 仅加分支, 不动主循环。

### 3.3 `simulation_ipc.py` (ADAPT)

新 4 命令 (LAYOUT §7.2):

| 枚举 | 用途 | 参数 |
|---|---|---|
| `INJECT_SCRIPT_EVENT` | 运行时注入单个剧本事件 | `event: dict` (含 `id / trigger / effect`) |
| `RELOAD_SCRIPTS` (C2) | 重读 scripts.yaml, 增量追加新 event_id | `scripts_path: str` |
| `LIST_PLACES` | UI 查询当前世界拓扑 | 无参数, 响应含 places + L_t |
| `MOVE_AGENT` | UI 强制移动 agent | `agent_id: int, place_id: str` |

每命令两端各加一对: client 侧 `send_inject_script_event() / send_reload_scripts() / send_list_places() / send_move_agent()`; runner 侧 server.py 同步加 4 个 handler。

### 3.4 `simulation_config_generator.py` (ADAPT)

顶层 dataclass (L150-174) 加三个 key, 由 LLM 生成:

- `world_config: { places, coverage, events }` — Pydantic schema 由 conscribe 自动生成 (Tier 1, LAYOUT §10.3)
- `channel_config: { default_delays, group_message, failed_attempt_ttl_ticks, group_event_ttl_ticks }` (LAYOUT §7.1)
- `memory_config: { compressor, retry_policy }`

LLM prompt 增加三段生成指令: 让模型按场景生成 places (含 `timezone` / `behavior_hint`)、coverage (含 `latency_ticks`)、events (剧本)。

### 3.5 `oasis_profile_generator.py` (ADAPT)

- `OasisAgentProfile` dataclass (L30-140) 加 6 字段: `location: str`、`relations: List[Tuple[int, str]]`、`capabilities: List[str]`、`soul: str`、`long_term_goal: str`、`current_state: str` (LAYOUT §2.G)。
- Twitter CSV 导出 (L1070-1119) 加列: `location`, `relations_json`, `capabilities_json`, `soul`, `long_term_goal`, `current_state`。
- Reddit JSON 导出 (L1146-1193) 加同名字段。
- LLM prompt 加生成指令: 让模型按场景写 soul (人格底色)、long_term_goal (中期目标)、current_state (当前状态), 并基于 world_config.places 选 location、关系/能力候选给出对应取值。

### 3.6 `report_agent.py` (ADAPT)

- 加世界级表查询: `relation` / `capability` / `agent_location` / `script_event_log` / `group_event` 的变迁统计 (按时间分桶)。
- 加跨 DB 复盘: 输入 `agent_id` + `[t_start, t_end]`, 联合查询 `world.db.{direct_message, script_event_log}` 与所有 `pool_*.db.trace`, 按 `attempted_at / created_at` 时间排序合并, 输出"一个 agent 一天做了什么"叙事 (LAYOUT §3.4 末尾决议)。
- 性能注: pool 数量 N → N+1 个 SQLite 连接顺序查询; MVP 不优化, 后期 D 类讨论 (LAYOUT §9.6 F)。

### 3.7 `zep_tools.py` / `zep_entity_reader.py` (COPY)

原样 import; `quick_search(graph_id, query, ...)` 与 `EntityReader(graph_id)` 已支持 graph_id 参数, 无须改动。`agent_world/memory/retrieval.py` 直接调用本服务。

### 3.8 SKIP 项

`ontology_generator.py` / `text_processor.py` / `graph_builder.py` — 三者均是 MiroFish 域建模管线的一部分, Agent World 的 schema 由 LLM + 人工写, 不复用。

## 4. 核心逻辑

### 4.1 数据结构

- **OasisAgentProfile (扩展)**: 原 MiroFish 字段 + 6 新字段; 用作 LLM 生成 + CSV/JSON 序列化双载体。
- **SimulationConfig (顶层 dataclass)**: 加 `world_config` / `channel_config` / `memory_config` 三 key, 其余沿用 MiroFish。
- **IPC Command (新 4 项)**: `CommandType` enum 扩展; payload 走 JSON 文件 (LAYOUT §1 目录树 `ipc_commands/`)。

### 4.2 关键流程

```
启动仿真:
  api/simulation.start_simulation(payload)
  → simulation_config_generator.generate(payload)   # 加 3 顶层 key
  → oasis_profile_generator.generate(...)           # 6 新字段进 csv/json
  → simulation_manager.start(simulation_id)
      ↓ subprocess.Popen(script_path=runner/run_agent_world_simulation.py, ...)
  → simulation_runner.attach_monitor(...)           # 守 actions.jsonl tail

UI 注入剧本事件:
  api/simulation.inject_event(...)
  → simulation_ipc.send_inject_script_event(...)
  → 写 ipc_commands/<seq>.json
  → runner 侧 server.py handler 调 ScriptEngine.inject_event(...)

UI 列地点:
  api/world.places(...)
  → simulation_ipc.send_list_places(...)
  → runner 侧 handler 读 world.db.{place, agent_location}
  → ipc_responses/<seq>.json

复盘:
  api/report.generate(simulation_id, agent_id, [t_start, t_end])
  → report_agent.query_world_tables(...)            # world.db.{direct_message, script_event_log, group_event, relation, capability}
  → report_agent.query_pool_traces(...)             # 遍历 pool_*.db.trace
  → report_agent.merge_and_format(...)
```

### 4.3 与其他模块的交互

- **上游调用方**: `app/api/simulation.py`、`app/api/world.py`、`app/api/report.py`、`app/api/graph.py`。
- **下游被调方**: 子进程 (`runner/run_agent_world_simulation.py`)、`agent_world/memory/{updater,manager,retrieval}` (Zep 客户端)、Zep SDK。
- **共享状态**:
  - `simulation_manager` 持子进程对象 (内存)
  - `simulation_ipc` 读写 `ipc_commands/` 与 `ipc_responses/` 目录
  - `simulation_runner` tail `actions.jsonl`
  - `report_agent` 只读 `world.db` 12 张表 + `pool_*.db` 13 张表
  - `oasis_profile_generator` 只生成静态文件; 不读 DB
  - `simulation_config_generator` 写 `simulation_config.json`

## 5. 暴露 API

### 5.1 公开 class / function 签名 (伪代码)

```python
# simulation_manager.py
class SimulationManager:
    def start(self, simulation_id: str, config_path: str) -> SimulationProcess: ...
    def stop(self, simulation_id: str) -> None: ...
    def status(self, simulation_id: str) -> dict: ...

# simulation_runner.py
class SimulationRunner:
    def attach_monitor(self, simulation_id: str, actions_jsonl_path: str) -> None: ...
    def stream_actions(self, simulation_id: str) -> Iterator[dict]: ...

# simulation_ipc.py
class CommandType(str, Enum):
    INTERVIEW = "INTERVIEW"
    BATCH_INTERVIEW = "BATCH_INTERVIEW"
    CLOSE_ENV = "CLOSE_ENV"
    INJECT_SCRIPT_EVENT = "INJECT_SCRIPT_EVENT"   # 新
    RELOAD_SCRIPTS = "RELOAD_SCRIPTS"             # 新
    LIST_PLACES = "LIST_PLACES"                   # 新
    MOVE_AGENT = "MOVE_AGENT"                     # 新

class SimulationIPCClient:
    def send_inject_script_event(self, sim_id: str, event: dict) -> int: ...
    def send_reload_scripts(self, sim_id: str, scripts_path: str) -> int: ...
    def send_list_places(self, sim_id: str) -> int: ...
    def send_move_agent(self, sim_id: str, agent_id: int, place_id: str) -> int: ...
    def wait_response(self, sim_id: str, seq: int, timeout: float) -> dict: ...

# simulation_config_generator.py
@dataclass
class SimulationConfig:
    simulation_id: str
    project_id: str
    graph_id: str
    world_graphs: dict
    time_config: dict
    agent_configs: list
    event_config: dict
    world_config: dict        # 新
    channel_config: dict      # 新
    memory_config: dict       # 新
    twitter_config: dict | None
    reddit_config: dict | None

def generate_config(payload: dict) -> SimulationConfig: ...

# oasis_profile_generator.py
@dataclass
class OasisAgentProfile:
    # ... MiroFish 原字段 ...
    location: str                          # 新
    relations: list[tuple[int, str]]       # 新
    capabilities: list[str]                # 新
    soul: str                              # 新
    long_term_goal: str                    # 新
    current_state: str                     # 新

def generate_profiles(scenario: dict, n: int) -> list[OasisAgentProfile]: ...
def export_twitter_csv(profiles: list[OasisAgentProfile], path: str) -> None: ...
def export_reddit_json(profiles: list[OasisAgentProfile], path: str) -> None: ...

# report_agent.py
class ReportAgent:
    def query_world_tables(self, sim_id: str, agent_id: int, t_range: tuple[int, int]) -> dict: ...
    def query_pool_traces(self, sim_id: str, agent_id: int, t_range: tuple[int, int]) -> list[dict]: ...
    def generate_report(self, sim_id: str, agent_id: int, t_range: tuple[int, int]) -> str: ...
```

### 5.2 IPC / Flask / SQL

- **IPC 命令**: 4 项新增, 见 `ipc_layer.md`。
- **Flask 路由**: 由 `app/api/*` 调用本层, 详见 `app_api.md`。
- **SQL**:
  - `report_agent` 只读: `world.db.{direct_message, script_event_log, group_event, relation, capability, agent_location}` ∪ `pool_*.db.{trace, post, like, follow}`
  - 其余 service 不直接碰 DB

## 6. 配置入口

从 `simulation_config.json` 读取:

- `simulation_id / project_id / graph_id` — 全部 service 通用
- `world_graphs.{world, per_agent_template, per_place_template}` — `report_agent` 与 zep_* 服务用
- `world_config.{places, coverage, events}` — `simulation_config_generator` 生成, `report_agent` 复盘时引用
- `channel_config.*` — runner / Bus 用 (本层只透传)
- `memory_config.compressor.*` — `report_agent` 展示压缩摘要时用

默认值与验证:
- 4 个 IPC 命令的 timeout 默认 30s (与 MiroFish 一致)
- `report_agent` 的 t_range 缺省 = `[0, world.t]`

## 7. 待决策 / 风险

- LAYOUT §9.6 F: `report_agent` 跨 DB 复盘性能 (N+1 SQLite 连接) 仍开放; 后期可加 cache layer 或 ETL 到单表。
- LAYOUT §9.5 #8: 100w agent scale 下 `oasis_profile_generator` 单次 LLM 生成成本 + CSV 大小; MVP 不限制。
- `RELOAD_SCRIPTS` (C2) 增量语义: client 端拼 IPC 时不带 events 内容, 由 runner 侧重读文件; 文件路径解析为绝对路径以避免 cwd 漂移。
- `simulation_runner` 解析 `actions.jsonl` 的兼容: 老 demo 不写 `arrive_at` 等字段, 用 `dict.get(..., None)` 兜底, 并打 warn 日志一次。
