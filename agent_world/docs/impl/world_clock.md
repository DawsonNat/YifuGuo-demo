# Clock 实现文档

> 路径: `agent_world/world/clock.py`
> 对应 LAYOUT §: §2.A Clock（表行 206）/ §6.1 步骤 11（`world.t += 1`）/ §7.1 channel_config.default_delays / §9.5 #2（B2 决议：单全局 Clock）
> 上游依赖文档: 无（基础设施层；不依赖项目内其他文档）
> 下游依赖文档: `world_state.md`（`WorldState.clock` 持引用，`world.t` property 委托）, `world_step.md`（步骤 11 调 `clock.advance()`；shuffle 用 `world.t` 作 seed；步骤 5 / 7 / 9 都按 `t` 过滤 SQL）, `perception.md`（`arrive_at <= t`、`occurred_at == t-1`、`attempted_at == t-1` 等过滤条件）, `buses_remote_message.md`（`arrive_at = t + delay`）, `buses_group_message.md`（同上）, `script_engine.md`（`AtTime` trigger 比较 `t`）, `memory_compressor.md`（segment.RawEntry.t 时间戳）

## 1. 模块定位

`Clock` 是 Agent World 的**单全局模拟时间**——整个世界共享一个 `t: int`，每轮 `WorldStep` 末尾 `t += 1`。它不存储任何 wall-clock 时间，也不区分地点 / 时区——LAYOUT §9.5 #2（B2 决议）明确**单全局 Clock**：`place.attrs.timezone: str` 仅叙事用、不影响 tick；通道延迟由 `coverage.latency_ticks` 或 `channel_config.default_delays` 提供，不再引入每地点独立时钟。

- **输入**：仅在启动时构造（`Clock(t0=0)`）。
- **输出**：`clock.t` 被全局读，`clock.advance()` 被 `WorldStep` 在每轮末调用。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `Clock` 类（单全局 `t` + `advance()`） | OASIS | `vendor/oasis/oasis/clock/clock.py` | KEEP | LAYOUT §2.A 表行 206 / §4 表行 425 都标记为 KEEP（原样复用） |

## 3. 关键改动 (相对 OASIS `oasis/clock/clock.py`)

**无改动**——OASIS 原 Clock 的语义（单全局 `t`，每步 `+= 1`）正好契合 B2 决议。

LAYOUT §2.A 表行 206 备注："无（保持单全局 `t`）"。本项目仅 `import` 即可，不在 `agent_world/world/clock.py` 重新实现，保持 thin re-export 即可（或直接在 `WorldState.load` 时 `from oasis.clock.clock import Clock`）。

> **设计说明**：明确**不**支持以下需求（防止后期被错误扩展）：
> - 每地点独立 Clock（B2 否决）
> - wall-clock 同步（`t` 是无量纲整数 tick）
> - 时区感知的相对时间（`place.attrs.timezone` 仅在 prompt 里给 LLM 看，引擎不消费）

## 4. 核心逻辑

### 4.1 数据结构

```
class Clock:
    t: int      # 单调递增；初始 0；语义是"当前正在执行的轮次"
```

**不变量**：
- `t >= 0`
- `t` 仅由 `WorldStep._phase_c_lockstep_post` 步骤 11 调 `advance()` 推进；其他模块**只读**
- 一轮 `WorldStep.run_one_tick()` 期间 `t` 不变；步骤 11 才 `+= 1`（这是 micro-tick 内 `arrive_at = t` 的 F2F 立即可见性的前提，LAYOUT §6.1 行 521-522）

### 4.2 关键流程 / 算法

`Clock` 是被动数据对象，自身只有两个原子操作：

```
read()      -> int          # 直接返回 self.t
advance()   -> int          # self.t += 1; return self.t
```

**典型读取场景**（其他模块怎么用 `world.clock.t`）：

```
# 1. WorldStep.run_one_tick 开头取 t
t = world.clock.t

# 2. 同地点 shuffle 用 t 作 seed（LAYOUT §6.1 步骤 6，可复现性）
random.Random(world.clock.t).shuffle(agents)

# 3. Bus 写 arrive_at（LAYOUT §6.1 步骤 7 / §6.2）
INSERT direct_message(... attempted_at=world.clock.t,
                          arrive_at=world.clock.t + channel_delay)

# 4. PerceptionBuilder 过滤 incoming（LAYOUT §6.3）
SELECT * FROM direct_message
WHERE recipient=? AND delivered=1 AND arrive_at <= world.clock.t
                                  AND arrive_at >  agent.last_message_seen_at

# 5. PerceptionBuilder 过滤 1 轮透传字段
WHERE attempted_at == world.clock.t - 1     # B9 obs.recent_failed_attempts
WHERE occurred_at  == world.clock.t - 1     # B6 obs.group_events
WHERE attempted_at >= world.clock.t - 1     # obs.overheard

# 6. ScriptEngine.due_events 比较 trigger.t 与 world.clock.t（AtTime trigger）

# 7. memory.segment.RawEntry(t=world.clock.t, kind=..., payload=...)

# 8. WorldStep 步骤 11 推进
world.clock.advance()    # 等价 world.t += 1
```

