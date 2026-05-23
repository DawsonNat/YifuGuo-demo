"""Flask-side game logic for HBM demo."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from openai import OpenAI

from agent_world.hbm_demo.config_loader import load_scenario
from agent_world.hbm_demo.env_status import is_runner_ready, read_env_status
from agent_world.hbm_demo.ipc_helper import get_ipc_client, send_inject_batch
from agent_world.hbm_demo.kernel import resolve_api_key

log = logging.getLogger("agent_world.hbm_demo.game_service")

DEFAULT_SIM_ID = "hbm_memory_war"
DEFAULT_PLACE_ID = "nvidia_reception"
DEFAULT_PHASE = "Phase 1"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "hbm_scenario.yaml"

INITIAL_STATS: Dict[str, int] = {
    "vision": 0,
    "execution": 0,
    "trust": 10,
    "burnout": 0,
}

SESSION_KEY = "hbm_game"
TASKS_KEY = "hbm_tasks"

SYSTEM_SENDER_NAME = "彭博终端"

PHASE_RDC_PAIRS: Dict[str, List[Tuple[int, int]]] = {
    "Phase 1": [(1, 2)],
    "Phase 2": [(2, 3), (3, 2)],
    "Phase 3": [
        (2, 3), (3, 2),
        (4, 2), (5, 2), (6, 2),
        (4, 5), (4, 6), (5, 6),
        (7, 2),
    ],
    "Phase 4": [(2, 3), (3, 2)],
}

BAD_END_PUBLIC_MESSAGES = [
    {
        "sender": "接待前台",
        "content": "保安，请这位先生离开。",
        "type": "F2F",
    }
]

IMMEDIATE_MSG_PLACEHOLDER = "前台接待员听完你的话，若有所思…"

_scenario_cache: Dict[str, Any] | None = None
_name_map_cache: Dict[int, str] | None = None


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


@dataclass
class PendingTask:
    task_id: str
    start_tick: int
    place_id: str
    phase: str
    player_turn: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "start_tick": self.start_tick,
            "place_id": self.place_id,
            "phase": self.phase,
            "player_turn": self.player_turn,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PendingTask":
        return cls(
            task_id=str(data["task_id"]),
            start_tick=int(data["start_tick"]),
            place_id=str(data["place_id"]),
            phase=str(data["phase"]),
            player_turn=int(data["player_turn"]),
        )


def get_sim_dir() -> Path:
    pkg = Path(__file__).resolve().parent
    default = pkg / "sim" / DEFAULT_SIM_ID
    raw = Path(__import__("os").environ.get("HBM_SIM_DIR", str(default)))
    return raw.resolve()


def get_world_db_path(sim_dir: Path | None = None) -> Path:
    return (sim_dir or get_sim_dir()) / "world.db"


def get_scenario() -> Dict[str, Any]:
    global _scenario_cache
    if _scenario_cache is None:
        _scenario_cache = load_scenario(DEFAULT_CONFIG)
    return _scenario_cache


def get_name_map() -> Dict[int, str]:
    global _name_map_cache
    if _name_map_cache is None:
        _name_map_cache = {
            int(a["agent_id"]): str(a.get("name") or f"agent_{a['agent_id']}")
            for a in get_scenario().get("agents", [])
        }
    return _name_map_cache


def sender_display_name(sender_id: Optional[int], name_map: Dict[int, str]) -> str:
    if sender_id is None:
        return "未知"
    sid = int(sender_id)
    if sid == -1:
        return SYSTEM_SENDER_NAME
    return name_map.get(sid, f"agent_{sid}")


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
        for _attempt in range(retries):
            try:
                conn = self._connect()
                try:
                    return fn(conn)
                finally:
                    conn.close()
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "locked" not in str(exc).lower():
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

    def fetch_f2f_history_at(
        self,
        place_id: str,
        t_now: int,
        since_t: int,
        *,
        limit: int = 30,
    ) -> List[Tuple[int, int, int, str]]:
        def _query(conn: sqlite3.Connection) -> List[Tuple[int, int, int, str]]:
            rows = conn.execute(
                """
                SELECT MIN(message_id) AS message_id, sender_id,
                       attempted_at, content
                FROM direct_message
                WHERE channel_type='F2F' AND place_id=?
                  AND attempted_at >= ? AND attempted_at <= ?
                GROUP BY sender_id, attempted_at, content
                ORDER BY attempted_at, message_id
                LIMIT ?
                """,
                (place_id, since_t, t_now, limit),
            ).fetchall()
            return [
                (
                    int(r["attempted_at"]),
                    int(r["sender_id"]),
                    int(r["message_id"]),
                    str(r["content"]),
                )
                for r in rows
            ]

        return self._with_retry(_query)

    def fetch_messages_since(
        self,
        *,
        channel_type: str,
        since_t: int,
        t_now: int,
    ) -> List[sqlite3.Row]:
        def _query(conn: sqlite3.Connection) -> List[sqlite3.Row]:
            return conn.execute(
                """
                SELECT message_id, sender_id, recipient_id, group_id,
                       channel_type, content, place_id, attempted_at
                FROM direct_message
                WHERE channel_type=? AND attempted_at > ? AND attempted_at <= ?
                ORDER BY attempted_at, message_id
                """,
                (channel_type, since_t, t_now),
            ).fetchall()

        return self._with_retry(_query)

    def has_f2f_after(
        self, place_id: str, start_tick: int, t_now: int
    ) -> bool:
        def _query(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                """
                SELECT 1 FROM direct_message
                WHERE channel_type='F2F' AND place_id=?
                  AND attempted_at > ? AND attempted_at <= ?
                LIMIT 1
                """,
                (place_id, start_tick, t_now),
            ).fetchone()
            return row is not None

        return bool(self._with_retry(_query))

    def has_rdc_pair_after(
        self,
        pairs: List[Tuple[int, int]],
        start_tick: int,
        t_now: int,
    ) -> bool:
        if not pairs:
            return False

        def _query(conn: sqlite3.Connection) -> bool:
            rows = conn.execute(
                """
                SELECT sender_id, recipient_id FROM direct_message
                WHERE channel_type='RDC'
                  AND attempted_at > ? AND attempted_at <= ?
                """,
                (start_tick, t_now),
            ).fetchall()
            for row in rows:
                s, r = int(row["sender_id"]), int(row["recipient_id"])
                for a, b in pairs:
                    if (s, r) == (a, b) or (s, r) == (b, a):
                        return True
            return False

        return bool(self._with_retry(_query))

    def has_grp_after(
        self, group_ids: Set[int], start_tick: int, t_now: int
    ) -> bool:
        if not group_ids:
            return False

        def _query(conn: sqlite3.Connection) -> bool:
            placeholders = ",".join("?" for _ in group_ids)
            params = [start_tick, t_now, *sorted(group_ids)]
            row = conn.execute(
                f"""
                SELECT 1 FROM direct_message
                WHERE channel_type='GRP'
                  AND attempted_at > ? AND attempted_at <= ?
                  AND group_id IN ({placeholders})
                LIMIT 1
                """,
                params,
            ).fetchone()
            return row is not None

        return bool(self._with_retry(_query))


def initial_stats() -> Dict[str, int]:
    return dict(INITIAL_STATS)


def create_session(sim_dir: Path | None = None) -> HbmSession:
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


def save_task(
    flask_session: Any,
    task: PendingTask,
    sim_id: str = DEFAULT_SIM_ID,
) -> None:
    store = flask_session.setdefault(TASKS_KEY, {})
    sim_tasks = store.setdefault(sim_id, {})
    sim_tasks[task.task_id] = task.to_dict()
    sim_tasks["__latest__"] = task.task_id


def load_task(
    flask_session: Any,
    task_id: str,
    sim_id: str = DEFAULT_SIM_ID,
) -> Optional[PendingTask]:
    store = flask_session.get(TASKS_KEY) or {}
    raw = (store.get(sim_id) or {}).get(task_id)
    if not raw:
        return None
    return PendingTask.from_dict(raw)


def _llm_client() -> OpenAI:
    llm_cfg = get_scenario().get("llm", {}) or {}
    return OpenAI(
        api_key=resolve_api_key(llm_cfg),
        base_url=llm_cfg.get("base_url", "https://api.deepseek.com"),
    )


def _parse_stats_json(text: str) -> Dict[str, int]:
    raw = text.strip()
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        raw = match.group(0)
    data = json.loads(raw)
    return {
        "vision_delta": int(data.get("vision_delta", 0) or 0),
        "execution_delta": int(data.get("execution_delta", 0) or 0),
        "trust_delta": int(data.get("trust_delta", 0) or 0),
        "burnout_delta": int(data.get("burnout_delta", 0) or 0),
    }


def _heuristic_stats(session: HbmSession, player_text: str) -> Dict[str, int]:
    """Fallback when LLM scoring fails."""
    text = player_text.lower()
    tech_kw = ("显存", "算法", "80%", "内存", "优化", "架构", "降低")
    if any(k in player_text for k in tech_kw):
        return {
            "vision_delta": 5,
            "execution_delta": 4,
            "trust_delta": 1,
            "burnout_delta": 0,
        }
    if len(text) < 8:
        return {
            "vision_delta": 0,
            "execution_delta": 0,
            "trust_delta": 0,
            "burnout_delta": 1,
        }
    return {
        "vision_delta": 1,
        "execution_delta": 1,
        "trust_delta": 0,
        "burnout_delta": 0,
    }


def apply_stat_deltas(session: HbmSession, deltas: Dict[str, int]) -> None:
    session.stats["vision"] = max(
        0, min(999, session.stats["vision"] + int(deltas.get("vision_delta", 0)))
    )
    session.stats["execution"] = max(
        0, min(999, session.stats["execution"] + int(deltas.get("execution_delta", 0)))
    )
    session.stats["trust"] = max(
        0, min(999, session.stats["trust"] + int(deltas.get("trust_delta", 0)))
    )
    session.stats["burnout"] = max(
        0, min(100, session.stats["burnout"] + int(deltas.get("burnout_delta", 0)))
    )


def score_player_turn(session: HbmSession, player_text: str) -> Dict[str, int]:
    """Score player turn via DeepSeek JSON deltas (§4.2)."""
    llm_cfg = get_scenario().get("llm", {}) or {}
    model = llm_cfg.get("model", "deepseek-chat")
    system = (
        "你是《HBM 显存价格保卫战》的游戏裁判。"
        "根据玩家本回合发言与当前 Phase，输出四维属性增量 JSON。"
        "只输出 JSON，字段：vision_delta, execution_delta, trust_delta, "
        "burnout_delta, reason。"
    )
    user = json.dumps(
        {
            "player_text": player_text,
            "phase": session.phase,
            "player_turn": session.player_turn,
            "stats": session.stats,
        },
        ensure_ascii=False,
    )
    try:
        resp = _llm_client().chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=200,
        )
        content = (resp.choices[0].message.content or "").strip()
        return _parse_stats_json(content)
    except Exception as exc:  # noqa: BLE001
        log.warning("score_player_turn LLM failed, using heuristic: %s", exc)
        return _heuristic_stats(session, player_text)


def _call_immediate_llm(session: HbmSession, player_text: str) -> str:
    llm_cfg = get_scenario().get("llm", {}) or {}
    model = llm_cfg.get("model", "deepseek-chat")
    resp = _llm_client().chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "用一句中文描写 NPC 听完玩家发言后的即时反应，20字以内。",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "phase": session.phase,
                        "place_id": session.place_id,
                        "player_text": player_text,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0.8,
        max_tokens=60,
    )
    text = (resp.choices[0].message.content or "").strip()
    return text or IMMEDIATE_MSG_PLACEHOLDER


def generate_immediate_msg(
    session: HbmSession,
    player_text: str,
    *,
    timeout: float = 1.0,
) -> str:
    """Generate one-line scene reaction; fall back on timeout (§API 1 step 4)."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_call_immediate_llm, session, player_text)
            return fut.result(timeout=timeout)
    except Exception:  # noqa: BLE001
        return IMMEDIATE_MSG_PLACEHOLDER


