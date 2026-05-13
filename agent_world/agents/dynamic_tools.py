"""Dynamic per-step tool filtering (LAYOUT §2.G / §6.1 / impl/agents_dynamic_tools_and_profile.md §3.1).

Computes the *exposed* tool subset for a single agent at world tick ``t`` based on:

- **Capability** via :class:`CapabilityTable` (e.g. ``signal_uplink``, ``account_<feed>``)
- **Connectivity** via :class:`ConnectivityResolver` (φ_RDC for SEND_MESSAGE, etc.)
- **Place state** via :class:`PlaceStore` (co-located peers for SPEAK_TO_LOCAL,
  outbound coverage for REQUEST_MOVE, ``feeds_at(L_t)`` for FEED actions)
- **Group membership** for the four GRP-class actions

This replaces OASIS's assembly-time static filter (``social_agent/agent.py:87-104``)
with a per-step recomputation: the L4 ``SocialAgent.perform_action_by_llm`` rewrites
``self.tools = compute_available_tools(world, agent, t)`` on every micro-tick so the
LLM sees a fresh tool set after each MOVE / capability change / relation edit.

MVP shape: returns a list of ``{name, description, schema}`` dicts (typed alias
:data:`Tool`). The L4 agent fork wires those into ``camel.ChatAgent.tools``; if/when
L4 needs camel-native ``FunctionTool`` objects, a small adapter belongs there — not
here, so this module stays free of camel imports.

The function is sync, pure read-only, and never touches I/O — see impl doc §4.2.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from agent_world.world.connectivity import (
    FEED_CAPABILITY_PREFIX,
    GRP_CAPABILITY,
    RDC_CAPABILITY,
)

# Tool descriptor returned to the L4 agent fork. Keys: name / description / schema.
Tool = Dict[str, Any]

# Per-feed FEED-class action subsets (LAYOUT §4 OASIS typing.py). Each entry maps
# ActionType.value (kept as plain strings to avoid a hard dep on the fork enum).
_FEED_ACTIONS_TWITTER: tuple[str, ...] = (
    "create_post", "repost", "quote_post",
    "like_post", "unlike_post",
    "follow", "unfollow", "mute", "unmute",
    "search_posts", "search_user", "trend", "refresh",
)
_FEED_ACTIONS_REDDIT: tuple[str, ...] = (
    "create_post", "create_comment",
    "like_post", "dislike_post", "undo_dislike_post",
    "like_comment", "dislike_comment", "undo_dislike_comment",
    "follow", "mute",
    "search_posts", "search_user", "trend", "refresh",
)
_FEED_ACTION_MAP: Dict[str, tuple[str, ...]] = {
    "twitter": _FEED_ACTIONS_TWITTER,
    "reddit": _FEED_ACTIONS_REDDIT,
}


def _tool(tool_name: str, description: str, /, **properties: Any) -> Tool:
    """Build an MVP tool descriptor (no camel.FunctionTool construction here).

    Positional-only first arg so callers can pass a property literally named
    ``name`` (e.g. ``create_group(name=...)``) without collision.
    """
    return {
        "name": tool_name,
        "description": description,
        "schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties.keys()),
        },
    }


def _co_located(world: Any, agent_id: int) -> Iterable[int]:
    place = world.places.L_t(agent_id)
    if place is None:
        return ()
    return world.places.agents_at(place) - {agent_id}


def _place_feeds(world: Any, place_id: str | None) -> Iterable[str]:
    if place_id is None:
        return ()
    pools = getattr(world, "pools", None)
    if pools is not None and hasattr(pools, "feeds_at"):
        try:
            return list(pools.feeds_at(place_id))
        except Exception:  # noqa: BLE001 — MVP: pools may not yet be wired
            pass
    # Fallback: ConnectivityResolver's MVP shim (see connectivity.set_place_feeds)
    conn = getattr(world, "connectivity", None)
    if conn is not None:
        return list(getattr(conn, "_place_feeds", {}).get(place_id, ()))
    return ()


def _has_outbound(world: Any, place_id: str | None) -> bool:
    if place_id is None:
        return False
    for (src, dst), edge in world.places.coverage_map.items():
        if src == place_id and dst != place_id and edge.can_reach:
            return True
    return False


def _agent_groups(world: Any, agent_id: int) -> List[int]:
    conn = getattr(world, "connectivity", None)
    if conn is None:
        return []
    return [gid for gid, members in getattr(conn, "_group_members", {}).items()
            if agent_id in members]


def compute_available_tools(world: Any, agent: Any, t: int) -> List[Tool]:
    """Return the per-step tool subset for ``agent`` at tick ``t`` (impl §4.2).

    Pure in-memory filter; no LLM / I/O. Called by ``SocialAgent.perform_action_by_llm``
    every micro-tick (LAYOUT §6.1 step 7).
    """
    aid: int = int(getattr(agent, "agent_id", getattr(agent, "social_agent_id", -1)))
    place = world.places.L_t(aid)
    caps = world.capabilities.of(aid) if hasattr(world, "capabilities") else set()
    tools: List[Tool] = []

    # ----- Always available ----------------------------------------------------
    tools.append(_tool("update_state", "Rewrite your own current_state (B5).",
                       new_state={"type": "string"}))
    tools.append(_tool("relation_change", "Create or break a typed relation edge.",
                       target={"type": "integer"},
                       relation_type={"type": "string"},
                       op={"type": "string", "enum": ["create", "break"]}))
    tools.append(_tool("capability_change", "Self-grant or self-revoke a capability.",
                       capability={"type": "string"},
                       op={"type": "string", "enum": ["grant", "revoke"]}))

    # ----- F2F (SPEAK_TO_LOCAL) ------------------------------------------------
    if any(True for _ in _co_located(world, aid)):
        tools.append(_tool("speak_to_local",
                           "Speak aloud to everyone co-located. Overheard by all.",
                           content={"type": "string"}))

    # ----- RDC (SEND_MESSAGE) --------------------------------------------------
    has_rdc_cap = RDC_CAPABILITY in caps
    if has_rdc_cap and hasattr(world, "connectivity"):
        contacts = list(world.relations.contacts_of(aid))
        if any(world.connectivity.phi_rdc(aid, b) for (b, _r) in contacts):
            tools.append(_tool("send_message",
                               "Send a private remote message to a contact.",
                               target={"type": "integer"},
                               content={"type": "string"}))

    # ----- MOVE (REQUEST_MOVE) -------------------------------------------------
    if _has_outbound(world, place):
        tools.append(_tool("request_move",
                           "Request relocation to a reachable place_id.",
                           target_place={"type": "string"}))

    # ----- FEED-class (per-pool capabilities at current place) -----------------
    for feed in _place_feeds(world, place):
        if f"{FEED_CAPABILITY_PREFIX}{feed}" not in caps:
            continue
        feed_actions = _FEED_ACTION_MAP.get(feed.lower(), _FEED_ACTIONS_TWITTER)
        for action_name in feed_actions:
            tools.append(_tool(action_name,
                               f"OASIS {feed} action: {action_name}",
                               feed={"type": "string", "const": feed}))

    # ----- GROUP-class (signal_uplink + member-of-group / coverage) -----------
    if GRP_CAPABILITY in caps:
        tools.append(_tool("create_group", "Create a new chat group.",
                           name={"type": "string"}))
        tools.append(_tool("join_group", "Join an existing chat group by id.",
                           group_id={"type": "integer"}))
        if _agent_groups(world, aid):
            tools.append(_tool("leave_group", "Leave a chat group you belong to.",
                               group_id={"type": "integer"}))
            tools.append(_tool("send_to_group", "Send message to a chat group.",
                               group_id={"type": "integer"},
                               content={"type": "string"}))

    return tools


__all__ = ["Tool", "compute_available_tools"]
