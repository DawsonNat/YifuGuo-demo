# Config 层实现文档

> 路径: `agent_world/config/{world_config.py, simulation_config_ext.py, stubs/}`
> 对应 LAYOUT §: §2.I (IPC + Runner + Config) + §7.1 (配置结构) + §10 (conscribe 用法约定)
> 上游依赖文档: 无 (静态配置定义)
> 下游依赖文档: `app_services.md` (`simulation_config_generator` 生成此 schema 实例), `runner.md` (启动时加载), `app_api.md` (路由间接消费)

## 1. 模块定位

Agent World 的配置 schema 层。`world_config.py` 是 places / coverage / events 的 Pydantic schema, 由 conscribe 自动生成 (Tier 1: 每个 effect/trigger/relation_type 类的 `__init__` 签名 + Google docstring 即 schema)。`simulation_config_ext.py` 在 MiroFish 的 `simulation_config_generator` dataclass 顶层加 `world_config / channel_config / memory_config` 三个 key。`stubs/` 放 conscribe 生成的 `.pyi` 文件 (IDE 自动补全), MVP 不接 CI, 本地按需手跑。

输入: `simulation_config.json` 文件 / 内存 dict。
输出: 经过 Pydantic 校验的 `SimulationConfig` 实例; 校验失败抛错 (字段缺失 / 类型错 / discriminator type 不存在)。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `simulation_config_ext.py` (顶层 dataclass) | MiroFish | `backend/app/services/simulation_config_generator.py` (L150-174) | ADAPT | 加 `world_config / channel_config / memory_config` 三 key |
| `world_config.py` (Pydantic schema) | conscribe | conscribe 生成 (Tier 1, LAYOUT §10.3) | NEW (生成器) | 由 5 个 Registrar (effect/trigger/relation_type/capability_type/place_type) 派生 |
| `stubs/*.pyi` | conscribe | `conscribe generate-stubs --layer xxx --output-dir ...` | NEW (生成器) | LAYOUT §10.5; MVP 不接 CI, 本地按需手跑 |

### 2.1 NEW/ADAPT 一览

| 文件 | 决定 | 改动要点 |
|---|---|---|
| `simulation_config_ext.py` | **ADAPT** | MiroFish dataclass 加 3 顶层 key; 复用 MiroFish 现有字段 |
| `world_config.py` | **NEW** | conscribe 自动生成的 Pydantic schema (places/coverage/events) |
| `stubs/*.pyi` | **NEW** | conscribe 自动生成 (effect/trigger/relation_type/capability_type/place_type 5 文件) |

## 3. 关键改动 (相对 MiroFish)

### 3.1 `simulation_config_ext.py` (ADAPT)

MiroFish `simulation_config_generator.py` (L150-174) 的顶层 dataclass 在原有字段基础上加三个 key。LAYOUT §7.1 完整顶层 schema:

```yaml
simulation_id: str
project_id: str
graph_id: str                    # MiroFish 兼容单 graph
world_graphs:                    # 新增
  world: str                     # world graph_id
  per_agent_template: str        # "agent_{id}"
  per_place_template: str        # "place_{id}"
time_config: { ... }             # MiroFish 原样
agent_configs: [ ... ]           # ADAPT: 加 6 字段 (location/relations/capabilities/soul/long_term_goal/current_state)
event_config: { ... }            # MiroFish 原 initial_posts (兼容旧 demo)
world_config:                    # 新增 (来自 conscribe Pydantic schema)
  places: [ ... ]
  coverage: [ ... ]
  events: [ ... ]
channel_config:                  # 新增 (B1.1 + B6 + B9)
  default_delays:
    F2F: 0
    RDC: 1
    GRP: 1
  group_message:
    redeliver_undelivered: true
  failed_attempt_ttl_ticks: 1
  group_event_ttl_ticks: 1
memory_config:                   # 新增 (行为级压缩)
  compressor:
    enabled: true
    model: "claude-haiku-4-5-20251001"
    max_raw_actions: 30
    summary_sentences: "1-3"
  retry_policy:                  # B4 + B9 配套
    parse_error_max_retry: 1
    arg_missing_max_retry: 1
    other_max_retry: 0           # silent
twitter_config / reddit_config: ...   # MiroFish 兼容
```

### 3.2 `world_config.py` (NEW, 由 conscribe 生成)

