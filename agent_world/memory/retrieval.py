"""Multi-graph Zep retrieval (L1).

Wraps MiroFish's ``zep_tools.quick_search`` and runs a parallel matrix of
``graph_id`` queries. Output is a flat list of small dicts that the
PerceptionBuilder can drop into ``Observation.relevant_memories``.

LAYOUT v0.3 §2.F retrieval / §6.3 PerceptionBuilder.relevant_memories.

Design notes:
    * The Zep client is duck-typed: callers pass any object exposing
      ``quick_search(graph_id, query, limit) -> SearchResult-like``. For the
      MVP demo we don't import a real SDK -- the wiring is left as TODO.
    * Failures on a single graph (e.g. NotFound on a freshly started
      simulation) collapse to an empty list and never break the gather.
    * Results carry a ``[from {graph_id}]`` provenance prefix so the LLM can
      tell apart agent / place / world memories.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# TODO(L2): once MiroFish's `zep_tools` lands in this repo (or as a vendored
# package), replace the duck-typed `client` arg with:
#     from app.services.zep_tools import quick_search, SearchResult
# For the MVP we keep the import path open and let the caller inject any
# object that exposes ``quick_search``.


# --------------------------------------------------------------------------- #
# data class                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class RetrievedMemory:
    graph_id: str
    content: str            # provenance-prefixed text
    score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "content": self.content,
            "score": self.score,
        }


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _result_text(item: Any) -> str:
    """Extract a text payload from a SearchResult-like item."""
    if isinstance(item, str):
        return item
    for attr in ("text", "content", "fact", "summary"):
        v = getattr(item, attr, None)
        if v:
            return str(v)
    if isinstance(item, dict):
        for k in ("text", "content", "fact", "summary"):
            if item.get(k):
                return str(item[k])
    return str(item)


def _result_score(item: Any) -> float:
    for attr in ("score", "rank", "relevance"):
        v = getattr(item, attr, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    if isinstance(item, dict):
        for k in ("score", "rank", "relevance"):
            if k in item:
                try:
                    return float(item[k])
                except (TypeError, ValueError):
                    pass
    return 0.0


def _iter_results(raw: Any) -> Iterable[Any]:
    """Best-effort iterator over a SearchResult-like value."""
    if raw is None:
        return ()
    for attr in ("results", "edges", "nodes", "items"):
        v = getattr(raw, attr, None)
        if v:
            return v
    if isinstance(raw, dict):
        for k in ("results", "edges", "nodes", "items"):
            if raw.get(k):
                return raw[k]
    if isinstance(raw, (list, tuple)):
        return raw
    return ()


# --------------------------------------------------------------------------- #
# retriever                                                                   #
# --------------------------------------------------------------------------- #


class MultiGraphRetriever:
    """Run parallel ``quick_search`` calls across several Zep graphs."""

    def __init__(self, client: Any, default_top_k: int = 5) -> None:
        # Duck-typed: must expose ``quick_search(graph_id, query, limit)``.
        self._client = client
        self._default_top_k = default_top_k

    async def search(
        self,
        graph_ids: List[str],
        query: str,
        top_k: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Iterate over graph_ids in parallel, merge by score desc.

        Returns a list of small dicts ``{graph_id, content, score}``.
        """
        if not graph_ids or not query:
            return []
        limit = top_k or self._default_top_k

        # Mirrors MiroFish edges/nodes parallel pattern via asyncio.gather
        tasks = [self._search_one(g, query, limit) for g in graph_ids]
        per_graph = await asyncio.gather(*tasks, return_exceptions=False)

        bag: List[RetrievedMemory] = []
        for chunk in per_graph:
            bag.extend(chunk)
        bag.sort(key=lambda m: m.score, reverse=True)
        return [m.to_dict() for m in bag[: limit * len(graph_ids)]]

    async def _search_one(
        self, graph_id: str, query: str, limit: int
    ) -> List[RetrievedMemory]:
        try:
            raw = self._client.quick_search(
                graph_id=graph_id, query=query, limit=limit
            )
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception:
            # NotFound on a fresh graph or any transient failure: empty result.
            return []

        out: List[RetrievedMemory] = []
        for item in _iter_results(raw):
            text = _result_text(item)
            if not text:
                continue
            out.append(
                RetrievedMemory(
                    graph_id=graph_id,
                    content=f"[from {graph_id}] {text}",
                    score=_result_score(item),
                    metadata={"raw": item} if not isinstance(item, str) else {},
                )
            )
        return out


__all__ = ["MultiGraphRetriever", "RetrievedMemory"]
