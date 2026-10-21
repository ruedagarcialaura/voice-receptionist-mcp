"""Storage: the SQLite schema, the synthetic seed data and the append-only audit log.

practice_rules.yaml holds the practice STRUCTURE (offices, providers, schedules).
SQLite holds TRANSACTIONAL data (patients, appointments, escalations, audit log).

Rebuild the database:  python -m ehr_server.db
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .rules import BusyBlock, Patient, PracticeConfig, check_booking

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id                  TEXT PRIMARY KEY,
    first_name          TEXT NOT NULL,
    last_name           TEXT NOT NULL,
    date_of_birth       TEXT NOT NULL,          -- YYYY-MM-DD
    phone               TEXT,
    insurance_payer     TEXT NOT NULL,          -- payer name or 'self_pay'
    insurance_member_id TEXT,
    coverage_active     INTEGER NOT NULL,
    is_new_patient      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    id                  TEXT PRIMARY KEY,
    patient_id          TEXT NOT NULL REFERENCES patients(id),
    provider_id         TEXT NOT NULL,
    office_id           TEXT NOT NULL,
    appointment_type_id TEXT NOT NULL,
    start               TEXT NOT NULL,          -- practice-local YYYY-MM-DDTHH:MM
    end                 TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('booked', 'cancelled', 'rescheduled')),
    replaced_by         TEXT,
    cancel_reason       TEXT,
    created_at          TEXT NOT NULL
);

-- Last line of defence against double booking: even if two requests pass the
-- rule check at the same moment, the database refuses the second one.
CREATE UNIQUE INDEX IF NOT EXISTS one_booking_per_provider_start
    ON appointments(provider_id, start) WHERE status = 'booked';

CREATE TABLE IF NOT EXISTS escalations (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    patient_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,              -- wall-clock UTC
    tool            TEXT NOT NULL,
    args_json       TEXT NOT NULL,
    ok              INTEGER NOT NULL,
    error_code      TEXT,
    appointment_id  TEXT,
    result_json     TEXT NOT NULL
);

-- Append-only, enforced by the database itself, not just by convention.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    # isolation_level=None: we control transactions explicitly with BEGIN IMMEDIATE.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------------------------- seed data

# Synthetic only. id, first, last, dob, phone, payer, member_id, coverage_active, is_new_patient
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
    return after + timedelta(days=(weekday - after.weekday()) % 7 or 7)


def seed(conn: sqlite3.Connection, cfg: PracticeConfig, now: datetime) -> None:
    """Insert the synthetic patients and three appointments placed relative to "now". Each appointment is
    checked with the same rules the tools use, so the seed can never contain a booking the rules refuse."""
    conn.executemany("INSERT INTO patients VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", PATIENTS)

    # (appointment_id, patient_id, provider_id, office_id, type_id, weekday Mon=0, hour)
    appointments = [
        ("APT-0001", "P-1001", "DR-PATEL", "OFFICE-A", "sick_visit", 1, 10),    # Maria, next Tuesday
        ("APT-0002", "P-1002", "DR-JOHNSON", "OFFICE-A", "skin_check", 4, 14),  # James takes Johnson's Friday slot
        ("APT-0003", "P-1003", "DR-OKAFOR", "OFFICE-B", "well_child", 3, 10),   # Lily, next Thursday
    ]
    patients = {p[0]: p for p in PATIENTS}
    busy: list[BusyBlock] = []
    for appt_id, patient_id, provider_id, office_id, type_id, weekday, hour in appointments:
        provider, appt_type, p = cfg.provider(provider_id), cfg.appointment_type(type_id), patients[patient_id]
        patient = Patient(p[0], p[1], p[2], date.fromisoformat(p[3]), p[5], bool(p[7]), bool(p[8]))
        # Use the first matching weekday that passes every rule (e.g. skips a provider's leave days).
        first = datetime.combine(_next_weekday(now.date(), weekday), datetime.min.time()).replace(hour=hour)
        for start in (first + timedelta(weeks=w) for w in range(8)):
            violations = check_booking(cfg, provider=provider, office_id=office_id, start=start,
                                       appt_type=appt_type, patient=patient, now=now, busy=busy)
            if not violations:
                break
        else:
            raise ValueError(f"Seed appointment {appt_id} breaks a rule: {violations[0].message}")
        end = start + timedelta(minutes=appt_type.duration_minutes)
        conn.execute(
            "INSERT INTO appointments (id, patient_id, provider_id, office_id, appointment_type_id, "
            "start, end, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'booked', ?)",
            (appt_id, patient_id, provider_id, office_id, type_id, start.isoformat(timespec="minutes"),
             end.isoformat(timespec="minutes"), now.isoformat(timespec="minutes")),
        )
        busy.append(BusyBlock(appt_id, provider_id, patient_id, start, end))


# ---------------------------------------------------------------------------------------------- audit log

class AuditLog:
    """Every tool call, including rejected ones: "the agent tried Dr. Johnson on a Tuesday and was refused"
    is exactly what you want when reviewing a call or turning it into an eval case."""

    def __init__(self, connect: Callable[[], AbstractContextManager[sqlite3.Connection]]):
        self._connect = connect

    def record(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        error = result.get("error") or {}
        appointment_id = (result.get("appointment") or {}).get("appointment_id") or args.get("appointment_id")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, tool, args_json, ok, error_code, appointment_id, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(timespec="seconds"), tool, json.dumps(args, default=str),
                 int(bool(result.get("ok"))), error.get("code"), appointment_id, json.dumps(result, default=str)),
            )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_audit_row(r) for r in rows]

    def stats(self) -> dict[str, int]:
        """Counters for the dashboard, computed from the audit log."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(tool = 'book_appointment' AND ok = 1) AS booked, "
                "SUM(tool = 'verify_insurance' AND ok = 1) AS insurance_verified, "
                "SUM(tool = 'escalate_to_human' AND ok = 1) AS transferred_to_staff, "
                "SUM(tool IN ('book_appointment', 'reschedule_appointment') AND ok = 0) AS refused_by_rules "
                "FROM audit_log"
            ).fetchone()
        return {key: row[key] or 0 for key in row.keys()}

    def for_appointment(self, appointment_id: str) -> list[dict[str, Any]]:
        """History of one appointment, including the reschedule that replaced it."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE appointment_id = ? "
                "OR json_extract(result_json, '$.replaced_appointment_id') = ? ORDER BY id",
                (appointment_id, appointment_id),
            ).fetchall()
        return [_audit_row(r) for r in rows]


def _audit_row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "ts": r["ts"], "tool": r["tool"], "args": json.loads(r["args_json"]),
            "ok": bool(r["ok"]), "error_code": r["error_code"], "appointment_id": r["appointment_id"]}


if __name__ == "__main__":
    from .ehr import EHR

    ehr = EHR.from_env()
    ehr.reset()
    print(f"Database rebuilt at {ehr.db_path} with synthetic data (now = {ehr.now():%Y-%m-%d %H:%M}).")