def build_dialogue_injection_events(
    session: HbmSession,
    player_text: str,
    *,
    task_id: str,
    sim_dir: Path | None = None,
) -> List[Dict[str, Any]]:
    """Build DialogueInjection events for agents at **session.place_id**."""
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
                "id": f"{task_id}_agent_{aid}",
                "trigger": {"type": "at_condition", "expr": "True"},
                "effect": {
                    "type": "dialogue_injection",
                    "agent_id": int(aid),
                    "text": text,
                },
            }
        )
    return events


def _check_turn4_bad_end(session: HbmSession) -> bool:
    return (
        session.player_turn == 4
        and session.stats["vision"] + session.stats["execution"] < 15
    )


def run_debug_inject(
    session: HbmSession,
    player_text: str,
    *,
    sim_dir: Path | None = None,
    tick_count: int = 6,
    timeout: float = 600.0,
) -> Dict[str, Any]:
    """Phase 2 debug path — kept for compatibility."""
    sim = sim_dir or get_sim_dir()
    if not is_runner_ready(sim):
        raise RuntimeError(
            "Runner not ready: start run_hbm first and wait for env_status.status=running"
        )
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    events = build_dialogue_injection_events(
        session, player_text, task_id=task_id, sim_dir=sim
    )
    if not events:
        raise RuntimeError(f"no agents at place_id={session.place_id!r}")

    resp = send_inject_batch(
        get_ipc_client(str(sim)),
        events=events,
        tick_count=tick_count,
        timeout=timeout,
    )
    if resp.status.value != "completed":
        raise RuntimeError(resp.error or f"IPC inject failed: {resp.status.value}")

    session.player_turn += 1
    return {
        "ipc": dict(resp.result or {}),
        "events_count": len(events),
        "agent_ids": [ev["effect"]["agent_id"] for ev in events],
    }