LAYOUT §10.2 + §10.3 + §10.7. `world_config` 节由 conscribe 5 个 Registrar 派生的 Pydantic schema 校验:

| 子节 | 子类来源 | discriminator | strip_suffixes | 路径 |
|---|---|---|---|---|
| `world_config.places[*].type` | `PlaceTypeRegistrar` | `type` | `["Place"]` | `agent_world/world/place_types/*.py` |
| `world_config.places[*].attrs.timezone` | 约定字段 (B2) | — | — | 直接 string |
| `world_config.places[*].attrs.behavior_hint` | 约定字段 (B5) | — | — | 直接 string |
| `world_config.coverage[*].latency_ticks` | 直接 int | — | — | LAYOUT §3.2 + §7.1 |
| `world_config.events[*].trigger.type` | `TriggerRegistrar` | `type` | `["Trigger"]` | `agent_world/script/triggers/*.py` |
| `world_config.events[*].effect.type` | `EffectRegistrar` | `type` | `["Effect"]` | `agent_world/script/effects/*.py` |
| `agent_configs[*].relations[*].type` | `RelationTypeRegistrar` | `type` | `["Relation"]` | `agent_world/world/relation_types/*.py` |
| `agent_configs[*].capabilities[*].type` | `CapabilityTypeRegistrar` | `type` | `["Capability"]` | `agent_world/world/capability_types/*.py` |

启动期调 `conscribe.discover("agent_world.script.effects")` (以及其他 4 个包) 触发 import → 子类创建 → 自动注册; conscribe 据此生成 Pydantic Discriminated Union, `world_config.py` 是顶层组装文件 (`PlacesConfig`, `CoverageConfig`, `EventsConfig` 三个 Pydantic 模型)。

#### 3.2.1 places 约定字段 (LAYOUT §7.1 + §6.3)

- `place_id: str` — 唯一标识
- `type: str` — discriminator, 落到 `PlaceTypeRegistrar` 注册的子类
- `attrs.timezone: str` — IANA 时区名 (例: `"America/New_York"`); 仅叙事用, 不影响 tick (B2)
- `attrs.behavior_hint: str | None` — 注入 system prompt 第 4 段; None 时该段写 `"(none)"` (B5)
- `attrs.*` — 其他用户自定义字段不限

#### 3.2.2 coverage 字段 (LAYOUT §3.2 + §7.1)

- `src: str` — 源 place_id
- `dst: str` — 目标 place_id
- `latency_ticks: int` — channel delay (B1.1 channel delay 计算源, 默认 0)

#### 3.2.3 events 字段 (LAYOUT §10.2 + §2.E)

- `id: str` — 用户自写 (C2 reload 时去重比对)
- `trigger: { type: str, ... }` — discriminator 路由到 TriggerRegistrar 子类 (AtTime / AtCondition / OnAction / OnDuration)
- `effect: { type: str, ... }` — discriminator 路由到 EffectRegistrar 子类 (Move / RelationChange / CapabilityChange / BroadcastEvent / DialogueInjection / PlaceMutation / **StateChange**)

### 3.3 `stubs/*.pyi` (NEW, 由 conscribe 生成)

LAYOUT §10.5. 本地命令:

```bash
conscribe generate-stubs --layer effect --output-dir agent_world/config/stubs/
conscribe generate-stubs --layer trigger --output-dir agent_world/config/stubs/
conscribe generate-stubs --layer relation_type --output-dir agent_world/config/stubs/
conscribe generate-stubs --layer capability_type --output-dir agent_world/config/stubs/
conscribe generate-stubs --layer place_type --output-dir agent_world/config/stubs/
```

输出 5 个 `.pyi` 文件给 IDE 做参数自动补全。MVP 不接 pre-commit / CI 校验漂移 (LAYOUT §9.6 B); 后期 D 类讨论再决定。

## 4. 核心逻辑

### 4.1 数据结构

