"""LLM-backed agent for HBM demo — extends DemoAgent with script/memory hooks."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_world.demo.demo_agent import (
    DemoAgent,
    TOOLS as DEMO_TOOLS,
    _Response,
    _ToolCall,
)

log = logging.getLogger(__name__)

RELATION_CHANGE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "relation_change",
        "description": (
            "修改你与另一 agent 之间的社交关系（商业盟友、上下级等）。"
            "create=建立关系，break=断绝关系。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "integer", "description": "目标 agent_id"},
                "relation_type": {
                    "type": "string",
                    "description": "关系类型，如 business_partner / ally",
                },
                "op": {
                    "type": "string",
                    "enum": ["create", "break"],
                    "description": "create=建立，break=断绝",
                },
            },
            "required": ["target", "relation_type", "op"],
            "additionalProperties": False,
        },
    },
}

HBM_TOOLS: List[Dict[str, Any]] = list(DEMO_TOOLS) + [RELATION_CHANGE_TOOL]


def _adapt_relation_change_args(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Map LLM tool args to ActionDispatcher kwargs."""
    out = dict(kwargs)
    if "target" in out:
        out["dst"] = out.pop("target")
    op = out.get("op")
    if op == "break":
        out["op"] = "remove"
    elif op == "create":
        out["op"] = "add"
    return out


@dataclass
class HbmAgent(DemoAgent):
    """HBM scenario agent with DialogueInjection memory + relation_change tool."""

    player_memory: List[Dict[str, str]] = field(default_factory=list)

    async def update_memory(
        self,
        content: Optional[str] = None,
        role: str = "system",
        message: Any = None,
        **kwargs: Any,
    ) -> None:
        """Append player/system dialogue injected by DialogueInjectionEffect."""
        if content is None and message is not None:
            content = getattr(message, "content", None) or str(message)
        if content is None:
            content = kwargs.get("text", "")
        self.player_memory.append(
            {"role": str(role), "content": str(content).strip()}
        )

    async def perform_action_by_llm(self, world: Any, t: int) -> _Response:
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
                tools=HBM_TOOLS,
                tool_choice="auto",
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("agent %s LLM call failed: %s", self.agent_id, exc)
            return _Response(info={"tool_calls": []})

        msg = resp.choices[0].message
        tool_calls: List[_ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            name = tc.function.name
            if name == "relation_change":
                args = _adapt_relation_change_args(args)
            tool_calls.append(_ToolCall(tool_name=name, args=args))

        if not tool_calls:
            content = (msg.content or "").strip()
            if content:
                log.info("agent %s thought: %s", self.agent_id, content[:200])
            tool_calls = [_ToolCall(tool_name="do_nothing", args={})]

        return _Response(info={"tool_calls": tool_calls})

    def _observation_to_text(self, obs: Any, t: int) -> str:
        prefix: List[str] = []

        if self.player_memory:
            prefix.append("# 玩家/系统注入的对话记忆（必须认真回应）：")
            for entry in self.player_memory:
                prefix.append(f"  - [{entry['role']}] {entry['content']}")

        scripted = getattr(obs, "scripted_notification", None)
        if scripted:
            prefix.append("# 剧本通知：")
            if isinstance(scripted, list):
                for item in scripted:
                    prefix.append(f"  - {item}")
            else:
                prefix.append(f"  - {scripted}")

        base = super()._observation_to_text(obs, t)
        if prefix:
            return "\n".join(prefix) + "\n\n" + base
        return base
