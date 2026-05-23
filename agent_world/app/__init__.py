"""Agent World Flask app package.

COPY + ADAPT from MiroFish ``backend/app/__init__.py``. Real services /
DB / Zep wiring lives at L3+ in ``app/services/*``; this L0 shell only
wires blueprints and config so the HTTP surface is callable.
"""

from __future__ import annotations

from flask import Flask

from .config import Config


def create_app(config_class: type = Config) -> Flask:
    """Flask application factory."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    if hasattr(app, "json") and hasattr(app.json, "ensure_ascii"):
        app.json.ensure_ascii = False

    # Register blueprints. Imported here (not at module top) to avoid
    # circular imports during partial L0 wiring.
    from .api import simulation_bp
    from agent_world.hbm_demo.routes import hbm_bp

    app.register_blueprint(simulation_bp, url_prefix="/api/simulation")
    app.register_blueprint(hbm_bp, url_prefix="/api/hbm")

    @app.route("/health")
    def health() -> dict:
        return {"status": "ok", "service": "agent_world"}

    return app
