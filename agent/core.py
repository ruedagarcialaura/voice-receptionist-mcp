"""The agent loop, independent of text or voice.

    user text -> LLM -> (tool calls -> MCP server -> results -> LLM)* -> reply text

The LLM is reached through the OpenAI-compatible chat API, so the same code
works with Groq, Ollama, or any other compatible provider by changing .env.
The text and voice front-ends both call `ReceptionistAgent.respond()`.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import openai
from mcp import Client
from openai import AsyncOpenAI

MAX_TOOL_ROUNDS = 8  # safety stop so a confused model cannot loop forever


@dataclass
class TurnStats:
    """Where the time went in one caller turn (used for the latency report)."""

    llm_seconds: list[float] = field(default_factory=list)
    tool_seconds: list[float] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(self.llm_seconds) + sum(self.tool_seconds)


EventHandler = Callable[[str, dict[str, Any]], None]


def mcp_tool_to_openai(tool: Any) -> dict[str, Any]:
    """MCP describes tools with JSON Schema; OpenAI-style APIs want the same schema in a wrapper."""
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description or "", "parameters": tool.input_schema},
    }


def llm_from_env() -> tuple[AsyncOpenAI, str]:
    client = AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ.get("LLM_API_KEY", "none"))
    return client, os.environ["LLM_MODEL"]


async def build_instructions(mcp: Client) -> str:
    """System prompt = the server's `receptionist_call` prompt + the `practice://directory` resource.
    Shared by the text and voice agents, so both follow the same script."""
    prompt = await mcp.get_prompt("receptionist_call")
    instructions = "\n".join(m.content.text for m in prompt.messages if m.content.type == "text")
    directory = (await mcp.read_resource("practice://directory")).contents[0].text
    return f"{instructions}\n\nPractice directory (JSON, includes the current date and a calendar):\n{directory}"


def compact_json(text: str) -> str:
    """Re-serialize JSON without indentation: the tool results stay in the conversation, so every
    byte is re-sent on each later request (and counts against tokens-per-minute limits)."""
    try:
        return json.dumps(json.loads(text), separators=(",", ":"), ensure_ascii=False)
    except json.JSONDecodeError:
        return text


class ReceptionistAgent:
    def __init__(self, mcp: Client, llm: AsyncOpenAI, model: str, on_event: EventHandler | None = None):
        self.mcp = mcp
        self.llm = llm
        self.model = model
        self.on_event = on_event or (lambda kind, data: None)
        self.messages: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        # Reasoning models (e.g. gpt-oss) think before answering; "low" is faster and uses fewer tokens.
        effort = os.environ.get("LLM_REASONING_EFFORT")
        self.extra: dict[str, Any] = {"reasoning_effort": effort} if effort else {}

    async def start(self) -> None:
        """Discover tools and build the system prompt from the server's prompt + directory resource."""
        self.tools = [mcp_tool_to_openai(t) for t in (await self.mcp.list_tools()).tools]
        self.messages = [{"role": "system", "content": await build_instructions(self.mcp)}]

    def add_assistant_message(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})

    async def respond(self, user_text: str) -> tuple[str, TurnStats]:
        self.messages.append({"role": "user", "content": user_text})
        stats = TurnStats()

        for _ in range(MAX_TOOL_ROUNDS):
            t0 = time.perf_counter()
            try:
                completion = await self.llm.chat.completions.create(
                    model=self.model, messages=self.messages, tools=self.tools, temperature=0.2, **self.extra,
                )
            except openai.BadRequestError as exc:
                # Small/open models sometimes emit malformed tool calls; some providers reject them.
                self.on_event("llm_error", {"error": str(exc)})
                reply = "Sorry, I didn't catch that. Could you say it again?"
                self.add_assistant_message(reply)
                return reply, stats
            stats.llm_seconds.append(time.perf_counter() - t0)
            msg = completion.choices[0].message

            if not msg.tool_calls:
                reply = msg.content or ""
                self.add_assistant_message(reply)
                return reply, stats

            self.messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in msg.tool_calls
                ],
            })
            for call in msg.tool_calls:
                result_text = await self._run_tool(call.function.name, call.function.arguments, stats)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})

        reply = "I'm having trouble with that request. Let me have a staff member call you back."
        self.add_assistant_message(reply)
        return reply, stats

    async def _run_tool(self, name: str, raw_args: str, stats: TurnStats) -> str:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": "Arguments were not valid JSON."}})

        self.on_event("tool_call", {"name": name, "args": args})
        t0 = time.perf_counter()
        result = await self.mcp.call_tool(name, args)
        stats.tool_seconds.append(time.perf_counter() - t0)
        stats.tool_calls.append(name)

        text = compact_json("\n".join(c.text for c in result.content if c.type == "text"))
        self.on_event("tool_result", {"name": name, "text": text, "is_error": result.is_error})
        return text
