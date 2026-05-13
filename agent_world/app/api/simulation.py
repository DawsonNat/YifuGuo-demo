"""Simulation HTTP routes (L0 outer shell).

ADAPT from MiroFish ``backend/app/api/simulation.py``. The full
MiroFish lifecycle endpoints (start / stop / status / interview / ...)
will be ported in L3 alongside the services layer; here we provide
just the 5 new Agent World routes with stub handlers so the surface is
exercisable end-to-end.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import jsonify, request

from . import simulation_bp


def _json_body() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


# ---- POST /simulations/<id>/inject-event ------------------------------------


@simulation_bp.route("/simulations/<sim_id>/inject-event", methods=["POST"])
def inject_event(sim_id: str):
    """Hot-inject one script event into the live simulation."""
    body = _json_body()
    event = body.get("event") or {}
    # TODO L3: wire to services (simulation_ipc.send_inject_script_event)
    return (
        jsonify(
            {
                "success": True,
                "simulation_id": sim_id,
                "stub": True,
                "data": {"event_id": event.get("id", "")},
            }
        ),
        200,
    )


# ---- POST /simulations/<id>/reload-scripts ----------------------------------


@simulation_bp.route("/simulations/<sim_id>/reload-scripts", methods=["POST"])
def reload_scripts(sim_id: str):
    """Incrementally append events from a YAML file (C2)."""
    body = _json_body()
    scripts_path = body.get("scripts_path", "")
    # TODO L3: wire to services (simulation_ipc.send_reload_scripts)
    return (
        jsonify(
            {
                "success": True,
                "simulation_id": sim_id,
                "stub": True,
                "data": {
                    "scripts_path": scripts_path,
                    "added_event_ids": [],
                    "skipped_expired": [],
                },
            }
        ),
        200,
    )


# ---- GET /simulations/<id>/places -------------------------------------------


@simulation_bp.route("/simulations/<sim_id>/places", methods=["GET"])
def list_places(sim_id: str):
    """List places + current agent locations."""
    # TODO L3: wire to services (simulation_ipc.send_list_places)
    return (
        jsonify(
            {
                "success": True,
                "simulation_id": sim_id,
                "stub": True,
                "data": {"places": [], "agent_locations": {}},
            }
        ),
        200,
    )


# ---- POST /simulations/<id>/move-agent --------------------------------------


@simulation_bp.route("/simulations/<sim_id>/move-agent", methods=["POST"])
def move_agent(sim_id: str):
    """Force-move an agent to a place (admin / debug tool)."""
    body = _json_body()
    agent_id = body.get("agent_id")
    place_id = body.get("place_id")
    if agent_id is None or not place_id:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "agent_id and place_id are required",
                }
            ),
            400,
        )
    # TODO L3: wire to services (simulation_ipc.send_move_agent)
    return (
        jsonify(
            {
                "success": True,
                "simulation_id": sim_id,
                "stub": True,
                "data": {
                    "agent_id": agent_id,
                    "old_place": "",
                    "new_place": place_id,
                },
            }
        ),
        200,
    )


# ---- GET /simulations/<id>/world-state --------------------------------------


@simulation_bp.route("/simulations/<sim_id>/world-state", methods=["GET"])
def world_state(sim_id: str):
    """Read-only snapshot of the entire world (places/relations/caps/locs)."""
    # TODO L3: read world.db directly with a READ-ONLY connection
    return (
        jsonify(
            {
                "success": True,
                "simulation_id": sim_id,
                "stub": True,
                "data": {
                    "places": [],
                    "relations": [],
                    "capabilities": [],
                    "agent_locations": {},
                },
            }
        ),
        200,
    )
