"""LLM-driven generator for ``simulation_config.json``.

ADAPT from MiroFish ``backend/app/services/simulation_config_generator.py``
(L1-991). Per LAYOUT v0.3 §2.B and ``app_services.md`` §3.4, the top-level
``SimulationParameters`` dataclass gains three new keys:

* ``world_config:   {places, coverage, events}``    (LAYOUT §2.A / §2.E)
* ``channel_config: {default_delays, group_message, failed_attempt_ttl_ticks,
                     group_event_ttl_ticks}``       (LAYOUT §7.1)
* ``memory_config:  {compressor, retry_policy}``    (LAYOUT §2.F)

The full LLM pipeline (multi-step time / event / agent generation, JSON
repair, retry) from MiroFish is **not** copied verbatim — it lives behind a
single ``generate_config()`` callable that produces a sane, populated
:class:`SimulationParameters` even without an LLM available, so downstream
runner code can boot. Replacing the body with the real LLM chain is a
later-stage TODO; the dataclass shape is the load-bearing contract.

What IS preserved:
* The ``AgentActivityConfig / TimeSimulationConfig / EventConfig /
  PlatformConfig`` shapes, so existing MiroFish profile-generator output
  remains assignable.
* JSON serialization via :py:meth:`SimulationParameters.to_json` /
  :py:meth:`to_dict`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ..config import Config

logger = logging.getLogger("agent_world.simulation_config_generator")


# --------------------------------------------------------------------------- #
# Per-agent / time / event / platform sub-configs (KEEP from MiroFish)
# --------------------------------------------------------------------------- #


@dataclass
class AgentActivityConfig:
    agent_id: int
    entity_uuid: str
    entity_name: str
    entity_type: str
    activity_level: float = 0.5
    posts_per_hour: float = 1.0
    comments_per_hour: float = 2.0
    active_hours: List[int] = field(default_factory=lambda: list(range(8, 23)))
    response_delay_min: int = 5
    response_delay_max: int = 60
    sentiment_bias: float = 0.0
    stance: str = "neutral"
    influence_weight: float = 1.0


@dataclass
class TimeSimulationConfig:
    total_simulation_hours: int = 72
    minutes_per_round: int = 60
    agents_per_hour_min: int = 5
    agents_per_hour_max: int = 20
    peak_hours: List[int] = field(default_factory=lambda: [19, 20, 21, 22])
    peak_activity_multiplier: float = 1.5
    off_peak_hours: List[int] = field(
        default_factory=lambda: [0, 1, 2, 3, 4, 5]
    )
    off_peak_activity_multiplier: float = 0.05
    morning_hours: List[int] = field(default_factory=lambda: [6, 7, 8])
    morning_activity_multiplier: float = 0.4
    work_hours: List[int] = field(default_factory=lambda: list(range(9, 19)))
    work_activity_multiplier: float = 0.7


@dataclass
class EventConfig:
    initial_posts: List[Dict[str, Any]] = field(default_factory=list)
    scheduled_events: List[Dict[str, Any]] = field(default_factory=list)
    hot_topics: List[str] = field(default_factory=list)
    narrative_direction: str = ""


@dataclass
class PlatformConfig:
    platform: str
    recency_weight: float = 0.4
    popularity_weight: float = 0.3
    relevance_weight: float = 0.3
    viral_threshold: int = 10
    echo_chamber_strength: float = 0.5


# --------------------------------------------------------------------------- #
# NEW Agent World blocks
# --------------------------------------------------------------------------- #


@dataclass
class WorldConfig:
    """``world_config.{places, coverage, events}`` (LAYOUT §2.A, §10.3 Tier-1).

    Schema is *intentionally* loose at this layer — the conscribe-generated
    Pydantic schema lives in ``agent_world/config/world_config.py`` and the
    runner validates against it on load. The service-layer dataclass just
    holds opaque dict blocks so the LLM prompt has somewhere to drop
    JSON.
    """

    places: List[Dict[str, Any]] = field(default_factory=list)
    coverage: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ChannelConfig:
    """``channel_config.*`` (LAYOUT §7.1 + B1.1)."""

    # F2F = 0, RDC = 1, GRP = 0; cross-pool / cross-planet override per-edge.
    default_delays: Dict[str, int] = field(
        default_factory=lambda: {"F2F": 0, "RDC": 1, "GRP": 0}
    )
    group_message: Dict[str, Any] = field(default_factory=dict)
    failed_attempt_ttl_ticks: int = 1
    group_event_ttl_ticks: int = 1


@dataclass
class MemoryConfig:
    """``memory_config.*`` (LAYOUT §2.F + v0.3 BehaviorCompressor)."""

    compressor: Dict[str, Any] = field(
        default_factory=lambda: {
            "model": "claude-haiku",
            "max_raw_actions": 30,
            "summary_target_sentences": [1, 3],
            "timeout_seconds": 30,
        }
    )
    retry_policy: Dict[str, Any] = field(
        default_factory=lambda: {"max_retries": 1, "backoff_seconds": 2}
    )


# --------------------------------------------------------------------------- #
# Top-level config (ADAPT — new keys added)
# --------------------------------------------------------------------------- #


@dataclass
class SimulationParameters:
    """Top-level config dataclass.

    The MiroFish original (L150-174) had: ``simulation_id / project_id /
    graph_id / simulation_requirement / time_config / agent_configs /
    event_config / twitter_config / reddit_config + LLM metadata``. Agent
    World **adds**:

    * ``world_config``    -- places / coverage / events
    * ``channel_config``  -- delays, group message, TTL
    * ``memory_config``   -- compressor + retry policy
    """

    simulation_id: str
    project_id: str
    graph_id: str
    simulation_requirement: str

    # KEPT from MiroFish ----------------------------------------------------
    time_config: TimeSimulationConfig = field(default_factory=TimeSimulationConfig)
    agent_configs: List[AgentActivityConfig] = field(default_factory=list)
    event_config: EventConfig = field(default_factory=EventConfig)
    twitter_config: Optional[PlatformConfig] = None
    reddit_config: Optional[PlatformConfig] = None

    # NEW Agent World keys --------------------------------------------------
    world_config: WorldConfig = field(default_factory=WorldConfig)
    channel_config: ChannelConfig = field(default_factory=ChannelConfig)
    memory_config: MemoryConfig = field(default_factory=MemoryConfig)

    # Generation metadata ---------------------------------------------------
    llm_model: str = ""
    llm_base_url: str = ""
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    generation_reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "project_id": self.project_id,
            "graph_id": self.graph_id,
            "simulation_requirement": self.simulation_requirement,
            "time_config": asdict(self.time_config),
            "agent_configs": [asdict(a) for a in self.agent_configs],
            "event_config": asdict(self.event_config),
            "twitter_config": (
                asdict(self.twitter_config) if self.twitter_config else None
            ),
            "reddit_config": (
                asdict(self.reddit_config) if self.reddit_config else None
            ),
            # NEW keys
            "world_config": asdict(self.world_config),
            "channel_config": asdict(self.channel_config),
            "memory_config": asdict(self.memory_config),
            # Metadata
            "llm_model": self.llm_model,
            "llm_base_url": self.llm_base_url,
            "generated_at": self.generated_at,
            "generation_reasoning": self.generation_reasoning,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #


class SimulationConfigGenerator:
    """LLM-backed generator for :class:`SimulationParameters`.

    The full MiroFish multi-step prompt chain (time -> events -> agents
    in batches -> platform) is omitted in this skeleton; only the entry
    point :meth:`generate_config` is provided so the rest of the system
    can compile and the runner has something to load. The generator falls
    back to deterministic defaults whenever the LLM call is skipped or
    fails, ensuring output remains a valid :class:`SimulationParameters`.

    TODO (port from MiroFish L535-989):
    * ``_generate_time_config`` / ``_parse_time_config``
    * ``_generate_event_config`` / ``_parse_event_config``
    * ``_generate_agent_configs_batch``
    * ``_assign_initial_post_agents``
    * Add three NEW prompts for ``world_config`` / ``channel_config`` /
      ``memory_config`` (LAYOUT §10.3 Tier-1 schema sources).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model_name = model_name or Config.LLM_MODEL_NAME

    # ---- public API --------------------------------------------------------

    def generate_config(
        self,
        simulation_id: str,
        project_id: str,
        graph_id: str,
        simulation_requirement: str,
        document_text: str = "",
        entities: Optional[List[Any]] = None,
        enable_twitter: bool = True,
        enable_reddit: bool = True,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> SimulationParameters:
        """Build a :class:`SimulationParameters` for the given simulation."""
        entities = entities or []
        logger.info(
            "generate_config: simulation=%s entities=%d", simulation_id, len(entities)
        )

        if progress_callback:
            progress_callback(1, 4, "generating time / agent / event configs")

        time_config = TimeSimulationConfig()
        agent_configs = self._default_agent_configs(entities)
        event_config = EventConfig()

        if progress_callback:
            progress_callback(2, 4, "generating world_config")
        world_config = self._default_world_config(simulation_requirement)

        if progress_callback:
            progress_callback(3, 4, "generating channel_config & memory_config")
        channel_config = ChannelConfig()
        memory_config = MemoryConfig()

        twitter_config = (
            PlatformConfig(
                platform="twitter",
                recency_weight=0.4,
                popularity_weight=0.3,
                relevance_weight=0.3,
                viral_threshold=10,
                echo_chamber_strength=0.5,
            )
            if enable_twitter
            else None
        )
        reddit_config = (
            PlatformConfig(
                platform="reddit",
                recency_weight=0.3,
                popularity_weight=0.4,
                relevance_weight=0.3,
                viral_threshold=15,
                echo_chamber_strength=0.6,
            )
            if enable_reddit
            else None
        )

        if progress_callback:
            progress_callback(4, 4, "assembling SimulationParameters")

        params = SimulationParameters(
            simulation_id=simulation_id,
            project_id=project_id,
            graph_id=graph_id,
            simulation_requirement=simulation_requirement,
            time_config=time_config,
            agent_configs=agent_configs,
            event_config=event_config,
            twitter_config=twitter_config,
            reddit_config=reddit_config,
            world_config=world_config,
            channel_config=channel_config,
            memory_config=memory_config,
            llm_model=self.model_name or "",
            llm_base_url=self.base_url or "",
            generation_reasoning=(
                "skeleton generator — defaults applied; replace with the "
                "MiroFish multi-step LLM chain to produce a tuned config"
            ),
        )
        return params

    # ---- defaults ----------------------------------------------------------

    @staticmethod
    def _default_agent_configs(entities: List[Any]) -> List[AgentActivityConfig]:
        out: List[AgentActivityConfig] = []
        for i, ent in enumerate(entities):
            uuid_ = getattr(ent, "uuid", "")
            name = getattr(ent, "name", f"agent_{i}")
            etype_fn = getattr(ent, "get_entity_type", None)
            etype = etype_fn() if callable(etype_fn) else "Unknown"
            out.append(
                AgentActivityConfig(
                    agent_id=i,
                    entity_uuid=uuid_,
                    entity_name=name,
                    entity_type=etype or "Unknown",
                )
            )
        return out

    @staticmethod
    def _default_world_config(simulation_requirement: str) -> WorldConfig:
        # Empty by design — the LLM must populate places/coverage/events
        # against the conscribe-generated schema. We leave structure
        # placeholders for downstream IDEs / docs.
        return WorldConfig(places=[], coverage=[], events=[])


# Convenience module-level shim — mirrors MiroFish API surface.
def generate_config(payload: Dict[str, Any]) -> SimulationParameters:
    """Build a :class:`SimulationParameters` from an HTTP-style payload dict."""
    gen = SimulationConfigGenerator()
    return gen.generate_config(
        simulation_id=payload["simulation_id"],
        project_id=payload.get("project_id", ""),
        graph_id=payload.get("graph_id", ""),
        simulation_requirement=payload.get("simulation_requirement", ""),
        document_text=payload.get("document_text", ""),
        entities=payload.get("entities", []),
        enable_twitter=payload.get("enable_twitter", True),
        enable_reddit=payload.get("enable_reddit", True),
    )


__all__ = [
    "AgentActivityConfig",
    "ChannelConfig",
    "EventConfig",
    "MemoryConfig",
    "PlatformConfig",
    "SimulationConfigGenerator",
    "SimulationParameters",
    "TimeSimulationConfig",
    "WorldConfig",
    "generate_config",
]
