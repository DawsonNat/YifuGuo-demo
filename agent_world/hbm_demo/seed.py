"""World DB seeding for HBM demo (copied from ``demo/run_demo._seed_world``)."""

from __future__ import annotations

import json
from typing import Any, Dict

from agent_world.persistence.world_db import WorldDB
from agent_world.world.capability_table import CapabilityTable
from agent_world.world.connectivity import ConnectivityResolver
from agent_world.world.relation_graph import RelationGraph


async def seed_world(
    world_db: WorldDB,
    capability_table: CapabilityTable,
    relation_graph: RelationGraph,
    connectivity: ConnectivityResolver,
    scenario: Dict[str, Any],
) -> None:
    """Seed places / coverage / agent_locations / capabilities / groups / relations."""
    for p in scenario.get("places", []):
        attrs = p.get("attrs") or {}
        await world_db.upsert_place(
            place_id=p["place_id"],
            place_type=p.get("place_type", "default"),
            parent_id=p.get("parent_id"),
            capacity=p.get("capacity"),
            attrs=json.dumps(attrs),
        )

    for c in scenario.get("coverage", []):
        await world_db.upsert_coverage(
            src_place=c["src"],
            dst_place=c["dst"],
            latency_ticks=int(c.get("latency_ticks", 0)),
            can_reach=int(c.get("can_reach", 1)),
        )
    for p in scenario.get("places", []):
        await world_db.upsert_coverage(
            src_place=p["place_id"],
            dst_place=p["place_id"],
            latency_ticks=0,
            can_reach=1,
        )

    for a in scenario.get("agents", []):
        await world_db.set_location(
            agent_id=int(a["agent_id"]),
            place_id=a["location"],
            t=0,
        )

    for entry in scenario.get("capabilities", []) or []:
        await capability_table.grant(
            int(entry["agent_id"]),
            str(entry["capability"]),
        )

    group_members_map: Dict[int, set] = {}
    for g in scenario.get("groups", []) or []:
        group_id = int(g["group_id"])
        async with world_db._write_lock:
            world_db._exec(
                "INSERT OR REPLACE INTO chat_group(group_id, name) VALUES(?, ?)",
                (group_id, str(g.get("name") or f"group_{group_id}")),
            )
        members = [int(m) for m in g.get("members", [])]
        for aid in members:
            await world_db.insert_group_member(group_id, aid)
            await world_db.insert_group_event(
                group_id=group_id,
                agent_id=aid,
                event_type="join",
                occurred_at=-1,
                actor_id=int(g.get("creator_id", aid)),
            )
        group_members_map[group_id] = set(members)

    if hasattr(connectivity, "set_group_members"):
        for gid, members in group_members_map.items():
            connectivity.set_group_members(gid, members)

    for r in scenario.get("relations", []) or []:
        src, dst = int(r["src"]), int(r["dst"])
        rtype = str(r.get("type", "contact"))
        await relation_graph.add(src, dst, rtype, t=0)
        if r.get("symmetric") and src != dst:
            try:
                await relation_graph.add(dst, src, rtype, t=0)
            except Exception:  # noqa: BLE001 — symmetric duplicate
                pass
