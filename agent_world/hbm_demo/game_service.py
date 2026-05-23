"""Flask-side game logic for HBM demo (Phase 2 skeleton)."""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_world.hbm_demo.env_status import is_runner_ready, read_env_status
from agent_world.hbm_demo.ipc_helper import get_ipc_client, send_inject_batch

DEFAULT_SIM_ID = "hbm_memory_war"
DEFAULT_PLACE_ID = "nvidia_reception"
DEFAULT_PHASE = "Phase 1"

INITIAL_STATS: Dict[str, int] = {
    "vision": 0,
    "execution": 0,
    "trust": 10,
    "burnout": 0,
}

SESSION_KEY = "hbm_game"


@dataclass
class HbmSession:
    task_id: str
    start_tick: int
    place_id: str = DEFAULT_PLACE_ID
    phase: str = DEFAULT_PHASE
    player_turn: int = 1
    stats: Dict[str, int] = field(default_factory=lambda: dict(INITIAL_STATS))
    phase2_start_tick: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "start_tick": self.start_tick,
            "place_id": self.place_id,
            "phase": self.phase,
            "player_turn": self.player_turn,
            "stats": dict(self.stats),
            "phase2_start_tick": self.phase2_start_tick,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HbmSession":
        stats = dict(INITIAL_STATS)
        stats.update(data.get("stats") or {})
        return cls(
            task_id=str(data.get("task_id") or uuid.uuid4().hex),
            start_tick=int(data.get("start_tick", 0)),
            place_id=str(data.get("place_id") or DEFAULT_PLACE_ID),
            phase=str(data.get("phase") or DEFAULT_PHASE),
            player_turn=int(data.get("player_turn", 1)),
            stats=stats,
            phase2_start_tick=data.get("phase2_start_tick"),
        )


def get_sim_dir() -> Path:
    """Resolve simulation directory (``HBM_SIM_DIR`` env or default)."""
    pkg = Path(__file__).resolve().parent
    default = pkg / "sim" / DEFAULT_SIM_ID
    raw = Path(
        __import__("os").environ.get("HBM_SIM_DIR", str(default))
    )
    return raw.resolve()


def get_world_db_path(sim_dir: Path | None = None) -> Path:
    sim = sim_dir or get_sim_dir()
    return sim / "world.db"


class ReadOnlyWorldDB:
    """Flask-side read-only SQLite accessor with lock retry."""

    def __init__(self, db_path: Path, *, timeout: float = 5.0) -> None:
        self.db_path = db_path
        self.timeout = timeout

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=self.timeout,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _with_retry(self, fn: Any, *, retries: int = 4) -> Any:
        delay = 0.05
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                conn = self._connect()
                try:
                    return fn(conn)
                finally:
                    conn.close()
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "locked" not in str(exc).lower() and attempt == 0:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("database read failed")

    def agents_at(self, place_id: str) -> List[int]:
        def _query(conn: sqlite3.Connection) -> List[int]:
            rows = conn.execute(
                "SELECT agent_id FROM agent_location WHERE place_id=?",
                (place_id,),
            ).fetchall()
            return [int(r["agent_id"]) for r in rows]

        return self._with_retry(_query)


def initial_stats() -> Dict[str, int]:
    return dict(INITIAL_STATS)


def create_session(sim_dir: Path | None = None) -> HbmSession:
    """Create a new game session anchored at current env tick."""
    sim = sim_dir or get_sim_dir()
    env = read_env_status(sim) or {}
    start_tick = int(env.get("current_tick", 0))
    return HbmSession(
        task_id=f"task_{uuid.uuid4().hex[:12]}",
        start_tick=start_tick,
        place_id=DEFAULT_PLACE_ID,
        phase=DEFAULT_PHASE,
        player_turn=1,
        stats=initial_stats(),
    )


def save_session(flask_session: Any, hbm: HbmSession, sim_id: str = DEFAULT_SIM_ID) -> None:
    store = flask_session.setdefault(SESSION_KEY, {})
    store[sim_id] = hbm.to_dict()


def load_session(
    flask_session: Any,
    sim_id: str = DEFAULT_SIM_ID,
) -> Optional[HbmSession]:
    store = flask_session.get(SESSION_KEY) or {}
    raw = store.get(sim_id)
    if not raw:
        return None
    return HbmSession.from_dict(raw)


def get_or_create_session(
    flask_session: Any,
    sim_id: str = DEFAULT_SIM_ID,
    *,
    sim_dir: Path | None = None,
) -> HbmSession:
    existing = load_session(flask_session, sim_id)
    if existing is not None:
        return existing
    hbm = create_session(sim_dir)
    save_session(flask_session, hbm, sim_id)
    return hbm


def build_dialogue_injection_events(
    session: HbmSession,
    player_text: str,
    *,
    sim_dir: Path | None = None,
) -> List[Dict[str, Any]]:
    """Build DialogueInjection events for agents at session.place_id."""
    sim = sim_dir or get_sim_dir()
    db = ReadOnlyWorldDB(get_world_db_path(sim))
    agent_ids = db.agents_at(session.place_id)
    if not agent_ids:
        return []

    text = player_text.strip()
    if not text.startswith("玩家"):
        text = f"玩家说：{text}"

    events: List[Dict[str, Any]] = []
    for aid in sorted(agent_ids):
        events.append(
            {
                "id": f"{session.task_id}_agent_{aid}",
                "trigger": {"type": "at_condition", "expr": "True"},
                "effect": {
                    "type": "dialogue_injection",
                    "agent_id": int(aid),
                    "text": text,
                },
            }
        )
    return events


def run_debug_inject(
    session: HbmSession,
    player_text: str,
    *,
    sim_dir: Path | None = None,
    tick_count: int = 6,
    timeout: float = 600.0,
) -> Dict[str, Any]:
    """Phase 2: inject player dialogue via IPC and return Runner ack."""
    sim = sim_dir or get_sim_dir()
    sim_str = str(sim)

    if not is_runner_ready(sim):
        raise RuntimeError(
            "Runner not ready: start run_hbm first and wait for env_status.status=running"
        )

    events = build_dialogue_injection_events(session, player_text, sim_dir=sim)
    if not events:
        raise RuntimeError(f"no agents at place_id={session.place_id!r}")

    client = get_ipc_client(sim_str)
    resp = send_inject_batch(
        client,
        events=events,
        tick_count=tick_count,
        timeout=timeout,
    )
    if resp.status.value != "completed":
        raise RuntimeError(resp.error or f"IPC inject failed: {resp.status.value}")

    result = dict(resp.result or {})
    session.player_turn += 1
    return {
        "ipc": result,
        "events_count": len(events),
        "agent_ids": [ev["effect"]["agent_id"] for ev in events],
    }