```python
# simulation_config_ext.py 顶层 (扩展自 MiroFish)
@dataclass
class SimulationConfig:
    simulation_id: str
    project_id: str
    graph_id: str
    world_graphs: WorldGraphsConfig         # 新
    time_config: TimeConfig
    agent_configs: list[AgentConfig]        # ADAPT: 加 6 字段
    event_config: EventConfig               # MiroFish 兼容
    world_config: WorldConfig               # 新 (Pydantic, conscribe 生成)
    channel_config: ChannelConfig           # 新
    memory_config: MemoryConfig             # 新
    twitter_config: TwitterConfig | None    # 兼容
    reddit_config: RedditConfig | None      # 兼容

# world_config.py (conscribe 生成, 简略)
class WorldConfig(BaseModel):
    places: list[PlaceConfig]
    coverage: list[CoverageEdge]
    events: list[EventConfig]

class PlaceConfig(BaseModel):
    place_id: str
    type: str                         # discriminator (PlaceTypeRegistrar)
    attrs: PlaceAttrs

class PlaceAttrs(BaseModel):
    timezone: str
    behavior_hint: str | None = None
    # 其他自定义字段开放 (extra="allow")

class CoverageEdge(BaseModel):
    src: str
    dst: str
    latency_ticks: int = 0

class EventConfig(BaseModel):
    id: str
    trigger: TriggerConfig            # Discriminated Union (TriggerRegistrar)
    effect: EffectConfig              # Discriminated Union (EffectRegistrar)

class ChannelConfig(BaseModel):
    default_delays: dict[str, int]    # {"F2F": 0, "RDC": 1, "GRP": 1}
    group_message: GroupMessageConfig
    failed_attempt_ttl_ticks: int = 1
    group_event_ttl_ticks: int = 1

class GroupMessageConfig(BaseModel):
    redeliver_undelivered: bool = True

class MemoryConfig(BaseModel):
    compressor: CompressorConfig
    retry_policy: RetryPolicyConfig

class CompressorConfig(BaseModel):
    enabled: bool = True
    model: str = "claude-haiku-4-5-20251001"
    max_raw_actions: int = 30
    summary_sentences: str = "1-3"

class RetryPolicyConfig(BaseModel):
    parse_error_max_retry: int = 1
    arg_missing_max_retry: int = 1
    other_max_retry: int = 0
```

### 4.2 关键流程

```
启动期 (子进程 runner / Flask 进程通用):
  1. conscribe.discover("agent_world.script.effects")        # import 触发 EffectBase 子类注册
  2. conscribe.discover("agent_world.script.triggers")
  3. conscribe.discover("agent_world.world.relation_types")
  4. conscribe.discover("agent_world.world.capability_types")
  5. conscribe.discover("agent_world.world.place_types")
  6. SimulationConfig.parse_file("simulation_config.json")
       -> Pydantic 自动校验:
            * 顶层字段缺失 -> ValidationError
            * world_config.events[*].trigger.type 不在注册表 -> ValidationError
            * world_config.events[*].effect.type 不在注册表 -> ValidationError
            * latency_ticks < 0 -> ValidationError (Annotated[int, Field(ge=0)])
            * id 重复 -> 由 ScriptEngine.load 二次校验

配置层覆盖优先级 (B1.1 channel delay):
  1. coverage[src→dst].latency_ticks (具体, 最优先)
  2. channel_config.default_delays[channel_type]
  3. fallback 0 (F2F 默认)

热加载 (C2):
  IPC RELOAD_SCRIPTS -> ScriptEngine.reload_from_yaml(path)
    -> 重新 Pydantic 校验 events 节
    -> 比对 loaded_event_ids: 新 id 加入; 已存在跳过; 过期 id (trigger.t <= world.t) 忽略 + warn
```

### 4.3 与其他模块的交互

- **上游调用方**:
  - `simulation_config_generator.py` (LLM 生成 → 序列化为 JSON 文件)
  - `runner/run_agent_world_simulation.py` (启动时 parse_file)
  - `script/loader.py` (events 节)
- **下游被调方**:
  - `agent_world/world/{place_store, relation_graph, capability_table}.py` (各自从 `WorldConfig.places / agent_configs[*].relations / agent_configs[*].capabilities` 读取)
  - `agent_world/buses/*.py` (从 `channel_config` 读 delay)
  - `agent_world/memory/{compressor, dispatcher}.py` (从 `memory_config` 读)
  - `agent_world/script/engine.py` (从 `world_config.events` 读)
- **共享状态**: 启动期一次性加载到内存; 仅 `script.events` 节支持热加载 (IPC `RELOAD_SCRIPTS`); 其他子节运行时不变。

## 5. 暴露 API

### 5.1 公开 class / function 签名 (伪代码)

