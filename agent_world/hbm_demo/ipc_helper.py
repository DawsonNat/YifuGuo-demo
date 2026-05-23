"""Flask-side IPC helpers for HBM demo (batch inject / move)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_world.app.services.simulation_ipc import IPCResponse, SimulationIPCClient
from agent_world.ipc.commands import CommandType


def get_ipc_client(sim_dir: str) -> SimulationIPCClient:
    """Return a file-IPC client for the given simulation directory."""
    return SimulationIPCClient(sim_dir)


def send_inject_batch(
    client: SimulationIPCClient,
    *,
    events: List[Dict[str, Any]],
    tick_count: int = 6,
    broadcast: Optional[Dict[str, Any]] = None,
    timeout: float = 600.0,
) -> IPCResponse:
    """Send batch script inject with optional broadcast and tick_count."""
    payload: Dict[str, Any] = {"events": events, "tick_count": tick_count}
    if broadcast:
        payload["broadcast"] = broadcast
    return client.send_command(
        CommandType.INJECT_SCRIPT_EVENT,
        payload,
        timeout=timeout,
    )


def send_move_agent(
    client: SimulationIPCClient,
    *,
    agent_id: int,
    place_id: str,
    timeout: float = 30.0,
) -> IPCResponse:
    """Force-move an agent via IPC MOVE_AGENT."""
    return client.send_command(
        CommandType.MOVE_AGENT,
        {"agent_id": int(agent_id), "place_id": str(place_id)},
        timeout=timeout,
    )
