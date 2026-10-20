"""End-to-end through the real MCP protocol, using an in-process client."""

import json

import pytest
from mcp import Client

from ehr_server.server import create_server

pytestmark = pytest.mark.anyio


async def call(client, tool, args):
    result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


async def test_lists_all_tools_with_annotations(ehr):
    async with Client(create_server(ehr)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "find_patient", "search_slots", "book_appointment", "reschedule_appointment",
        "cancel_appointment", "verify_insurance", "escalate_to_human",
    }
    assert tools["search_slots"].annotations.read_only_hint is True
    assert tools["cancel_appointment"].annotations.destructive_hint is True
    assert "YYYY-MM-DDTHH:MM" in json.dumps(tools["book_appointment"].input_schema)


async def test_booking_flow_over_mcp(ehr):
    async with Client(create_server(ehr)) as client:
        patient = await call(client, "find_patient", {"first_name": "James", "last_name": "Carter", "date_of_birth": "1972-11-02"})
        pid = patient["patient"]["patient_id"]
        rejected = await call(client, "book_appointment", {
            "patient_id": pid, "provider_id": "DR-JOHNSON", "start": "2026-10-27T14:00", "appointment_type_id": "skin_check"})
        assert rejected["error"]["code"] == "PROVIDER_NOT_WORKING"
        alt = rejected["alternatives"][0]
        booked = await call(client, "book_appointment", {
            "patient_id": pid, "provider_id": alt["provider_id"], "office_id": alt["office_id"],
            "start": alt["start"], "appointment_type_id": "skin_check"})
        assert booked["ok"]


async def test_schema_validation_rejects_bad_enum(ehr):
    async with Client(create_server(ehr)) as client:
        result = await client.call_tool("escalate_to_human", {"reason": "bored", "summary": "x"})
    assert result.is_error


async def test_resources_and_prompt(ehr):
    async with Client(create_server(ehr)) as client:
        directory = json.loads((await client.read_resource("practice://directory")).contents[0].text)
        assert directory["current_time"] == "Monday 2026-10-19 09:00"
        assert directory["calendar"][1] == "Tuesday 2026-10-20"
        assert {p["provider_id"] for p in directory["providers"]} == {"DR-JOHNSON", "DR-PATEL", "DR-MORALES", "DR-OKAFOR"}

        rules = (await client.read_resource("practice://rules")).contents[0].text
        assert "fri 14:00-14:30 at OFFICE-A" in rules

        await call(client, "cancel_appointment", {"appointment_id": "APT-0002"})
        recent = json.loads((await client.read_resource("audit://recent")).contents[0].text)
        assert recent[0]["tool"] == "cancel_appointment"
        history = json.loads((await client.read_resource("audit://appointments/APT-0002")).contents[0].text)
        assert [e["tool"] for e in history] == ["cancel_appointment"]

        prompt = await client.get_prompt("receptionist_call")
        assert "Lakeside Family Clinic" in prompt.messages[0].content.text
