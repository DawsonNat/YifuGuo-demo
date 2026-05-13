# Memory Retrieval 实现文档

> 路径: `agent_world/memory/retrieval.py`
> 对应 LAYOUT §: §2.F retrieval / §6.3 PerceptionBuilder.relevant_memories
> 上游依赖文档: 无 (retrieval 是 PerceptionBuilder 的下游服务)
> 下游依赖文档: 无 (终点是 zep_tools.quick_search)

## 1. 模块定位

按 `graph_id` 列表对 Zep 做并行 edges + nodes 检索，聚合为 PerceptionBuilder 可直接塞进 `Observation.relevant_memories` 的文本片段。是三层记忆从 Zep 拉回 prompt 的唯一通道。

- 输入: 一组 `graph_ids` (典型: `[f"agent_{a}", f"place_{p}"]`，可选附 `"world"`) + query 字符串 + topK
- 输出: 一组按 score 合并去重后的文本片段 (`List[str]`)
- 必须存在的理由: (a) PerceptionBuilder 每轮都要为每个 active agent 检索，必须并行；(b) 三层 graph 来源不同，需要统一聚合层；(c) 复用 MiroFish 已稳定的 quick_search 包装。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| edges + nodes 并行检索模式 | MiroFish | `backend/app/services/oasis_profile_generator.py:286-412` `_search_zep_for_entity` | PATTERN | 并行结构沿用 |
| `quick_search` 调用 | MiroFish | `backend/app/services/zep_tools.py:1237-1270` | KEEP | 直接 import 用 |
| `SearchResult.to_text` 序列化 | MiroFish | `backend/app/services/zep_tools.py:45-54` | KEEP | 输出文本由它生成 |
| `zep_paging` 工具 | MiroFish | `backend/app/utils/zep_paging.py` | KEEP | 大结果集分页 |

## 3. 关键改动 (相对来源仓库)

- **改动 1 (graph_id 参数化)**: MiroFish `_search_zep_for_entity` 单 graph 写死；本项目把 `graph_id` 提到入参，再通过 `asyncio.gather` 并行多 graph + (edges/nodes) 矩阵。
- **改动 2 (聚合 + 去重)**: 多 graph 结果合并时按 (graph_id, edge_or_node_id) 去重；按 score 排序取 topK。
- **改动 3 (provenance 标签)**: 输出文本前缀加 `[from {graph_id}]` 标签，让 LLM 区分来源——避免 agent 把 place 层记忆误认为自己的私人回忆。
- **改动 4 (空结果短路)**: 任一 graph 在 Zep 中尚未创建时，quick_search 抛 NotFound——本模块捕获并视为空集，不传播错误 (新仿真启动期常见)。
- **改动 5 (PerceptionBuilder 集成)**: PerceptionBuilder 调本模块时传 `query = agent.recent_intent`；recent_intent 由 Agent.astep 上一轮的 thought / 上一条 action 文本派生 (具体来源由 PerceptionBuilder 决定，本模块不关心)。

## 4. 核心逻辑

### 4.1 数据结构

```python
@dataclass
class RetrievedMemory:
    graph_id: str
    kind: Literal["edge", "node"]
    text: str               # SearchResult.to_text 输出
    score: float
    metadata: dict          # raw 透传

# 配置
DEFAULT_TOPK_PER_GRAPH = 5
MAX_AGGREGATED = 10
```

不变量:
- `graph_ids` 顺序无关；输出按 score desc
- 同一 (graph_id, item_id) 仅出现一次
- 单 graph 检索失败不影响其他 graph 结果返回

### 4.2 关键流程 / 算法

**主入口:**

```
async def search(graph_ids, query, topk_per_graph=5, max_aggregated=10) -> List[RetrievedMemory]:
    # 矩阵: graph × {edges, nodes}
    tasks = []
    for g in graph_ids:
        tasks.append(_quick_search_safe(g, query, scope='edges', limit=topk_per_graph))
        tasks.append(_quick_search_safe(g, query, scope='nodes', limit=topk_per_graph))
    results = await asyncio.gather(*tasks, return_exceptions=False)
    # 合并 + 去重 + 排序
    bag = []
    for r in results:
        bag.extend(r)        # r 是 List[RetrievedMemory]
    bag = _dedup_by_id(bag)
    bag.sort(key=lambda m: m.score, reverse=True)
    return bag[:max_aggregated]

async def _quick_search_safe(graph_id, query, scope, limit):
    try:
        raw = await zep_tools.quick_search(graph_id=graph_id, query=query,
                                            scope=scope, limit=limit)
        return [RetrievedMemory(graph_id=graph_id, kind=scope[:-1],
                                text=f"[from {graph_id}] " + r.to_text(),
                                score=r.score, metadata=r.raw)
                for r in raw]
    except NotFound:
        return []
```

**PerceptionBuilder 集成 (LAYOUT §6.3):**

```
# perception.py 内伪代码
obs.relevant_memories = await retrieval.search(
    graph_ids=[f"agent_{a.id}", f"place_{obs.self_location}"],
    query=a.recent_intent,
)
```

### 4.3 与其他模块的交互

- 上游调用方:
  - `world/perception.py` PerceptionBuilder.build (每 active agent / 每 micro-tick)
  - 可选: `oasis_profile_generator` 在 LLM 生成 profile 时复用同一个 search (P3 阶段)
- 下游被调方:
  - `zep_tools.quick_search` (Zep SDK 包装)
  - `SearchResult.to_text` (MiroFish 工具)
- 共享状态:
  - 读: Zep 三层 graph (`agent_{id}` / `place_{id}` / `world`)
  - 不写 Zep / world.db / pool_*.db / ChatMemory

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
@dataclass
class RetrievedMemory:
    graph_id: str
    kind: Literal["edge", "node"]
    text: str
    score: float
    metadata: dict

class MultiGraphRetriever:
    def __init__(self, zep_client, world_graphs_cfg): ...

    async def search(
        self,
        graph_ids: List[str],
        query: str,
        topk_per_graph: int = 5,
        max_aggregated: int = 10,
    ) -> List[RetrievedMemory]: ...

    async def search_for_agent(
        self,
        agent_id: int,
        place_id: str,
        query: str,
        include_world: bool = False,
    ) -> List[RetrievedMemory]:
        """语义糖：自动拼 [agent_{id}, place_{place_id}] (+ world 可选)。"""
```

### 5.2 IPC / Flask / SQL

- 不暴露 IPC / Flask 路由
- 不读写任何 SQL 表
- 仅读 Zep (通过 `zep_tools.quick_search`)

## 6. 配置入口

来自 `simulation_config.json.memory_config`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `memory_config.retrieval.topk_per_graph` | 5 | 单 graph × 单 scope (edges/nodes) 拉取上限 |
| `memory_config.retrieval.max_aggregated` | 10 | 聚合后传给 PerceptionBuilder 的最终条数 |
| `memory_config.retrieval.include_world_by_default` | false | search_for_agent 是否默认带 world graph |

## 7. 待决策 / 风险

- (LAYOUT N3 / #8 D 类) 100w agent 时每轮检索量级 = active_agents × graphs × 2 (edges/nodes)；MVP 阶段量级可控，后期需要批量 API 或缓存层。
- `query` 字符串的来源 (recent_intent) 由 PerceptionBuilder 决定，本模块不参与；如 query 质量不佳导致召回率低，问题在调用方。
- Zep `quick_search` 的 latency 是 PerceptionBuilder 主路径的 P95 来源；P5 阶段需要打 metric。
- 同一 agent 在同一 micro-tick 内不重复检索 (PerceptionBuilder 自身保证)；本模块不缓存。
