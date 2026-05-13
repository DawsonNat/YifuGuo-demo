# Flask API 与入口实现文档

> 路径: `agent_world/app/api/*` + `run.py` + `agent_world/app/__init__.py` + `agent_world/app/config.py` + `agent_world/app/models/*`
> 对应 LAYOUT §: §2.B (Flask 后端) + §7.3 (路由表) + §1 (顶层目录树)
> 上游依赖文档: 无 (HTTP 边界, 由前端 / 用户调用)
> 下游依赖文档: `app_services.md`, `ipc_layer.md`, `config_layer.md`

## 1. 模块定位

Flask 后端的 HTTP 边界。`run.py` 是进程入口, `app/__init__.py` 装配 Flask app, `app/config.py` 持配置常量, `app/models/` 是 ORM (project / task), `app/api/*` 是路由层 (按业务拆 blueprint)。Agent World 几乎照搬 MiroFish 的 Flask 外壳; 仅 `api/simulation.py` 扩展 3 个新路由, 新增 `api/world.py` 一个 blueprint。

输入: HTTP 请求 (JSON body / query string)。
输出: HTTP 响应 (JSON), 业务委派给 `app/services/*`。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `run.py` | MiroFish | `backend/run.py` | COPY | Flask 进程入口 |
| `app/__init__.py` | MiroFish | `backend/app/__init__.py` | COPY | `create_app()` 工厂 |
| `app/config.py` | MiroFish | `backend/app/config.py` | COPY | DB / Zep / OpenAI 配置常量 |
| `app/models/project.py` | MiroFish | `backend/app/models/project.py` | COPY | Project ORM |
| `app/models/task.py` | MiroFish | `backend/app/models/task.py` | COPY | Task ORM |
| `app/api/simulation.py` | MiroFish | `backend/app/api/simulation.py` 全 | ADAPT | 加 3 路由 (inject-event / places / move-agent), 现有路由不动 |
| `app/api/graph.py` | MiroFish | `backend/app/api/graph.py` | COPY | 原样 |
| `app/api/report.py` | MiroFish | `backend/app/api/report.py` | COPY | 原样 |
| `app/api/world.py` | — | — | NEW | 全新, 地点/关系/能力查询 + 剧本注入 |

### 2.1 COPY/ADAPT/NEW 一览

| 文件 | 决定 | 改动要点 |
|---|---|---|
| `run.py` | **COPY** | Flask 入口, app factory + `app.run()`; 不动 |
| `app/__init__.py` | **COPY** | `create_app(config_class)`; 注册 blueprints (新增 `world_bp` 一行) |
| `app/config.py` | **COPY** | 配置常量; 加 `WORLD_DB_DIR_TEMPLATE / POOL_DB_DIR_TEMPLATE` 两常量 (可选) |
| `app/models/project.py` | **COPY** | 不动 |
| `app/models/task.py` | **COPY** | 不动 |
| `app/api/simulation.py` | **ADAPT** | 加 3 路由 (`inject-event / places / move-agent`); 已有 simulation lifecycle 路由不动 |
| `app/api/world.py` | **NEW** | 新 blueprint: 地点 / 关系 / 能力 / 剧本相关查询与变更 |
| `app/api/graph.py` | **COPY** | Zep graph 查询 |
| `app/api/report.py` | **COPY** | 复盘报告生成 |

## 3. 关键改动 (相对 MiroFish)

### 3.1 `run.py` (COPY)

完全照搬 MiroFish `backend/run.py`。文件位置从 `MiroFish/backend/run.py` 移到 `ramus/run.py` (LAYOUT §1 顶层目录树)。

### 3.2 `app/__init__.py` (COPY + 一行)

`create_app()` 工厂模式不动; 注册 blueprints 时多加一行 `app.register_blueprint(world_bp, url_prefix="/api/v1")`。

### 3.3 `app/config.py` (COPY)

DB / Zep / OpenAI / Anthropic 等环境变量读取不动。可选加两个路径模板:

- `WORLD_DB_PATH_TEMPLATE = "{simulation_dir}/world.db"`
- `POOL_DB_PATH_TEMPLATE = "{simulation_dir}/pools/pool__{place}__{feed}.db"`

### 3.4 `app/models/{project,task}.py` (COPY)

不动。

### 3.5 `app/api/simulation.py` (ADAPT)

现有 `/simulations/...` 路由 (start / stop / status / list / interview / batch-interview / close-env) 全部保留不动 (LAYOUT §7.3 第一行)。新增三个路由委派给 `simulation_ipc`:

