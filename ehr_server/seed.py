"""Synthetic seed data. No real patient data, ever.

Existing appointments are placed relative to "now" so the demo always has
something to find, and each one is validated against the same rules the
tools use, so the seed can never contain a booking the rules would refuse.

Usage:  python -m ehr_server.seed      (deletes and rebuilds the database)
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from .config import PracticeConfig
from .rules import BusyBlock, Patient, check_booking

# id, first, last, dob, phone, payer, member_id, coverage_active, is_new_patient
PATIENTS = [
    ("P-1001", "Maria", "Lopez", "1985-03-14", "555-0101", "BlueCross", "BC-448812", 1, 0),
    ("P-1002", "James", "Carter", "1972-11-02", "555-0102", "Aetna", "AE-993021", 1, 0),
    ("P-1003", "Lily", "Chen", "2015-06-21", "555-0103", "BlueCross", "BC-220931", 1, 0),
    ("P-1004", "Robert", "Smith", "1990-01-30", "555-0104", "UnitedHealth", "UH-551203", 0, 0),
    ("P-1005", "Aisha", "Khan", "1999-08-09", "555-0105", "Cigna", "CI-781144", 1, 1),
    ("P-1006", "Daniel", "Brooks", "1965-05-05", "555-0106", "self_pay", None, 1, 0),
    ("P-1007", "John", "Miller", "1980-04-04", "555-0107", "Aetna", "AE-120045", 1, 0),
    ("P-1008", "John", "Miller", "1955-09-19", "555-0108", "Cigna", "CI-300876", 1, 0),
]


def _next_weekday(after: date, weekday: int) -> date:
    """First date strictly after `after` that falls on `weekday` (Mon=0)."""
    days = (weekday - after.weekday()) % 7 or 7
    return after + timedelta(days=days)


def seed(conn: sqlite3.Connection, cfg: PracticeConfig, now: datetime) -> None:
    conn.executemany("INSERT INTO patients VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", PATIENTS)

    # (appointment_id, patient_id, provider_id, office_id, type_id, weekday Mon=0, hour)
    appointments = [
        # Maria: sick visit with Dr. Patel, next Tuesday 10:00
        ("APT-0001", "P-1001", "DR-PATEL", "OFFICE-A", "sick_visit", 1, 10),
        # James: takes Dr. Johnson's only slot on the coming Friday
        ("APT-0002", "P-1002", "DR-JOHNSON", "OFFICE-A", "skin_check", 4, 14),
        # Lily: well-child with Dr. Okafor, next Thursday 10:00
        ("APT-0003", "P-1003", "DR-OKAFOR", "OFFICE-B", "well_child", 3, 10),
    ]

    patients = {p[0]: p for p in PATIENTS}
    busy: list[BusyBlock] = []
    for appt_id, patient_id, provider_id, office_id, type_id, weekday, hour in appointments:
        provider = cfg.provider(provider_id)
        appt_type = cfg.appointment_type(type_id)
        p = patients[patient_id]
        patient = Patient(p[0], p[1], p[2], date.fromisoformat(p[3]), p[5], bool(p[7]), bool(p[8]))
        # Use the first matching weekday that passes every rule (e.g. skips a provider's leave days).
        first = datetime.combine(_next_weekday(now.date(), weekday), datetime.min.time()).replace(hour=hour)
        for start in (first + timedelta(weeks=w) for w in range(8)):
            violations = check_booking(
                cfg, provider=provider, office_id=office_id, start=start, appt_type=appt_type,
                patient=patient, now=now, busy=busy,
            )
            if not violations:
                break
        else:
            raise ValueError(f"Seed appointment {appt_id} breaks a rule: {violations[0].message}")
        end = start + timedelta(minutes=appt_type.duration_minutes)
        conn.execute(
            "INSERT INTO appointments (id, patient_id, provider_id, office_id, appointment_type_id, "
            "start, end, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'booked', ?)",
            (appt_id, patient_id, provider_id, office_id, type_id,
             start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"),
             now.isoformat(timespec="minutes")),
        )
        busy.append(BusyBlock(appt_id, provider_id, patient_id, start, end))


if __name__ == "__main__":
    from .ehr import EHR

    ehr = EHR.from_env()
    ehr.reset()
    print(f"Database rebuilt at {ehr.db_path} with synthetic data (now = {ehr.now():%Y-%m-%d %H:%M}).")
