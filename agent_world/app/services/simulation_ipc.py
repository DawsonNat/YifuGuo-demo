"""File-based IPC between the Flask backend and the simulation child process.

ADAPT from MiroFish ``backend/app/services/simulation_ipc.py`` (L1-394).
Adds the 4 Agent World commands listed in LAYOUT v0.3 §7.2:

* ``INJECT_SCRIPT_EVENT`` -- runtime script-event injection.
* ``RELOAD_SCRIPTS``      -- C2 incremental ``scripts.yaml`` re-read.
* ``LIST_PLACES``         -- UI query for current world topology.
* ``MOVE_AGENT``          -- UI-forced agent relocation.

Each command pairs a client-side ``send_xxx()`` helper with a server-side
handler dispatch on the runner. The protocol unchanged from MiroFish:

1. Client writes ``ipc_commands/<command_id>.json``.
2. Runner polls ``ipc_commands/`` and writes ``ipc_responses/<command_id>.json``.
3. Client polls ``ipc_responses/`` and consumes both files on success.

The :class:`CommandType` / :class:`CommandStatus` enums are re-exports of
``agent_world.ipc.commands`` (single source of truth so the runner side and
this client agree).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# Single source of truth for the enum — defined under ``agent_world/ipc``.
from agent_world.ipc.commands import CommandStatus, CommandType

logger = logging.getLogger("agent_world.simulation_ipc")


# --------------------------------------------------------------------------- #
# Wire-format dataclasses (verbatim shape from MiroFish; only naming clarified)
# --------------------------------------------------------------------------- #


@dataclass
class IPCCommand:
    """A single IPC request payload."""

    command_id: str
    command_type: CommandType
    args: Dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": self.command_type.value,
            "args": self.args,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IPCCommand":
        return cls(
            command_id=data["command_id"],
            command_type=CommandType(data["command_type"]),
            args=data.get("args", {}),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
        )


@dataclass
class IPCResponse:
    """A single IPC response payload."""

    command_id: str
    status: CommandStatus
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IPCResponse":
        return cls(
            command_id=data["command_id"],
            status=CommandStatus(data["status"]),
            result=data.get("result"),
            error=data.get("error"),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
        )


# --------------------------------------------------------------------------- #
# Client (Flask side)
# --------------------------------------------------------------------------- #


class SimulationIPCClient:
    """File-IPC client used from the Flask process / API layer."""

    def __init__(self, simulation_dir: str) -> None:
        self.simulation_dir = simulation_dir
        self.commands_dir = os.path.join(simulation_dir, "ipc_commands")
        self.responses_dir = os.path.join(simulation_dir, "ipc_responses")
        os.makedirs(self.commands_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)

    # ---- generic send / wait ------------------------------------------------

    def send_command(
        self,
        command_type: CommandType,
        args: Dict[str, Any],
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> IPCResponse:
        """Write a command file and block until the runner replies (or timeout)."""
        command_id = str(uuid.uuid4())
        command = IPCCommand(command_id=command_id, command_type=command_type, args=args)

        command_file = os.path.join(self.commands_dir, f"{command_id}.json")
        with open(command_file, "w", encoding="utf-8") as f:
            json.dump(command.to_dict(), f, ensure_ascii=False, indent=2)

        logger.info(
            "IPC send: %s command_id=%s args_keys=%s",
            command_type.value,
            command_id,
            list(args.keys()),
        )

        response_file = os.path.join(self.responses_dir, f"{command_id}.json")
        deadline = time.time() + timeout

        while time.time() < deadline:
            if os.path.exists(response_file):
                try:
                    with open(response_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    response = IPCResponse.from_dict(data)
                    # Best-effort cleanup of both files.
                    for path in (command_file, response_file):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                    logger.info(
                        "IPC recv: command_id=%s status=%s",
                        command_id,
                        response.status.value,
                    )
                    return response
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.warning("IPC parse failed: %s", exc)
            time.sleep(poll_interval)

        # Timeout — drop the command file so the runner doesn't keep retrying.
        logger.error("IPC timeout: command_id=%s", command_id)
        try:
            os.remove(command_file)
        except OSError:
            pass
        raise TimeoutError(
            f"IPC command {command_type.value} timed out after {timeout:.1f}s"
        )

    # ---- MiroFish (KEPT) ----------------------------------------------------

    def send_interview(
        self,
        agent_id: int,
        prompt: str,
        platform: Optional[str] = None,
        timeout: float = 60.0,
    ) -> IPCResponse:
        args: Dict[str, Any] = {"agent_id": agent_id, "prompt": prompt}
        if platform:
            args["platform"] = platform
        return self.send_command(CommandType.INTERVIEW, args, timeout=timeout)

    def send_batch_interview(
        self,
        interviews: List[Dict[str, Any]],
        platform: Optional[str] = None,
        timeout: float = 120.0,
    ) -> IPCResponse:
        args: Dict[str, Any] = {"interviews": interviews}
        if platform:
            args["platform"] = platform
        return self.send_command(CommandType.BATCH_INTERVIEW, args, timeout=timeout)

    def send_close_env(self, timeout: float = 30.0) -> IPCResponse:
        return self.send_command(CommandType.CLOSE_ENV, {}, timeout=timeout)

    # ---- Agent World (NEW, LAYOUT §7.2) ------------------------------------

    def send_inject_script_event(
        self, event: Dict[str, Any], timeout: float = 30.0
    ) -> IPCResponse:
        """Inject a single ad-hoc script event (id / trigger / effect)."""
        return self.send_command(
            CommandType.INJECT_SCRIPT_EVENT, {"event": event}, timeout=timeout
        )

    def send_reload_scripts(
        self, scripts_path: str, timeout: float = 30.0
    ) -> IPCResponse:
        """C2 incremental reload — runner re-reads the YAML file from disk."""
        return self.send_command(
            CommandType.RELOAD_SCRIPTS,
            {"scripts_path": os.path.abspath(scripts_path)},
            timeout=timeout,
        )

    def send_list_places(self, timeout: float = 30.0) -> IPCResponse:
        """Request a snapshot of the current places + ``L_t`` index."""
        return self.send_command(CommandType.LIST_PLACES, {}, timeout=timeout)

    def send_move_agent(
        self, agent_id: int, place_id: str, timeout: float = 30.0
    ) -> IPCResponse:
        """Force-move an agent to ``place_id`` (UI-driven)."""
        return self.send_command(
            CommandType.MOVE_AGENT,
            {"agent_id": agent_id, "place_id": place_id},
            timeout=timeout,
        )

    # ---- env-status helper (KEPT) ------------------------------------------

    def check_env_alive(self) -> bool:
        status_file = os.path.join(self.simulation_dir, "env_status.json")
        if not os.path.exists(status_file):
            return False
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                status = json.load(f)
            return status.get("status") == "alive"
        except (json.JSONDecodeError, OSError):
            return False


# --------------------------------------------------------------------------- #
# Server (runner side)
# --------------------------------------------------------------------------- #


class SimulationIPCServer:
    """File-IPC server — polled from inside the simulation child process.

    The runner registers handlers via :meth:`register_handler` and then loops
    on :meth:`poll_commands` -> dispatch -> :meth:`send_response`. The 4 new
    Agent World handlers are wired by ``agent_world.runner.run_agent_world_simulation``.
    """

    def __init__(self, simulation_dir: str) -> None:
        self.simulation_dir = simulation_dir
        self.commands_dir = os.path.join(simulation_dir, "ipc_commands")
        self.responses_dir = os.path.join(simulation_dir, "ipc_responses")
        os.makedirs(self.commands_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)
        self._running = False
        # CommandType -> callable(args: dict) -> dict | None
        self._handlers: Dict[CommandType, Any] = {}

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._update_env_status("alive")

    def stop(self) -> None:
        self._running = False
        self._update_env_status("stopped")

    def _update_env_status(self, status: str) -> None:
        path = os.path.join(self.simulation_dir, "env_status.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"status": status, "timestamp": datetime.now().isoformat()},
                f,
                ensure_ascii=False,
                indent=2,
            )

    # ---- handler registry --------------------------------------------------

    def register_handler(self, command_type: CommandType, handler: Any) -> None:
        """Register a callable for a given CommandType.

        Handler signature: ``handler(args: dict) -> dict | None``.
        Raising any ``Exception`` is converted to a FAILED response.
        """
        self._handlers[command_type] = handler

    # ---- poll / dispatch ---------------------------------------------------

    def poll_commands(self) -> Optional[IPCCommand]:
        if not os.path.exists(self.commands_dir):
            return None
        files: List[tuple[str, float]] = []
        for name in os.listdir(self.commands_dir):
            if name.endswith(".json"):
                p = os.path.join(self.commands_dir, name)
                files.append((p, os.path.getmtime(p)))
        files.sort(key=lambda x: x[1])
        for path, _ in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return IPCCommand.from_dict(data)
            except (json.JSONDecodeError, KeyError, OSError) as exc:
                logger.warning("read command %s failed: %s", path, exc)
                continue
        return None

    def dispatch(self, command: IPCCommand) -> IPCResponse:
        """Run the registered handler for ``command`` and build a response."""
        handler = self._handlers.get(command.command_type)
        if handler is None:
            return IPCResponse(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                error=f"no handler registered for {command.command_type.value}",
            )
        try:
            result = handler(command.args) or {}
            return IPCResponse(
                command_id=command.command_id,
                status=CommandStatus.COMPLETED,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001 — surface any handler failure
            logger.exception("handler %s failed", command.command_type.value)
            return IPCResponse(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                error=str(exc),
            )

    # ---- response writers --------------------------------------------------

    def send_response(self, response: IPCResponse) -> None:
        path = os.path.join(self.responses_dir, f"{response.command_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(response.to_dict(), f, ensure_ascii=False, indent=2)
        # Drop the corresponding command file.
        cmd_path = os.path.join(self.commands_dir, f"{response.command_id}.json")
        try:
            os.remove(cmd_path)
        except OSError:
            pass

    def send_success(self, command_id: str, result: Dict[str, Any]) -> None:
        self.send_response(
            IPCResponse(
                command_id=command_id, status=CommandStatus.COMPLETED, result=result
            )
        )

    def send_error(self, command_id: str, error: str) -> None:
        self.send_response(
            IPCResponse(command_id=command_id, status=CommandStatus.FAILED, error=error)
        )


__all__ = [
    "CommandStatus",
    "CommandType",
    "IPCCommand",
    "IPCResponse",
    "SimulationIPCClient",
    "SimulationIPCServer",
]