```
POST /simulations/<sim_id>/inject-event     -> ipc.send_inject_script_event(sim_id, event)
POST /simulations/<sim_id>/reload-scripts   -> ipc.send_reload_scripts(sim_id, scripts_path)
POST /simulations/<sim_id>/move-agent       -> ipc.send_move_agent(sim_id, agent_id, place_id)
```

### 3.6 `app/api/world.py` (NEW)

新 blueprint, 路径 `/world/...` 或 `/simulations/<sim_id>/...`。提供:

- 实时世界态查询 (`GET /simulations/<sim_id>/world-state`, `GET /simulations/<sim_id>/places`) — 走 `simulation_ipc.send_list_places()` 或直读 `world.db`
- 关系 / 能力查询 — 直读 `world.db.{relation, capability}`
- 剧本相关 — 路由 + payload 校验后调 `simulation_ipc.send_inject_script_event` / `send_reload_scripts`

### 3.7 `app/api/graph.py` (COPY) / `app/api/report.py` (COPY)

Zep graph 查询、复盘报告生成原样复用; `report.py` 调用本项目的 `report_agent`(已 ADAPT, 见 `app_services.md` §3.6) 自动获得跨 DB 复盘能力。

## 4. 核心逻辑

### 4.1 数据结构

- **Project / Task ORM**: SQLAlchemy 模型, MiroFish 原样。
- **Request / Response 格式**: JSON; 错误用 HTTP 4xx + `{"error": "..."}` body。
- **Blueprint 注册顺序**: `simulation_bp` → `world_bp` (新) → `graph_bp` → `report_bp`。

### 4.2 关键流程

```
启动仿真:
  POST /simulations              (api/simulation.py)
  → simulation_config_generator.generate_config(payload)
  → oasis_profile_generator.generate_profiles(...)
  → simulation_manager.start(simulation_id)
  → 200 {"simulation_id": ..., "status": "running"}

注入剧本事件:
  POST /simulations/<id>/inject-event   (api/simulation.py 新)
  → 校验 event JSON (含 id / trigger / effect)
  → simulation_ipc.send_inject_script_event(id, event)
  → 等待 ack (timeout 30s)
  → 200 / 504

列地点:
  GET /simulations/<id>/places          (api/simulation.py 新 OR api/world.py 新)
  → simulation_ipc.send_list_places(id)
  → 等待响应 (含 places + L_t)
  → 200 {"places": [...], "agent_locations": {...}}

强制移动:
  POST /simulations/<id>/move-agent     (api/simulation.py 新)
  → 校验 agent_id / place_id
  → simulation_ipc.send_move_agent(id, agent_id, place_id)
  → 200 / 504

实时世界态 (UI 拉取):
  GET /simulations/<id>/world-state     (api/world.py 新)
  → 直读 world.db (只读连接, READ-ONLY 模式)
  → 200 {"places": [...], "relations": [...], "capabilities": [...], "agent_locations": {...}}

复盘:
  POST /reports/generate                (api/report.py)
  → report_agent.generate_report(sim_id, agent_id, t_range)
  → 200 {"narrative": "...", "raw_traces": [...]}
```

### 4.3 与其他模块的交互

- **上游调用方**: 前端 / 用户 / 集成测试。
- **下游被调方**: `app/services/*` (全部); 通过 services 间接走 `ipc/` 与 `world.db`。
- **共享状态**:
  - `world-state` 路由直读 `world.db` (READ-ONLY 连接, 不影响 runner 单写者 Lock — LAYOUT §9.6 G)
  - 其余路由不直接碰 DB / Zep, 全部走 services

## 5. 暴露 API

### 5.1 公开 class / function 签名 (伪代码)

```python
# run.py
if __name__ == "__main__":
    app = create_app(Config)
    app.run(host=Config.HOST, port=Config.PORT)

# app/__init__.py
def create_app(config_cls: type) -> Flask: ...

# app/config.py
class Config:
    DATABASE_URI: str
    ZEP_API_KEY: str
    ZEP_BASE_URL: str
    OPENAI_API_KEY: str
    ANTHROPIC_API_KEY: str
    SIMULATIONS_DIR: str
    WORLD_DB_PATH_TEMPLATE: str = "{simulation_dir}/world.db"
    POOL_DB_PATH_TEMPLATE: str = "{simulation_dir}/pools/pool__{place}__{feed}.db"

# app/api/simulation.py (新增 3 路由的 view function 签名)
@simulation_bp.route("/simulations/<sim_id>/inject-event", methods=["POST"])
def inject_event(sim_id: str): ...

@simulation_bp.route("/simulations/<sim_id>/reload-scripts", methods=["POST"])
def reload_scripts(sim_id: str): ...

@simulation_bp.route("/simulations/<sim_id>/move-agent", methods=["POST"])
def move_agent(sim_id: str): ...

# app/api/world.py (NEW blueprint)
world_bp = Blueprint("world", __name__)

@world_bp.route("/simulations/<sim_id>/world-state", methods=["GET"])
def get_world_state(sim_id: str): ...

@world_bp.route("/simulations/<sim_id>/places", methods=["GET"])
def list_places(sim_id: str): ...

@world_bp.route("/simulations/<sim_id>/relations", methods=["GET"])
def list_relations(sim_id: str): ...

@world_bp.route("/simulations/<sim_id>/capabilities", methods=["GET"])
def list_capabilities(sim_id: str): ...
```

