"""Flask Blueprint for HBM demo HTTP API."""

from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint, jsonify, request, session

from agent_world.hbm_demo import game_service as gs
from agent_world.hbm_demo.env_status import read_env_status

hbm_bp = Blueprint("hbm", __name__)

SIM_ID = gs.DEFAULT_SIM_ID


def _json_body() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


def _bad_request(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


def _check_sim_id(sim_id: str):
    if sim_id != SIM_ID:
        return _bad_request(f"unknown simulation_id: {sim_id}", 404)
    return None


@hbm_bp.route("/simulations/<sim_id>/session/start", methods=["POST"])
def session_start(sim_id: str):
    """Initialize Flask session with default stats / phase / place_id."""
    err = _check_sim_id(sim_id)
    if err:
        return err

    hbm = gs.create_session()
    gs.save_session(session, hbm, sim_id)
    env = read_env_status(gs.get_sim_dir()) or {}

    return jsonify(
        {
            "success": True,
            "data": {
                "task_id": hbm.task_id,
                "start_tick": hbm.start_tick,
                "place_id": hbm.place_id,
                "phase": hbm.phase,
                "player_turn": hbm.player_turn,
                "stats": hbm.stats,
                "env_status": env,
            },
        }
    )


@hbm_bp.route("/simulations/<sim_id>/env-status", methods=["GET"])
def env_status(sim_id: str):
    """Read Runner ``env_status.json``."""
    err = _check_sim_id(sim_id)
    if err:
        return err

    env = read_env_status(gs.get_sim_dir())
    if env is None:
        return _bad_request("env_status.json not found; is run_hbm started?", 503)

    return jsonify({"success": True, "data": env})


@hbm_bp.route("/simulations/<sim_id>/player-turn", methods=["POST"])
def player_turn(sim_id: str):
    """API 1 — score, inject, return task_id for polling."""
    err = _check_sim_id(sim_id)
    if err:
        return err

    body = _json_body()
    player_text = str(body.get("player_text") or "").strip()
    if not player_text:
        return _bad_request("player_text is required")

    try:
        result = gs.handle_player_turn(
            session,
            sim_id=sim_id,
            player_text=player_text,
            request_place_id=body.get("place_id"),
            request_phase=body.get("phase"),
            request_player_turn=body.get("player_turn"),
            tick_count=int(body.get("tick_count", 6)),
        )
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503

    return jsonify({"success": True, "data": result})


@hbm_bp.route("/simulations/<sim_id>/action-result", methods=["GET"])
def action_result(sim_id: str):
    """API 2 — poll until NPC activity completes or timeout."""
    err = _check_sim_id(sim_id)
    if err:
        return err

    task_id = str(request.args.get("task_id") or "").strip()
    if not task_id:
        return _bad_request("task_id query parameter is required")

    try:
        result = gs.get_action_result(
            session,
            sim_id=sim_id,
            task_id=task_id,
            request_place_id=request.args.get("place_id"),
        )
    except KeyError as exc:
        return _bad_request(str(exc), 404)

    return jsonify({"success": True, "data": result})


@hbm_bp.route("/simulations/<sim_id>/debug-inject", methods=["POST"])
def debug_inject(sim_id: str):
    """Phase 2 temporary endpoint — kept for manual IPC testing."""
    err = _check_sim_id(sim_id)
    if err:
        return err

    body = _json_body()
    player_text = str(body.get("player_text") or "").strip()
    if not player_text:
        return _bad_request("player_text is required")

    tick_count = int(body.get("tick_count", 6))
    hbm = gs.get_or_create_session(session, sim_id)

    try:
        inject_result = gs.run_debug_inject(
            hbm,
            player_text,
            tick_count=tick_count,
        )
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503

    gs.save_session(session, hbm, sim_id)
    env = read_env_status(gs.get_sim_dir()) or {}

    return jsonify(
        {
            "success": True,
            "data": {
                "task_id": hbm.task_id,
                "player_turn": hbm.player_turn,
                "inject": inject_result,
                "env_status": env,
            },
        }
    )
