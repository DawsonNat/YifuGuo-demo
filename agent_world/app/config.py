"""Flask config (mirrors MiroFish ``backend/app/config.py`` minimal subset).

Only env-driven fields needed for the L0 outer shell are kept; expand
with Zep / OpenAI / Anthropic blocks when L3 services come online.
"""

from __future__ import annotations

import os


class Config:
    """Flask configuration class."""

    # ---- Flask basics ----
    SECRET_KEY = os.environ.get("SECRET_KEY", "agent-world-secret-key")
    DEBUG = os.environ.get("FLASK_DEBUG", "True").lower() == "true"
    HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
    PORT = int(os.environ.get("FLASK_PORT", "5000"))

    # JSON: keep CJK readable.
    JSON_AS_ASCII = False

    # ---- Simulation directories (used by IPC + services in L3) ----
    SIMULATIONS_DIR = os.environ.get(
        "AGENT_WORLD_SIMULATIONS_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "simulations"),
    )
    WORLD_DB_PATH_TEMPLATE = "{simulation_dir}/world.db"
    POOL_DB_PATH_TEMPLATE = "{simulation_dir}/pools/pool__{place}__{feed}.db"

    # ---- LLM / Zep (placeholders so L3 services can read them) ----
    LLM_API_KEY = os.environ.get("LLM_API_KEY")
    LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "gpt-4o-mini")
    ZEP_API_KEY = os.environ.get("ZEP_API_KEY")
    ZEP_BASE_URL = os.environ.get("ZEP_BASE_URL")

    @classmethod
    def validate(cls) -> list[str]:
        """Return a list of missing-required-config error strings."""
        errors: list[str] = []
        # L0 has no hard requirements; L3 will append checks here.
        return errors