### 5.2 IPC / Flask / SQL

#### Flask 路由表 (LAYOUT §7.3 完整列出)

| 方法 | 路由 | 来源 | 处理者 |
|---|---|---|---|
| 全部 | `/simulations/...` (start/stop/status/list/interview/batch-interview/close-env) | MiroFish (COPY) | `api/simulation.py` |
| POST | `/simulations/<id>/inject-event` | NEW | `api/simulation.py` → `simulation_ipc.send_inject_script_event` |
| POST | `/simulations/<id>/reload-scripts` | NEW (C2) | `api/simulation.py` → `simulation_ipc.send_reload_scripts` |
| GET  | `/simulations/<id>/places` | NEW | `api/simulation.py` 或 `api/world.py` → `simulation_ipc.send_list_places` |
| POST | `/simulations/<id>/move-agent` | NEW | `api/simulation.py` → `simulation_ipc.send_move_agent` |
| GET  | `/simulations/<id>/world-state` | NEW | `api/world.py` (直读 world.db, READ-ONLY) |
| GET  | `/simulations/<id>/relations` | NEW | `api/world.py` (直读 world.db.relation) |
| GET  | `/simulations/<id>/capabilities` | NEW | `api/world.py` (直读 world.db.capability) |
| 全部 | `/graphs/...` | MiroFish (COPY) | `api/graph.py` |
| 全部 | `/reports/...` | MiroFish (COPY) | `api/report.py` |

#### SQL 输入 / 输出表清单

- **`api/world.py` 直读** (READ-ONLY SQLite 连接):
  - `world.db.place`, `world.db.agent_location`, `world.db.relation`, `world.db.capability`, `world.db.coverage`
- **不直接写**: 所有写操作通过 IPC 路由到 runner 进程, 由 runner 侧的 ActionDispatcher / ScriptEngine 写入。

## 6. 配置入口

从 `app/config.py` (环境变量) 读取:

- `DATABASE_URI` — Flask app 自身的元数据 DB (project / task), 与仿真 `world.db` 无关
- `SIMULATIONS_DIR` — 仿真根目录, 用于解析 `world.db` / `pool_*.db` 路径
- `ZEP_API_KEY / ZEP_BASE_URL` — `api/graph.py` + `api/report.py` 用
- `OPENAI_API_KEY / ANTHROPIC_API_KEY` — services 层用 (本层不直接用)

`simulation_config.json` 顶层字段 `simulation_id` 是 API 路径中的 `<sim_id>` 来源; 其余字段不在 API 层解析, 由 services / runner 处理。

默认值: 见 MiroFish `config.py` 原值。
验证规则: `<sim_id>` 必须为合法 UUID / slug; payload JSON schema 校验在 view function 入口完成。

## 7. 待决策 / 风险

- LAYOUT §9.6 G: `world-state` 路由直读 `world.db` 与 runner 单写者 Lock 是否冲突 — SQLite WAL + READ-ONLY 连接可读, 但 MVP 不依赖 WAL (LAYOUT v0.3 B8); 实测如有读阻塞则改走 IPC `LIST_PLACES` 拉取。
- 新 4 IPC 路由的 timeout: 默认 30s; UI 操作 (move-agent / inject-event) 失败重试由前端处理, 后端不重试。
- `api/world.py` 与 `api/simulation.py` 的边界: 强 IPC 类操作放 `simulation.py` (沿 MiroFish 习惯), 纯查询放 `world.py`; `places` 路由两边都能放, 选择 `simulation.py` 与 LAYOUT §7.3 一致。
- `RELOAD_SCRIPTS` 路由的 payload 校验: `scripts_path` 必须在 `simulation_dir` 下, 防止任意文件读取 (路径白名单)。
