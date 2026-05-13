"""IPC CommandType enum.

ADAPT from MiroFish ``backend/app/services/simulation_ipc.py`` (L25-29).
Keeps the 3 MiroFish commands and adds 4 Agent World commands listed in
LAYOUT v0.3 §7.2.
"""

from enum import Enum


class CommandType(str, Enum):
    """All IPC command types shared between client and server."""

    # ---- MiroFish (KEEP) ----
    INTERVIEW = "INTERVIEW"
    BATCH_INTERVIEW = "BATCH_INTERVIEW"
    CLOSE_ENV = "CLOSE_ENV"

    # ---- Agent World (NEW, LAYOUT §7.2) ----
    INJECT_SCRIPT_EVENT = "INJECT_SCRIPT_EVENT"
    RELOAD_SCRIPTS = "RELOAD_SCRIPTS"  # C2: incremental scripts.yaml append
    LIST_PLACES = "LIST_PLACES"
    MOVE_AGENT = "MOVE_AGENT"


class CommandStatus(str, Enum):
    """Response status enum (sym MiroFish)."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
