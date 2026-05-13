"""Agent World Flask services package (L3, LAYOUT §2.B).

ADAPT/COPY from MiroFish ``backend/app/services/*``. The HTTP layer in
``agent_world/app/api/*`` calls into these modules; the simulation core
(``agent_world/{world,buses,pools,...}``) lives behind a child process boundary
spoken to via the file-based IPC protocol in ``agent_world/ipc/``.

Public surface (cf. ``docs/impl/app_services.md`` §5):

* :class:`SimulationManager` -- subprocess lifecycle (start/stop/status).
* :class:`SimulationRunner`  -- attach monitor thread + actions.jsonl tail.
* :class:`SimulationIPCClient` -- file IPC with the 3 MiroFish + 4 new
  Agent World commands (``INJECT_SCRIPT_EVENT`` / ``RELOAD_SCRIPTS`` /
  ``LIST_PLACES`` / ``MOVE_AGENT``).
* :func:`generate_config` -- LLM-driven simulation_config.json builder
  with the three new top-level keys ``world_config / channel_config /
  memory_config``.
* :class:`ReportAgent` -- cross-DB report assembler (world.db + pool_*.db).
* ``zep_tools`` -- Zep wrappers (verbatim from MiroFish; already accept
  ``graph_id``).
"""

from .simulation_ipc import (
    CommandStatus,
    CommandType,
    IPCCommand,
    IPCResponse,
    SimulationIPCClient,
    SimulationIPCServer,
)

__all__ = [
    "CommandStatus",
    "CommandType",
    "IPCCommand",
    "IPCResponse",
    "SimulationIPCClient",
    "SimulationIPCServer",
]
