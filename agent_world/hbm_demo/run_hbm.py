"""HBM demo Runner — Phase 1: seed world + LLM agents + IPC inject tick loop.

Usage:
    python -m agent_world.hbm_demo.run_hbm \\
        --config agent_world/hbm_demo/hbm_scenario.yaml \\
        --sim-dir agent_world/hbm_demo/sim/hbm_memory_war/
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

from agent_world.hbm_demo.config_loader import load_scenario
from agent_world.hbm_demo.env_status import patch_ipc_server_env_status, write_env_status
from agent_world.hbm_demo.ipc_handlers import wire_handlers
from agent_world.hbm_demo.kernel import build_kernel
from agent_world.ipc.server import IPCServer

log = logging.getLogger("agent_world.hbm_demo")

_PKG_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = _PKG_DIR / "hbm_scenario.yaml"
DEFAULT_SIM_DIR = _PKG_DIR / "sim" / "hbm_memory_war"


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HBM demo simulation runner")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to hbm_scenario.yaml",
    )
    parser.add_argument(
        "--sim-dir",
        type=Path,
        default=DEFAULT_SIM_DIR,
        help="Simulation directory (world.db + ipc + env_status.json)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


async def _run(sim_dir: Path, scenario: dict) -> int:
    kernel = await build_kernel(scenario, sim_dir)
    sim_dir_str = str(sim_dir.resolve())

    write_env_status(sim_dir_str, kernel.clock.t, status="starting")

    ipc_server = IPCServer(
        simulation_dir=sim_dir_str,
        world_state=kernel.world_state,
        script_engine=kernel.script_engine,
    )
    patch_ipc_server_env_status(
        ipc_server,
        sim_dir_str,
        lambda: int(kernel.world_state.clock.t),
    )
    wire_handlers(
        ipc_server,
        world_db=kernel.world_db,
        world_state=kernel.world_state,
        place_store=kernel.place_store,
        script_engine=kernel.script_engine,
        world_step=kernel.world_step,
        sim_dir=sim_dir_str,
        get_current_tick=lambda: int(kernel.world_state.clock.t),
    )

    write_env_status(sim_dir_str, kernel.clock.t, status="running")
    log.info(
        "HBM runner ready sim_dir=%s simulation_id=%s agents=%d places=%d",
        sim_dir_str,
        scenario.get("simulation_id"),
        len(kernel.agents),
        len(scenario.get("places", [])),
    )

    def _request_stop(*_args: object) -> None:
        log.info("shutdown requested")
        ipc_server.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop())

    try:
        await ipc_server.run_forever()
    finally:
        write_env_status(sim_dir_str, kernel.clock.t, status="stopped")
        kernel.world_db.close()
        log.info("HBM runner exit")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = args.config.resolve()
    sim_dir = args.sim_dir.resolve()

    if not config_path.is_file():
        log.error("config not found: %s", config_path)
        return 1

    scenario = load_scenario(config_path)
    try:
        return asyncio.run(_run(sim_dir, scenario))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
