"""``BroadcastEventEffect`` —— 写一条 system message 到 ``direct_message``。

用户规范：``BroadcastEventEffect(message, scope="world"|"place")``；
作为 system 来源写进 ``world.db.direct_message``（LAYOUT B7 / direct
message 表附 ``arrive_at / attempted_at / delivered`` 字段）。MVP 阶段
对接 ``world.world_db.insert_direct_message`` 这一稳定 API；不存在时
回退到 ``world.db.execute``。
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from agent_world.script._registrars import EffectBase

log = logging.getLogger(__name__)

# system 来源用 ``-1`` 占位（与 LAYOUT 中"系统侧"事件惯例一致）
_SYSTEM_SENDER_ID = -1


class BroadcastEventEffect(EffectBase):
    """Write a system broadcast message into ``direct_message``.

    Args:
        message: Body text. Surfaces to recipient agents as a system
            message (``sender = -1``).
        scope: ``"world"`` (broadcast to all agents) or
            ``"place"`` (broadcast to agents currently inside
            ``place_id``). When ``scope="place"`` you must also pass
            ``place_id``.
        place_id: Target place id when ``scope="place"``.
    """

    def __init__(
        self,
        *,
        message: str,
        scope: Literal["world", "place"] = "world",
        place_id: Optional[str] = None,
    ) -> None:
        if scope not in ("world", "place"):
            raise ValueError(f"BroadcastEventEffect.scope must be 'world' or 'place', got {scope!r}")
        if scope == "place" and not place_id:
            raise ValueError("BroadcastEventEffect requires place_id when scope='place'")
        self.message = str(message)
        self.scope = scope
        self.place_id = place_id

    async def apply(self, world: Any) -> None:
        targets = self._resolve_targets(world)
        if not targets:
            log.info("BroadcastEventEffect resolved 0 targets (scope=%s)", self.scope)
            return

        t = getattr(world, "t", 0)
        await self._write_messages(world, targets, t)

    # -- helpers -----------------------------------------------------------

    def _resolve_targets(self, world: Any) -> list[int]:
        agents_obj = getattr(world, "agents", None)
        if agents_obj is None:
            return []

        # Best-effort: agents 可能是 dict[id]=agent 或可迭代的 (id, agent)。
        try:
            all_ids = list(agents_obj.keys())  # type: ignore[union-attr]
        except AttributeError:
            try:
                all_ids = [int(a) for a in agents_obj]
            except Exception:  # noqa: BLE001
                return []

        if self.scope == "world":
            return [int(a) for a in all_ids]

        # scope == "place": 用 PlaceStore 的反向索引（如有）筛选。
        places = getattr(world, "places", None)
        if places is None:
            return []
        for attr in ("agents_at", "list_agents_at", "members_of"):
            fn = getattr(places, attr, None)
            if callable(fn):
                try:
                    return [int(a) for a in fn(self.place_id)]
                except Exception:  # noqa: BLE001
                    continue
        # fallback: 顺扫 L_t
        l_t = getattr(places, "L_t", None)
        if isinstance(l_t, dict):
            return [int(a) for a, p in l_t.items() if p == self.place_id]
        return []

    async def _write_messages(self, world: Any, targets: list[int], t: int) -> None:
        wdb = getattr(world, "world_db", None) or getattr(world, "db", None)
        insert_fn = getattr(wdb, "insert_direct_message", None) if wdb is not None else None

        for receiver_id in targets:
            if insert_fn is not None:
                result = insert_fn(
                    sender_id=_SYSTEM_SENDER_ID,
                    receiver_id=receiver_id,
                    content=self.message,
                    sent_at=t,
                    arrive_at=t,
                )
                if hasattr(result, "__await__"):
                    await result
                continue

            # Fallback: 直接 SQL（schema 见 persistence_schema_world.md）。
            execute = getattr(wdb, "execute", None)
            if execute is None:
                log.warning("BroadcastEventEffect could not locate world.db writer")
                return
            sql = (
                "INSERT INTO direct_message "
                "(sender_id, receiver_id, content, sent_at, arrive_at, delivered) "
                "VALUES (?, ?, ?, ?, ?, 0)"
            )
            result = execute(
                sql,
                (_SYSTEM_SENDER_ID, receiver_id, self.message, t, t),
            )
            if hasattr(result, "__await__"):
                await result