def handle_player_turn(
    flask_session: Any,
    *,
    sim_id: str,
    player_text: str,
    request_place_id: Optional[str] = None,
    request_phase: Optional[str] = None,
    request_player_turn: Optional[int] = None,
    sim_dir: Path | None = None,
    tick_count: int = 6,
    ipc_timeout: float = 600.0,
) -> Dict[str, Any]:
    """API 1 — Phase 1 flow (steps 1–7; routing deferred to Phase 4)."""
    sim = sim_dir or get_sim_dir()
    if not is_runner_ready(sim):
        raise RuntimeError(
            "Runner not ready: start run_hbm first and wait for env_status.status=running"
        )

    hbm = get_or_create_session(flask_session, sim_id, sim_dir=sim)
    if request_place_id and request_place_id != hbm.place_id:
        log.debug(
            "ignoring request place_id=%s; session authority=%s",
            request_place_id,
            hbm.place_id,
        )
    if request_phase and request_phase != hbm.phase:
        log.debug(
            "ignoring request phase=%s; session authority=%s",
            request_phase,
            hbm.phase,
        )
    if request_player_turn is not None and int(request_player_turn) != hbm.player_turn:
        log.debug(
            "ignoring request player_turn=%s; session authority=%s",
            request_player_turn,
            hbm.player_turn,
        )

    env = read_env_status(sim) or {}
    start_tick = int(env.get("current_tick", 0))
    task_id = f"task_{uuid.uuid4().hex[:12]}"

    deltas = score_player_turn(hbm, player_text)
    apply_stat_deltas(hbm, deltas)

    if _check_turn4_bad_end(hbm):
        save_session(flask_session, hbm, sim_id)
        return {
            "status": "game_over",
            "ending_id": "bad_reject",
            "public_messages": list(BAD_END_PUBLIC_MESSAGES),
            "stats_update": dict(hbm.stats),
            "current_phase": hbm.phase,
        }

    immediate_msg = generate_immediate_msg(hbm, player_text, timeout=1.0)

    events = build_dialogue_injection_events(
        hbm, player_text, task_id=task_id, sim_dir=sim
    )
    if not events:
        raise RuntimeError(f"no agents at session.place_id={hbm.place_id!r}")

    resp = send_inject_batch(
        get_ipc_client(str(sim)),
        events=events,
        tick_count=tick_count,
        timeout=ipc_timeout,
    )
    if resp.status.value != "completed":
        raise RuntimeError(resp.error or f"IPC inject failed: {resp.status.value}")

    task = PendingTask(
        task_id=task_id,
        start_tick=start_tick,
        place_id=hbm.place_id,
        phase=hbm.phase,
        player_turn=hbm.player_turn,
    )
    save_task(flask_session, task, sim_id)

    hbm.player_turn += 1
    save_session(flask_session, hbm, sim_id)

    return {
        "task_id": task_id,
        "immediate_msg": immediate_msg,
        "status": "processing",
        "stats_update": dict(hbm.stats),
        "current_phase": hbm.phase,
        "start_tick": start_tick,
        "ipc": dict(resp.result or {}),
    }


