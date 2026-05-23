"""Load HBM scenario YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml


def load_scenario(config_path: Path) -> Dict[str, Any]:
    with config_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"scenario root must be a mapping: {config_path}")
    return data
