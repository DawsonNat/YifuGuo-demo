"""Agent World simulation subprocess (LAYOUT §2.I / impl/runner.md).

Entry point::

    python -m agent_world.runner.run_agent_world_simulation \\
        --config /path/to/simulation_config.yaml \\
        --simulation-dir /path/to/sim_dir

Boot sequence (LAYOUT §6.1 + impl/runner.md §4.2):

    1. parse argv -> simulation_id, simulation_dir, config path
    2. load SimulationConfig via config.simulation_config_ext.load_from_yaml
    3. construct WorldDB({sim_dir}/world.db) + init_schema
    4. construct PlaceStore / RelationGraph / CapabilityTable + load_from_db
    5. construct Clock + ConnectivityResolver + PlatformFactory + PoolManager
    6. construct ScriptEngine + load YAML if scripts.yaml exists
    7. construct 3 Buses (F2F / RDC / GRP) + SegmentStore + BehaviorCompressor
       + MultiGraphManager
    8. construct WorldState + PerceptionBuilder + ActionDispatcher + WorldStep
    9. start IPC server task (handlers wired via register_handler)
    10. run main loop: await world_step.run_one_tick() N times
    11. on shutdown: flush manager, close DBs, stop IPC

Many components have ``try/except + TODO`` guards because external clients
(LLM / Zep) are out-of-scope for this MVP wiring.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent_world.runner")


# --------------------------------------------------------------------------- #
# Default knobs                                                               #
# --------------------------------------------------------------------------- #

DEFAULT_NUM_TICKS = 20
DEFAULT_LOG_LEVEL = "INFO"


# --------------------------------------------------------------------------- #
# argv                                                                        #
# --------------------------------------------------------------------------- #


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse runner argv. Both flags accept positional fallbacks for the
    ``simulation_manager.start()`` 2-positional-arg protocol (sim_id, sim_dir)."""

    parser = argparse.ArgumentParser(
        prog="agent_world.runner.run_agent_world_simulation",
        description="Agent World simulation subprocess entry point.",
    )
    parser.add_argument(
        "--config",
        dest="config",
        default=None,
        help="Path to simulation_config.yaml / .json",
    )
    parser.add_argument(
        "--simulation-dir",
        dest="simulation_dir",
        default=None,
        help="Filesystem dir for world.db / pools/ / ipc_*/",
    )
    parser.add_argument(
        "--simulation-id",
        dest="simulation_id",
        default=None,
        help="Logical simulation id (free-form string).",
    )
    parser.add_argument(
        "--num-ticks",
        "--max-rounds",
        dest="num_ticks",
        type=int,
        default=None,
        help="Override max_ticks; --max-rounds is accepted as an alias.",
    )
    parser.add_argument(
        "--log-level",
        dest="log_level",
        default=DEFAULT_LOG_LEVEL,
        help="Python logging level name (default INFO).",
    )
    parser.add_argument(
        "positional",
        nargs="*",
        help="Optional positional fallbacks: <simulation_id> <simulation_dir>.",
    )
    args = parser.parse_args(argv)

    # Positional compatibility with simulation_manager.start().
    if args.positional:
        if args.simulation_id is None and len(args.positional) >= 1:
            args.simulation_id = args.positional[0]
        if args.simulation_dir is None and len(args.positional) >= 2:
            args.simulation_dir = args.positional[1]

    if args.simulation_dir is None:
        if args.config is not None:
            args.simulation_dir = str(Path(args.config).resolve().parent)
        else:
            parser.error("--simulation-dir or positional sim_dir is required")

    if args.config is None:
        for fname in ("simulation_config.yaml", "simulation_config.json"):
            cand = os.path.join(args.simulation_dir, fname)
            if os.path.exists(cand):
                args.config = cand
                break
        if args.config is None:
            parser.error(
                f"--config not given and no simulation_config.yaml/.json in "
                f"{args.simulation_dir!r}"
            )

    if args.simulation_id is None:
        args.simulation_id = os.path.basename(
            os.path.normpath(args.simulation_dir)
        ) or "agent_world_sim"

    return args


# --------------------------------------------------------------------------- #
# WorldStep import shim                                                       #
# --------------------------------------------------------------------------- #


