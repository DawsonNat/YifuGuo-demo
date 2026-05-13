"""agent_world.agents package: dynamic per-step tool computation + profile dataclass.

L2 leaf module per impl/agents_dynamic_tools_and_profile.md. Re-exports the two
public surfaces consumed by L4 (``fork_oasis_agent`` / ``fork_oasis_agents_generator``).
"""

from agent_world.agents.dynamic_tools import (
    Tool,
    compute_available_tools,
)
from agent_world.agents.profile import OasisAgentProfile

__all__ = ["Tool", "compute_available_tools", "OasisAgentProfile"]