def _rdc_pairs_for_phase(phase: str) -> List[Tuple[int, int]]:
    return list(PHASE_RDC_PAIRS.get(phase, PHASE_RDC_PAIRS[DEFAULT_PHASE]))


def check_action_complete(
    task: PendingTask,
    current_tick: int,
    db: ReadOnlyWorldDB,
) -> bool:
    """Return True when API 2 should return ``completed`` (§API 2)."""
    start = task.start_tick
    if current_tick < start + 3:
        return False
    if current_tick >= start + 8:
        return True

    if db.has_f2f_after(task.place_id, start, current_tick):
        return True
    if db.has_rdc_pair_after(
        _rdc_pairs_for_phase(task.phase), start, current_tick
    ):
        return True
    if db.has_grp_after({100, 200}, start, current_tick):
        return True
    return False


def format_messages(
    rows: List[Any],
    name_map: Dict[int, str],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        ch = str(row["channel_type"])
        item: Dict[str, Any] = {
            "sender": sender_display_name(row["sender_id"], name_map),
            "content": str(row["content"] or ""),
            "type": ch,
            "attempted_at": int(row["attempted_at"]),
        }
        if ch == "RDC":
            item["recipient"] = sender_display_name(row["recipient_id"], name_map)
        if ch == "GRP" and row["group_id"] is not None:
            item["group_id"] = int(row["group_id"])
        if row["place_id"]:
            item["place_id"] = str(row["place_id"])
        out.append(item)
    return out


def format_f2f_public_messages(
    history: List[Tuple[int, int, int, str]],
    name_map: Dict[int, str],
) -> List[Dict[str, Any]]:
    return [
        {
            "sender": sender_display_name(sender_id, name_map),
            "content": content,
            "type": "F2F",
            "attempted_at": at_t,
        }
        for at_t, sender_id, _mid, content in history
        if at_t > 0
    ]


def get_action_result(
    flask_session: Any,
    *,
    sim_id: str,
    task_id: str,
    request_place_id: Optional[str] = None,
    sim_dir: Path | None = None,
) -> Dict[str, Any]:
    """API 2 — poll action completion and fetch messages."""
    sim = sim_dir or get_sim_dir()
    hbm = load_session(flask_session, sim_id)
    task = load_task(flask_session, task_id, sim_id)
    if task is None:
        raise KeyError(f"unknown task_id: {task_id}")

    if request_place_id and request_place_id != task.place_id:
        log.debug(
            "ignoring request place_id=%s; task authority=%s",
            request_place_id,
            task.place_id,
        )

    env = read_env_status(sim)
    if not env or "current_tick" not in env:
        return {"status": "processing", "task_id": task_id}

    current_tick = int(env["current_tick"])
    db = ReadOnlyWorldDB(get_world_db_path(sim))
    name_map = get_name_map()

    if not check_action_complete(task, current_tick, db):
        return {
            "status": "processing",
            "task_id": task_id,
            "current_tick": current_tick,
            "start_tick": task.start_tick,
        }

    since_t = task.start_tick
    f2f_history = db.fetch_f2f_history_at(
        task.place_id, current_tick, since_t
    )
    public_messages = format_f2f_public_messages(
        [h for h in f2f_history if h[0] > since_t],
        name_map,
    )

    rdc_rows = db.fetch_messages_since(
        channel_type="RDC", since_t=since_t, t_now=current_tick
    )
    observer_messages = format_messages(rdc_rows, name_map)

    grp_rows = db.fetch_messages_since(
        channel_type="GRP", since_t=since_t, t_now=current_tick
    )
    group_messages = format_messages(grp_rows, name_map)

    stats_update = dict(hbm.stats) if hbm else initial_stats()
    current_phase = hbm.phase if hbm else task.phase

    return {
        "status": "completed",
        "task_id": task_id,
        "end_tick": current_tick,
        "public_messages": public_messages,
        "observer_messages": observer_messages,
        "group_messages": group_messages,
        "stats_update": stats_update,
        "current_phase": current_phase,
    }
