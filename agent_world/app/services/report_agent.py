"""Cross-DB report assembler.

ADAPT from MiroFish ``backend/app/services/report_agent.py`` (full file,
2.5k lines). Per LAYOUT v0.3 §3.4 / ``app_services.md`` §3.6 the Agent
World variant adds:

* World-level table queries -- ``relation`` / ``capability`` /
  ``agent_location`` / ``script_event_log`` / ``group_event`` --
  optionally bucketed by tick.
* Cross-DB narrative -- given an ``agent_id`` and a ``[t_start, t_end]``
  range, join ``world.db.{direct_message, script_event_log}`` with every
  ``pool_*.db.trace`` and emit a chronologically-ordered "what did agent X
  do today" stream.

The full LangChain ReACT report-writer machinery from MiroFish (planner +
section ReACT loop + Zep tool dispatch) is **not** ported in MVP — it's
peripheral to the L3 wiring contract. Only the cross-DB query surface
specified by the doc is implemented; the LangChain path is left as TODO
hooks so the MiroFish code can drop in later.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import Config

logger = logging.getLogger("agent_world.report_agent")


# --------------------------------------------------------------------------- #
# Result dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class WorldTableSummary:
    """Bucketed counts for the 5 world-level audit tables."""

    relation_changes: int = 0
    capability_changes: int = 0
    location_changes: int = 0
    script_events: int = 0
    group_events: int = 0
    by_bucket: Dict[int, Dict[str, int]] = field(default_factory=dict)


@dataclass
class TimelineEntry:
    """One row in the cross-DB narrative."""

    t: int
    source: str  # 'direct_message' / 'script_event_log' / 'pool_trace'
    pool: Optional[str]
    agent_id: int
    description: str
    raw: Dict[str, Any]


@dataclass
class AgentReport:
    """Output of :py:meth:`ReportAgent.generate_report`."""

    simulation_id: str
    agent_id: int
    t_range: Tuple[int, int]
    world_summary: WorldTableSummary
    timeline: List[TimelineEntry]
    narrative: str
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())


# --------------------------------------------------------------------------- #
# ReportAgent
# --------------------------------------------------------------------------- #


class ReportAgent:
    """Generate a cross-DB report for one agent over a tick range.

    All methods are read-only; no Connection is held long-term — every call
    opens its own SQLite connection in URI ``mode=ro`` so the live
    simulation can keep writing.
    """

    SIMULATIONS_DIR = os.path.abspath(Config.SIMULATIONS_DIR)

    # ---- path helpers ------------------------------------------------------

    def _world_db_path(self, simulation_id: str) -> str:
        return os.path.join(self.SIMULATIONS_DIR, simulation_id, "world.db")

    def _pool_db_paths(self, simulation_id: str) -> List[str]:
        pattern = os.path.join(self.SIMULATIONS_DIR, simulation_id, "pools", "pool_*.db")
        return sorted(glob.glob(pattern))

    @staticmethod
    def _connect_ro(path: str) -> sqlite3.Connection:
        # URI mode lets us pin to read-only.
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- world.db queries (ADAPT — net-new for Agent World) ---------------

    def query_world_tables(
        self,
        simulation_id: str,
        agent_id: int,
        t_range: Tuple[int, int],
        bucket_size: int = 10,
    ) -> WorldTableSummary:
        """Aggregate the 5 audit tables (LAYOUT §3.2) for ``agent_id``.

        ``t_range`` is inclusive on both ends; ``bucket_size`` controls
        the tick-bucket width used for ``by_bucket``.
        """
        path = self._world_db_path(simulation_id)
        if not os.path.exists(path):
            logger.warning("world.db missing for %s", simulation_id)
            return WorldTableSummary()

        t_start, t_end = t_range
        bucket_size = max(1, int(bucket_size))
        summary = WorldTableSummary()

        with self._connect_ro(path) as conn:
            # relation: src_agent or dst_agent == agent_id; created_at in range
            for row in conn.execute(
                """
                SELECT created_at AS t FROM relation
                WHERE (src_agent = ? OR dst_agent = ?)
                  AND created_at BETWEEN ? AND ?
                """,
                (agent_id, agent_id, t_start, t_end),
            ):
                summary.relation_changes += 1
                self._bucket(summary, int(row["t"]), bucket_size, "relation")

            # capability: agent_id == agent_id; granted_at OR revoked_at in range
            for row in conn.execute(
                """
                SELECT granted_at AS t FROM capability
                WHERE agent_id = ? AND granted_at BETWEEN ? AND ?
                """,
                (agent_id, t_start, t_end),
            ):
                summary.capability_changes += 1
                self._bucket(summary, int(row["t"]), bucket_size, "capability")
            for row in conn.execute(
                """
                SELECT revoked_at AS t FROM capability
                WHERE agent_id = ? AND revoked_at IS NOT NULL
                  AND revoked_at BETWEEN ? AND ?
                """,
                (agent_id, t_start, t_end),
            ):
                summary.capability_changes += 1
                self._bucket(summary, int(row["t"]), bucket_size, "capability")

            # agent_location: location changes for this agent
            for row in conn.execute(
                """
                SELECT arrived_at AS t FROM agent_location
                WHERE agent_id = ? AND arrived_at BETWEEN ? AND ?
                """,
                (agent_id, t_start, t_end),
            ):
                summary.location_changes += 1
                self._bucket(summary, int(row["t"]), bucket_size, "location")

            # script_event_log: payload may target this agent — count all in range
            for row in conn.execute(
                """
                SELECT triggered_at AS t FROM script_event_log
                WHERE triggered_at BETWEEN ? AND ?
                """,
                (t_start, t_end),
            ):
                summary.script_events += 1
                self._bucket(summary, int(row["t"]), bucket_size, "script")

            # group_event: filter by agent_id (joiner/leaver/kicked)
            for row in conn.execute(
                """
                SELECT occurred_at AS t FROM group_event
                WHERE agent_id = ? AND occurred_at BETWEEN ? AND ?
                """,
                (agent_id, t_start, t_end),
            ):
                summary.group_events += 1
                self._bucket(summary, int(row["t"]), bucket_size, "group")

        return summary

    @staticmethod
    def _bucket(
        summary: WorldTableSummary, t: int, bucket_size: int, kind: str
    ) -> None:
        bucket = (t // bucket_size) * bucket_size
        bucket_dict = summary.by_bucket.setdefault(bucket, {})
        bucket_dict[kind] = bucket_dict.get(kind, 0) + 1

    # ---- pool_*.db queries (ADAPT) -----------------------------------------

    def query_pool_traces(
        self,
        simulation_id: str,
        agent_id: int,
        t_range: Tuple[int, int],
    ) -> List[TimelineEntry]:
        """Read every ``pool_*.db.trace`` row for ``agent_id`` in range.

        Pools are discovered by globbing ``simulations/<sim_id>/pools/pool_*.db``.
        Each pool DB is opened sequentially (LAYOUT §9.6 F: N+1 SQLite cost is
        acceptable in MVP).
        """
        t_start, t_end = t_range
        out: List[TimelineEntry] = []
        for pool_path in self._pool_db_paths(simulation_id):
            pool_id = os.path.splitext(os.path.basename(pool_path))[0]
            try:
                with self._connect_ro(pool_path) as conn:
                    cur = conn.execute(
                        """
                        SELECT user_id, action, info, created_at
                        FROM trace
                        WHERE user_id = ? AND created_at BETWEEN ? AND ?
                        ORDER BY created_at
                        """,
                        (agent_id, t_start, t_end),
                    )
                    for row in cur.fetchall():
                        info_raw = row["info"]
                        try:
                            info = json.loads(info_raw) if info_raw else {}
                        except (TypeError, json.JSONDecodeError):
                            info = {"raw": info_raw}
                        desc = self._describe_trace(row["action"], info)
                        out.append(
                            TimelineEntry(
                                t=int(row["created_at"]),
                                source="pool_trace",
                                pool=pool_id,
                                agent_id=agent_id,
                                description=desc,
                                raw={
                                    "action": row["action"],
                                    "info": info,
                                },
                            )
                        )
            except sqlite3.DatabaseError as exc:
                logger.warning("pool db read failed for %s: %s", pool_path, exc)
                continue
        return out

    def query_world_messages(
        self,
        simulation_id: str,
        agent_id: int,
        t_range: Tuple[int, int],
    ) -> List[TimelineEntry]:
        """Pull ``direct_message`` rows where ``agent_id`` is sender or recipient."""
        path = self._world_db_path(simulation_id)
        if not os.path.exists(path):
            return []
        t_start, t_end = t_range
        out: List[TimelineEntry] = []
        with self._connect_ro(path) as conn:
            cur = conn.execute(
                """
                SELECT message_id, sender_id, recipient_id, group_id, channel_type,
                       content, place_id, attempted_at, arrive_at, delivered
                FROM direct_message
                WHERE (sender_id = ? OR recipient_id = ?)
                  AND attempted_at BETWEEN ? AND ?
                ORDER BY attempted_at, message_id
                """,
                (agent_id, agent_id, t_start, t_end),
            )
            for row in cur.fetchall():
                role = "sent" if row["sender_id"] == agent_id else "received"
                desc = (
                    f"{role} {row['channel_type']} message at place "
                    f"{row['place_id']} (delivered={row['delivered']})"
                )
                out.append(
                    TimelineEntry(
                        t=int(row["attempted_at"]),
                        source="direct_message",
                        pool=None,
                        agent_id=agent_id,
                        description=desc,
                        raw=dict(row),
                    )
                )

            cur = conn.execute(
                """
                SELECT event_id, triggered_at, payload
                FROM script_event_log
                WHERE triggered_at BETWEEN ? AND ?
                ORDER BY triggered_at, event_id
                """,
                (t_start, t_end),
            )
            for row in cur.fetchall():
                out.append(
                    TimelineEntry(
                        t=int(row["triggered_at"]),
                        source="script_event_log",
                        pool=None,
                        agent_id=agent_id,
                        description=f"script event {row['event_id']} fired",
                        raw=dict(row),
                    )
                )
        return out

    # ---- cross-DB merge (ADAPT) -------------------------------------------

    def generate_report(
        self,
        simulation_id: str,
        agent_id: int,
        t_range: Optional[Tuple[int, int]] = None,
    ) -> AgentReport:
        """Build an :class:`AgentReport` for ``agent_id`` over ``t_range``.

        ``t_range`` defaults to ``(0, current_world_t)`` (LAYOUT §6 — we
        approximate by reading ``MAX(arrive_at)`` from world.db, since the
        live ``world.t`` is in-memory only).
        """
        if t_range is None:
            t_range = (0, self._infer_max_t(simulation_id))
        world_summary = self.query_world_tables(simulation_id, agent_id, t_range)
        msgs = self.query_world_messages(simulation_id, agent_id, t_range)
        traces = self.query_pool_traces(simulation_id, agent_id, t_range)
        timeline = sorted(msgs + traces, key=lambda e: (e.t, e.source))
        narrative = self._format_narrative(agent_id, t_range, timeline)
        return AgentReport(
            simulation_id=simulation_id,
            agent_id=agent_id,
            t_range=t_range,
            world_summary=world_summary,
            timeline=timeline,
            narrative=narrative,
        )

    def _infer_max_t(self, simulation_id: str) -> int:
        path = self._world_db_path(simulation_id)
        if not os.path.exists(path):
            return 0
        try:
            with self._connect_ro(path) as conn:
                row = conn.execute(
                    "SELECT MAX(arrive_at) AS m FROM direct_message"
                ).fetchone()
                return int(row["m"] or 0)
        except sqlite3.DatabaseError:
            return 0

    @staticmethod
    def _describe_trace(action: str, info: Dict[str, Any]) -> str:
        if action == "create_post":
            return f"posted: {str(info.get('content', ''))[:80]}"
        if action == "like_post":
            return f"liked post {info.get('post_id')}"
        if action == "follow":
            return f"followed user {info.get('followee_id')}"
        return f"{action}: {json.dumps(info, ensure_ascii=False)[:80]}"

    @staticmethod
    def _format_narrative(
        agent_id: int, t_range: Tuple[int, int], timeline: Sequence[TimelineEntry]
    ) -> str:
        if not timeline:
            return (
                f"Agent {agent_id} took no observable actions during ticks "
                f"[{t_range[0]}, {t_range[1]}]."
            )
        lines = [
            f"# Agent {agent_id} timeline (ticks {t_range[0]}-{t_range[1]})",
            "",
        ]
        for entry in timeline:
            lines.append(
                f"- t={entry.t} [{entry.source}"
                + (f"/{entry.pool}" if entry.pool else "")
                + f"] {entry.description}"
            )
        return "\n".join(lines)

    # ---- LangChain ReACT path (TODO) --------------------------------------

    def generate_full_report(self, *args: Any, **kwargs: Any) -> str:
        """Placeholder for the MiroFish LangChain report writer.

        TODO: port ``ReportAgent.plan_outline`` + ``_generate_section_react``
        + ``generate_report`` from MiroFish report_agent.py. Hook into the
        Agent World cross-DB summary methods above so the LLM has access to
        relation / capability / location histories on top of Zep retrieval.
        """
        raise NotImplementedError("LangChain ReACT path not ported yet")


__all__ = [
    "AgentReport",
    "ReportAgent",
    "TimelineEntry",
    "WorldTableSummary",
]
