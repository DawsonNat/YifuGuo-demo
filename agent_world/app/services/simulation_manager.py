"""Simulation life-cycle manager.

COPY/ADAPT from MiroFish ``backend/app/services/simulation_manager.py``
(L1-530). Behaviour preserved verbatim except:

* ``script_path`` now points to ``agent_world.runner.run_agent_world_simulation``.
* Imports adjusted to ``agent_world.app...`` paths.
* The lighter ``logging`` module is used in lieu of MiroFish's ``utils.logger``.
* MiroFish locale helpers (``utils.locale.t``) are replaced with plain English
  strings inline.
* Legacy upload-folder layout is kept identical so the IPC dirs land in the
  same relative location the runner expects.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from ..config import Config

logger = logging.getLogger("agent_world.simulation_manager")


# --------------------------------------------------------------------------- #
# Status enums (KEPT verbatim)
# --------------------------------------------------------------------------- #


class SimulationStatus(str, Enum):
    CREATED = "created"
    PREPARING = "preparing"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class PlatformType(str, Enum):
    TWITTER = "twitter"
    REDDIT = "reddit"


# --------------------------------------------------------------------------- #
# State dataclass
# --------------------------------------------------------------------------- #


@dataclass
class SimulationState:
    simulation_id: str
    project_id: str
    graph_id: str

    enable_twitter: bool = True
    enable_reddit: bool = True

    status: SimulationStatus = SimulationStatus.CREATED

    # preparation outputs
    entities_count: int = 0
    profiles_count: int = 0
    entity_types: List[str] = field(default_factory=list)
    config_generated: bool = False
    config_reasoning: str = ""

    # runtime snapshot mirrors
    current_round: int = 0
    twitter_status: str = "not_started"
    reddit_status: str = "not_started"

    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "project_id": self.project_id,
            "graph_id": self.graph_id,
            "enable_twitter": self.enable_twitter,
            "enable_reddit": self.enable_reddit,
            "status": self.status.value,
            "entities_count": self.entities_count,
            "profiles_count": self.profiles_count,
            "entity_types": self.entity_types,
            "config_generated": self.config_generated,
            "config_reasoning": self.config_reasoning,
            "current_round": self.current_round,
            "twitter_status": self.twitter_status,
            "reddit_status": self.reddit_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }

    def to_simple_dict(self) -> Dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "project_id": self.project_id,
            "graph_id": self.graph_id,
            "status": self.status.value,
            "entities_count": self.entities_count,
            "profiles_count": self.profiles_count,
            "entity_types": self.entity_types,
            "config_generated": self.config_generated,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# SimulationManager
# --------------------------------------------------------------------------- #


class SimulationManager:
    """High-level orchestrator over a single simulation directory.

    Responsibilities:

    * Persist :class:`SimulationState` JSON under ``simulations/<sim_id>/state.json``.
    * Drive the LLM-based config / profile generators (stubbed at this layer
      — actual generation lives in :mod:`simulation_config_generator` and
      ``oasis_profile_generator``; the latter is **not** ported in MVP).
    * Hand back a CLI command pointing at ``run_agent_world_simulation``.
    """

    SIMULATION_DATA_DIR = os.path.abspath(Config.SIMULATIONS_DIR)

    # The script that the runner invokes. ADAPT: MiroFish pointed to
    # ``backend/scripts/run_twitter_simulation.py``; Agent World replaces it
    # with the unified runner module.
    RUNNER_MODULE = "agent_world.runner.run_agent_world_simulation"

    def __init__(self) -> None:
        os.makedirs(self.SIMULATION_DATA_DIR, exist_ok=True)
        self._simulations: Dict[str, SimulationState] = {}

    # ---- paths -------------------------------------------------------------

    def _get_simulation_dir(self, simulation_id: str) -> str:
        sim_dir = os.path.join(self.SIMULATION_DATA_DIR, simulation_id)
        os.makedirs(sim_dir, exist_ok=True)
        # Mirror MiroFish IPC layout so the client/server agree on paths.
        os.makedirs(os.path.join(sim_dir, "ipc_commands"), exist_ok=True)
        os.makedirs(os.path.join(sim_dir, "ipc_responses"), exist_ok=True)
        os.makedirs(os.path.join(sim_dir, "pools"), exist_ok=True)
        return sim_dir

    # ---- state I/O ---------------------------------------------------------

    def _save_simulation_state(self, state: SimulationState) -> None:
        sim_dir = self._get_simulation_dir(state.simulation_id)
        state.updated_at = datetime.now().isoformat()
        with open(os.path.join(sim_dir, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        self._simulations[state.simulation_id] = state

    def _load_simulation_state(
        self, simulation_id: str
    ) -> Optional[SimulationState]:
        if simulation_id in self._simulations:
            return self._simulations[simulation_id]
        sim_dir = self._get_simulation_dir(simulation_id)
        state_file = os.path.join(sim_dir, "state.json")
        if not os.path.exists(state_file):
            return None
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = SimulationState(
            simulation_id=simulation_id,
            project_id=data.get("project_id", ""),
            graph_id=data.get("graph_id", ""),
            enable_twitter=data.get("enable_twitter", True),
            enable_reddit=data.get("enable_reddit", True),
            status=SimulationStatus(data.get("status", "created")),
            entities_count=data.get("entities_count", 0),
            profiles_count=data.get("profiles_count", 0),
            entity_types=data.get("entity_types", []),
            config_generated=data.get("config_generated", False),
            config_reasoning=data.get("config_reasoning", ""),
            current_round=data.get("current_round", 0),
            twitter_status=data.get("twitter_status", "not_started"),
            reddit_status=data.get("reddit_status", "not_started"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            error=data.get("error"),
        )
        self._simulations[simulation_id] = state
        return state

    # ---- public API --------------------------------------------------------

    def create_simulation(
        self,
        project_id: str,
        graph_id: str,
        enable_twitter: bool = True,
        enable_reddit: bool = True,
    ) -> SimulationState:
        simulation_id = f"sim_{uuid.uuid4().hex[:12]}"
        state = SimulationState(
            simulation_id=simulation_id,
            project_id=project_id,
            graph_id=graph_id,
            enable_twitter=enable_twitter,
            enable_reddit=enable_reddit,
            status=SimulationStatus.CREATED,
        )
        self._save_simulation_state(state)
        logger.info(
            "create_simulation: %s project=%s graph=%s",
            simulation_id,
            project_id,
            graph_id,
        )
        return state

    def prepare_simulation(
        self,
        simulation_id: str,
        simulation_requirement: str,
        document_text: str = "",
        defined_entity_types: Optional[List[str]] = None,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> SimulationState:
        """Lightweight Agent-World stub of MiroFish's prepare flow.

        TODO (L4+): wire ZepEntityReader -> oasis_profile_generator ->
        simulation_config_generator, mirroring MiroFish L262-447.
        For now we just transition status to READY so callers can chain into
        :meth:`get_run_instructions`.
        """
        state = self._load_simulation_state(simulation_id)
        if state is None:
            raise ValueError(f"simulation not found: {simulation_id}")

        state.status = SimulationStatus.PREPARING
        self._save_simulation_state(state)

        if progress_callback:
            progress_callback("preparing", 0, "preparing simulation")

        # TODO: invoke ZepEntityReader.filter_defined_entities(...).
        # TODO: invoke OasisProfileGenerator.generate_profiles_from_entities(...).
        # TODO: invoke SimulationConfigGenerator.generate_config(...).

        sim_dir = self._get_simulation_dir(simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        if not os.path.exists(config_path):
            # Drop a minimal placeholder so the runner can boot.
            placeholder = {
                "simulation_id": simulation_id,
                "project_id": state.project_id,
                "graph_id": state.graph_id,
                "simulation_requirement": simulation_requirement,
                "world_config": {},
                "channel_config": {},
                "memory_config": {},
                "agent_configs": [],
                "event_config": {},
                "time_config": {"total_simulation_hours": 24, "minutes_per_round": 60},
            }
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(placeholder, f, ensure_ascii=False, indent=2)

        state.config_generated = True
        state.status = SimulationStatus.READY
        self._save_simulation_state(state)

        if progress_callback:
            progress_callback("ready", 100, "simulation ready")

        logger.info("prepare_simulation done: %s", simulation_id)
        return state

    def get_simulation(self, simulation_id: str) -> Optional[SimulationState]:
        return self._load_simulation_state(simulation_id)

    def list_simulations(
        self, project_id: Optional[str] = None
    ) -> List[SimulationState]:
        out: List[SimulationState] = []
        if not os.path.exists(self.SIMULATION_DATA_DIR):
            return out
        for sim_id in os.listdir(self.SIMULATION_DATA_DIR):
            sim_path = os.path.join(self.SIMULATION_DATA_DIR, sim_id)
            if sim_id.startswith(".") or not os.path.isdir(sim_path):
                continue
            state = self._load_simulation_state(sim_id)
            if state and (project_id is None or state.project_id == project_id):
                out.append(state)
        return out

    def get_simulation_config(self, simulation_id: str) -> Optional[Dict[str, Any]]:
        sim_dir = self._get_simulation_dir(simulation_id)
        path = os.path.join(sim_dir, "simulation_config.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_run_instructions(self, simulation_id: str) -> Dict[str, Any]:
        """Build the CLI invocation pointing at the new Agent World runner.

        ADAPT: MiroFish offered one command per platform (twitter / reddit /
        parallel). Agent World has a single unified runner module.
        """
        sim_dir = self._get_simulation_dir(simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        cmd = (
            f"python -m {self.RUNNER_MODULE} --config {config_path} "
            f"--simulation-dir {sim_dir}"
        )
        return {
            "simulation_dir": sim_dir,
            "config_file": config_path,
            "runner_module": self.RUNNER_MODULE,
            "command": cmd,
            "instructions": (
                "1. Activate the agent_world environment.\n"
                f"2. Run: {cmd}"
            ),
        }


__all__ = [
    "PlatformType",
    "SimulationManager",
    "SimulationState",
    "SimulationStatus",
]
