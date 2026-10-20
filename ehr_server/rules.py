"""Deterministic booking rules.

Pure functions: no database, no clock, no LLM. Everything they need (config,
patient, "now", existing bookings) is passed in, so each rule can be unit-tested
in isolation and gives the same answer every time.

The same `check_booking` function is used by `book_appointment`,
`reschedule_appointment` AND `search_slots`, so search can never offer a slot
that booking would then reject.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .config import WEEKDAYS, AppointmentType, PracticeConfig, Provider
from .errors import ErrorCode

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
    cfg: PracticeConfig,
    provider: Provider,
    appt_type: AppointmentType,
    patient: Patient | None,
    on_day: date,
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
            ErrorCode.NOT_ACCEPTING_NEW_PATIENTS,
            f"{provider.name} is not accepting new patients.",
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
                ErrorCode.COVERAGE_INACTIVE,
                f"The patient's {patient.insurance_payer} coverage is inactive.",
                {"payer": patient.insurance_payer},
            ))
        if patient.insurance_payer not in provider.accepted_payers:
            v.append(Violation(
                ErrorCode.OUT_OF_NETWORK,
                f"{provider.name} does not accept {patient.insurance_payer}.",
                {"provider_id": provider.id, "payer": patient.insurance_payer},
            ))
    return v


def check_time(
    cfg: PracticeConfig,
    provider: Provider,
    office_id: str,
    start: datetime,
    appt_type: AppointmentType,
    now: datetime,
) -> list[Violation]:
    """Rules about WHEN and WHERE: notice period, horizon, schedule, office."""
    end = start + timedelta(minutes=appt_type.duration_minutes)
    p = cfg.practice

    if start < now:
        return [Violation(ErrorCode.IN_THE_PAST, f"{start:%Y-%m-%d %H:%M} is in the past.")]
    if start < now + timedelta(hours=p.min_notice_hours):
        return [Violation(
            ErrorCode.TOO_SOON,
            f"Appointments need at least {p.min_notice_hours} hours notice.",
            {"min_notice_hours": p.min_notice_hours},
        )]
    if start.date() > now.date() + timedelta(days=p.max_days_ahead):
        return [Violation(
            ErrorCode.TOO_FAR_AHEAD,
            f"Appointments can be booked at most {p.max_days_ahead} days ahead.",
            {"max_days_ahead": p.max_days_ahead},
        )]
    if start.date() in provider.blocked_dates:
        return [Violation(
            ErrorCode.PROVIDER_ON_LEAVE,
            f"{provider.name} is out of office on {start:%Y-%m-%d}.",
            {"provider_id": provider.id, "date": start.date().isoformat()},
        )]
    if office_id not in provider.offices():
        return [Violation(
            ErrorCode.PROVIDER_NOT_AT_OFFICE,
            f"{provider.name} does not see patients at {office_id}.",
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
            ErrorCode.INVALID_START_TIME,
            f"Appointments start every {p.slot_minutes} minutes from {window.start:%H:%M}.",
            {"slot_minutes": p.slot_minutes},
        )]
    return []


def check_conflicts(
    provider_id: str,
    patient_id: str | None,
    start: datetime,
    end: datetime,
    busy: list[BusyBlock],
    ignore_appointment_id: str | None = None,
) -> list[Violation]:
    """No overlapping bookings for the provider or for the patient."""
    v: list[Violation] = []
    for b in busy:
        if b.appointment_id == ignore_appointment_id:
            continue
        overlaps = b.start < end and start < b.end
        if not overlaps:
            continue
        if b.provider_id == provider_id:
            v.append(Violation(ErrorCode.SLOT_TAKEN, "That time is already booked.", {"start": start.isoformat()}))
        elif patient_id is not None and b.patient_id == patient_id:
            v.append(Violation(
                ErrorCode.PATIENT_DOUBLE_BOOKED,
                "The patient already has an appointment at that time.",
                {"existing_appointment_id": b.appointment_id},
            ))
    return v


def check_booking(
    cfg: PracticeConfig,
    *,
    provider: Provider,
    office_id: str,
    start: datetime,
    appt_type: AppointmentType,
    patient: Patient | None,
    now: datetime,
    busy: list[BusyBlock],
    ignore_appointment_id: str | None = None,
) -> list[Violation]:
    """All rules, in the order the caller should hear about them."""
    violations = check_provider_fit(cfg, provider, appt_type, patient, start.date())
    violations += check_time(cfg, provider, office_id, start, appt_type, now)
    if not violations:
        end = start + timedelta(minutes=appt_type.duration_minutes)
        violations += check_conflicts(
            provider.id, patient.id if patient else None, start, end, busy, ignore_appointment_id
        )
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
