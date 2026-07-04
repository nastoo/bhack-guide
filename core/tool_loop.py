"""Bounded tool-calling loop for the voice agent."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]


@dataclass
class Result:
    final_text: str
    steps: int = 0
    history: list = field(default_factory=list)


def _openai_tools(tools: list[Tool]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def run(
    goal: str,
    tools: list[Tool],
    *,
    client,
    model: str,
    system: str = "",
    max_steps: int = 4,
    keep_last: int = 3,
) -> Result:
    by_name = {tool.name: tool for tool in tools}
    extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
    base: list[dict] = []
    if system:
        base.append({"role": "system", "content": system})
    base.append({"role": "user", "content": goal})

    tool_msgs: list[dict] = []
    kwargs: dict = {"model": model, "extra_body": extra_body, "tools": _openai_tools(tools)}

    steps = 0
    while steps < max_steps:
        resp = client.chat.completions.create(messages=base + tool_msgs[-(keep_last * 2) :], **kwargs)
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            return Result(final_text=(msg.content or "").strip(), steps=steps, history=tool_msgs)

        steps += 1
        call = calls[0]
        name = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            tool_msgs.append({"role": "assistant", "content": f"(invalid tool call {name})"})
            tool_msgs.append({"role": "user", "content": "Answer the user directly in one short sentence."})
            continue

        tool = by_name.get(name)
        if tool is None:
            observation = f"Unknown tool: {name}"
        else:
            try:
                observation = str(tool.fn(**args))
            except Exception as exc:
                observation = f"Tool {name} failed: {exc}"

        tool_msgs.append({"role": "assistant", "content": f"Called {name}({args})"})
        tool_msgs.append({"role": "user", "content": f"Result: {observation}"})

    final = client.chat.completions.create(
        messages=base + tool_msgs[-(keep_last * 2) :] + [{"role": "user", "content": "Reply briefly to the user."}],
        model=model,
        extra_body=extra_body,
    )
    return Result(final_text=(final.choices[0].message.content or "").strip(), steps=steps, history=tool_msgs)
