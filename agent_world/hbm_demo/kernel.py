"""Simulation kernel for HBM demo — Phase 0 minimal + Phase 1 full pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from agent_world.buses.face_to_face import FaceToFaceBus
from agent_world.buses.group_message import GroupMessageBus
from agent_world.buses.remote_message import RemoteMessageBus
from agent_world.hbm_demo.hbm_agent import HbmAgent
from agent_world.hbm_demo.seed import seed_world
from agent_world.hbm_demo.world_step import HbmWorldStep
from agent_world.memory.segment import SegmentStore
from agent_world.persistence.world_db import WorldDB
from agent_world.script.engine import ScriptEngine
from agent_world.world.capability_table import CapabilityTable
from agent_world.world.clock import Clock
from agent_world.world.connectivity import ConnectivityResolver
from agent_world.world.dispatcher import ActionDispatcher
from agent_world.world.perception import PerceptionBuilder
from agent_world.world.place_store import PlaceStore
from agent_world.world.relation_graph import RelationGraph
from agent_world.world.state import WorldState
from agent_world.world.step import WorldStep


class NullPoolManager:
    """No-op stand-in for MultiPoolPlatformManager (no FEED actions in HBM demo)."""

    pools: Dict[str, Any] = {}

    def feeds_at(self, place_id: str) -> List[str]:  # noqa: ARG002
        return []

    def platform_for(self, place_id: str, feed: str):  # noqa: ARG002
        return None

    async def update_all(self) -> None:
        return None

    async def dispatch(self, *args: Any, **kwargs: Any):  # noqa: ARG002
        raise RuntimeError("FEED actions disabled in HBM demo")


@dataclass
class MinimalKernel:
    world_db: WorldDB
    world_state: WorldState
    place_store: PlaceStore
    relation_graph: RelationGraph
    capability_table: CapabilityTable
    connectivity: ConnectivityResolver
    clock: Clock


@dataclass
class HbmKernel(MinimalKernel):
    script_engine: ScriptEngine
    world_step: WorldStep
    agents: List[HbmAgent]
    perception: PerceptionBuilder
    dispatcher: ActionDispatcher


def _load_dotenv_into_environ() -> None:
    pkg = Path(__file__).resolve().parent
    candidates = (pkg / ".env", pkg.parent / "demo" / ".env")
    for candidate in candidates:
        if not candidate.exists():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def resolve_api_key(llm_cfg: Dict[str, Any]) -> str:
    literal = llm_cfg.get("api_key")
    if literal and not str(literal).startswith("$"):
        return str(literal)

    _load_dotenv_into_environ()
    env_name = llm_cfg.get("api_key_env", "DMXAPI_KEY")
    val = os.environ.get(env_name) or os.environ.get("DMXAPI_KEY")
    if val:
        return val

    raise RuntimeError(
        "HBM demo: no API key found. Set llm.api_key in scenario YAML, "
        f"export {env_name}=sk-..., or add {env_name}=... to hbm_demo/.env"
    )


async def _init_stores(
    scenario: Dict[str, Any],
    sim_dir: Path,
) -> tuple[
    WorldDB,
    Clock,
    PlaceStore,
    RelationGraph,
    CapabilityTable,
    ConnectivityResolver,
]:
    sim_dir.mkdir(parents=True, exist_ok=True)
    world_db = WorldDB(str(sim_dir / "world.db"))
    world_db.init_schema()

    clock = Clock(t0=0)
    place_store = PlaceStore(world_db)
    relation_graph = RelationGraph(world_db)
    capability_table = CapabilityTable(world_db)
    connectivity = ConnectivityResolver(
        places=place_store,
        relations=relation_graph,
        caps=capability_table,
    )

    await seed_world(
        world_db,
        capability_table,
        relation_graph,
        connectivity,
        scenario,
    )

    place_store.load_from_db(world_db)
    relation_graph.load_from_db(world_db)
    capability_table.load_from_db(world_db)

    return (
        world_db,
        clock,
        place_store,
        relation_graph,
        capability_table,
        connectivity,
    )


async def build_minimal_kernel(
    scenario: Dict[str, Any],
    sim_dir: Path,
) -> MinimalKernel:
    """Construct WorldDB + stores + WorldState; seed from scenario YAML."""
    (
        world_db,
        clock,
        place_store,
        relation_graph,
        capability_table,
        connectivity,
    ) = await _init_stores(scenario, sim_dir)

    world_state = WorldState(
        world_db=world_db,
        places=place_store,
        relations=relation_graph,
        caps=capability_table,
        clock=clock,
        pool_manager=NullPoolManager(),
    )

    return MinimalKernel(
        world_db=world_db,
        world_state=world_state,
        place_store=place_store,
        relation_graph=relation_graph,
        capability_table=capability_table,
        connectivity=connectivity,
        clock=clock,
    )


async def build_kernel(
    scenario: Dict[str, Any],
    sim_dir: Path,
    *,
    tick_budget: int = 0,
) -> HbmKernel:
    """Full kernel: ScriptEngine + WorldStep + 7×HbmAgent (Phase 1+)."""
    (
        world_db,
        clock,
        place_store,
        relation_graph,
        capability_table,
        connectivity,
    ) = await _init_stores(scenario, sim_dir)

    pool_manager = NullPoolManager()

    script_engine = ScriptEngine(
        world_db=world_db,
        places=place_store,
        relations=relation_graph,
        caps=capability_table,
    )

    f2f_bus = FaceToFaceBus(
        world_db=world_db,
        places=place_store,
        connectivity=connectivity,
        clock=clock,
    )
    rdc_bus = RemoteMessageBus(
        world_db=world_db,
        places=place_store,
        relations=relation_graph,
        caps=capability_table,
        connectivity=connectivity,
        clock=clock,
    )
    grp_bus = GroupMessageBus(
        world_db=world_db,
        places=place_store,
        connectivity=connectivity,
        clock=clock,
    )

    segment_store = SegmentStore()

    world_state = WorldState(
        world_db=world_db,
        places=place_store,
        relations=relation_graph,
        caps=capability_table,
        clock=clock,
        pool_manager=pool_manager,
    )

    perception = PerceptionBuilder(
        world_db=world_db,
        places=place_store,
        relations=relation_graph,
        caps=capability_table,
        connectivity=connectivity,
        retriever=None,
        script_engine=script_engine,
        pool_manager=pool_manager,
        segment_headers=(
            "# 人格内核",
            "# 长期目标",
            "# 当前状态",
            "# 当前小目标",
            "# 场景行为规则",
        ),
    )

    dispatcher = ActionDispatcher(
        world_state=world_state,
        f2f_bus=f2f_bus,
        rdc_bus=rdc_bus,
        grp_bus=grp_bus,
        pool_manager=pool_manager,
        script_engine=script_engine,
        compressor=None,
        segment_store=segment_store,
    )

    clock_cfg = scenario.get("clock") or {}
    wall_start_time = str(clock_cfg.get("start_time", "14:00")).strip()
    minutes_per_tick = int(clock_cfg.get("minutes_per_tick", 2))

    llm_cfg = scenario.get("llm", {}) or {}
    api_key = resolve_api_key(llm_cfg)
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=llm_cfg.get("base_url", "https://api.deepseek.com"),
    )

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
            groups_for_agent.setdefault(aid, []).append(
                {
                    "group_id": gid,
                    "name": g.get("name"),
                    "members": members,
                }
            )

    agents: List[HbmAgent] = []
    for a in scenario.get("agents", []):
        aid = int(a["agent_id"])
        agent = HbmAgent(
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
            segment_store=segment_store,
            total_ticks=tick_budget,
            wall_start_time=wall_start_time,
            minutes_per_tick=minutes_per_tick,
            client=client,
            model=llm_cfg.get("model", "deepseek-chat"),
            temperature=float(llm_cfg.get("temperature", 0.85)),
            max_tokens=int(llm_cfg.get("max_tokens", 500)),
        )
        world_state.register_agent(aid, agent)
        agents.append(agent)

    runner_cfg = scenario.get("runner") or {}
    parallel_agents = bool(runner_cfg.get("parallel_agent_decisions", True))
    step_cls = HbmWorldStep if parallel_agents else WorldStep

    world_step = step_cls(
        world_state=world_state,
        perception_builder=perception,
        dispatcher=dispatcher,
        script_engine=script_engine,
        pool_manager=pool_manager,
        f2f_bus=f2f_bus,
        rdc_bus=rdc_bus,
        grp_bus=grp_bus,
        segment_store=segment_store,
        compressor=None,
        world_db=world_db,
        scheduler=None,
        sim_id=scenario.get("simulation_id", "hbm_memory_war"),
    )

    return HbmKernel(
        world_db=world_db,
        world_state=world_state,
        place_store=place_store,
        relation_graph=relation_graph,
        capability_table=capability_table,
        connectivity=connectivity,
        clock=clock,
        script_engine=script_engine,
        world_step=world_step,
        agents=agents,
        perception=perception,
        dispatcher=dispatcher,
    )