```python
# simulation_config_ext.py
@dataclass
class SimulationConfig: ...   # 见 §4.1

def load_simulation_config(path: str) -> SimulationConfig: ...
def dump_simulation_config(config: SimulationConfig, path: str) -> None: ...

# world_config.py (Pydantic, conscribe 生成 + 手写顶层装配)
class WorldConfig(BaseModel): ...
class PlaceConfig(BaseModel): ...
class PlaceAttrs(BaseModel): ...
class CoverageEdge(BaseModel): ...
class EventConfig(BaseModel): ...
class ChannelConfig(BaseModel): ...
class MemoryConfig(BaseModel): ...

# 工具函数
def resolve_channel_delay(
    src: str, dst: str, channel_type: str,
    coverage: list[CoverageEdge], default_delays: dict[str, int]
) -> int:
    """B1.1 配置层覆盖优先级实现"""
    ...
```

### 5.2 IPC / Flask / SQL

- **IPC**: `RELOAD_SCRIPTS` 命令触发 `world_config.events` 节重读 (C2); 详见 `ipc_layer.md` §3.2。
- **Flask**: 不直接暴露; `app/api/simulation.py` 通过 `simulation_config_generator` 生成实例。
- **SQL**: 不直接读写; 但 `world_config.places` / `coverage` 在 runner 启动时被写入 `world.db.{place, coverage}` (LAYOUT §3.2)。

## 6. 配置入口

本模块本身就是配置入口。`simulation_config.json` 顶层完整字段在 §3.1 已列出, 严格匹配 LAYOUT §7.1。

#### 默认值

- `channel_config.default_delays`: `{"F2F": 0, "RDC": 1, "GRP": 1}`
- `channel_config.failed_attempt_ttl_ticks`: 1
- `channel_config.group_event_ttl_ticks`: 1
- `channel_config.group_message.redeliver_undelivered`: true
- `memory_config.compressor.enabled`: true
- `memory_config.compressor.model`: `"claude-haiku-4-5-20251001"`
- `memory_config.compressor.max_raw_actions`: 30
- `memory_config.compressor.summary_sentences`: `"1-3"`
- `memory_config.retry_policy`: `{1, 1, 0}`
- `coverage[*].latency_ticks`: 0 (省略时)

#### 验证规则 (Pydantic, conscribe Tier 1 + 手写 Tier 2)

- `place_id` 在 `places` 中唯一
- `coverage[*].src / dst` 必须在 `places[*].place_id` 中存在
- `coverage[*].latency_ticks >= 0`
- `events[*].id` 在 `events` 中唯一; `trigger.type / effect.type` 必须在 conscribe 注册表中
- `agent_configs[*].location` 必须在 `places[*].place_id` 中存在
- `agent_configs[*].relations[*].dst_agent` 必须在 `agent_configs[*].agent_id` 中存在
- `memory_config.compressor.max_raw_actions >= 1`
- `time_config.max_ticks >= 1`

## 7. 待决策 / 风险

- **LAYOUT §9.6 B**: stub CI 不接 (MVP); 本地手跑 `conscribe generate-stubs`。后期 D 类讨论是否接 pre-commit + 漂移检测。
- **conscribe `discover` 时机**: 必须在 `SimulationConfig.parse_file` 之前调用, 否则 discriminator 注册表为空导致校验失败。runner 入口 + Flask `create_app()` 都要在最早处调用 (建议 `app/__init__.py` 顶部 + `runner/run_agent_world_simulation.py main` 顶部)。
- **C2 RELOAD_SCRIPTS 部分失败**: 单 event 校验失败时, 整次 reload 视为失败 (返回错误), 不做半成功; 由 client 端重试。
- **MiroFish `event_config.initial_posts` 与 `world_config.events` 重叠**: MVP 双轨并存兼容旧 demo; runner 启动时把 `event_config.initial_posts` 译成等价的 `world_config.events[*]` (`type=at_time, t=0` + `type=create_post`) 注入 ScriptEngine。后期 D 类讨论是否废弃 `event_config`。
- **Tier 1 → Tier 2 升级时机**: MVP 仅靠 `__init__` 签名 + Google docstring; 当某 effect/trigger 需要范围约束 (例如 `latency_ticks > 0`) 时, 升 Tier 2 (`Annotated[int, Field(gt=0)]`)。LAYOUT §10.3 已规定 5 层从 Tier 1 起步。
