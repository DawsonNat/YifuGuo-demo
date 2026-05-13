"""Action / event translator (L1, pure function).

Converts a single structured action record (dict produced by ActionDispatcher
or a cross-module event such as a relation / capability / state change) into
one line of natural language. The output has no I/O side effects: it is
consumed by `memory.segment` (RawEntry.payload.text) and by
`memory.compressor` when assembling the raw-log block of the Haiku prompt.

LAYOUT v0.3 §2.F: translator's responsibility shifted -- do NOT write to Zep
here; just translate to text.

Design notes:
    * Input: a dict with at minimum an ``action_type`` (string or ActionType
      enum) plus arbitrary payload fields (``t``, ``sender_id``,
      ``target_id``, ``place_id``, ``content``, ...).
    * Output: a single line. Multi-field segments use ``" | "`` as separator.
    * Time prefix ``[t=N]`` is always emitted so the compressor can sort.
    * Unknown action types fall back to a generic template (no exception),
      mirroring MiroFish ``_describe_generic``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

try:  # OASIS fork enum (extended in L0)
    from oasis.social_platform.typing import ActionType  # type: ignore
except Exception:  # pragma: no cover - vendor not on path during unit tests
    ActionType = None  # type: ignore


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _key(action_type: Any) -> str:
    """Normalise action_type to its string value (case-insensitive)."""
    if action_type is None:
        return ""
    if hasattr(action_type, "value"):  # Enum
        return str(action_type.value).lower()
    return str(action_type).lower()


def _g(action: Dict[str, Any], *names: str, default: Any = "") -> Any:
    """Get the first present field from ``action`` (top-level or under
    ``args``/``extra``)."""
    for n in names:
        if n in action and action[n] not in (None, ""):
            return action[n]
    for bucket in ("args", "extra", "action_args", "payload"):
        sub = action.get(bucket)
        if isinstance(sub, dict):
            for n in names:
                if n in sub and sub[n] not in (None, ""):
                    return sub[n]
    return default


def _prefix(action: Dict[str, Any]) -> str:
    t = _g(action, "t", "tick", "round_num", default="?")
    a = _g(action, "sender_id", "agent_id", "agent_name", default="?")
    return f"[t={t}] agent {a}"


# --------------------------------------------------------------------------- #
# per-action templates                                                        #
# --------------------------------------------------------------------------- #


def _t_create_post(a):
    return f"{_prefix(a)} posted on {_g(a, 'feed', default='feed')}: \"{_g(a, 'content')}\""


def _t_like_post(a):
    return f"{_prefix(a)} liked post {_g(a, 'post_id')} by {_g(a, 'post_author_name', 'post_author', default='someone')}"


def _t_dislike_post(a):
    return f"{_prefix(a)} disliked post {_g(a, 'post_id')}"


def _t_repost(a):
    return f"{_prefix(a)} reposted {_g(a, 'original_author_name', default='a post')}: \"{_g(a, 'original_content')}\""


def _t_quote_post(a):
    return f"{_prefix(a)} quoted a post: \"{_g(a, 'quote_content', 'content')}\""


def _t_follow(a):
    return f"{_prefix(a)} followed agent {_g(a, 'target_user_id', 'target_id', 'target_user_name')}"


def _t_create_comment(a):
    return f"{_prefix(a)} commented on post {_g(a, 'post_id')}: \"{_g(a, 'content')}\""


def _t_like_comment(a):
    return f"{_prefix(a)} liked comment {_g(a, 'comment_id')}"


def _t_dislike_comment(a):
    return f"{_prefix(a)} disliked comment {_g(a, 'comment_id')}"


def _t_search_posts(a):
    return f"{_prefix(a)} searched posts for \"{_g(a, 'query', 'keyword')}\""


def _t_search_user(a):
    return f"{_prefix(a)} searched users for \"{_g(a, 'query', 'username')}\""


def _t_mute(a):
    return f"{_prefix(a)} muted agent {_g(a, 'target_user_id', 'target_id')}"


# --- 6 Agent World additions ---------------------------------------------- #


def _t_speak_to_local(a):
    return (f"{_prefix(a)} said in {_g(a, 'place_id', default='?')} "
            f"to co-located: \"{_g(a, 'content')}\"")


def _t_send_message(a):
    ch = _g(a, "channel", default="RDC")
    return (f"{_prefix(a)} sent a {ch} message to agent "
            f"{_g(a, 'target_id', 'target')}: \"{_g(a, 'content')}\"")


def _t_request_move(a):
    return (f"{_prefix(a)} requested to move from {_g(a, 'from_place', 'old_place', default='?')} "
            f"to {_g(a, 'target_place', 'to_place')} "
            f"(reason: {_g(a, 'reason', default='n/a')})")


def _t_relation_change(a):
    op = _g(a, "op", default="add")
    op_word = "added" if str(op).lower() in ("add", "+", "added") else "removed"
    return (f"[t={_g(a, 't', default='?')}] relation {_g(a, 'relation_type', 'type')} "
            f"{op_word} between agent {_g(a, 'src', 'sender_id')} and agent "
            f"{_g(a, 'dst', 'target_id')}")


def _t_capability_change(a):
    op = _g(a, "op", default="grant")
    op_word = "granted" if str(op).lower() in ("grant", "add", "granted", "+") else "revoked"
    return (f"[t={_g(a, 't', default='?')}] capability {_g(a, 'capability', 'cap')} "
            f"{op_word} for agent {_g(a, 'agent_id', 'sender_id')}")


def _t_update_state(a):
    return f"{_prefix(a)} updated current_state to: \"{_g(a, 'new_state', 'state', 'content')}\""


# group-chat (kept minimal for MVP) ---------------------------------------- #


def _t_create_group(a):
    return f"{_prefix(a)} created group {_g(a, 'group_id', 'group_name')}"


def _t_join_group(a):
    return f"{_prefix(a)} joined group {_g(a, 'group_id')}"


def _t_leave_group(a):
    return f"{_prefix(a)} left group {_g(a, 'group_id')}"


def _t_send_to_group(a):
    return (f"{_prefix(a)} sent to group {_g(a, 'group_id')}: "
            f"\"{_g(a, 'content')}\"")


# --------------------------------------------------------------------------- #
# dispatch table (keyed by lowercased ActionType.value)                       #
# --------------------------------------------------------------------------- #


_TRANSLATORS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "create_post": _t_create_post,
    "like_post": _t_like_post,
    "dislike_post": _t_dislike_post,
    "repost": _t_repost,
    "quote_post": _t_quote_post,
    "follow": _t_follow,
    "create_comment": _t_create_comment,
    "like_comment": _t_like_comment,
    "dislike_comment": _t_dislike_comment,
    "search_posts": _t_search_posts,
    "search_user": _t_search_user,
    "mute": _t_mute,
    # Agent World additions (6)
    "speak_to_local": _t_speak_to_local,
    "send_message": _t_send_message,
    "request_move": _t_request_move,
    "relation_change": _t_relation_change,
    "capability_change": _t_capability_change,
    "update_state": _t_update_state,
    # group-chat
    "create_group": _t_create_group,
    "join_group": _t_join_group,
    "leave_group": _t_leave_group,
    "send_to_group": _t_send_to_group,
}


# --------------------------------------------------------------------------- #
# public API                                                                  #
# --------------------------------------------------------------------------- #


def translate(action: Dict[str, Any]) -> str:
    """Translate a single action record dict to one line of natural language.

    The dict must carry an ``action_type`` (str or ``ActionType``) plus any
    payload fields the per-type template expects. Missing fields degrade
    gracefully to ``""``; unknown action types fall back to a generic line.
    """
    if not isinstance(action, dict):
        return f"[t=?] (invalid action: {type(action).__name__})"
    at = _key(action.get("action_type") or action.get("type"))
    fn = _TRANSLATORS.get(at)
    if fn is None:
        return f"{_prefix(action)} did {at or 'unknown_action'} (no translator)"
    try:
        return fn(action)
    except Exception as e:  # never raise into dispatcher
        return f"{_prefix(action)} did {at} (translate error: {e!s})"


def translate_event(event_kind: str, payload: Dict[str, Any]) -> str:
    """Translate a non-ActionType event (relation_change / capability_change /
    state_change / overhear / group_event). Thin shim over ``translate`` that
    injects ``action_type=event_kind`` so the same dispatch table is reused."""
    merged = dict(payload or {})
    merged["action_type"] = event_kind
    return translate(merged)


def register_translator(action_type: Any,
                        fn: Callable[[Dict[str, Any]], str]) -> None:
    """Register / override a translator for an ActionType (conscribe-style
    extension hook; not required for MVP)."""
    _TRANSLATORS[_key(action_type)] = fn


__all__ = ["translate", "translate_event", "register_translator"]
