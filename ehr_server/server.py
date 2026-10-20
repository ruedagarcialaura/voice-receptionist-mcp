"""MCP server for the fake EHR.

A thin adapter: each MCP tool validates its arguments (via type hints) and calls
the matching EHR method. The three MCP building blocks are all used:

- Tools     (model-controlled): actions the LLM decides to call.
- Resources (application-controlled): read-only context the client app chooses to load.
- Prompts   (user-controlled): reusable templates; here, the receptionist call script.

Run:  python -m ehr_server.server        (stdio transport)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from .ehr import EHR, EscalationReason, PartOfDay

PROMPT_PATH = Path(__file__).with_name("receptionist_prompt.md")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

DateStr = Annotated[str, Field(description="Date as YYYY-MM-DD")]
DateTimeStr = Annotated[str, Field(description="Practice-local date and time as YYYY-MM-DDTHH:MM, e.g. 2026-10-30T14:00")]


def create_server(ehr: EHR | None = None) -> MCPServer:
    ehr = ehr or EHR.from_env()
    mcp = MCPServer(
        name="fake-ehr",
        instructions=f"Scheduling system for {ehr.cfg.practice.name} (synthetic data). "
        "Read practice://directory for providers, offices and visit types.",
    )

    # ------------------------------------------------------------------ tools

    @mcp.tool(annotations=READ_ONLY)
    def find_patient(first_name: str, last_name: str, date_of_birth: DateStr) -> dict[str, Any]:
        """Verify a caller's identity and look up their record. All three fields must match.
        Returns patient_id, insurance payer and upcoming appointments (with appointment ids)."""
        return ehr.find_patient(first_name=first_name, last_name=last_name, date_of_birth=date_of_birth)

    @mcp.tool(annotations=READ_ONLY)
    def search_slots(
        appointment_type_id: Annotated[str, Field(description="Visit type id, e.g. sick_visit")],
        patient_id: Annotated[str | None, Field(description="Pass it when known so results respect the patient's insurance, age and new-patient status")] = None,
        provider_id: Annotated[str | None, Field(description="Restrict to one provider")] = None,
        office_id: Annotated[str | None, Field(description="Restrict to one office")] = None,
        date_from: Annotated[str | None, Field(description="First day to search, YYYY-MM-DD (default today)")] = None,
        date_to: Annotated[str | None, Field(description="Last day to search, YYYY-MM-DD (default 14 days after date_from)")] = None,
        part_of_day: PartOfDay = "any",
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> dict[str, Any]:
        """Find bookable appointment times. Every slot returned already satisfies all practice rules.
        excluded_providers explains which providers were skipped and why."""
        return ehr.search_slots(
            appointment_type_id=appointment_type_id, patient_id=patient_id, provider_id=provider_id,
            office_id=office_id, date_from=date_from, date_to=date_to, part_of_day=part_of_day, limit=limit,
        )

    @mcp.tool(annotations=WRITE)
    def book_appointment(
        patient_id: str,
        provider_id: str,
        start: DateTimeStr,
        appointment_type_id: str,
        office_id: Annotated[str | None, Field(description="Optional if the provider works at only one office that day")] = None,
    ) -> dict[str, Any]:
        """Book an appointment after the caller confirmed the details. The server checks every rule;
        on failure it returns error.code, error.how_to_fix and valid alternatives."""
        return ehr.book_appointment(
            patient_id=patient_id, provider_id=provider_id, start=start,
            appointment_type_id=appointment_type_id, office_id=office_id,
        )

    @mcp.tool(annotations=WRITE)
    def reschedule_appointment(
        appointment_id: str,
        new_start: DateTimeStr,
        new_provider_id: Annotated[str | None, Field(description="Only if changing provider")] = None,
        new_office_id: Annotated[str | None, Field(description="Only if changing office")] = None,
    ) -> dict[str, Any]:
        """Move an existing appointment to a new time (atomically: the old slot is freed only if the new one is booked)."""
        return ehr.reschedule_appointment(
            appointment_id=appointment_id, new_start=new_start,
            new_provider_id=new_provider_id, new_office_id=new_office_id,
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
    def cancel_appointment(appointment_id: str, reason: str = "caller_request") -> dict[str, Any]:
        """Cancel an upcoming appointment after the caller confirmed."""
        return ehr.cancel_appointment(appointment_id=appointment_id, reason=reason)

    @mcp.tool(annotations=READ_ONLY)
    def verify_insurance(patient_id: str, provider_id: str | None = None) -> dict[str, Any]:
        """Check the patient's insurance eligibility (simulated) and which providers are in network."""
        return ehr.verify_insurance(patient_id=patient_id, provider_id=provider_id)

    @mcp.tool(annotations=WRITE)
    def escalate_to_human(
        reason: EscalationReason,
        summary: Annotated[str, Field(description="One or two sentences for the staff member: who called and what they need")],
        patient_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a callback ticket for front-desk staff. Use when the caller asks for a person,
        has a clinical question, or there is a problem you cannot solve."""
        return ehr.escalate_to_human(reason=reason, summary=summary, patient_id=patient_id)

    # ------------------------------------------------------------------ resources

    @mcp.resource("practice://directory", mime_type="application/json",
                  description="Offices, providers (with schedules), visit types and the current practice time.")
    def directory() -> str:
        return json.dumps(ehr.directory(), indent=2)

    @mcp.resource("practice://rules", mime_type="text/markdown",
                  description="Human-readable booking protocol, generated from the same config the server enforces.")
    def rules() -> str:
        return ehr.rules_markdown()

    @mcp.resource("audit://recent", mime_type="application/json",
                  description="The 50 most recent tool calls, including rejected ones.")
    def audit_recent() -> str:
        return json.dumps(ehr.audit.recent(50), indent=2)

    @mcp.resource("audit://appointments/{appointment_id}", mime_type="application/json",
                  description="Audit history of one appointment.")
    def audit_appointment(appointment_id: str) -> str:
        return json.dumps(ehr.audit.for_appointment(appointment_id), indent=2)

    # ------------------------------------------------------------------ prompt

    @mcp.prompt(description="System instructions for the receptionist agent.")
    def receptionist_call() -> str:
        return PROMPT_PATH.read_text(encoding="utf-8").format(practice_name=ehr.cfg.practice.name)

    return mcp


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)  # logs go to stderr; stdout carries MCP messages
    create_server().run()