def _import_world_step() -> Any:
    """Return ``agent_world.world.step.WorldStep`` if available.

    The L5 sister module ``world/step.py`` is parallel work; absence
    must not prevent the runner module from importing. Returns ``None``
    in that case so :func:`main` can fall back to a stub loop.
    """
    try:
        from agent_world.world.step import WorldStep  # type: ignore

        return WorldStep
    except Exception as exc:  # noqa: BLE001
        logger.warning("world.step.WorldStep not importable: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Pool inference                                                              #
# --------------------------------------------------------------------------- #


def _infer_pools(config: Any) -> List[Dict[str, Any]]:
    """Best-effort enumeration of pools from ``simulation_config``.

    Priority order:

    1. ``config.world_config.pools`` -- explicit list (TODO: not in MVP schema).
    2. ``config.twitter_config`` / ``config.reddit_config`` -- MiroFish
       compat: emit one pool per place that lists the matching feed in
       ``capabilities``.
    3. Otherwise return ``[]`` (the runner runs without pools).

    Each entry: ``{"pool_id", "place_ids", "feed", "recsys_type"}``.
    """
    pools: List[Dict[str, Any]] = []

    explicit = getattr(config.world_config, "pools", None)
    if isinstance(explicit, list) and explicit:
        for spec in explicit:
            if isinstance(spec, dict):
                pools.append(
                    {
                        "pool_id": spec.get("pool_id")
                        or f"pool_{spec.get('feed', 'unknown')}",
                        "place_ids": list(spec.get("place_ids", [])),
                        "feed": spec.get("feed", "twitter"),
                        "recsys_type": spec.get("recsys_type", "twitter"),
                    }
                )
        if pools:
            return pools

    # MiroFish-compat fallback.
    place_ids = [p.place_id for p in config.world_config.places]
    if not place_ids:
        return pools

    if getattr(config, "twitter_config", None):
        pools.append(
            {
                "pool_id": "pool_twitter",
                "place_ids": list(place_ids),
                "feed": "twitter",
                "recsys_type": "twitter",
            }
        )
    if getattr(config, "reddit_config", None):
        pools.append(
            {
                "pool_id": "pool_reddit",
                "place_ids": list(place_ids),
                "feed": "reddit",
                "recsys_type": "reddit",
            }
        )
    return pools


# --------------------------------------------------------------------------- #
# Boot helpers                                                                #
# --------------------------------------------------------------------------- #


def _resolve_num_ticks(config: Any, override: Optional[int]) -> int:
    if override is not None and override > 0:
        return int(override)
    tc = getattr(config, "time_config", None) or {}
    if isinstance(tc, dict):
        for k in ("max_ticks", "num_ticks", "total_rounds"):
            v = tc.get(k)
            if isinstance(v, int) and v > 0:
                return v
    return DEFAULT_NUM_TICKS


def _setup_logging(level_name: str, simulation_dir: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    log_path = os.path.join(simulation_dir, "runner.log")
    os.makedirs(simulation_dir, exist_ok=True)
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError as exc:  # pragma: no cover
        print(f"warning: could not open runner.log: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #


async def main(
    simulation_id: Optional[str] = None,
    simulation_dir: Optional[str] = None,
    *,
    config_path: Optional[str] = None,
    num_ticks: Optional[int] = None,
) -> int:
    """Async runner main; returns a process exit code.

    ``simulation_id`` / ``simulation_dir`` mirror the
    :class:`subprocess.Popen` argv the Flask side sends. When invoked
    via the CLI, ``__main__`` calls :func:`_parse_args` first and then
    forwards into us.
    """
    if simulation_dir is None:
        return 2  # programmer error
    sim_dir = os.path.abspath(simulation_dir)
    os.makedirs(sim_dir, exist_ok=True)

    # ---- 1. config -------------------------------------------------------
    from agent_world.config.simulation_config_ext import load_from_yaml

    cfg_path = config_path or os.path.join(sim_dir, "simulation_config.yaml")
    config = load_from_yaml(cfg_path)
    sim_id = simulation_id or getattr(config, "simulation_id", None) or os.path.basename(sim_dir)

    logger.info(
        "agent_world runner boot: sim_id=%s sim_dir=%s config=%s",
        sim_id, sim_dir, cfg_path,
    )

    total_ticks = _resolve_num_ticks(config, num_ticks)
    logger.info("running for %d ticks (default=%d)", total_ticks, DEFAULT_NUM_TICKS)

    # ---- 2. WorldDB ------------------------------------------------------
    from agent_world.persistence.world_db import WorldDB

    world_db_path = os.path.join(sim_dir, "world.db")
    world_db = WorldDB(world_db_path)
    world_db.init_schema()

    # Seed world_config.places + coverage into world.db (idempotent upsert)
    # so the in-memory stores see them on load_from_db below.
    try:
        for place in config.world_config.places:
            await world_db.upsert_place(
                place_id=place.place_id,
                parent_id=None,
                place_type=str(place.attrs.get("type", "place")),
                attrs=_attrs_json(place.attrs),
                capacity=place.attrs.get("capacity"),
                created_at=0,
            )
        for edge in config.world_config.coverage:
            await world_db.upsert_coverage(
                src_place=edge.src,
                dst_place=edge.dst,
                latency_ticks=edge.latency_ticks,
                can_reach=1,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("world_config seed failed (continuing): %s", exc)

    # ---- 3. Stores -------------------------------------------------------
    from agent_world.world.capability_table import CapabilityTable
    from agent_world.world.clock import Clock
    from agent_world.world.connectivity import ConnectivityResolver
    from agent_world.world.place_store import PlaceStore
    from agent_world.world.relation_graph import RelationGraph

    clock = Clock(t0=0)
    place_store = PlaceStore(world_db)
    relation_graph = RelationGraph(world_db)
    capability_table = CapabilityTable(world_db)

    # load_from_db is sync; safe before the asyncio event loop starts mutating.
    place_store.load_from_db(world_db)
    relation_graph.load_from_db(world_db)
    capability_table.load_from_db(world_db)

    # Seed agent_configs (B5 6 fields) -- best-effort.
    await _seed_agents(config, world_db, place_store, relation_graph, capability_table)

    connectivity = ConnectivityResolver(place_store, relation_graph, capability_table)

    # ---- 4. Pools --------------------------------------------------------
    from agent_world.pools.manager import MultiPoolPlatformManager
    from agent_world.pools.platform_factory import PlatformFactory

    platform_factory = PlatformFactory(clock=clock)
    pool_manager = MultiPoolPlatformManager(
        places=place_store,
        caps=capability_table,
        connectivity=connectivity,
        factory=platform_factory,
        base_dir=sim_dir,
    )
    for pool_spec in _infer_pools(config):
        try:
            pool_manager.add_pool(
                pool_id=pool_spec["pool_id"],
                place_ids=pool_spec["place_ids"],
                feed=pool_spec["feed"],
                recsys_type=pool_spec.get("recsys_type", "twitter"),
            )
        except Exception as exc:  # noqa: BLE001
            # TODO: pool wiring still depends on OASIS Platform fork (P3);
            # log and continue so the F2F-only demo still runs.
            logger.warning(
                "add_pool(%s) failed (continuing without pool): %s",
                pool_spec.get("pool_id"), exc,
            )

    # Project relation -> follow once stores are populated.
    try:
        pool_manager.register_relation_change_hook(relation_graph)
    except Exception as exc:  # noqa: BLE001
        logger.debug("register_relation_change_hook skipped: %s", exc)

    # ---- 5. ScriptEngine -------------------------------------------------
    from agent_world.script.engine import ScriptEngine

    script_engine = ScriptEngine(
        world_db=world_db,
        places=place_store,
        relations=relation_graph,
        caps=capability_table,
    )
    scripts_yaml = os.path.join(sim_dir, "scripts.yaml")
    if os.path.exists(scripts_yaml):
        try:
            stats = await script_engine.load_from_yaml(scripts_yaml, world=None)
            logger.info("ScriptEngine load_from_yaml: %s", stats)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ScriptEngine.load_from_yaml failed: %s", exc)

    # ---- 6. Buses + memory ----------------------------------------------
    from agent_world.buses.face_to_face import FaceToFaceBus
    from agent_world.buses.group_message import GroupMessageBus
    from agent_world.buses.remote_message import RemoteMessageBus
    from agent_world.memory.compressor import BehaviorCompressor
    from agent_world.memory.manager import MultiGraphManager
    from agent_world.memory.segment import SegmentStore

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
        channel_config=config.channel_config.default_delays.model_dump(),
    )
    grp_bus = GroupMessageBus(
        world_db=world_db,
        places=place_store,
        connectivity=connectivity,
        clock=clock,
        grp_delay=config.channel_config.default_delays.GRP,
    )

    segment_store = SegmentStore()

    # MultiGraphManager + BehaviorCompressor: Zep client / LLM client are
    # out of scope for the MVP wiring -> dry-run mode (logs warning).
    graph_manager = MultiGraphManager(client=None)
    try:
        from agent_world.memory.translator import translate as _translate  # type: ignore
    except Exception:  # noqa: BLE001
        def _translate(entry: Dict[str, Any]) -> str:
            return str(entry)

    compressor = BehaviorCompressor(
        segment_store=segment_store,
        manager=graph_manager,
        translator_fn=_translate,
        llm_client=None,  # TODO: wire Anthropic Haiku client when available.
        model=config.memory_config.compressor.model,
        sim_id=str(sim_id),
    )

    # ---- 7. WorldState + perception + dispatcher ------------------------
    from agent_world.world.dispatcher import ActionDispatcher
    from agent_world.world.perception import PerceptionBuilder
    from agent_world.world.state import WorldState

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
        retriever=None,  # TODO: MultiGraphRetriever wiring.
        script_engine=script_engine,
        pool_manager=pool_manager,
    )

    dispatcher = ActionDispatcher(
        world_state=world_state,
        f2f_bus=f2f_bus,
        rdc_bus=rdc_bus,
        grp_bus=grp_bus,
        pool_manager=pool_manager,
        script_engine=script_engine,
        compressor=compressor,
        segment_store=segment_store,
    )
    # Wire the central dispatcher into pool_manager so its dispatch() method
    # can delegate non-FEED actions (B3 + group + UPDATE_STATE) — agent_action
    # methods call platform_manager.dispatch(...) which now routes correctly.
    if hasattr(pool_manager, "attach_action_dispatcher"):
        pool_manager.attach_action_dispatcher(dispatcher)
        # Also expose the clock for tick-resolution inside non-FEED dispatch.
        pool_manager._world = world_state
        pool_manager._clock = clock

    # ---- 8. ActionLogger -------------------------------------------------
    from agent_world.runner.action_logger import ActionLogger

    action_logger = ActionLogger(os.path.join(sim_dir, "actions.jsonl"))

    # ---- 9. WorldStep ----------------------------------------------------
    WorldStep = _import_world_step()
    world_step: Any = None
    if WorldStep is not None:
        # LAYOUT §6.1 + impl/world_step.md: full kwargs-style construction.
        kwargs: Dict[str, Any] = {
            "world_state": world_state,
            "perception_builder": perception,
            "dispatcher": dispatcher,
            "script_engine": script_engine,
            "pool_manager": pool_manager,
            "f2f_bus": f2f_bus,
            "rdc_bus": rdc_bus,
            "grp_bus": grp_bus,
            "segment_store": segment_store,
            "compressor": compressor,
            "world_db": world_db,
            "sim_id": str(sim_id),
        }
        try:
            world_step = WorldStep(**kwargs)
        except TypeError as exc:
            logger.warning(
                "WorldStep kwargs mismatch (%s); falling back to positional ctor", exc
            )
            try:
                world_step = WorldStep(
                    world_state, perception, dispatcher
                )  # type: ignore[arg-type]
            except Exception as exc2:  # noqa: BLE001
                logger.error("WorldStep ctor failed: %s", exc2)
                world_step = None

    # ---- 10. IPC server --------------------------------------------------
    from agent_world.ipc.commands import CommandType
    from agent_world.ipc.server import IPCServer

    ipc_server = IPCServer(
        simulation_dir=sim_dir,
        world_state=world_state,
        script_engine=script_engine,
    )
    _wire_ipc_handlers(
        ipc_server,
        world_state=world_state,
        place_store=place_store,
        script_engine=script_engine,
        scripts_yaml=scripts_yaml,
    )

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    ipc_task = asyncio.create_task(ipc_server.run_forever(), name="ipc_server")

    # ---- 11. main loop ---------------------------------------------------
    try:
        if world_step is not None and hasattr(world_step, "run_one_tick"):
            await _run_main_loop(world_step, total_ticks, stop_event)
        elif world_step is not None and hasattr(world_step, "run_loop"):
            # LAYOUT §6.1 alternate API.
            await world_step.run_loop(num_ticks=total_ticks)
        else:
            logger.warning(
                "WorldStep unavailable; ticking clock for %d steps as fallback.",
                total_ticks,
            )
            for _ in range(total_ticks):
                if stop_event.is_set():
                    break
                clock.advance(1)
                await asyncio.sleep(0)
    except asyncio.CancelledError:
        logger.info("main loop cancelled")
    finally:
        # ---- 12. shutdown ------------------------------------------------
        try:
            await graph_manager.flush_all(sim_id=str(sim_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph_manager.flush_all failed: %s", exc)
        try:
            await pool_manager.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.warning("pool_manager.shutdown failed: %s", exc)
        try:
            action_logger.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("action_logger.close failed: %s", exc)
        try:
            ipc_server.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ipc_server.stop failed: %s", exc)
        ipc_task.cancel()
        try:
            await ipc_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            world_db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("world_db.close failed: %s", exc)

    logger.info("agent_world runner exit (sim_id=%s)", sim_id)
    return 0


# --------------------------------------------------------------------------- #
# Sub-helpers                                                                 #
# --------------------------------------------------------------------------- #


def _attrs_json(attrs: Dict[str, Any]) -> str:
    import json

    try:
        return json.dumps(attrs or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


async def _seed_agents(
    config: Any,
    world_db: Any,
    place_store: Any,
    relation_graph: Any,
    capability_table: Any,
) -> None:
    """Apply ``agent_configs[*].location/relations/capabilities`` to world.db.

    All writes are best-effort: a malformed agent block is logged and
    skipped. ``soul / long_term_goal / current_state`` are pushed into a
    lightweight :class:`AgentRuntime` once :class:`WorldState` exists --
    that registration happens after this seeding step.
    """
    agent_configs = getattr(config, "agent_configs", None) or []
    for ac in agent_configs:
        try:
            agent_id = int(_get(ac, "agent_id"))
        except (TypeError, ValueError):
            logger.warning("skip agent without numeric agent_id: %r", ac)
            continue

        # location
        loc = _get(ac, "location")
        if isinstance(loc, str) and loc:
            try:
                await world_db.set_location(agent_id, loc, t=0)
                place_store.L[agent_id] = loc
                place_store._agents_at.setdefault(loc, set()).add(agent_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent %s set_location failed: %s", agent_id, exc)

        # capabilities
        caps = _get(ac, "capabilities") or []
        for cap in caps:
            try:
                await world_db.grant(agent_id, str(cap), granted_at=0)
                capability_table.by_agent.setdefault(agent_id, set()).add(str(cap))
                capability_table.by_capability.setdefault(str(cap), set()).add(agent_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent %s grant(%s) failed: %s", agent_id, cap, exc
                )

        # relations: list[dict{type, target}]
        rels = _get(ac, "relations") or []
        for r in rels:
            try:
                rtype = str(_get(r, "type") or _get(r, "relation_type"))
                tgt = int(_get(r, "target") or _get(r, "dst_agent"))
                await world_db.add_relation(
                    src_agent=agent_id,
                    dst_agent=tgt,
                    relation_type=rtype,
                    created_at=0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent %s add_relation(%r) failed: %s", agent_id, r, exc
                )


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# --------------------------------------------------------------------------- #
# Main loop                                                                   #
# --------------------------------------------------------------------------- #


async def _run_main_loop(
    world_step: Any,
    total_ticks: int,
    stop_event: asyncio.Event,
) -> None:
    """Tick ``world_step.run_one_tick()`` until ``total_ticks`` or stop_event."""
    for tick in range(total_ticks):
        if stop_event.is_set():
            logger.info("stop_event set; breaking main loop at tick=%d", tick)
            break
        try:
            await world_step.run_one_tick()
        except Exception as exc:  # noqa: BLE001
            logger.exception("world_step.run_one_tick failed at tick=%d: %s", tick, exc)
            # Don't propagate -- per impl/runner.md keep ticking so IPC stays
            # responsive; production builds may want a fail-fast knob here.
        if (tick + 1) % 10 == 0 or tick == 0:
            logger.info("tick %d/%d complete", tick + 1, total_ticks)


# --------------------------------------------------------------------------- #
# IPC handler wiring                                                          #
# --------------------------------------------------------------------------- #


def _wire_ipc_handlers(
    ipc_server: Any,
    *,
    world_state: Any,
    place_store: Any,
    script_engine: Any,
    scripts_yaml: str,
) -> None:
    """Replace the L0 stub handlers with real implementations."""
    from agent_world.ipc.commands import CommandType

    async def handle_inject_script_event(payload: Dict[str, Any]) -> Dict[str, Any]:
        event = payload.get("event") or {}
        try:
            # Use ScriptLoader.load_dict to parse a single inline event.
            from agent_world.script.loader import ScriptLoader

            result = ScriptLoader.load_dict(
                {"events": [event]},
                existing_ids=script_engine.loaded_event_ids,
            )
            for ev in result.events:
                script_engine.events_by_id[ev.id] = ev
                script_engine.loaded_event_ids.add(ev.id)
            return {"event_id": event.get("id", ""), "added": len(result.events)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("INJECT_SCRIPT_EVENT failed: %s", exc)
            return {"event_id": event.get("id", ""), "error": str(exc)}

    async def handle_reload_scripts(payload: Dict[str, Any]) -> Dict[str, Any]:
        path = payload.get("scripts_path") or scripts_yaml
        if not os.path.exists(path):
            return {"added_event_ids": [], "skipped_expired": [], "error": "file not found"}
        try:
            stats = await script_engine.load_from_yaml(path, world=world_state)
            return {
                "added_event_ids": [],  # detailed list left for L3+ extension
                "skipped_expired": [],
                "stats": stats,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("RELOAD_SCRIPTS failed: %s", exc)
            return {"added_event_ids": [], "skipped_expired": [], "error": str(exc)}

    async def handle_list_places(payload: Dict[str, Any]) -> Dict[str, Any]:
        places = []
        for rec in place_store.all_places():
            places.append(
                {
                    "place_id": rec.place_id,
                    "parent_id": rec.parent_id,
                    "place_type": rec.place_type,
                    "capacity": rec.capacity,
                    "attrs": dict(rec.attrs),
                }
            )
        agent_locations = dict(place_store.L)
        return {"places": places, "agent_locations": agent_locations}

    async def handle_move_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            agent_id = int(payload["agent_id"])
            new_place = str(payload["place_id"])
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": f"bad payload: {exc}"}
        old_place = place_store.L_t(agent_id) or ""
        try:
            await place_store.move(
                agent_id, new_place, world=world_state, t=world_state.t
            )
            return {"old_place": old_place, "new_place": new_place}
        except Exception as exc:  # noqa: BLE001
            return {"old_place": old_place, "new_place": new_place, "error": str(exc)}

    async def handle_close_env(payload: Dict[str, Any]) -> Dict[str, Any]:
        ipc_server.stop()
        return {}

    ipc_server.register_handler(
        CommandType.INJECT_SCRIPT_EVENT, handle_inject_script_event
    )
    ipc_server.register_handler(CommandType.RELOAD_SCRIPTS, handle_reload_scripts)
    ipc_server.register_handler(CommandType.LIST_PLACES, handle_list_places)
    ipc_server.register_handler(CommandType.MOVE_AGENT, handle_move_agent)
    ipc_server.register_handler(CommandType.CLOSE_ENV, handle_close_env)


# --------------------------------------------------------------------------- #
# Signal handling                                                             #
# --------------------------------------------------------------------------- #


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called outside an event loop — fall back to sync signal.signal.
        loop = None

    def _handle_signal(signame: str) -> None:
        logger.info("signal %s received; stopping", signame)
        stop_event.set()

    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        installed = False
        if loop is not None:
            try:
                loop.add_signal_handler(sig, _handle_signal, signame)
                installed = True
            except (NotImplementedError, RuntimeError):
                installed = False
        if not installed:
            try:
                signal.signal(sig, lambda *_: stop_event.set())
            except (OSError, ValueError):  # pragma: no cover
                pass


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def cli(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    _setup_logging(args.log_level, args.simulation_dir)
    return asyncio.run(
        main(
            simulation_id=args.simulation_id,
            simulation_dir=args.simulation_dir,
            config_path=args.config,
            num_ticks=args.num_ticks,
        )
    )


if __name__ == "__main__":
    sys.exit(cli())


__all__ = ["main", "cli"]
