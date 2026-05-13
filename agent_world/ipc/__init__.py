"""Agent World file-based IPC layer.

Bridges the Flask backend (web server process) with the simulation
sub-process via JSON command/response files written to:

    {simulation_dir}/ipc_commands/<seq>.json    # client -> server
    {simulation_dir}/ipc_responses/<seq>.json   # server -> client

L0 (zero-dependency) outer shell. Real handlers wire in at L3+ (see
``app_services.md`` and ``runner.md``).
"""

from .commands import CommandType
from .client import IPCClient
from .server import IPCServer

__all__ = ["CommandType", "IPCClient", "IPCServer"]
