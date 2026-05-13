# Action Translator 实现文档

> 路径: `agent_world/memory/translator.py`
> 对应 LAYOUT §: §2.F translator + 6 个新 action 翻译
> 上游依赖文档: 无 (translator 是纯函数层，不依赖其他 memory 模块)
> 下游依赖文档: `memory_segment.md` (segment 内 RawEntry 拼装时调本模块), `memory_compressor.md` (compressor 拼 raw log prompt 时调本模块)

## 1. 模块定位

把 ActionDispatcher 接受到的结构化 action / 跨模块事件 (relation 变更、capability 变更、状态变更等) 翻译为**单行自然语言文本**。该文本不直接写 Zep；它有两个消费者:

1. `segment.py` 内 RawEntry 的 `payload` 字段——保留单行表征，方便后续 LLM 阅读
2. `compressor.py` 拼 Haiku 摘要 prompt 时把若干 RawEntry 文本拼成 raw log

- 输入: action_type + 参数 dict + 上下文 (sender_id, target_id, t, place_id 等)
- 输出: 一行 (可选含 `\n`) 自然语言字符串
- 必须存在的理由: 让 raw log / 摘要 prompt 的输入与 Zep episode 文本格式统一；隔离 LLM 对 OASIS ActionType enum 的直接耦合。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `to_episode_text()` 大派发 | MiroFish | `backend/app/services/zep_graph_memory_updater.py:35-199` | PATTERN | 框架借鉴；不再直接写 Zep |
| 12 个 OASIS FEED action 模板 | MiroFish | 同上 | EDIT | 句式微调，参数对齐 OASIS fork 后字段 |
| Time / agent_id 句首格式 | MiroFish | 同上 | KEEP | `[t={t}] agent {a} ...` |

## 3. 关键改动 (相对来源仓库)

- **改动 1 (职责调整)**: 不再直接写 Zep。MiroFish 版 `to_episode_text` 翻译完即推 buffer；本项目把翻译产物返回给 segment / compressor 做二次组合。Translator 是无副作用纯函数。
- **改动 2 (新增 6 个 action 翻译)**:
  - `SPEAK_TO_LOCAL(content, place_id)` → `[t=N] agent A said in {place} to co-located: "{content}"`
  - `SEND_MESSAGE(target, content, channel='RDC')` → `[t=N] agent A sent a remote message to agent T: "{content}"`
  - `REQUEST_MOVE(target_place, reason?)` → `[t=N] agent A requested to move from {old} to {new_place} (reason: {reason})`
  - `RELATION_CHANGE(src, dst, relation_type, op)` → `[t=N] relation {type} {add|remove} between agent {src} and agent {dst}`
  - `CAPABILITY_CHANGE(agent, capability, op)` → `[t=N] capability {cap} {granted|revoked} for agent {a}`
  - `UPDATE_STATE(new_state)` → `[t=N] agent A updated current_state to: "{new_state}"`
- **改动 3**: 群聊 4 个枚举 (CREATE_GROUP / JOIN_GROUP / LEAVE_GROUP / SEND_TO_GROUP) 增补翻译；MiroFish 群聊代码已删，模板按 LAYOUT §2.C 的 GroupMessageBus 字段填。
- **改动 4**: 失败 action (`delivered=0`) 不进 segment、不调 translator——B9 规定失败仅在 `obs.recent_failed_attempts` 透传 1 轮，不进 ChatMemory / Zep / segment。
- **改动 5**: 句首统一 `[t=N]` 时间戳格式，便于 compressor 拼 prompt 时给 Haiku 一致的时间线索。

## 4. 核心逻辑

### 4.1 数据结构

```python
class TranslateContext(TypedDict, total=False):
    t: int                    # world.t
    sender_id: int            # 主体 agent
    target_id: int | None     # 接收方 (RDC / GRP / RELATION_CHANGE)
    place_id: str | None      # 发生地点
    extra: dict               # 类型特定字段 (content / capability / new_state / ...)

# 派发表
_TRANSLATORS: Dict[ActionType, Callable[[TranslateContext], str]]
```

不变量:
- 输出始终是单行字符串 (不允许内嵌真实换行；多字段段用 ` | ` 分隔)
- 时间戳前缀 `[t=N]` 总是出现，便于 compressor 排序
- 翻译函数纯函数，无 I/O，无 logging

### 4.2 关键流程 / 算法

**主入口:**

```
def translate(action_type: ActionType, ctx: TranslateContext) -> str:
    fn = _TRANSLATORS.get(action_type)
    if fn is None:
        return f"[t={ctx['t']}] agent {ctx['sender_id']} did {action_type.value} (no translator)"
    return fn(ctx)
```

**6 个新 action 翻译 (示例):**

```
def _t_speak_to_local(ctx):
    return (f"[t={ctx['t']}] agent {ctx['sender_id']} said in "
            f"{ctx['place_id']} to co-located: \"{ctx['extra']['content']}\"")

def _t_update_state(ctx):
    return (f"[t={ctx['t']}] agent {ctx['sender_id']} "
            f"updated current_state to: \"{ctx['extra']['new_state']}\"")

# 其余 4 个同模板
```

**FEED 类沿用 MiroFish:**

```
def _t_create_post(ctx):
    return (f"[t={ctx['t']}] agent {ctx['sender_id']} posted on "
            f"{ctx['extra']['feed']}: \"{ctx['extra']['content']}\"")
# 其他 LIKE / FOLLOW / COMMENT 同
```

### 4.3 与其他模块的交互

- 上游调用方:
  - `segment.append(agent_id, raw_entry)` 内部调 `translator.translate(...)` 生成 `payload.text`
  - `compressor._build_prompt(segment)` 把 segment 中所有 RawEntry 的 text 字段 join 成 raw log
- 下游被调方: 无 (纯函数)
- 共享状态: 不读写 world.db / pool_*.db / Zep / ChatMemory；只读传入 ctx

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class TranslateContext(TypedDict, total=False):
    t: int
    sender_id: int
    target_id: int | None
    place_id: str | None
    extra: dict

def translate(action_type: ActionType, ctx: TranslateContext) -> str:
    """把一条 action / 事件翻译为单行自然语言。"""

def translate_event(event_kind: str, payload: dict) -> str:
    """非 action 事件翻译入口 (relation_change / capability_change / state_change /
    group_event / overhear)。event_kind 是字符串 key，独立于 ActionType enum。"""

# 注册器 (供 conscribe 风格扩展，不强求 MVP 用)
def register_translator(action_type: ActionType, fn): ...
```

### 5.2 IPC / Flask / SQL

- 不暴露 IPC / Flask
- 不读写任何 SQL 表
- 不直接调 Zep；不直接调 ChatMemory；不直接调 world.db.direct_message

## 6. 配置入口

无显式配置；翻译模板在源码中定义。如未来允许多语言模板，再加 `memory_config.translator.locale`。

## 7. 待决策 / 风险

- 翻译模板硬编码在 .py 中；如需国际化或剧本作者改文案，后期可改为模板文件 + 占位符替换 (MVP 不做)。
- 失败 action 是否需要专门翻译进入 ChatMemory？目前**否** (B9 决议)；`recent_failed_attempts` 仅 1 轮 obs 透传，不进 segment / 不调 translator。
- 当 OASIS fork 内增加新 ActionType 但 translator 漏注册时，落到默认 fallback (`did {action_type} (no translator)`)；不抛错，避免 dispatcher 中断。仅在 P5 阶段加 lint 校验注册完备性。
