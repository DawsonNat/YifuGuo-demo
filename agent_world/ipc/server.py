"""IPC server used by the simulation runner subprocess.

COPY + ADAPT from MiroFish ``backend/app/services/simulation_ipc.py``.
Provides a polling loop over ``ipc_commands/`` plus a handler dispatch
table. Real handlers are wired at L3+ (see ``runner.md``); the
defaults here are stubs that echo a placeholder response so the
outer-shell surface is callable end-to-end before the kernel exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from .commands import CommandStatus, CommandType

logger = logging.getLogger("agent_world.ipc.server")

IPC_POLL_INTERVAL_SERVER = 0.1

Handler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


class IPCServer:
    """File-IPC server. One instance per simulation runner.

    Construction does **not** require a ``WorldState`` / ``ScriptEngine``
    yet (those are L3+); pass ``None`` and register real handlers later
    via :meth:`register_handler`.
    """

    def __init__(
        self,
        simulation_dir: str,
        world_state: Any = None,
        script_engine: Any = None,
    ) -> None:
        self.simulation_dir = simulation_dir
        self.commands_dir = os.path.join(simulation_dir, "ipc_commands")
        self.responses_dir = os.path.join(simulation_dir, "ipc_responses")
        os.makedirs(self.commands_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)

        self.world_state = world_state
        self.script_engine = script_engine

        self._running = False
        self._handlers: Dict[CommandType, Handler] = {}
        self._register_default_handlers()

    # ---- registration --------------------------------------------------------

    def register_handler(
        self, cmd_type: CommandType, handler: Handler
    ) -> None:
        """Register a real coroutine handler (overrides the stub)."""
        self._handlers[cmd_type] = handler

    def _register_default_handlers(self) -> None:
        self._handlers[CommandType.INTERVIEW] = self.handle_interview
        self._handlers[CommandType.BATCH_INTERVIEW] = self.handle_batch_interview
        self._handlers[CommandType.CLOSE_ENV] = self.handle_close_env
        self._handlers[CommandType.INJECT_SCRIPT_EVENT] = (
            self.handle_inject_script_event
        )
        self._handlers[CommandType.RELOAD_SCRIPTS] = self.handle_reload_scripts
        self._handlers[CommandType.LIST_PLACES] = self.handle_list_places
        self._handlers[CommandType.MOVE_AGENT] = self.handle_move_agent

    # ---- lifecycle -----------------------------------------------------------

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

    # ---- main loop -----------------------------------------------------------

    async def run_forever(
        self, poll_interval: float = IPC_POLL_INTERVAL_SERVER
    ) -> None:
        """Poll ``ipc_commands/`` and dispatch each command concurrently."""
        self.start()
        try:
            while self._running:
                cmds = self._collect_pending_commands()
                for cmd in cmds:
                    asyncio.create_task(self._handle_one(cmd))
                await asyncio.sleep(poll_interval)
        finally:
            self.stop()

    def _collect_pending_commands(self):
        """Read + delete every JSON file in commands/. Returns a list of dicts."""
        out = []
        if not os.path.isdir(self.commands_dir):
            return out
        try:
            entries = sorted(
                (
                    e
                    for e in os.listdir(self.commands_dir)
                    if e.endswith(".json") and not e.startswith(".")
                ),
                key=lambda n: os.path.getmtime(
                    os.path.join(self.commands_dir, n)
                ),
            )
        except OSError:
            return out
        for name in entries:
            path = os.path.join(self.commands_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                os.remove(path)  # consumed
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("failed to read command %s: %s", path, e)
                continue
            out.append(data)
        return out

    async def _handle_one(self, cmd: Dict[str, Any]) -> None:
        seq = cmd.get("command_id", "")
        try:
            cmd_type = CommandType(cmd["command_type"])
        except (KeyError, ValueError) as e:
            self._write_response(seq, ok=False, data=None, error=f"bad command: {e}")
            return
        handler = self._handlers.get(cmd_type)
        if handler is None:
            self._write_response(
                seq, ok=False, data=None, error=f"no handler for {cmd_type}"
            )
            return
        payload = cmd.get("args", {}) or {}
        try:
            data = await handler(payload)
            self._write_response(seq, ok=True, data=data, error=None)
        except Exception as e:  # noqa: BLE001 — surface to client
            logger.exception("handler %s failed", cmd_type)
            self._write_response(seq, ok=False, data=None, error=str(e))

    def _write_response(
        self,
        seq: str,
        *,
        ok: bool,
        data: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> None:
        body = {
            "command_id": seq,
            "status": (
                CommandStatus.COMPLETED.value if ok else CommandStatus.FAILED.value
            ),
            "ok": ok,
            "result": data,
            "error": error,
            "timestamp": datetime.now().isoformat(),
        }
        target = os.path.join(self.responses_dir, f"{seq}.json")
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{seq}.", suffix=".json.tmp", dir=self.responses_dir
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

    # ---- handler stubs (real impls land in L3+) -----------------------------
    # Each handler returns the ``data`` field that the client sees inside
    # ``{"ok": True, "result": ..., ...}``. Keep return shapes aligned with
    # ipc_layer.md §3.2.

    async def handle_interview(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # TODO L3: wire to services (OASIS interview round-trip)
        return {"response": "<stub: INTERVIEW not wired yet>"}

    async def handle_batch_interview(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        # TODO L3: wire to services
        return {"responses": {}}

    async def handle_close_env(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # TODO L3: wire to services (graceful runner shutdown)
        self.stop()
        return {}

    async def handle_inject_script_event(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        # TODO L3: wire to ScriptEngine.inject_event(event)
        event = payload.get("event") or {}
        return {"event_id": event.get("id", "")}

    async def handle_reload_scripts(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        # TODO L3: wire to ScriptEngine.reload_from_yaml(path)
        return {"added_event_ids": [], "skipped_expired": []}

    async def handle_list_places(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # TODO L3: SELECT world.db.place + world.db.agent_location
        return {"places": [], "agent_locations": {}}

    async def handle_move_agent(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # TODO L3: wire to WorldState.places.force_move(agent_id, place_id)
        return {
            "old_place": "",
            "new_place": payload.get("place_id", ""),
        }
