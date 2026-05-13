"""IPC client used by the Flask web process.

COPY + ADAPT from MiroFish ``backend/app/services/simulation_ipc.py``.
The transport (atomic JSON write under ``ipc_commands/`` + poll
``ipc_responses/``) is unchanged. Adds 4 ``send_xxx`` helpers for the
new Agent World commands listed in LAYOUT §7.2.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .commands import CommandStatus, CommandType

# ---- Tunables (also documented in ipc_layer.md §6) ---------------------------
IPC_POLL_INTERVAL_CLIENT = 0.05
IPC_DEFAULT_TIMEOUT = 30.0


class IPCClient:
    """File-IPC client used inside the Flask process.

    One instance per simulation. Methods ``send_xxx`` write the command
    file and return the assigned ``seq`` (string UUID). Callers usually
    pair them with :meth:`wait_response` to block until the runner has
    written the matching response file.
    """

    def __init__(self, simulation_dir: str) -> None:
        self.simulation_dir = simulation_dir
        self.commands_dir = os.path.join(simulation_dir, "ipc_commands")
        self.responses_dir = os.path.join(simulation_dir, "ipc_responses")
        os.makedirs(self.commands_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)

    # ---- low-level transport -------------------------------------------------

    def _write_command(
        self, command_type: CommandType, payload: Dict[str, Any]
    ) -> str:
        seq = str(uuid.uuid4())
        body = {
            "command_id": seq,
            "command_type": command_type.value,
            "args": payload,
            "timestamp": datetime.now().isoformat(),
        }
        target = os.path.join(self.commands_dir, f"{seq}.json")
        # Atomic write: tempfile in same dir + os.rename (POSIX).
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{seq}.", suffix=".json.tmp", dir=self.commands_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=2)
            os.rename(tmp_path, target)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        return seq

    def wait_response(
        self,
        seq: str,
        timeout: float = IPC_DEFAULT_TIMEOUT,
        poll_interval: float = IPC_POLL_INTERVAL_CLIENT,
    ) -> Dict[str, Any]:
        """Block until the runner writes ``{seq}.json`` under responses/.

        Returns the parsed JSON dict. On timeout returns a synthetic
        ``{"ok": False, "error": "timeout"}`` dict instead of raising,
        so the Flask layer can map it to HTTP 504 cleanly.
        """
        response_file = os.path.join(self.responses_dir, f"{seq}.json")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(response_file):
                try:
                    with open(response_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    time.sleep(poll_interval)
                    continue
                # Cleanup
                try:
                    os.remove(response_file)
                except OSError:
                    pass
                return data
            time.sleep(poll_interval)
        # Timeout: leave the command file in place; runner may still
        # process it eventually.
        return {
            "command_id": seq,
            "status": CommandStatus.FAILED.value,
            "ok": False,
            "error": "timeout",
        }

    # ---- MiroFish commands (COPY) -------------------------------------------

    def send_interview(
        self, agent_id: int, prompt: str, platform: Optional[str] = None
    ) -> str:
        payload: Dict[str, Any] = {"agent_id": agent_id, "prompt": prompt}
        if platform:
            payload["platform"] = platform
        return self._write_command(CommandType.INTERVIEW, payload)

    def send_batch_interview(
        self,
        interviews: List[Dict[str, Any]],
        platform: Optional[str] = None,
    ) -> str:
        payload: Dict[str, Any] = {"interviews": interviews}
        if platform:
            payload["platform"] = platform
        return self._write_command(CommandType.BATCH_INTERVIEW, payload)

    def send_close_env(self) -> str:
        return self._write_command(CommandType.CLOSE_ENV, {})

    # ---- Agent World commands (NEW) -----------------------------------------

    def send_inject_script_event(self, event: Dict[str, Any]) -> str:
        """C1: hot-inject a single script event (id/trigger/effect)."""
        return self._write_command(
            CommandType.INJECT_SCRIPT_EVENT, {"event": event}
        )

    def send_reload_scripts(self, scripts_path: str) -> str:
        """C2: incrementally append events from ``scripts_path``."""
        return self._write_command(
            CommandType.RELOAD_SCRIPTS, {"scripts_path": scripts_path}
        )

    def send_list_places(self) -> str:
        """List all places + current agent locations from world.db."""
        return self._write_command(CommandType.LIST_PLACES, {})

    def send_move_agent(self, agent_id: int, place_id: str) -> str:
        """Force-move an agent (triggers BehaviorCompressor.on_move)."""
        return self._write_command(
            CommandType.MOVE_AGENT,
            {"agent_id": agent_id, "place_id": place_id},
        )
