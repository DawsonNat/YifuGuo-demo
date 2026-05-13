"""``RelationChangeEffect`` —— 增加 / 删除一条关系边。

调 ``world.relations.add`` / ``.remove``；symmetric / mutually_exclusive
等语义由 RelationGraph 内部处理。
"""

from __future__ import annotations

from typing import Any, Literal

from agent_world.script._registrars import EffectBase


class RelationChangeEffect(EffectBase):
    """Add or remove a typed relation edge.

    Args:
        src: Source agent ID.
        dst: Destination agent ID.
        relation_type: Registered relation_type key (e.g. ``"lover"``).
        op: ``"add"`` (default) or ``"remove"``.
    """

    def __init__(
        self,
        *,
        src: int,
        dst: int,
        relation_type: str,
        op: Literal["add", "remove"] = "add",
    ) -> None:
        if op not in ("add", "remove"):
            raise ValueError(f"RelationChangeEffect.op must be 'add' or 'remove', got {op!r}")
        self.src = int(src)
        self.dst = int(dst)
        self.relation_type = str(relation_type)
        self.op = op

    async def apply(self, world: Any) -> None:
        relations = getattr(world, "relations", None)
        if relations is None:
            raise RuntimeError("RelationChangeEffect requires world.relations")
        fn = getattr(relations, self.op, None)
        if fn is None:
            raise RuntimeError(f"world.relations missing '{self.op}' method")
        result = fn(self.src, self.dst, self.relation_type)
        if hasattr(result, "__await__"):
            await result
