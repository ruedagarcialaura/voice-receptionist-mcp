"""Structured results shared by every tool.

Every tool returns either
    {"ok": true, ...data}
or
    {"ok": false, "error": {"code", "message", "how_to_fix", "details"}, "alternatives": [...]}

A stable machine-readable `code` lets the agent (and the evals) branch on the
failure; `how_to_fix` tells the LLM what to do next instead of letting it guess;
`alternatives` gives it valid options so it can recover in the same turn.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    PATIENT_NOT_FOUND = "PATIENT_NOT_FOUND"
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    OFFICE_NOT_FOUND = "OFFICE_NOT_FOUND"
    APPOINTMENT_TYPE_NOT_FOUND = "APPOINTMENT_TYPE_NOT_FOUND"
    APPOINTMENT_NOT_FOUND = "APPOINTMENT_NOT_FOUND"
    APPOINTMENT_NOT_ACTIVE = "APPOINTMENT_NOT_ACTIVE"
    # Provider/patient fit
    TYPE_NOT_OFFERED_BY_PROVIDER = "TYPE_NOT_OFFERED_BY_PROVIDER"
    NOT_ACCEPTING_NEW_PATIENTS = "NOT_ACCEPTING_NEW_PATIENTS"
    AGE_RESTRICTION = "AGE_RESTRICTION"
    OUT_OF_NETWORK = "OUT_OF_NETWORK"
    COVERAGE_INACTIVE = "COVERAGE_INACTIVE"
    # Time
    IN_THE_PAST = "IN_THE_PAST"
    TOO_SOON = "TOO_SOON"
    TOO_FAR_AHEAD = "TOO_FAR_AHEAD"
    PROVIDER_ON_LEAVE = "PROVIDER_ON_LEAVE"
    PROVIDER_NOT_AT_OFFICE = "PROVIDER_NOT_AT_OFFICE"
    PROVIDER_NOT_WORKING = "PROVIDER_NOT_WORKING"
    INVALID_START_TIME = "INVALID_START_TIME"
    # Conflicts
    SLOT_TAKEN = "SLOT_TAKEN"
    PATIENT_DOUBLE_BOOKED = "PATIENT_DOUBLE_BOOKED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


HOW_TO_FIX: dict[ErrorCode, str] = {
    ErrorCode.INVALID_INPUT: "Fix the argument format (dates YYYY-MM-DD, times YYYY-MM-DDTHH:MM) and retry.",
    ErrorCode.PATIENT_NOT_FOUND: "Ask the caller to repeat their full name and date of birth. Do not reveal whether the name exists. After two failures, offer escalate_to_human.",
    ErrorCode.PROVIDER_NOT_FOUND: "Use a provider_id from the practice directory.",
    ErrorCode.OFFICE_NOT_FOUND: "Use an office_id from the practice directory.",
    ErrorCode.APPOINTMENT_TYPE_NOT_FOUND: "Use an appointment_type_id from the practice directory.",
    ErrorCode.APPOINTMENT_NOT_FOUND: "Call find_patient to get the caller's upcoming appointment ids.",
    ErrorCode.APPOINTMENT_NOT_ACTIVE: "This appointment is cancelled, already rescheduled or in the past. Offer to book a new one.",
    ErrorCode.TYPE_NOT_OFFERED_BY_PROVIDER: "Offer one of the alternatives (other providers who offer this visit type).",
    ErrorCode.NOT_ACCEPTING_NEW_PATIENTS: "Offer one of the alternatives (providers accepting new patients).",
    ErrorCode.AGE_RESTRICTION: "Offer one of the alternatives (providers who see patients of this age).",
    ErrorCode.OUT_OF_NETWORK: "Tell the caller this provider is out of network for their plan and offer the in-network alternatives.",
    ErrorCode.COVERAGE_INACTIVE: "Tell the caller their coverage shows as inactive. Offer escalate_to_human so staff can update it. Do not book.",
    ErrorCode.IN_THE_PAST: "Ask for a future date and time, or offer the alternatives.",
    ErrorCode.TOO_SOON: "This is inside the minimum notice period. Offer the alternatives.",
    ErrorCode.TOO_FAR_AHEAD: "This is beyond the booking horizon. Offer an earlier date.",
    ErrorCode.PROVIDER_ON_LEAVE: "The provider is out that day. Offer the alternatives.",
    ErrorCode.PROVIDER_NOT_AT_OFFICE: "The provider does not work at that office. Offer the alternatives at the correct office.",
    ErrorCode.PROVIDER_NOT_WORKING: "The provider does not see patients at that day/time. Offer the alternatives.",
    ErrorCode.INVALID_START_TIME: "Appointments start on the practice's time grid. Offer the alternatives.",
    ErrorCode.SLOT_TAKEN: "That time was just taken. Offer the alternatives.",
    ErrorCode.PATIENT_DOUBLE_BOOKED: "The patient already has an appointment at that time. Offer a different time or a reschedule.",
    ErrorCode.INTERNAL_ERROR: "Apologize and use escalate_to_human.",
}


def ok(**data: Any) -> dict[str, Any]:
    return {"ok": True, **data}


def error(
    code: ErrorCode,
    message: str,
    details: dict[str, Any] | None = None,
    alternatives: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code.value,
            "message": message,
            "how_to_fix": HOW_TO_FIX[code],
            "details": details or {},
        },
    }
    if alternatives is not None:
        result["alternatives"] = alternatives
    return result