**通道延迟的查询路径**（LAYOUT §7.1 配置层覆盖优先级）：

```
def channel_delay(world, src_place, dst_place, channel_type) -> int:
    # 优先级 1: coverage.latency_ticks（具体 src→dst）
    if (src_place, dst_place) in world.places.coverage:
        return world.places.coverage[(src_place, dst_place)].latency_ticks
    # 优先级 2: channel_config.default_delays[channel_type]
    return config.channel_config.default_delays[channel_type]
    # 优先级 3 (fallback): 0（F2F 默认）
```

注意 `channel_delay` 不属于 Clock（它是 ConnectivityResolver 或 Bus 的职责），但因为它**直接消费 `t`**，本节列出以澄清边界——Clock 自身不知道任何 delay；它只提供 `t`。

### 4.3 与其他模块的交互

- **上游调用方**：
  - `WorldStep._phase_c_lockstep_post`（写：调用 `advance()`，**唯一写者**）
  - `WorldState.t` property（只读，委托 `self.clock.t`）
  - 其他所有模块（只读 `world.clock.t` 或 `world.t`）
- **下游被调方**：无。`Clock` 是叶子节点。
- **共享状态**：无。`Clock` 是纯内存对象，**不**持久化到任何 DB——崩溃恢复时由 runner 从 `world.db` 最新 `script_event_log.triggered_at` 或 `direct_message.attempted_at` 的 max 推算 `t0`，作为 `Clock(t0=...)` 的初值（MVP 可不做，仅支持 fresh start）。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class Clock:
    t: int

    def __init__(self, t0: int = 0) -> None: ...

    def advance(self, delta: int = 1) -> int:
        """推进 t；返回推进后的 t。仅 WorldStep 调用。"""
        ...
```

> 实现可能直接 `from oasis.clock.clock import Clock as _OasisClock`，再 `Clock = _OasisClock` 做 re-export；或薄薄包一层。LAYOUT §2.A 表行 206 / §4 表行 425 都明确 KEEP。

### 5.2 IPC / Flask / SQL

- 不暴露 IPC / Flask 路由。
- 不读写任何 SQL 表（不持久化）。

## 6. 配置入口

从 `simulation_config.json`（LAYOUT §7.1）：

- 不直接读 `Clock` 自己的配置（`Clock(t0=0)` 是默认）。
- **相关但不属于 Clock 的字段**（由其他模块读取消费 `t`）：
  - `time_config.{tick_interval, total_ticks}` → runner 用，Clock 不消费 `tick_interval`（无 wall-clock）
  - `channel_config.default_delays.{F2F: 0, RDC: 1, GRP: 1}` → ConnectivityResolver / Bus 读
  - `world_config.coverage[*].latency_ticks` → 同上
  - `place.attrs.timezone: str` → 仅 PerceptionBuilder 拼 `obs.location_attrs` 时塞进 prompt（叙事用，**不影响 tick**）

**默认值**：
- `t0 = 0`

**验证规则**：
- 由 runner 在 `Clock` 之外校验：`channel_config.default_delays.F2F == 0`、`default_delays.RDC >= 1`（见 `world_step.md` §6 配置入口）

## 7. 待决策 / 风险

- **B2 已决**（LAYOUT §9.5 #2）：单全局 Clock；不引入每地点独立时钟。本模块设计已固化此决议。
- **N5**（LAYOUT §9.5.1）：`arrive_at` 字段对 OASIS Platform 的兼容性。已决议——仅 `world.db.direct_message` 有 `arrive_at`，pool_*.db 的 trace 不受影响；Clock 本身不参与，但通道延迟语义依赖 `t` 严格单调。
- **崩溃恢复**：MVP 仅支持 fresh start（`t0=0`）；如需中途恢复，需要从 `world.db.script_event_log.triggered_at` / `direct_message.attempted_at` 的 max 推算 `t0` 并跳过已 applied 的 effect。开放项，与 `script_engine.md` 的 `applied_events` 持久化方案绑定。
- **风险**：因为 Clock 是被多个 module 同时读的全局变量，任何模块若错误地缓存 `t` 并跨 micro-tick 使用，会导致 `arrive_at` 计算错位。约定：**所有 SQL 写都现读 `world.clock.t`**，不缓存（除非在同 micro-step 内）。本模块本身无法强制此约定，需在代码评审中把控。
- **风险**：若未来需要支持 `time.sleep(tick_interval)` 等 wall-clock 节流，应放在 runner 而非 Clock；保持 Clock 纯整数语义。
