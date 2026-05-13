"""Background simulation runner — process supervisor + actions.jsonl tail.

COPY/ADAPT from MiroFish ``backend/app/services/simulation_runner.py``
(L1-1768). Behaviour preserved verbatim except:

* Imports adjusted to ``agent_world.app.*``.
* Logger name + locale helpers replaced with stdlib ``logging``.
* Subprocess command targets the unified ``run_agent_world_simulation``
  runner module (rather than MiroFish's per-platform script tree).
* The action-log tail understands the v0.3 extra fields the Agent World
  ``action_logger`` emits: ``place_id`` / ``channel_type`` / ``arrive_at`` /
  ``attempted_at`` / ``delivered``. Missing fields fall back to ``None`` and
  log a warning once per simulation.
* Twin-platform layout (twitter/reddit subdirs) is retained because the
  runner still keeps that split for FEED-class actions; MOVE/SPEAK_TO_LOCAL
  /SEND_MESSAGE etc. write to a top-level ``actions.jsonl``.

The full MiroFish surface (interview helpers, get_timeline, get_agent_stats
…) is *not* re-exported here — only the boot/monitor/stop core. Interview
plumbing returns once :class:`SimulationIPCClient` (already adapted with the
4 new commands) is wired by the API layer.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from queue import Queue
from typing import Any, Dict, List, Optional

from ..config import Config
from .simulation_ipc import SimulationIPCClient

logger = logging.getLogger("agent_world.simulation_runner")

IS_WINDOWS = sys.platform == "win32"
_cleanup_registered = False


class RunnerStatus(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


# --------------------------------------------------------------------------- #
# Action / round dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class AgentAction:
    """A single Agent action row pulled from ``actions.jsonl``.

    The Agent World logger (LAYOUT §2.I) emits 5 extra fields beyond the
    MiroFish original; they are kept ``Optional`` so old demo logs still parse.
    """

    round_num: int
    timestamp: str
    platform: str
    agent_id: int
    agent_name: str
    action_type: str
    action_args: Dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    success: bool = True

    # ---- v0.3 Agent World extras (LAYOUT §6 / §7.1 / B1.1) -----------------
    place_id: Optional[str] = None
    channel_type: Optional[str] = None
    arrive_at: Optional[int] = None
    attempted_at: Optional[int] = None
    delivered: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_num": self.round_num,
            "timestamp": self.timestamp,
            "platform": self.platform,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "action_type": self.action_type,
            "action_args": self.action_args,
            "result": self.result,
            "success": self.success,
            "place_id": self.place_id,
            "channel_type": self.channel_type,
            "arrive_at": self.arrive_at,
            "attempted_at": self.attempted_at,
            "delivered": self.delivered,
        }


@dataclass
class SimulationRunState:
    simulation_id: str
    runner_status: RunnerStatus = RunnerStatus.IDLE

    current_round: int = 0
    total_rounds: int = 0
    simulated_hours: int = 0
    total_simulation_hours: int = 0

    twitter_running: bool = False
    reddit_running: bool = False
    twitter_actions_count: int = 0
    reddit_actions_count: int = 0
    twitter_completed: bool = False
    reddit_completed: bool = False

    recent_actions: List[AgentAction] = field(default_factory=list)
    max_recent_actions: int = 50

    started_at: Optional[str] = None
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    error: Optional[str] = None
    process_pid: Optional[int] = None

    def add_action(self, action: AgentAction) -> None:
        self.recent_actions.insert(0, action)
        if len(self.recent_actions) > self.max_recent_actions:
            self.recent_actions = self.recent_actions[: self.max_recent_actions]
        if action.platform == "twitter":
            self.twitter_actions_count += 1
        elif action.platform == "reddit":
            self.reddit_actions_count += 1
        self.updated_at = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "runner_status": self.runner_status.value,
            "current_round": self.current_round,
            "total_rounds": self.total_rounds,
            "simulated_hours": self.simulated_hours,
            "total_simulation_hours": self.total_simulation_hours,
            "progress_percent": round(
                self.current_round / max(self.total_rounds, 1) * 100, 1
            ),
            "twitter_running": self.twitter_running,
            "reddit_running": self.reddit_running,
            "twitter_completed": self.twitter_completed,
            "reddit_completed": self.reddit_completed,
            "twitter_actions_count": self.twitter_actions_count,
            "reddit_actions_count": self.reddit_actions_count,
            "total_actions_count": self.twitter_actions_count
            + self.reddit_actions_count,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "process_pid": self.process_pid,
            "recent_actions": [a.to_dict() for a in self.recent_actions],
        }


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


class SimulationRunner:
    """Class-method singleton — mirrors MiroFish's pattern."""

    RUN_STATE_DIR = os.path.abspath(Config.SIMULATIONS_DIR)
    # Agent World: single unified runner module.
    RUNNER_MODULE = "agent_world.runner.run_agent_world_simulation"

    _run_states: Dict[str, SimulationRunState] = {}
    _processes: Dict[str, subprocess.Popen] = {}
    _action_queues: Dict[str, Queue] = {}
    _monitor_threads: Dict[str, threading.Thread] = {}
    _stdout_files: Dict[str, Any] = {}
    _legacy_field_warned: Dict[str, bool] = {}
    _cleanup_done = False

    # ---- state I/O ---------------------------------------------------------

    @classmethod
    def _state_path(cls, simulation_id: str) -> str:
        return os.path.join(cls.RUN_STATE_DIR, simulation_id, "run_state.json")

    @classmethod
    def get_run_state(cls, simulation_id: str) -> Optional[SimulationRunState]:
        if simulation_id in cls._run_states:
            return cls._run_states[simulation_id]
        path = cls._state_path(simulation_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = SimulationRunState(
                simulation_id=simulation_id,
                runner_status=RunnerStatus(data.get("runner_status", "idle")),
                current_round=data.get("current_round", 0),
                total_rounds=data.get("total_rounds", 0),
                simulated_hours=data.get("simulated_hours", 0),
                total_simulation_hours=data.get("total_simulation_hours", 0),
                twitter_running=data.get("twitter_running", False),
                reddit_running=data.get("reddit_running", False),
                twitter_completed=data.get("twitter_completed", False),
                reddit_completed=data.get("reddit_completed", False),
                twitter_actions_count=data.get("twitter_actions_count", 0),
                reddit_actions_count=data.get("reddit_actions_count", 0),
                started_at=data.get("started_at"),
                updated_at=data.get("updated_at", datetime.now().isoformat()),
                completed_at=data.get("completed_at"),
                error=data.get("error"),
                process_pid=data.get("process_pid"),
            )
            cls._run_states[simulation_id] = state
            return state
        except Exception as exc:  # noqa: BLE001
            logger.warning("load run state failed: %s", exc)
            return None

    @classmethod
    def _save_run_state(cls, state: SimulationRunState) -> None:
        sim_dir = os.path.join(cls.RUN_STATE_DIR, state.simulation_id)
        os.makedirs(sim_dir, exist_ok=True)
        with open(os.path.join(sim_dir, "run_state.json"), "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        cls._run_states[state.simulation_id] = state

    # ---- start -------------------------------------------------------------

    @classmethod
    def start_simulation(
        cls,
        simulation_id: str,
        max_rounds: Optional[int] = None,
    ) -> SimulationRunState:
        existing = cls.get_run_state(simulation_id)
        if existing and existing.runner_status in (
            RunnerStatus.RUNNING,
            RunnerStatus.STARTING,
        ):
            raise ValueError(f"simulation already running: {simulation_id}")

        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        if not os.path.exists(config_path):
            raise ValueError("simulation_config.json missing — call /prepare first")

        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        time_config = config.get("time_config", {})
        total_hours = time_config.get("total_simulation_hours", 24)
        minutes_per_round = time_config.get("minutes_per_round", 60)
        total_rounds = max(1, int(total_hours * 60 / minutes_per_round))
        if max_rounds is not None and max_rounds > 0:
            total_rounds = min(total_rounds, max_rounds)

        state = SimulationRunState(
            simulation_id=simulation_id,
            runner_status=RunnerStatus.STARTING,
            total_rounds=total_rounds,
            total_simulation_hours=total_hours,
            started_at=datetime.now().isoformat(),
        )
        cls._save_run_state(state)

        cmd = [
            sys.executable,
            "-m",
            cls.RUNNER_MODULE,
            "--config",
            config_path,
            "--simulation-dir",
            sim_dir,
        ]
        if max_rounds is not None and max_rounds > 0:
            cmd.extend(["--max-rounds", str(max_rounds)])

        log_path = os.path.join(sim_dir, "simulation.log")
        log_file = open(log_path, "w", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            process = subprocess.Popen(
                cmd,
                cwd=sim_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
                start_new_session=not IS_WINDOWS,
            )
        except Exception as exc:  # noqa: BLE001
            log_file.close()
            state.runner_status = RunnerStatus.FAILED
            state.error = str(exc)
            cls._save_run_state(state)
            raise

        cls._stdout_files[simulation_id] = log_file
        cls._action_queues[simulation_id] = Queue()
        state.process_pid = process.pid
        state.runner_status = RunnerStatus.RUNNING
        cls._processes[simulation_id] = process
        cls._save_run_state(state)

        thread = threading.Thread(
            target=cls._monitor_simulation, args=(simulation_id,), daemon=True
        )
        thread.start()
        cls._monitor_threads[simulation_id] = thread

        logger.info("simulation started: %s pid=%s", simulation_id, process.pid)
        return state

    # ---- monitor / actions tail -------------------------------------------

    @classmethod
    def _monitor_simulation(cls, simulation_id: str) -> None:
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        # The runner writes a top-level actions.jsonl plus per-platform splits.
        action_logs = [
            (os.path.join(sim_dir, "actions.jsonl"), None),
            (os.path.join(sim_dir, "twitter", "actions.jsonl"), "twitter"),
            (os.path.join(sim_dir, "reddit", "actions.jsonl"), "reddit"),
        ]
        positions = {p: 0 for p, _ in action_logs}

        process = cls._processes.get(simulation_id)
        state = cls.get_run_state(simulation_id)
        if process is None or state is None:
            return

        try:
            while process.poll() is None:
                for path, default_platform in action_logs:
                    if os.path.exists(path):
                        positions[path] = cls._read_action_log(
                            path, positions[path], state, default_platform
                        )
                cls._save_run_state(state)
                time.sleep(2)

            # Final tail after process exit.
            for path, default_platform in action_logs:
                if os.path.exists(path):
                    cls._read_action_log(
                        path, positions[path], state, default_platform
                    )

            exit_code = process.returncode
            if exit_code == 0:
                state.runner_status = RunnerStatus.COMPLETED
                state.completed_at = datetime.now().isoformat()
            else:
                state.runner_status = RunnerStatus.FAILED
                state.error = f"process exit code {exit_code}"
            state.twitter_running = False
            state.reddit_running = False
            cls._save_run_state(state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("monitor thread crashed: %s", exc)
            state.runner_status = RunnerStatus.FAILED
            state.error = str(exc)
            cls._save_run_state(state)
        finally:
            cls._processes.pop(simulation_id, None)
            cls._action_queues.pop(simulation_id, None)
            f = cls._stdout_files.pop(simulation_id, None)
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass

    @classmethod
    def _read_action_log(
        cls,
        log_path: str,
        position: int,
        state: SimulationRunState,
        default_platform: Optional[str],
    ) -> int:
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                f.seek(position)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "event_type" in data:
                        cls._handle_event(data, state, default_platform)
                        continue
                    if "agent_id" not in data:
                        continue
                    cls._record_action(data, state, default_platform)
                return f.tell()
        except OSError as exc:
            logger.warning("read action log failed: %s — %s", log_path, exc)
            return position

    @classmethod
    def _handle_event(
        cls,
        data: Dict[str, Any],
        state: SimulationRunState,
        default_platform: Optional[str],
    ) -> None:
        event_type = data.get("event_type")
        if event_type == "round_end":
            round_num = int(data.get("round", 0))
            if round_num > state.current_round:
                state.current_round = round_num
            state.simulated_hours = int(data.get("simulated_hours", state.simulated_hours))
        elif event_type == "simulation_end":
            if default_platform == "twitter":
                state.twitter_completed = True
                state.twitter_running = False
            elif default_platform == "reddit":
                state.reddit_completed = True
                state.reddit_running = False

    @classmethod
    def _record_action(
        cls,
        data: Dict[str, Any],
        state: SimulationRunState,
        default_platform: Optional[str],
    ) -> None:
        platform = data.get("platform") or default_platform or ""

        # v0.3 extra fields with .get(...) fallbacks (LAYOUT §6 / §7.1 / B1.1).
        place_id = data.get("place_id")
        channel_type = data.get("channel_type")
        arrive_at = data.get("arrive_at")
        attempted_at = data.get("attempted_at")
        delivered = data.get("delivered")
        if (
            place_id is None
            and channel_type is None
            and arrive_at is None
            and attempted_at is None
            and delivered is None
            and not cls._legacy_field_warned.get(state.simulation_id)
        ):
            logger.warning(
                "actions.jsonl missing v0.3 extras (place_id/channel_type/...) "
                "for simulation_id=%s — falling back to None",
                state.simulation_id,
            )
            cls._legacy_field_warned[state.simulation_id] = True

        action = AgentAction(
            round_num=int(data.get("round", 0)),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
            platform=platform,
            agent_id=int(data.get("agent_id", 0)),
            agent_name=str(data.get("agent_name", "")),
            action_type=str(data.get("action_type", "")),
            action_args=data.get("action_args", {}) or {},
            result=data.get("result"),
            success=bool(data.get("success", True)),
            place_id=place_id,
            channel_type=channel_type,
            arrive_at=arrive_at,
            attempted_at=attempted_at,
            delivered=delivered,
        )
        state.add_action(action)
        if action.round_num > state.current_round:
            state.current_round = action.round_num

    # ---- stop / cleanup ----------------------------------------------------

    @classmethod
    def _terminate_process(
        cls, process: subprocess.Popen, simulation_id: str, timeout: int = 10
    ) -> None:
        if IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T"],
                    capture_output=True,
                    timeout=5,
                )
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(process.pid), "/T"],
                        capture_output=True,
                        timeout=5,
                    )
                    process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        else:
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    process.kill()
                process.wait(timeout=5)

    @classmethod
    def stop_simulation(cls, simulation_id: str) -> SimulationRunState:
        state = cls.get_run_state(simulation_id)
        if state is None:
            raise ValueError(f"simulation not found: {simulation_id}")
        if state.runner_status not in (RunnerStatus.RUNNING, RunnerStatus.PAUSED):
            return state
        state.runner_status = RunnerStatus.STOPPING
        cls._save_run_state(state)
        process = cls._processes.get(simulation_id)
        if process is not None and process.poll() is None:
            try:
                cls._terminate_process(process, simulation_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("terminate failed: %s", exc)
                try:
                    process.kill()
                except OSError:
                    pass
        state.runner_status = RunnerStatus.STOPPED
        state.completed_at = datetime.now().isoformat()
        cls._save_run_state(state)
        return state

    @classmethod
    def cleanup_all_simulations(cls) -> None:
        if cls._cleanup_done:
            return
        cls._cleanup_done = True
        for sim_id, process in list(cls._processes.items()):
            try:
                if process.poll() is None:
                    cls._terminate_process(process, sim_id, timeout=5)
            except Exception:  # noqa: BLE001
                pass
        cls._processes.clear()
        cls._action_queues.clear()
        for sim_id, fh in list(cls._stdout_files.items()):
            try:
                fh.close()
            except OSError:
                pass
        cls._stdout_files.clear()

    @classmethod
    def register_cleanup(cls) -> None:
        global _cleanup_registered
        if _cleanup_registered:
            return
        atexit.register(cls.cleanup_all_simulations)
        try:
            signal.signal(signal.SIGTERM, lambda *_: cls.cleanup_all_simulations())
            signal.signal(signal.SIGINT, lambda *_: cls.cleanup_all_simulations())
        except (ValueError, OSError):
            pass
        _cleanup_registered = True

    # ---- IPC convenience ---------------------------------------------------

    @classmethod
    def get_ipc_client(cls, simulation_id: str) -> SimulationIPCClient:
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            raise ValueError(f"simulation not found: {simulation_id}")
        return SimulationIPCClient(sim_dir)

    @classmethod
    def check_env_alive(cls, simulation_id: str) -> bool:
        try:
            return cls.get_ipc_client(simulation_id).check_env_alive()
        except ValueError:
            return False


__all__ = [
    "AgentAction",
    "RunnerStatus",
    "SimulationRunState",
    "SimulationRunner",
]
