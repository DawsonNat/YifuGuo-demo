"""``env_status.json`` writer — merge ``current_tick`` across IPCServer lifecycle."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def write_env_status(
    sim_dir: str | Path,
    current_tick: int,
    *,
    status: str = "running",
) -> None:
    """Write ``{sim_dir}/env_status.json`` with ``current_tick`` preserved."""
    path = Path(sim_dir) / "env_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "status": status,
        "current_tick": int(current_tick),
        "timestamp": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def patch_ipc_server_env_status(
    ipc_server: Any,
    sim_dir: str | Path,
    get_current_tick: Callable[[], int],
) -> None:
    """Replace ``IPCServer._update_env_status`` so it keeps ``current_tick``."""

    def _merged_update(status: str) -> None:
        write_env_status(sim_dir, get_current_tick(), status=status)

    ipc_server._update_env_status = _merged_update  # type: ignore[method-assign]
