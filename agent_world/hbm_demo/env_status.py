"""``env_status.json`` writer — merge ``current_tick`` across IPCServer lifecycle."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# IPCServer (MiroFish legacy) writes ``alive``; HBM docs standardize on ``running``.
_STATUS_NORMALIZE = {"alive": "running"}


def normalize_env_status(status: str) -> str:
    """Map engine IPC status strings to HBM demo convention."""
    return _STATUS_NORMALIZE.get(status, status)


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
        "status": normalize_env_status(status),
        "current_tick": int(current_tick),
        "timestamp": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def read_env_status(sim_dir: str | Path) -> dict[str, Any] | None:
    """Read ``env_status.json``; return ``None`` if missing or invalid."""
    path = Path(sim_dir) / "env_status.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if "status" in data:
        data = dict(data)
        data["status"] = normalize_env_status(str(data["status"]))
    return data


def is_runner_ready(sim_dir: str | Path) -> bool:
    """Return True when Runner is accepting IPC (``status == running``)."""
    data = read_env_status(sim_dir)
    return bool(data and data.get("status") == "running")


def patch_ipc_server_env_status(
    ipc_server: Any,
    sim_dir: str | Path,
    get_current_tick: Callable[[], int],
) -> None:
    """Replace ``IPCServer._update_env_status`` so it keeps ``current_tick``."""

    def _merged_update(status: str) -> None:
        write_env_status(
            sim_dir,
            get_current_tick(),
            status=normalize_env_status(status),
        )

    ipc_server._update_env_status = _merged_update  # type: ignore[method-assign]
