"""SQLite storage for the fake EHR.

Split of responsibilities:
- practice_rules.yaml holds the practice STRUCTURE (offices, providers, schedules).
- SQLite holds TRANSACTIONAL data (patients, appointments, escalations, audit log).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
