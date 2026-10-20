"""Load and validate practice_rules.yaml.

Pydantic validates the shape (types, required fields) and a model validator
checks references between sections, so a typo such as `office: OFFICE-C`
fails loudly at startup instead of silently making a provider unbookable.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

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
