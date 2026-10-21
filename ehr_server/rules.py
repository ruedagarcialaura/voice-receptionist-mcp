"""Everything that decides what is allowed: the practice config, the error codes and the rule checks.

1. Config: practice_rules.yaml is validated by Pydantic, including references between sections, so a
   typo such as `office: OFFICE-C` fails at startup instead of silently making a provider unbookable.
2. Errors: every tool returns {"ok": true, ...} or {"ok": false, "error": {code, message, how_to_fix,
   details}, "alternatives": [...]}, so the agent can branch on a stable code and recover.
3. Rules: pure functions (no database, no clock, no LLM). The same `check_booking` is used by search,
   booking and rescheduling, so search can never offer a slot that booking would then reject.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, model_validator

# ---------------------------------------------------------------------------------------------- config

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: list[Weekday] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Practice(BaseModel):
    name: str
    timezone: str
    slot_minutes: int
    min_notice_hours: int
    max_days_ahead: int
    require_active_coverage: bool = True
    escalation_callback: str


class Office(BaseModel):
    id: str
    name: str
    address: str


class AppointmentType(BaseModel):
    id: str
    name: str
    duration_minutes: int


class Window(BaseModel):
    """A recurring block of time when a provider sees patients at one office."""

    office: str
    days: list[Weekday]
    start: time
    end: time


class AgeRange(BaseModel):
    min: int | None = None
    max: int | None = None


class Provider(BaseModel):
    id: str
    name: str
    specialty: str
    appointment_types: list[str]
    accepts_new_patients: bool = True
    accepted_payers: list[str]
    availability: list[Window]
    blocked_dates: list[date] = []
    patient_age: AgeRange | None = None
    note: str = ""

    def offices(self) -> list[str]:
        return sorted({w.office for w in self.availability})

    def schedule_summary(self) -> str:
        """Human/LLM-readable schedule, e.g. 'fri 14:00-14:30 at OFFICE-A'."""
        return "; ".join(
            f"{','.join(w.days)} {w.start:%H:%M}-{w.end:%H:%M} at {w.office}" for w in self.availability
        )


class PracticeConfig(BaseModel):
    practice: Practice
    offices: list[Office]
    appointment_types: list[AppointmentType]
    providers: list[Provider]

    @model_validator(mode="after")
    def _check_references(self) -> PracticeConfig:
        office_ids = {o.id for o in self.offices}
        type_ids = {t.id for t in self.appointment_types}
        for p in self.providers:
            for t in p.appointment_types:
                if t not in type_ids:
                    raise ValueError(f"{p.id}: unknown appointment type {t!r}")
            for w in p.availability:
                if w.office not in office_ids:
                    raise ValueError(f"{p.id}: unknown office {w.office!r}")
                if w.start >= w.end:
                    raise ValueError(f"{p.id}: window start must be before end")
        return self

    def provider(self, provider_id: str) -> Provider | None:
        return next((p for p in self.providers if p.id == provider_id), None)

    def office(self, office_id: str) -> Office | None:
        return next((o for o in self.offices if o.id == office_id), None)

    def appointment_type(self, type_id: str) -> AppointmentType | None:
        return next((t for t in self.appointment_types if t.id == type_id), None)


def load_config(path: str | Path) -> PracticeConfig:
    with open(path, encoding="utf-8") as f:
        return PracticeConfig.model_validate(yaml.safe_load(f))


# ---------------------------------------------------------------------------------------------- errors

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
        "error": {"code": code.value, "message": message, "how_to_fix": HOW_TO_FIX[code], "details": details or {}},
    }
    if alternatives is not None:
        result["alternatives"] = alternatives
    return result


# ---------------------------------------------------------------------------------------------- rules

SELF_PAY = "self_pay"


@dataclass(frozen=True)
class Patient:
    id: str
    first_name: str
    last_name: str
    date_of_birth: date
    insurance_payer: str
    coverage_active: bool
    is_new_patient: bool


@dataclass(frozen=True)
class BusyBlock:
    """An existing booked appointment, as far as the rules care."""

    appointment_id: str
    provider_id: str
    patient_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Violation:
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)


# Violations where a different time with the same provider will not help.
PROVIDER_FIT_CODES = {
    ErrorCode.TYPE_NOT_OFFERED_BY_PROVIDER,
    ErrorCode.NOT_ACCEPTING_NEW_PATIENTS,
    ErrorCode.AGE_RESTRICTION,
    ErrorCode.OUT_OF_NETWORK,
}


def age_on(dob: date, day: date) -> int:
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


def check_provider_fit(
    cfg: PracticeConfig, provider: Provider, appt_type: AppointmentType, patient: Patient | None, on_day: date
) -> list[Violation]:
    """Rules about WHO can see WHOM, independent of the time slot."""
    v: list[Violation] = []
    if appt_type.id not in provider.appointment_types:
        v.append(Violation(
            ErrorCode.TYPE_NOT_OFFERED_BY_PROVIDER,
            f"{provider.name} does not offer '{appt_type.name}'.",
            {"provider_id": provider.id, "offers": provider.appointment_types},
        ))
    if patient is None:
        return v

    if patient.is_new_patient and not provider.accepts_new_patients:
        v.append(Violation(
            ErrorCode.NOT_ACCEPTING_NEW_PATIENTS, f"{provider.name} is not accepting new patients.",
            {"provider_id": provider.id},
        ))
    if provider.patient_age:
        age = age_on(patient.date_of_birth, on_day)
        lo, hi = provider.patient_age.min, provider.patient_age.max
        if (lo is not None and age < lo) or (hi is not None and age > hi):
            v.append(Violation(
                ErrorCode.AGE_RESTRICTION,
                f"{provider.name} only sees patients aged {lo or 0}-{hi if hi is not None else 'any'}; patient is {age}.",
                {"provider_id": provider.id, "patient_age": age, "allowed": provider.patient_age.model_dump()},
            ))
    if patient.insurance_payer != SELF_PAY:
        if cfg.practice.require_active_coverage and not patient.coverage_active:
            v.append(Violation(
                ErrorCode.COVERAGE_INACTIVE, f"The patient's {patient.insurance_payer} coverage is inactive.",
                {"payer": patient.insurance_payer},
            ))
        if patient.insurance_payer not in provider.accepted_payers:
            v.append(Violation(
                ErrorCode.OUT_OF_NETWORK, f"{provider.name} does not accept {patient.insurance_payer}.",
                {"provider_id": provider.id, "payer": patient.insurance_payer},
            ))
    return v


def check_time(
    cfg: PracticeConfig, provider: Provider, office_id: str, start: datetime, appt_type: AppointmentType, now: datetime
) -> list[Violation]:
    """Rules about WHEN and WHERE: notice period, horizon, schedule, office."""
    end = start + timedelta(minutes=appt_type.duration_minutes)
    p = cfg.practice

    if start < now:
        return [Violation(ErrorCode.IN_THE_PAST, f"{start:%Y-%m-%d %H:%M} is in the past.")]
    if start < now + timedelta(hours=p.min_notice_hours):
        return [Violation(
            ErrorCode.TOO_SOON, f"Appointments need at least {p.min_notice_hours} hours notice.",
            {"min_notice_hours": p.min_notice_hours},
        )]
    if start.date() > now.date() + timedelta(days=p.max_days_ahead):
        return [Violation(
            ErrorCode.TOO_FAR_AHEAD, f"Appointments can be booked at most {p.max_days_ahead} days ahead.",
            {"max_days_ahead": p.max_days_ahead},
        )]
    if start.date() in provider.blocked_dates:
        return [Violation(
            ErrorCode.PROVIDER_ON_LEAVE, f"{provider.name} is out of office on {start:%Y-%m-%d}.",
            {"provider_id": provider.id, "date": start.date().isoformat()},
        )]
    if office_id not in provider.offices():
        return [Violation(
            ErrorCode.PROVIDER_NOT_AT_OFFICE, f"{provider.name} does not see patients at {office_id}.",
            {"provider_id": provider.id, "works_at": provider.offices(), "schedule": provider.schedule_summary()},
        )]

    weekday = WEEKDAYS[start.weekday()]
    windows = [w for w in provider.availability if w.office == office_id and weekday in w.days]
    fitting = [w for w in windows if w.start <= start.time() and end.time() <= w.end and end.date() == start.date()]
    if not fitting:
        return [Violation(
            ErrorCode.PROVIDER_NOT_WORKING,
            f"{provider.name} does not see patients on {start:%A} at {start:%H:%M} at {office_id}.",
            {"provider_id": provider.id, "schedule": provider.schedule_summary()},
        )]
    window = fitting[0]
    minutes_in = (start.hour * 60 + start.minute) - (window.start.hour * 60 + window.start.minute)
    if minutes_in % p.slot_minutes != 0:
        return [Violation(
            ErrorCode.INVALID_START_TIME, f"Appointments start every {p.slot_minutes} minutes from {window.start:%H:%M}.",
            {"slot_minutes": p.slot_minutes},
        )]
    return []


def check_conflicts(
    provider_id: str, patient_id: str | None, start: datetime, end: datetime,
    busy: list[BusyBlock], ignore_appointment_id: str | None = None,
) -> list[Violation]:
    """No overlapping bookings for the provider or for the patient."""
    v: list[Violation] = []
    for b in busy:
        if b.appointment_id == ignore_appointment_id or not (b.start < end and start < b.end):
            continue
        if b.provider_id == provider_id:
            v.append(Violation(ErrorCode.SLOT_TAKEN, "That time is already booked.", {"start": start.isoformat()}))
        elif patient_id is not None and b.patient_id == patient_id:
            v.append(Violation(
                ErrorCode.PATIENT_DOUBLE_BOOKED, "The patient already has an appointment at that time.",
                {"existing_appointment_id": b.appointment_id},
            ))
    return v


def check_booking(
    cfg: PracticeConfig, *, provider: Provider, office_id: str, start: datetime, appt_type: AppointmentType,
    patient: Patient | None, now: datetime, busy: list[BusyBlock], ignore_appointment_id: str | None = None,
) -> list[Violation]:
    """All rules, in the order the caller should hear about them."""
    violations = check_provider_fit(cfg, provider, appt_type, patient, start.date())
    violations += check_time(cfg, provider, office_id, start, appt_type, now)
    if not violations:
        end = start + timedelta(minutes=appt_type.duration_minutes)
        violations += check_conflicts(provider.id, patient.id if patient else None, start, end, busy, ignore_appointment_id)
    return violations


def candidate_starts(
    cfg: PracticeConfig, provider: Provider, day: date, appt_type: AppointmentType, office_id: str | None = None
) -> list[tuple[str, datetime]]:
    """Every grid-aligned start time in the provider's windows on `day` (before rule checks)."""
    weekday = WEEKDAYS[day.weekday()]
    step = timedelta(minutes=cfg.practice.slot_minutes)
    duration = timedelta(minutes=appt_type.duration_minutes)
    out: list[tuple[str, datetime]] = []
    for w in provider.availability:
        if weekday not in w.days or (office_id and w.office != office_id):
            continue
        t = datetime.combine(day, w.start)
        window_end = datetime.combine(day, w.end)
        while t + duration <= window_end:
            out.append((w.office, t))
            t += step
    return out
