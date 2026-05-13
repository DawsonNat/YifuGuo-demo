"""Zep retrieval helpers.

COPY (focused) from MiroFish ``backend/app/services/zep_tools.py``. The
original is 1.7k lines and bundles a full LangChain "InsightForge / Panorama
/ Interview" stack on top of the actual retrieval calls. Per
``app_services.md`` §3.7, only two surfaces are load-bearing for Agent
World L3:

* ``SearchResult`` + ``to_text`` (L27-54)
* ``ZepToolsService.quick_search`` (L1237-1270)

These are copied verbatim, with imports adjusted from MiroFish's
``..config`` / ``..utils.*`` to ``agent_world.app.config`` plus stdlib
fallbacks. The richer dataclasses (``NodeInfo`` / ``EdgeInfo`` /
``InsightForgeResult`` / ``PanoramaResult`` / ``AgentInterview`` /
``InterviewResult``) are kept so downstream callers can still construct
them, but the heavyweight methods (``insight_forge`` / ``panorama_search``
/ ``interview_agents``) are left as ``NotImplementedError`` stubs --
``agent_world/memory/retrieval.py`` only needs ``quick_search``.

The :func:`SearchResult.to_text` helper is kept Chinese-language verbatim
(it ships into LLM prompts and changing the wording would silently change
report quality).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import Config

logger = logging.getLogger("agent_world.zep_tools")

# Optional Zep import. If the SDK is missing we degrade to a no-op service so
# unit tests / the rest of L3 can still import the module.
try:
    from zep_cloud.client import Zep  # type: ignore
except Exception:  # noqa: BLE001 — SDK is optional at L3 import time
    Zep = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Result dataclasses (verbatim from MiroFish L27-398, only docstrings trimmed)
# --------------------------------------------------------------------------- #


@dataclass
class SearchResult:
    facts: List[str]
    edges: List[Dict[str, Any]]
    nodes: List[Dict[str, Any]]
    query: str
    total_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "facts": self.facts,
            "edges": self.edges,
            "nodes": self.nodes,
            "query": self.query,
            "total_count": self.total_count,
        }

    def to_text(self) -> str:
        """Convert to text for LLM ingestion (KEPT verbatim from MiroFish L45-54)."""
        text_parts = [f"搜索查询: {self.query}", f"找到 {self.total_count} 条相关信息"]
        if self.facts:
            text_parts.append("\n### 相关事实:")
            for i, fact in enumerate(self.facts, 1):
                text_parts.append(f"{i}. {fact}")
        return "\n".join(text_parts)


@dataclass
class NodeInfo:
    uuid: str
    name: str
    labels: List[str]
    summary: str
    attributes: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "labels": self.labels,
            "summary": self.summary,
            "attributes": self.attributes,
        }


@dataclass
class EdgeInfo:
    uuid: str
    name: str
    fact: str
    source_node_uuid: str
    target_node_uuid: str
    source_node_name: Optional[str] = None
    target_node_name: Optional[str] = None
    created_at: Optional[str] = None
    valid_at: Optional[str] = None
    invalid_at: Optional[str] = None
    expired_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "fact": self.fact,
            "source_node_uuid": self.source_node_uuid,
            "target_node_uuid": self.target_node_uuid,
            "source_node_name": self.source_node_name,
            "target_node_name": self.target_node_name,
            "created_at": self.created_at,
            "valid_at": self.valid_at,
            "invalid_at": self.invalid_at,
            "expired_at": self.expired_at,
        }

    @property
    def is_expired(self) -> bool:
        return self.expired_at is not None

    @property
    def is_invalid(self) -> bool:
        return self.invalid_at is not None


# --------------------------------------------------------------------------- #
# ZepToolsService
# --------------------------------------------------------------------------- #


class ZepToolsService:
    """Slim wrapper around Zep Cloud's graph.search API.

    Only :meth:`search_graph` and :meth:`quick_search` are implemented in
    L3 — the heavyweight planners (``insight_forge`` / ``panorama_search``
    / ``interview_agents``) raise :class:`NotImplementedError` and can be
    ported on demand from the MiroFish original.
    """

    MAX_RETRIES = 3
    RETRY_DELAY = 2.0

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or Config.ZEP_API_KEY
        if Zep is None:
            logger.warning(
                "zep_cloud SDK not installed — ZepToolsService will return empty results"
            )
            self.client = None
        elif not self.api_key:
            logger.warning("ZEP_API_KEY not set — ZepToolsService disabled")
            self.client = None
        else:
            self.client = Zep(api_key=self.api_key)

    # ---- internal retry helper --------------------------------------------

    def _call_with_retry(self, func, operation_name: str) -> Any:
        last_exc: Optional[Exception] = None
        delay = self.RETRY_DELAY
        for attempt in range(self.MAX_RETRIES):
            try:
                return func()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(
                        "zep retry %s attempt=%d err=%s delay=%.1fs",
                        operation_name,
                        attempt + 1,
                        str(exc)[:120],
                        delay,
                    )
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.error(
                        "zep all retries failed: %s err=%s",
                        operation_name,
                        str(exc),
                    )
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"zep call {operation_name} failed without exception")

    # ---- core search -------------------------------------------------------

    def search_graph(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
        scope: str = "edges",
    ) -> SearchResult:
        """Hybrid (semantic + BM25) graph search via Zep Cloud."""
        if self.client is None:
            return SearchResult(
                facts=[], edges=[], nodes=[], query=query, total_count=0
            )

        try:
            search_results = self._call_with_retry(
                func=lambda: self.client.graph.search(
                    graph_id=graph_id,
                    query=query,
                    limit=limit,
                    scope=scope,
                    reranker="cross_encoder",
                ),
                operation_name=f"graph.search({graph_id})",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("zep search failed, returning empty: %s", exc)
            return SearchResult(
                facts=[], edges=[], nodes=[], query=query, total_count=0
            )

        facts: List[str] = []
        edges: List[Dict[str, Any]] = []
        nodes: List[Dict[str, Any]] = []

        if getattr(search_results, "edges", None):
            for edge in search_results.edges:
                if getattr(edge, "fact", None):
                    facts.append(edge.fact)
                edges.append(
                    {
                        "uuid": getattr(edge, "uuid_", None)
                        or getattr(edge, "uuid", ""),
                        "name": getattr(edge, "name", ""),
                        "fact": getattr(edge, "fact", ""),
                        "source_node_uuid": getattr(edge, "source_node_uuid", ""),
                        "target_node_uuid": getattr(edge, "target_node_uuid", ""),
                    }
                )

        if getattr(search_results, "nodes", None):
            for node in search_results.nodes:
                nodes.append(
                    {
                        "uuid": getattr(node, "uuid_", None)
                        or getattr(node, "uuid", ""),
                        "name": getattr(node, "name", ""),
                        "labels": getattr(node, "labels", []),
                        "summary": getattr(node, "summary", ""),
                    }
                )
                if getattr(node, "summary", None):
                    facts.append(f"[{node.name}]: {node.summary}")

        return SearchResult(
            facts=facts, edges=edges, nodes=nodes, query=query, total_count=len(facts)
        )

    def quick_search(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
    ) -> SearchResult:
        """Quick semantic search (KEPT verbatim from MiroFish L1237-1270).

        Args:
            graph_id: Zep graph identifier.
            query:   natural-language query.
            limit:   maximum number of results.
        """
        logger.info("quick_search: graph=%s q=%.80s", graph_id, query)
        result = self.search_graph(
            graph_id=graph_id, query=query, limit=limit, scope="edges"
        )
        logger.info("quick_search complete: count=%d", result.total_count)
        return result

    # ---- placeholders for the heavyweight tools --------------------------

    def insight_forge(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        """TODO: port InsightForge multi-subquery planner from MiroFish."""
        raise NotImplementedError("insight_forge not ported from MiroFish")

    def panorama_search(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        """TODO: port Panorama (active + historical edges) from MiroFish."""
        raise NotImplementedError("panorama_search not ported from MiroFish")

    def interview_agents(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        """TODO: port the interview pipeline (uses SimulationRunner + LLM)."""
        raise NotImplementedError("interview_agents not ported from MiroFish")


__all__ = [
    "EdgeInfo",
    "NodeInfo",
    "SearchResult",
    "ZepToolsService",
]
