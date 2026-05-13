"""Self-contained Agent World demo runner.

Wires the kernel pieces needed for the *full* P0+P1+P6+B5 path
(F2F + RDC + Group + REQUEST_MOVE + UPDATE_STATE + 4-segment system prompt)
without OASIS Platform / camel / Zep / scripts.  Reads ``scenario.yaml``
for places / coverage / capabilities / groups / agents / LLM, and runs N
micro-ticks via :class:`WorldStep`.

Usage:
    DMXAPI_KEY=sk-... python -m agent_world.demo.run_demo \
        --config agent_world/demo/scenario.yaml --num-ticks 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import yaml
from openai import AsyncOpenAI

from agent_world.buses.face_to_face import FaceToFaceBus
from agent_world.buses.group_message import GroupMessageBus
from agent_world.buses.remote_message import RemoteMessageBus
from agent_world.demo.demo_agent import DemoAgent
from agent_world.memory.segment import SegmentStore
from agent_world.persistence.world_db import WorldDB
from agent_world.world.capability_table import CapabilityTable
from agent_world.world.clock import Clock
from agent_world.world.connectivity import ConnectivityResolver
from agent_world.world.dispatcher import ActionDispatcher
from agent_world.world.perception import PerceptionBuilder
from agent_world.world.place_store import PlaceStore
from agent_world.world.relation_graph import RelationGraph
from agent_world.world.state import WorldState
from agent_world.world.step import WorldStep

log = logging.getLogger("agent_world.demo")


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _load_dotenv_into_environ() -> None:
    """Best-effort: load ``agent_world/demo/.env`` into ``os.environ``.

    Avoids the third-party ``python-dotenv`` dependency — we just parse a
    plain ``KEY=VALUE`` file. Lines starting with ``#`` and blank lines are
    skipped. Existing env vars take precedence.
    """
    candidate = Path(__file__).parent / ".env"
    if not candidate.exists():
        return
    for raw in candidate.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _resolve_api_key(llm_cfg: Dict[str, Any]) -> str:
    """Resolve the LLM API key in this priority order:

    1. ``llm.api_key`` literal in the scenario YAML (demo / local only).
    2. Env var named by ``llm.api_key_env`` (default ``DMXAPI_KEY``).
    3. Plain ``DMXAPI_KEY`` env var as a final fallback.

    Also tries to load ``agent_world/demo/.env`` once before reading env vars.
    """
    literal = llm_cfg.get("api_key")
    if literal and not str(literal).startswith("$"):
        return str(literal)

    _load_dotenv_into_environ()

    env_name = llm_cfg.get("api_key_env", "DMXAPI_KEY")
    val = os.environ.get(env_name) or os.environ.get("DMXAPI_KEY")
    if val:
        return val

    raise RuntimeError(
        "Demo: no API key found. Either:\n"
        f"  (a) set ``llm.api_key:`` in scenario.yaml, or\n"
        f"  (b) export {env_name}=sk-..., or\n"
        f"  (c) drop a line ``{env_name}=sk-...`` into "
        f"agent_world/demo/.env (gitignored — see README)."
    )


# --------------------------------------------------------------------------- #
# Stub pool_manager — the demo has no FEED actions.                           #
# --------------------------------------------------------------------------- #


class _NullPoolManager:
    """No-op stand-in for MultiPoolPlatformManager."""

    pools: Dict[str, Any] = {}

    def feeds_at(self, place_id: str) -> List[str]:  # noqa: ARG002
        return []

    def platform_for(self, place_id: str, feed: str):  # noqa: ARG002
        return None

    async def update_all(self) -> None:
        return None

    async def dispatch(self, *args: Any, **kwargs: Any):  # noqa: ARG002
        raise RuntimeError("FEED actions disabled in demo")


# --------------------------------------------------------------------------- #
# Boot helpers                                                                #
# --------------------------------------------------------------------------- #


async def _seed_world(
    world_db: WorldDB,
    capability_table: CapabilityTable,
    relation_graph: RelationGraph,
    connectivity: ConnectivityResolver,
    scenario: Dict[str, Any],
) -> None:
    """Seed places / coverage / agent_locations / capabilities / groups."""
    # Places.
    for p in scenario.get("places", []):
        attrs = p.get("attrs") or {}
        await world_db.upsert_place(
            place_id=p["place_id"],
            place_type=p.get("place_type", "default"),
            parent_id=p.get("parent_id"),
            capacity=p.get("capacity"),
            attrs=json.dumps(attrs),
        )

    # Coverage (explicit + auto self-edges for F2F latency=0).
    for c in scenario.get("coverage", []):
        await world_db.upsert_coverage(
            src_place=c["src"], dst_place=c["dst"],
            latency_ticks=int(c.get("latency_ticks", 0)),
            can_reach=int(c.get("can_reach", 1)),
        )
    for p in scenario.get("places", []):
        await world_db.upsert_coverage(
            src_place=p["place_id"], dst_place=p["place_id"],
            latency_ticks=0, can_reach=1,
        )

    # Initial agent locations.
    for a in scenario.get("agents", []):
        await world_db.set_location(
            agent_id=int(a["agent_id"]),
            place_id=a["location"], t=0,
        )

    # Capabilities (signal_uplink etc).
    for entry in scenario.get("capabilities", []) or []:
        await capability_table.grant(
            int(entry["agent_id"]), str(entry["capability"]),
        )

    # Groups.
    group_members_map: Dict[int, set] = {}
    for g in scenario.get("groups", []) or []:
        group_id = int(g["group_id"])
        # Use a raw INSERT to honour the explicit group_id from YAML
        # (insert_group() ignores group_id and uses AUTOINCREMENT).
        async with world_db._write_lock:
            world_db._exec(
                "INSERT OR REPLACE INTO chat_group(group_id, name) VALUES(?, ?)",
                (group_id, str(g.get("name") or f"group_{group_id}")),
            )
        members = [int(m) for m in g.get("members", [])]
        for aid in members:
            await world_db.insert_group_member(group_id, aid)
            await world_db.insert_group_event(
                group_id=group_id, agent_id=aid, event_type="join",
                occurred_at=-1, actor_id=int(g.get("creator_id", aid)),
            )
        group_members_map[group_id] = set(members)

    # Push group membership into ConnectivityResolver so phi_grp resolves.
    if hasattr(connectivity, "set_group_members"):
        for gid, members in group_members_map.items():
            connectivity.set_group_members(gid, members)

    # Relations (RDC contacts).
    for r in scenario.get("relations", []) or []:
        src, dst = int(r["src"]), int(r["dst"])
        rtype = str(r.get("type", "contact"))
        await relation_graph.add(src, dst, rtype, t=0)
        if r.get("symmetric") and src != dst:
            try:
                await relation_graph.add(dst, src, rtype, t=0)
            except Exception:  # noqa: BLE001 — already added by symmetric meta
                pass


async def _build_kernel(scenario: Dict[str, Any], sim_dir: Path,
                        num_ticks: int = 0):
    """Construct kernel; return (world, world_step, agents, world_db, groups)."""
    sim_dir.mkdir(parents=True, exist_ok=True)
    world_db = WorldDB(str(sim_dir / "world.db"))
    world_db.init_schema()

    clock = Clock(t0=0)
    place_store = PlaceStore(world_db)
    relation_graph = RelationGraph(world_db)
    capability_table = CapabilityTable(world_db)

    connectivity = ConnectivityResolver(
        places=place_store, relations=relation_graph, caps=capability_table
    )

    await _seed_world(
        world_db, capability_table, relation_graph, connectivity, scenario,
    )

    place_store.load_from_db(world_db)
    relation_graph.load_from_db(world_db)
    capability_table.load_from_db(world_db)

    pool_manager = _NullPoolManager()

    f2f_bus = FaceToFaceBus(
        world_db=world_db, places=place_store,
        connectivity=connectivity, clock=clock,
    )
    rdc_bus = RemoteMessageBus(
        world_db=world_db, places=place_store, relations=relation_graph,
        caps=capability_table, connectivity=connectivity, clock=clock,
    )
    grp_bus = GroupMessageBus(
        world_db=world_db, places=place_store,
        connectivity=connectivity, clock=clock,
    )

    segment_store = SegmentStore()

    perception = PerceptionBuilder(
        world_db=world_db, places=place_store, relations=relation_graph,
        caps=capability_table, connectivity=connectivity,
        retriever=None, script_engine=None, pool_manager=None,
        # B5 5-segment system prompt headers — Chinese for this demo.
        # 第 4 段『当前小目标』由 update_short_term_goal 工具维护，是防止
        # LLM 在 F2F 中陷入 loop 的关键锚点。
        segment_headers=(
            "# 人格内核",
            "# 长期目标",
            "# 当前状态",
            "# 当前小目标",
            "# 场景行为规则",
        ),
    )

    world_state = WorldState(
        world_db=world_db, places=place_store, relations=relation_graph,
        caps=capability_table, clock=clock, pool_manager=pool_manager,
    )

    dispatcher = ActionDispatcher(
        world_state=world_state,
        f2f_bus=f2f_bus, rdc_bus=rdc_bus, grp_bus=grp_bus,
        pool_manager=pool_manager,
        script_engine=None,
        compressor=None,
        segment_store=segment_store,
    )

    # ---- Wall-clock display config (demo-layer only; Clock kernel
    # ---- semantics unchanged — it stays a pure integer tick counter).
    clock_cfg = scenario.get("clock") or {}
    wall_start_time = str(clock_cfg.get("start_time", "20:30")).strip()
    minutes_per_tick = int(clock_cfg.get("minutes_per_tick", 5))

    # ---- LLM client ------------------------------------------------------
    llm_cfg = scenario.get("llm", {}) or {}
    api_key = _resolve_api_key(llm_cfg)
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=llm_cfg.get("base_url", "https://www.dmxapi.cn/v1"),
    )

    # ---- Build agents (with name directory + group memberships) ----------
    name_dir: Dict[int, str] = {
        int(a["agent_id"]): a.get("name", f"agent_{a['agent_id']}")
        for a in scenario.get("agents", [])
    }
    available_places: List[str] = [
        p["place_id"] for p in scenario.get("places", [])
    ]

    groups_for_agent: Dict[int, List[Dict[str, Any]]] = {}
    for g in scenario.get("groups", []) or []:
        gid = int(g["group_id"])
        members = [int(m) for m in g.get("members", [])]
        for aid in members:
            groups_for_agent.setdefault(aid, []).append({
                "group_id": gid,
                "name": g.get("name"),
                "members": members,
            })

    agents: List[DemoAgent] = []
    for a in scenario.get("agents", []):
        aid = int(a["agent_id"])
        agent = DemoAgent(
            agent_id=aid,
            name=a.get("name", f"agent_{aid}"),
            soul=a.get("soul", "").strip(),
            long_term_goal=a.get("long_term_goal", "").strip(),
            current_state=a.get("current_state", "").strip(),
            short_term_goal=a.get("short_term_goal", "").strip(),
            groups=groups_for_agent.get(aid, []),
            name_directory=name_dir,
            available_places=available_places,
            perception_builder=perception,
            segment_store=segment_store,        # so prompts see prior actions
            total_ticks=num_ticks,
            wall_start_time=wall_start_time,
            minutes_per_tick=minutes_per_tick,
            client=client,
            model=llm_cfg.get("model", "gpt-4.1-mini"),
            temperature=float(llm_cfg.get("temperature", 0.8)),
            max_tokens=int(llm_cfg.get("max_tokens", 500)),
        )
        world_state.register_agent(aid, agent)
        agents.append(agent)

    world_step = WorldStep(
        world_state=world_state,
        perception_builder=perception,
        dispatcher=dispatcher,
        script_engine=None,
        pool_manager=pool_manager,
        f2f_bus=f2f_bus, rdc_bus=rdc_bus, grp_bus=grp_bus,
        segment_store=segment_store,
        compressor=None,
        world_db=world_db,
        scheduler=None,
        sim_id=scenario.get("simulation_id", "demo"),
    )
    return world_state, world_step, agents, world_db


# --------------------------------------------------------------------------- #
# Pretty-printer                                                              #
# --------------------------------------------------------------------------- #


_CHANNEL_GLYPH = {"F2F": "🗣  ", "RDC": "📨 ", "GRP": "👥 "}


def _print_tick_report(tick: int, report: Dict[str, Any],
                       agents: List[DemoAgent],
                       world_state: WorldState,
                       world_db: WorldDB) -> None:
    # Use any agent's wall-clock helper (all share the same config).
    wall = agents[0]._wall_clock_label(tick) if agents else ""
    head = f" 世界时间 {wall}" if wall else ""
    print(f"\n=================== tick t={tick}{head} ===================")
    print(
        f"  active={report.get('active')}  places={report.get('places')}  "
        f"failures={len(report.get('failures', []))}"
    )
    if report.get("failures"):
        for f in report["failures"]:
            print(f"  FAIL {f}")

    name_of = {a.agent_id: a.name for a in agents}

    # Build a quick lookup for "did this agent move this tick?" by reading
    # agent_location.arrived_at. Equal to ``tick`` => the printed location is
    # the END-of-tick destination; messages further down were sent at the
    # START of the tick from the *previous* place, which is otherwise easy
    # to misread as a kernel inconsistency.
    moved_this_tick: Dict[int, bool] = {}
    # Skip the marker at tick 0: ``_seed_world`` calls ``set_location(t=0)``
    # for every agent at boot, which would otherwise paint everyone as
    # "just moved" on the very first print. Real moves only happen at tick
    # boundaries from t=1 onward.
    if int(tick) > 0:
        for a in agents:
            try:
                row = world_db._conn.execute(
                    "SELECT arrived_at FROM agent_location WHERE agent_id=?",
                    (a.agent_id,),
                ).fetchone()
            except Exception:
                row = None
            moved_this_tick[a.agent_id] = bool(
                row and int(row[0]) == int(tick)
            )

    print("  --- agents ---")
    for a in agents:
        loc = world_state.location_of(a.agent_id)
        cs = (a.current_state or "").replace("\n", " ").strip()
        move_tag = "  [本拍末移动到此]" if moved_this_tick.get(a.agent_id) else ""
        print(
            f"    [{a.agent_id}] {a.name:8s} @{loc:18s}{move_tag}"
            f" state={cs[:90]!r}"
        )

    rows = world_db._conn.execute(
        "SELECT message_id, sender_id, recipient_id, group_id, channel_type, "
        "content, place_id, attempted_at, arrive_at, delivered "
        "FROM direct_message WHERE attempted_at = ? ORDER BY message_id",
        (tick,),
    ).fetchall()
    if rows:
        print("  --- messages this tick ---")
        # Collapse F2F broadcast fan-out (same sender/content/place inserted
        # once per recipient) into a single line so the print mirrors the
        # actual utterance count instead of looking like the speaker repeated
        # the same line N times.
        printed_keys: set = set()
        rows_by_id = {int(r[0]): r for r in rows}
        for row in rows:
            mid = int(row[0])
            if mid in printed_keys:
                continue
            sender_id = row[1]
            ch = row[4]
            gid = row[3]
            content = (row[5] or "").replace("\n", " ")
            place_id = row[6]
            arr = row[8]
            ok = {1: "✓", 0: "✗", -1: "·"}.get(int(row[9]), "?")
            tag = f"{ch}#g{gid}" if gid else ch
            glyph = _CHANNEL_GLYPH.get(ch, "   ")
            sender = name_of.get(sender_id, f"#{sender_id}")
            if ch == "F2F":
                # Collect every sibling row with same sender/place/content
                # at this tick, regardless of message_id ordering.
                siblings = [
                    r for r in rows
                    if r[4] == "F2F" and r[1] == sender_id
                    and r[6] == place_id and (r[5] or "") == (row[5] or "")
                ]
                rcpts = sorted({int(r[2]) for r in siblings})
                printed_keys.update(int(r[0]) for r in siblings)
                rcpt_names = "[" + ", ".join(
                    name_of.get(rid, f"#{rid}") for rid in rcpts
                ) + "]"
                print(
                    f"    {glyph}[{tag}@{place_id}] {sender}->{rcpt_names} "
                    f"arrive_at={arr} {ok} :: {content[:140]}"
                )
            else:
                printed_keys.add(mid)
                recipient = name_of.get(row[2], f"#{row[2]}")
                print(
                    f"    {glyph}[{tag}] {sender}->{recipient:8s} "
                    f"arrive_at={arr} {ok} :: {content[:140]}"
                )


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #


async def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "scenario.yaml"),
    )
    parser.add_argument("--num-ticks", type=int, default=None)
    parser.add_argument(
        "--sim-dir", default=None,
        help="Directory for world.db (defaults to a tempdir).",
    )
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    scenario_path = Path(args.config)
    with scenario_path.open("r", encoding="utf-8") as fh:
        scenario = yaml.safe_load(fh) or {}

    sim_dir = (
        Path(args.sim_dir) if args.sim_dir
        else Path(tempfile.mkdtemp(prefix="agent_world_demo_"))
    )

    print(f"=== Agent World demo: {scenario.get('simulation_id')} ===")
    print(f"=== sim_dir={sim_dir} model={scenario.get('llm', {}).get('model')} ===")

    num_ticks = int(
        args.num_ticks if args.num_ticks is not None
        else scenario.get("num_ticks", 5)
    )

    world_state, world_step, agents, world_db = await _build_kernel(
        scenario, sim_dir, num_ticks=num_ticks,
    )

    for tick in range(num_ticks):
        report = await world_step.run_one_tick()
        _print_tick_report(tick, report, agents, world_state, world_db)

    print(f"\n=== done; world.db retained at {sim_dir / 'world.db'} ===")
    return 0


def run() -> None:
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    run()
