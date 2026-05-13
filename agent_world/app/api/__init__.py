"""Flask blueprints for Agent World HTTP API."""

from flask import Blueprint

simulation_bp = Blueprint("simulation", __name__)

# Import side-effects register the route handlers on the blueprints above.
from . import simulation  # noqa: E402, F401

__all__ = ["simulation_bp"]
