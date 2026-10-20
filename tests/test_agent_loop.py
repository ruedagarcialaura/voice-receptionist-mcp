"""The agent loop with a scripted fake LLM: no API key, no network, deterministic."""

import json
from types import SimpleNamespace

import pytest
from mcp import Client

from agent.core import ReceptionistAgent
from ehr_server.server import create_server

pytestmark = pytest.mark.anyio


def tool_call(call_id, name, args):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class FakeLLM:
    """Mimics AsyncOpenAI().chat.completions.create, returning scripted responses in order."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.script.pop(0)


async def test_agent_runs_tools_and_feeds_errors_back(ehr):
    llm = FakeLLM([
        reply(tool_calls=[tool_call("c1", "find_patient", {"first_name": "James", "last_name": "Carter", "date_of_birth": "1972-11-02"})]),
        reply(tool_calls=[tool_call("c2", "book_appointment", {"patient_id": "P-1002", "provider_id": "DR-JOHNSON",
                                                             "start": "2026-10-27T14:00", "appointment_type_id": "skin_check"})]),
        reply(content="Dr. Johnson only sees patients on Fridays at 2 p.m. Would Friday, October 30th work?"),
    ])
    events = []
    async with Client(create_server(ehr)) as mcp:
        agent = ReceptionistAgent(mcp, llm, "fake-model", on_event=lambda k, d: events.append((k, d)))
        await agent.start()
        text, stats = await agent.respond("I'm James Carter, born Nov 2 1972, I want Dr. Johnson next Tuesday at 2.")

    assert "Fridays" in text
    assert stats.tool_calls == ["find_patient", "book_appointment"]
    # The system prompt was built from the MCP prompt + directory resource.
    system = llm.requests[0]["messages"][0]["content"]
    assert "Lakeside Family Clinic" in system and "DR-JOHNSON" in system
    # The structured rule error reached the LLM as a tool message.
    last_tool_msg = [m for m in llm.requests[2]["messages"] if m["role"] == "tool"][-1]
    assert json.loads(last_tool_msg["content"])["error"]["code"] == "PROVIDER_NOT_WORKING"
    assert len(llm.requests[0]["tools"]) == 7


async def test_agent_survives_malformed_tool_arguments(ehr):
    bad = SimpleNamespace(id="c1", function=SimpleNamespace(name="find_patient", arguments="{not json"))
    llm = FakeLLM([reply(tool_calls=[bad]), reply(content="Could you repeat your date of birth?")])
    async with Client(create_server(ehr)) as mcp:
        agent = ReceptionistAgent(mcp, llm, "fake-model")
        await agent.start()
        text, _ = await agent.respond("hi")
    assert text.startswith("Could you")
    tool_msg = [m for m in llm.requests[1]["messages"] if m["role"] == "tool"][0]
    assert json.loads(tool_msg["content"])["error"]["code"] == "INVALID_ARGUMENTS"
