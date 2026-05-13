"""LLM-backed lightweight agent for the demo (no OASIS / camel deps).

The agent satisfies the surface used by ``WorldStep._run_place``:

* fields ``agent_id``, ``soul``, ``long_term_goal``, ``current_state``,
  ``last_message_seen_at``, ``location`` (read-through to ``world.places``).
* ``async perform_action_by_llm(world, t)`` — calls ``perception_builder.build``
  for the system+user prompt, sends both to an OpenAI-compatible chat
  completions endpoint with tool-calls enabled, returns a CAMEL-style
  ``response.info["tool_calls"]`` shape that ``WorldStep._extract_actions``
  understands.

The tool schema mirrors the demo-relevant subset of LAYOUT v0.3 actions:
``speak_to_local``, ``send_message``, ``request_move``, ``update_state``,
``do_nothing``.  Add more here when the scenario uses them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Tool schema (OpenAI tool-calls spec) — names match ActionType.value         #
# --------------------------------------------------------------------------- #

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "speak_to_local",
            "description": (
                "对当前地点里所有人当面说出口的话（同地点 F2F 广播）。"
                "重点：是『口语』，不是『短信/微信』。\n"
                "  ✓ 像当面对话：『回来啦？』『你又喝多少？』『靠过来点。』\n"
                "  ✗ 别用 SMS 套话：『辛苦了』『回来后...』『早点休息』『等你回家』"
                "    『记得吃饭』——这些是异地短信腔，对方就在你眼前不会这么说。\n"
                "  ✗ 别在 content 里写收信人名字当抬头（『思琪，...』很 SMS）；"
                "    口语里直接说事就行，称呼用昵称或语气词。\n"
                "适合：日常对话、当面调情、当面撒谎、当面甩脸子。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "你说出口的话，1-3 句，必须是口语。"
                            "禁止『辛苦了 / 回来后 / 早点休息 / 等你回家』式短信腔。"
                        ),
                    }
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "发私信/短信给某个具体 agent_id（异地 RDC 私聊，1 拍后送达）。"
                "这才是『SMS / 微信』通道——异地、异步、可以带『辛苦了』『回来后』"
                "『早点休息』这种文字消息腔。\n"
                "硬性前提：对方必须不在你当前位置；同地点的人请用 speak_to_local。\n"
                "适合：和情人对暗号、和老公远程汇报行踪、远程关心 / 撩拨。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "integer", "description": "收件人 agent_id"},
                    "content": {"type": "string"},
                },
                "required": ["target", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_move",
            "description": (
                "请求移动到另一个地点（本拍末结算，下一拍才真正在新地点）。"
                "想去咖啡馆和情人见面就用这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "string", "description": "目的地 place_id"},
                },
                "required": ["place_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_state",
            "description": (
                "改写自己的 current_state 字段（系统提示词里的『当前状态』段）。"
                "只在真的有心理变化时用——比如产生了愧疚、起了贼心、突然冷静下来。"
                "别为了每个小念头都改。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "new_state": {
                        "type": "string",
                        "description": "新的内心状态描述，1-3 句。",
                    },
                },
                "required": ["new_state"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_short_term_goal",
            "description": (
                "改写自己的『当前小目标』段（system prompt 里的第 4 段）。"
                "每拍可调用，专门用于在 F2F 中打住循环、明确『下一步要做什么』。"
                "好的小目标举例：『把张伟的怀疑暂时安抚住，然后离开去 boutique_cafe 找 Marcus』。"
                "差的小目标举例：『继续聊』、『随便』、『再看看』。"
                "如果你发现自己连续几拍都在和同一个人说同主题，先调它再说话。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "new_goal": {
                        "type": "string",
                        "description": "1-2 句话，明确指出下一步行动指向。",
                    },
                },
                "required": ["new_goal"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_to_group",
            "description": (
                "在你所在的群聊里发消息。所有群成员下一拍能看到（受网络覆盖限制）。"
                "适合：和闺蜜实时报备 / 吐槽 / 求助 / 撒娇。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer"},
                    "content": {"type": "string"},
                },
                "required": ["group_id", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "do_nothing",
            "description": "本拍不做任何动作（沉默 / 在心里盘算）。慎用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


# --------------------------------------------------------------------------- #
# CAMEL-shaped tool-call response (so WorldStep._extract_actions reads it)    #
# --------------------------------------------------------------------------- #


@dataclass
class _ToolCall:
    tool_name: str
    args: Dict[str, Any]


@dataclass
class _Response:
    info: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Demo agent                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class DemoAgent:
    agent_id: int
    name: str
    soul: str = ""
    long_term_goal: str = ""
    current_state: str = ""
    # Tick at which current_state was last written. 0 means "still the
    # scenario-seeded value"; bumped by dispatcher when UPDATE_STATE fires.
    current_state_set_at: int = 0
    # 5th system-prompt segment; mutated via update_short_term_goal action.
    short_term_goal: str = ""
    last_message_seen_at: int = -1
    # Group memberships: list of (group_id, group_name) — surfaced into the
    # observation so the LLM knows which group_ids are valid for send_to_group.
    groups: List[Dict[str, Any]] = field(default_factory=list)
    # Other agents the LLM knows about by name (id -> display name) — used
    # by the observation renderer to make co_located / contacts / messages
    # human-readable instead of bare integer IDs.
    name_directory: Dict[int, str] = field(default_factory=dict)
    # Available place_ids the LLM may pick for `request_move` — listed in
    # the observation so it can't hallucinate a name like "bar_cafe".
    available_places: List[str] = field(default_factory=list)
    # Wired by the runner before the loop starts.
    perception_builder: Any = None
    segment_store: Any = None      # so the agent sees its OWN past actions
    total_ticks: int = 0           # for "tick X / N" pressure in prompt
    # Wall-clock display: Clock stays a pure integer tick (LAYOUT B2);
    # this is a demo-layer mapping so the LLM sees "现在 21:05" instead of
    # bare tick numbers and can act on persona time-triggers (e.g. "after
    # 22:30 he panics"). ``wall_start_time`` parses as HH:MM 24-hour.
    wall_start_time: str = "20:30"
    minutes_per_tick: int = 5
    client: Optional[AsyncOpenAI] = None
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    max_tokens: int = 400

    async def perform_action_by_llm(self, world: Any, t: int) -> _Response:
        """One micro-tick decision (LAYOUT §6.1 step 7.b)."""
        if self.perception_builder is None or self.client is None:
            return _Response(info={"tool_calls": []})

        try:
            sys_prompt, obs = await self.perception_builder.build(self, world, t)
        except Exception as exc:  # noqa: BLE001
            log.error("agent %s perception failed: %s", self.agent_id, exc)
            return _Response(info={"tool_calls": []})

        user_text = self._observation_to_text(obs, t)

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_text},
        ]

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("agent %s LLM call failed: %s", self.agent_id, exc)
            return _Response(info={"tool_calls": []})

        msg = resp.choices[0].message
        tool_calls: List[_ToolCall] = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(_ToolCall(tool_name=tc.function.name, args=args))

        # No tool was called -> log the freeform thought and emit do_nothing.
        if not tool_calls:
            content = (msg.content or "").strip()
            if content:
                log.info("agent %s thought: %s", self.agent_id, content[:200])
            tool_calls = [_ToolCall(tool_name="do_nothing", args={})]

        return _Response(info={"tool_calls": tool_calls})

    # -- helpers -----------------------------------------------------------

    # ------------------------------------------------------------------
    # Helpers for human-readable IDs in the prompt
    # ------------------------------------------------------------------

    def _label(self, agent_id: Any) -> str:
        try:
            aid = int(agent_id)
        except (TypeError, ValueError):
            return str(agent_id)
        name = self.name_directory.get(aid)
        return f"agent_{aid} ({name})" if name else f"agent_{aid}"

    def _wall_clock_label(self, t: int) -> str:
        """Map tick → 'HH:MM' string using start_time + t * minutes_per_tick.

        Wraps modulo 24h. Returns '' if start_time fails to parse so the
        prompt degrades gracefully rather than crashing the agent loop.
        """
        try:
            h, m = self.wall_start_time.split(":")
            base_min = int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return ""
        total = (base_min + int(t) * int(self.minutes_per_tick)) % (24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"

    def _observation_to_text(self, obs: Any, t: int) -> str:
        lines: List[str] = []
        wall = self._wall_clock_label(t)
        wall_tag = f" 世界时间 {wall}" if wall else ""

        # ---- STALE-STATE HARD GUARD (top of prompt for max attention) ----
        # The user prompt is long; warnings buried below tend to be ignored.
        # When current_state hasn't been touched for ≥5 ticks the LLM has
        # almost always changed location / received plot beats, and a stale
        # state breaks roleplay. We promote this to a top-of-prompt hard
        # requirement that overrides the regular "pick any tool" choice.
        set_at = int(getattr(self, "current_state_set_at", 0) or 0)
        stale_ticks = max(0, int(t) - set_at)
        force_update_state = stale_ticks >= 5
        if force_update_state:
            mins_stale = stale_ticks * int(self.minutes_per_tick)
            lines.append(
                f"# 🛑 强制要求：你的 # 当前状态 已经 {stale_ticks} 拍 "
                f"(约 {mins_stale} 分钟) 没动了，严重滞后。"
                "**本拍必须且只能调用 update_state**——把『现在我在哪 / 心里"
                "在想什么 / 接下来要干嘛』用 1-3 句重写一遍；其它工具下一拍"
                "再用。下面的对话和动作选项本拍一律忽略。"
            )
        if self.total_ticks:
            lines.append(
                f"# 当前 tick：t={t} / 共 {self.total_ticks} 拍 "
                f"（剩 {max(self.total_ticks - t - 1, 0)} 拍剧情就结束了）"
                f"{wall_tag}"
            )
        else:
            lines.append(f"# 当前 tick：t={t}{wall_tag}")
        if wall:
            lines.append(
                f"  注意：世界时间在推进——每拍 +{self.minutes_per_tick} 分钟。"
                "如果你的 # 当前状态 里写的时间或心理（『现在 8:30』『还在等』）"
                "已经和真实时间对不上，必须 update_state 主动更新；"
                "人设里写的时间触发线（如『过了 22:30 就 panic』）一旦跨过，"
                "立刻按新状态行动。"
            )

        # ---- current_state staleness warning ----------------------------
        set_at = int(getattr(self, "current_state_set_at", 0) or 0)
        stale_ticks = int(t) - set_at
        if stale_ticks >= 3:
            mins_stale = stale_ticks * int(self.minutes_per_tick)
            lines.append(
                f"# ⚠ 你的 # 当前状态 已经 {stale_ticks} 拍（约 {mins_stale} 分钟）"
                "没动过。这期间你可能换了地点 / 收到新消息 / 时间过了关键线——"
                "请优先 update_state 写一句 1-3 句的新状态，把『现在我在哪 / 心里"
                "在想什么 / 接下来要干嘛』讲清楚，再继续别的动作。"
            )
        lines.append(f"# 你是：agent_id={self.agent_id}（{self.name}）")
        lines.append(f"# 你现在的位置：{obs.self_location}")
        briefs = list(getattr(obs, "available_places_brief", []) or [])
        if briefs:
            lines.append(
                "# 可以 request_move 到的其他地点（严格用以下 place_id；"
                "可见名册由地点 roster_visible 决定。看清楚『你和在场人的关系』"
                "再决定要不要去——闯到陌生人/敌对方的领地后果自负）："
            )
            for b in briefs:
                summary = (b.summary or "").strip()
                tail = f" —— {summary}" if summary else ""
                lines.append(f"  - {b.place_id}{tail}")
                if b.occupants is None:
                    lines.append("    目前在这里的人：未知")
                elif b.occupants:
                    occ_strs: List[str] = []
                    for oid in b.occupants:
                        rels = b.occupant_relations.get(oid, [])
                        rel_tag = (
                            "[" + "、".join(rels) + "]" if rels else "[陌生人]"
                        )
                        occ_strs.append(f"{self._label(oid)}{rel_tag}")
                    lines.append(
                        "    目前在这里的人：" + "、".join(occ_strs)
                    )
                else:
                    lines.append("    目前在这里的人：没人")
        elif self.available_places:
            # Fallback when PerceptionBuilder didn't fill briefs (older path).
            others = [p for p in self.available_places if p != obs.self_location]
            if others:
                lines.append(
                    "# 可以 request_move 到的其他地点："
                    + "、".join(others)
                )

        co = list(obs.co_located_agents) if obs.co_located_agents else []
        if co:
            lines.append(
                "# 同地点的人（可以用 speak_to_local 当面说话）："
                + "、".join(self._label(a) for a in co)
            )
        else:
            lines.append("# 同地点的人：没人——speak_to_local 没人会听到")

        arrivals = list(getattr(obs, "recent_arrivals", []) or [])
        departures = list(getattr(obs, "recent_departures", []) or [])
        if arrivals or departures:
            parts: List[str] = []
            if arrivals:
                parts.append(
                    "刚走进来：" + "、".join(self._label(a) for a in arrivals)
                )
            if departures:
                parts.append(
                    "刚离开：" + "、".join(self._label(a) for a in departures)
                )
            lines.append(
                "# 本拍开始时（也就是上一拍末）这里发生的人员变动——"
                "立刻更新你的『谁在房间里』认知："
            )
            for p in parts:
                lines.append(f"  - {p}")

        if obs.contacts:
            cs = []
            for c in obs.contacts:
                reach = "可达" if c.can_reach_now else "不可达"
                rels = list(getattr(c, "relation_types", []) or [])
                rel_tag = "、".join(rels) if rels else "陌生人"
                cs.append(
                    f"{self._label(c.agent_id)}[{rel_tag}|{reach}]"
                )
            lines.append("# 你的联系人（可以用 send_message 私信）：" + "、".join(cs))

        if self.groups:
            lines.append("# 你所在的群（用 send_to_group 在群里发言）：")
            for g in self.groups:
                lines.append(
                    f"  - group_id={g['group_id']} 名称={g.get('name')!r} "
                    f"成员={g.get('members', [])}"
                )

        if obs.incoming_messages:
            lines.append("# 你收到的消息（上一拍及之前送达的）：")
            for m in obs.incoming_messages:
                sender = getattr(m, "sender_id", None)
                content = getattr(m, "content", "")
                ch = getattr(m, "channel_type", "?")
                gid = getattr(m, "group_id", None)
                tag = ch + (f"#g{gid}" if gid else "")
                lines.append(f"  - [{tag}] 来自 {self._label(sender)}：{content}")

        f2f_history = list(getattr(obs, "f2f_local_history", []) or [])
        if f2f_history:
            # 标注当前是否还在场——防止 LLM 把已离开的人当成在场，
            # 然后徒劳地用 speak_to_local 回复。
            current_here = set(obs.co_located_agents or []) | {self.agent_id}
            lines.append(
                "# 你所在地点最近的 F2F 对话脉络（按时间顺序，含你自己说过的）："
            )
            lines.append(
                "  注意：speak_to_local 只能被『目前在这里』的人听到；"
                "[已离开] 标记的人已经不在本地，要联系他们用 send_message。"
            )
            for at_t, sender, _mid, content in f2f_history:
                tag = "" if sender in current_here else "  [已离开]"
                lines.append(
                    f"  - t={at_t} {self._label(sender)}{tag}：{content}"
                )

        if obs.overheard:
            lines.append("# 你旁听到的（不是发给你的，但你恰好在场）：")
            for o in obs.overheard:
                lines.append(f"  - {getattr(o, 'content', '')}")

        if obs.recent_failed_attempts:
            lines.append(
                f"# 上一拍你发了 {len(obs.recent_failed_attempts)} 条消息没送到"
                "（对方不可达 / 不在群里）。"
            )

        if obs.group_events:
            lines.append("# 上一拍发生的群事件：")
            for ge in obs.group_events:
                lines.append(
                    f"  - {getattr(ge, 'event_type', '?')} "
                    f"agent_{getattr(ge, 'agent_id', '?')} "
                    f"在 group_{getattr(ge, 'group_id', '?')}"
                )

        # ---- 你自己最近的动作（防 loop 关键）-------------------------------
        my_recent = self._recent_self_actions(limit=4)
        if my_recent:
            lines.append("# 你自己最近做过的动作（不要再重复说同样的话）：")
            for entry in my_recent:
                lines.append(f"  - t={entry['t']} {entry['summary']}")

        lines.append("")
        lines.append(
            "【本拍硬性要求】\n"
            "1) 必须选 1 个动作；必须推进剧情。\n"
            "2) 严禁与你自己上面列的『最近动作』内容雷同——别再绕同一句客套话。\n"
            "3) 如果上一拍说过『马上走 / 一会儿就走 / 出门』之类的话，这一拍\n"
            "   就 request_move 真的离开，不要再嘴上重复一次。\n"
            "4) 想联系不在身边的人 → send_message；想在群里搞事 → send_to_group。\n"
            "5) 想表达内心状态变了（愧疚 / 兴奋 / 怒火 / 醒悟）→ update_state。\n"
            "6) 时间有限，剧情要发展——靠重复对话拖时间会错过结局。\n"
            "7) 如果连续 ≥3 拍都在和同一个人说同一主题（看 F2F 对话脉络判断）\n"
            "   → 优先 update_short_term_goal 重新设定方向，而不是再说一句车轱辘话。\n"
            "   小目标越具体越好，例如『脱身去 boutique_cafe 找 Marcus』。\n"
            "8) speak_to_local 是当面口语，对方就在眼前——别写『今晚回来后...』\n"
            "   『辛苦了』『早点休息』这种短信腔。要写 SMS 腔，请用 send_message。\n"
            "9) 注意『# 同地点的人』和『# 本拍刚到/刚走』两段：有人刚走进来\n"
            "   或离开，你要在内心模型里立刻更新——别继续按上一拍的『他/她\n"
            "   还在 / 不在』逻辑说话。\n"
            "10) 看顶部『世界时间 HH:MM』：时间在推进。\n"
            "    a) current_state 里凡是带具体时间或时间感（『现在 8:30』、\n"
            "       『等到 10 点』）一旦和真实世界时间对不上 → update_state。\n"
            "    b) 人设 soul 里的时间触发线（如『过了 22:30 开始焦虑发作』）\n"
            "       一旦跨过，先 update_state 切换心理，再按新状态行动。\n"
            "    c) 别假装时间不存在——已经 23:00 还说『再过两小时就睡』是 OOC。\n"
            "\n可选工具：\n"
            "  • speak_to_local           —— 当面说话（只对同地点的人有效）\n"
            "  • send_message             —— 私信某个 agent_id（1 拍后到达）\n"
            "  • send_to_group            —— 在群里说话（你必须是群成员）\n"
            "  • request_move             —— 本拍末换地点（下一拍生效）\n"
            "  • update_state             —— 改写自己的当前内心状态\n"
            "  • update_short_term_goal   —— 改写当前小目标（防 loop 关键）\n"
            "  • do_nothing               —— 真的无话可说时才用，能不用就不用\n"
            "每一拍只调用一个工具，参数严格符合 schema。"
            "保持人物性格——人物是什么样就说什么样的话；不要 OOC，也不要互相『体面』下去。"
        )
        return "\n".join(lines)

    def _recent_self_actions(self, limit: int = 4) -> List[Dict[str, Any]]:
        """Read the agent's own recent actions out of the segment_store.

        Returns the last ``limit`` entries as ``[{'t', 'summary'}, ...]`` so
        the prompt can echo them back to the LLM. Without this, the LLM is
        blind to what it just did and tends to loop.
        """
        if self.segment_store is None:
            return []
        try:
            entries = self.segment_store.get(self.agent_id) or []
        except Exception:  # noqa: BLE001
            return []
        recent = entries[-limit:]
        out: List[Dict[str, Any]] = []
        for e in recent:
            kind = getattr(e, "kind", "")
            payload = getattr(e, "payload", {}) or {}
            t_e = getattr(e, "t", "?")
            if kind == "speak_to_local":
                summary = f"speak_to_local: {payload.get('content', '')}"
            elif kind == "send_message":
                tgt = self._label(payload.get("target"))
                summary = f"send_message → {tgt}: {payload.get('content', '')}"
            elif kind == "send_to_group":
                gid = payload.get("group_id")
                summary = f"send_to_group(g{gid}): {payload.get('content', '')}"
            elif kind == "request_move":
                summary = f"request_move → {payload.get('place_id')}"
            elif kind == "update_state":
                summary = f"update_state: {payload.get('new_state', '')}"
            elif kind == "update_short_term_goal":
                summary = (
                    f"update_short_term_goal: {payload.get('new_goal', '')}"
                )
            elif kind == "do_nothing":
                summary = "do_nothing"
            else:
                summary = f"{kind} {payload}"
            out.append({"t": t_e, "summary": summary[:200]})
        return out
