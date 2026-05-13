"""OasisAgentProfile (LAYOUT §2.G / B5 / impl/agents_dynamic_tools_and_profile.md §3.2).

Extends MiroFish's ``OasisAgentProfile`` dataclass
(``ref/MiroFish/backend/app/services/oasis_profile_generator.py:30-140``) with the
six fields introduced by Agent World v0.3:

- ``location: str`` — bootstrap place_id (world-state projection at startup)
- ``relations: list[dict]`` — typed edges to other agents
- ``capabilities: list[str]`` — granted capability names
- ``soul: str`` — long-stable persona (B5 §1, prompt-cache prefix)
- ``long_term_goal: str`` — months-to-years horizon (B5 §2)
- ``current_state: str`` — dynamic mood/intent; ``UPDATE_STATE`` rewrites it (B5 §3)

Per impl doc §3.2 改动 9: this file is the canonical dataclass; MiroFish's
``oasis_profile_generator`` should import from here in ADAPT (avoid duplicate def).
The original MiroFish fields are kept verbatim so existing CSV/JSON exporters keep
working; the six new fields are appended on top.

Helpers: :meth:`OasisAgentProfile.from_dict`, :meth:`to_dict`,
:meth:`to_csv_row` (Twitter CSV — six new columns appended at the end).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class OasisAgentProfile:
    """OASIS Agent profile data structure (Agent World v0.3 fork)."""

    # ---- MiroFish original fields (KEEP, do not reorder) -----------------
    user_id: int = 0
    user_name: str = ""
    name: str = ""
    bio: str = ""
    persona: str = ""

    # Reddit-flavoured
    karma: int = 1000

    # Twitter-flavoured
    friend_count: int = 100
    follower_count: int = 150
    statuses_count: int = 500

    # Optional persona enrichment
    age: Optional[int] = None
    gender: Optional[str] = None
    mbti: Optional[str] = None
    country: Optional[str] = None
    profession: Optional[str] = None
    interested_topics: List[str] = field(default_factory=list)

    # Source-entity backrefs (Zep ingestion)
    source_entity_uuid: Optional[str] = None
    source_entity_type: Optional[str] = None

    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))

    # ---- Agent World v0.3 additions (B5 + world-state projection) --------
    location: str = ""
    relations: List[Dict[str, Any]] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    soul: str = ""
    long_term_goal: str = ""
    current_state: str = ""

    # -- alias for fork-side code that reads ``profile.agent_id`` -----------
    @property
    def agent_id(self) -> int:
        return int(self.user_id)

    # ----------------------------------------------------------------------
    # Serialization helpers
    # ----------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Full dict serialization — MiroFish fields + six new ones."""
        return {
            # MiroFish-original
            "user_id": self.user_id,
            "user_name": self.user_name,
            "name": self.name,
            "bio": self.bio,
            "persona": self.persona,
            "karma": self.karma,
            "friend_count": self.friend_count,
            "follower_count": self.follower_count,
            "statuses_count": self.statuses_count,
            "age": self.age,
            "gender": self.gender,
            "mbti": self.mbti,
            "country": self.country,
            "profession": self.profession,
            "interested_topics": list(self.interested_topics),
            "source_entity_uuid": self.source_entity_uuid,
            "source_entity_type": self.source_entity_type,
            "created_at": self.created_at,
            # Agent World v0.3 additions
            "location": self.location,
            "relations": list(self.relations),
            "capabilities": list(self.capabilities),
            "soul": self.soul,
            "long_term_goal": self.long_term_goal,
            "current_state": self.current_state,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OasisAgentProfile":
        """Tolerant rehydration: ignores unknown keys, applies defaults."""
        if not isinstance(data, dict):
            raise TypeError(f"OasisAgentProfile.from_dict expects dict, got {type(data)!r}")
        # Accept either ``user_id`` or aliased ``agent_id``.
        if "user_id" not in data and "agent_id" in data:
            data = dict(data)
            data["user_id"] = data["agent_id"]
        kwargs: Dict[str, Any] = {}
        # Only feed known dataclass field names to avoid TypeError on stray keys.
        known = {f for f in cls.__dataclass_fields__}
        for key, val in data.items():
            if key in known:
                kwargs[key] = val
        return cls(**kwargs)

    def to_csv_row(self) -> List[str]:
        """Twitter-CSV row, with six new columns appended at the end.

        Original MiroFish Twitter CSV columns (oasis_profile_generator.py:1070-1119):
          user_id, user_name, name, bio, persona, friend_count, follower_count,
          statuses_count, age, gender, mbti, country, profession, interested_topics,
          source_entity_uuid, source_entity_type, created_at
        Appended (Agent World v0.3):
          location, relations_json, capabilities_json, soul, long_term_goal, current_state
        """
        return [
            str(self.user_id),
            self.user_name,
            self.name,
            self.bio,
            self.persona,
            str(self.friend_count),
            str(self.follower_count),
            str(self.statuses_count),
            "" if self.age is None else str(self.age),
            self.gender or "",
            self.mbti or "",
            self.country or "",
            self.profession or "",
            json.dumps(list(self.interested_topics), ensure_ascii=False),
            self.source_entity_uuid or "",
            self.source_entity_type or "",
            self.created_at,
            # six new columns (impl §3.2 改动 8)
            self.location,
            json.dumps(list(self.relations), ensure_ascii=False),
            json.dumps(list(self.capabilities), ensure_ascii=False),
            self.soul,
            self.long_term_goal,
            self.current_state,
        ]

    @staticmethod
    def csv_header() -> List[str]:
        return [
            "user_id", "user_name", "name", "bio", "persona",
            "friend_count", "follower_count", "statuses_count",
            "age", "gender", "mbti", "country", "profession",
            "interested_topics", "source_entity_uuid", "source_entity_type",
            "created_at",
            "location", "relations_json", "capabilities_json",
            "soul", "long_term_goal", "current_state",
        ]


__all__ = ["OasisAgentProfile"]
