"""The fake EHR: the domain operations behind the MCP tools.

This layer knows nothing about MCP. server.py is a thin adapter on top of it,
so the same operations could be exposed through a REST API or called directly
from tests. Auditing happens here (the @audited decorator), which means no
entry point can skip it.
"""

from __future__ import annotations

import functools
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

from .audit import AuditLog
from .config import WEEKDAYS, AppointmentType, PracticeConfig, Provider, load_config
from .db import connect, init_schema
from .errors import ErrorCode, error, ok
from .rules import (
    PROVIDER_FIT_CODES,
    SELF_PAY,
    BusyBlock,
    Patient,
    Violation,
    candidate_starts,
    check_booking,
    check_provider_fit,
)
from .seed import seed

PROJECT_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("ehr")

PartOfDay = Literal["any", "morning", "afternoon"]
EscalationReason = Literal["caller_request", "insurance_issue", "clinical_question", "cannot_verify_identity", "other"]


class ToolFailure(Exception):
    """Raised inside an operation to return a structured error immediately."""

    def __init__(self, result: dict[str, Any]):
        self.result = result


def audited(method: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Record every call (arguments + structured result) in the audit log."""

    @functools.wraps(method)
    def wrapper(self: EHR, **kwargs: Any) -> dict[str, Any]:
        try:
            result = method(self, **kwargs)
        except ToolFailure as f:
            result = f.result
        except Exception:
            # Log to stderr (stdout is the MCP stdio channel) and never crash the call.
            log.exception("Unhandled error in %s", method.__name__)
            result = error(ErrorCode.INTERNAL_ERROR, "Unexpected server error.")
        self.audit.record(method.__name__, kwargs, result)
        return result

    return wrapper


def _parse_datetime(value: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise ToolFailure(error(ErrorCode.INVALID_INPUT, f"{field} must look like 2026-10-23T14:00, got {value!r}."))
    return dt.replace(tzinfo=None, second=0, microsecond=0)


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ToolFailure(error(ErrorCode.INVALID_INPUT, f"{field} must look like 2026-10-23, got {value!r}."))


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    # IMMEDIATE takes the write lock up front, so "check rules, then insert"
    # cannot interleave with another booking between the check and the insert.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _to_patient(r: sqlite3.Row) -> Patient:
    return Patient(
        id=r["id"],
        first_name=r["first_name"],
        last_name=r["last_name"],
        date_of_birth=date.fromisoformat(r["date_of_birth"]),
        insurance_payer=r["insurance_payer"],
        coverage_active=bool(r["coverage_active"]),
        is_new_patient=bool(r["is_new_patient"]),
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


class EHR:
    def __init__(self, db_path: str | Path, cfg: PracticeConfig, now_fn: Callable[[], datetime] | None = None):
        self.db_path = Path(db_path)
        self.cfg = cfg
        self._now_fn = now_fn or (lambda: datetime.now().replace(second=0, microsecond=0))
        self.audit = AuditLog(self._db)

    @classmethod
    def from_env(cls) -> EHR:
        load_dotenv(PROJECT_ROOT / ".env")
        db_path = PROJECT_ROOT / os.getenv("EHR_DB_PATH", "data/ehr.db")
        rules_path = PROJECT_ROOT / os.getenv("EHR_RULES_PATH", "ehr_server/practice_rules.yaml")
        fixed_now = os.getenv("EHR_NOW")
        now_fn = (lambda: datetime.fromisoformat(fixed_now)) if fixed_now else None
        ehr = cls(db_path, load_config(rules_path), now_fn)
        if not ehr.db_path.exists():
            ehr.reset()
        return ehr

    def now(self) -> datetime:
        return self._now_fn()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def reset(self) -> None:
        """Delete and rebuild the database with synthetic seed data."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.unlink(missing_ok=True)
        with self._db() as conn:
            init_schema(conn)
            with _transaction(conn):
                seed(conn, self.cfg, self.now())

    # ------------------------------------------------------------------ lookups

    def _require_provider(self, provider_id: str) -> Provider:
        provider = self.cfg.provider(provider_id)
        if not provider:
            raise ToolFailure(error(
                ErrorCode.PROVIDER_NOT_FOUND, f"Unknown provider_id {provider_id!r}.",
                {"valid_provider_ids": [p.id for p in self.cfg.providers]},
            ))
        return provider

    def _require_office(self, office_id: str) -> None:
        if not self.cfg.office(office_id):
            raise ToolFailure(error(
                ErrorCode.OFFICE_NOT_FOUND, f"Unknown office_id {office_id!r}.",
                {"valid_office_ids": [o.id for o in self.cfg.offices]},
            ))

    def _require_type(self, type_id: str) -> AppointmentType:
        appt_type = self.cfg.appointment_type(type_id)
        if not appt_type:
            raise ToolFailure(error(
                ErrorCode.APPOINTMENT_TYPE_NOT_FOUND, f"Unknown appointment_type_id {type_id!r}.",
                {"valid_appointment_type_ids": [t.id for t in self.cfg.appointment_types]},
            ))
        return appt_type

    def _require_patient(self, conn: sqlite3.Connection, patient_id: str) -> Patient:
        r = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if not r:
            raise ToolFailure(error(ErrorCode.PATIENT_NOT_FOUND, f"Unknown patient_id {patient_id!r}. Use find_patient first."))
        return _to_patient(r)

    def _infer_office(self, provider: Provider, start: datetime, office_id: str | None) -> str:
        """Use the given office, or the only office the provider works at on that weekday."""
        if office_id:
            self._require_office(office_id)
            return office_id
        weekday = WEEKDAYS[start.weekday()]
        offices = sorted({w.office for w in provider.availability if weekday in w.days}) or provider.offices()
        if len(offices) == 1:
            return offices[0]
        raise ToolFailure(error(
            ErrorCode.INVALID_INPUT, f"office_id is required: {provider.name} works at {', '.join(offices)}.",
            {"offices": offices},
        ))

    def _busy(self, conn: sqlite3.Connection, day_from: date, day_to: date) -> list[BusyBlock]:
        rows = conn.execute(
            "SELECT id, provider_id, patient_id, start, end FROM appointments "
            "WHERE status = 'booked' AND start < ? AND end > ?",
            ((day_to + timedelta(days=1)).isoformat(), day_from.isoformat()),
        ).fetchall()
        return [
            BusyBlock(r["id"], r["provider_id"], r["patient_id"],
                      datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"]))
            for r in rows
        ]

    # ------------------------------------------------------------------ views

    def _slot_view(self, provider: Provider, office_id: str, start: datetime, appt_type: AppointmentType) -> dict[str, Any]:
        return {
            "provider_id": provider.id,
            "provider_name": provider.name,
            "office_id": office_id,
            "office_name": self.cfg.office(office_id).name,
            "start": _iso(start),
            "day": f"{start:%A}",
            "end": _iso(start + timedelta(minutes=appt_type.duration_minutes)),
            "appointment_type_id": appt_type.id,
        }

    def _appointment_view(self, r: sqlite3.Row) -> dict[str, Any]:
        provider = self.cfg.provider(r["provider_id"])
        office = self.cfg.office(r["office_id"])
        appt_type = self.cfg.appointment_type(r["appointment_type_id"])
        start = datetime.fromisoformat(r["start"])
        return {
            "appointment_id": r["id"],
            "patient_id": r["patient_id"],
            "provider_id": r["provider_id"],
            "provider_name": provider.name if provider else r["provider_id"],
            "office_id": r["office_id"],
            "office_name": office.name if office else r["office_id"],
            "office_address": office.address if office else None,
            "appointment_type_id": r["appointment_type_id"],
            "appointment_type": appt_type.name if appt_type else r["appointment_type_id"],
            "start": r["start"],
            "day": f"{start:%A}",
            "end": r["end"],
            "status": r["status"],
        }

    def _get_appointment(self, conn: sqlite3.Connection, appointment_id: str) -> sqlite3.Row:
        r = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        if not r:
            raise ToolFailure(error(ErrorCode.APPOINTMENT_NOT_FOUND, f"No appointment {appointment_id!r}."))
        return r

    def _require_active(self, r: sqlite3.Row) -> None:
        if r["status"] != "booked" or datetime.fromisoformat(r["start"]) < self.now():
            raise ToolFailure(error(
                ErrorCode.APPOINTMENT_NOT_ACTIVE,
                f"Appointment {r['id']} is {r['status']}" + (" and in the past." if r["status"] == "booked" else "."),
                {"status": r["status"], "replaced_by": r["replaced_by"]},
            ))

    # ------------------------------------------------------------------ slot search

    def _find_slots(
        self,
        conn: sqlite3.Connection,
        appt_type: AppointmentType,
        patient: Patient | None,
        providers: list[Provider],
        office_id: str | None,
        day_from: date | None,
        day_to: date | None,
        limit: int,
        part_of_day: PartOfDay = "any",
        ignore_appointment_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        now = self.now()
        first_day = max(day_from or now.date(), now.date())
        horizon = now.date() + timedelta(days=self.cfg.practice.max_days_ahead)
        last_day = min(day_to or first_day + timedelta(days=14), horizon)

        eligible, excluded = [], []
        for p in providers:
            fit = check_provider_fit(self.cfg, p, appt_type, patient, first_day)
            if fit:
                excluded.append({"provider_id": p.id, "provider_name": p.name,
                                 "reason": fit[0].code.value, "message": fit[0].message})
            else:
                eligible.append(p)

        busy = self._busy(conn, first_day, last_day)
        slots: list[dict[str, Any]] = []
        day = first_day
        while day <= last_day and len(slots) < limit:
            day_slots = []
            for p in eligible:
                for office, start in candidate_starts(self.cfg, p, day, appt_type, office_id):
                    if part_of_day == "morning" and start.hour >= 12:
                        continue
                    if part_of_day == "afternoon" and start.hour < 12:
                        continue
                    # The exact same rule check that booking uses.
                    if not check_booking(
                        self.cfg, provider=p, office_id=office, start=start, appt_type=appt_type,
                        patient=patient, now=now, busy=busy, ignore_appointment_id=ignore_appointment_id,
                    ):
                        day_slots.append(self._slot_view(p, office, start, appt_type))
            slots.extend(sorted(day_slots, key=lambda s: s["start"]))
            day += timedelta(days=1)
        return slots[:limit], excluded

    def _rejection(
        self,
        conn: sqlite3.Connection,
        violations: list[Violation],
        appt_type: AppointmentType,
        patient: Patient,
        provider: Provider,
        start: datetime,
        ignore_appointment_id: str | None = None,
    ) -> dict[str, Any]:
        """Turn rule violations into a structured error with valid alternatives."""
        v = violations[0]
        alternatives: list[dict[str, Any]] = []
        if v.code != ErrorCode.COVERAGE_INACTIVE:
            if v.code in PROVIDER_FIT_CODES:
                # A different time will not help: suggest other providers.
                providers = [p for p in self.cfg.providers if p.id != provider.id and appt_type.id in p.appointment_types]
            else:
                providers = [provider]
            day_from = None if v.code in {ErrorCode.IN_THE_PAST, ErrorCode.TOO_SOON, ErrorCode.TOO_FAR_AHEAD} else start.date()
            alternatives, _ = self._find_slots(
                conn, appt_type, patient, providers, None, day_from, None, 3,
                ignore_appointment_id=ignore_appointment_id,
            )
        details = dict(v.details)
        if len(violations) > 1:
            details["other_violations"] = [x.code.value for x in violations[1:]]
        return error(v.code, v.message, details, alternatives)

    def _insert_appointment(
        self, conn: sqlite3.Connection, patient: Patient, provider: Provider,
        office_id: str, appt_type: AppointmentType, start: datetime,
    ) -> str:
        count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        appt_id = f"APT-{count + 1:04d}"
        conn.execute(
            "INSERT INTO appointments (id, patient_id, provider_id, office_id, appointment_type_id, "
            "start, end, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'booked', ?)",
            (appt_id, patient.id, provider.id, office_id, appt_type.id, _iso(start),
             _iso(start + timedelta(minutes=appt_type.duration_minutes)), _iso(self.now())),
        )
        return appt_id

    def _confirmation(self, view: dict[str, Any]) -> str:
        start = datetime.fromisoformat(view["start"])
        return (f"{view['appointment_type']} with {view['provider_name']} on {start:%A, %B} {start.day} "
                f"at {start:%I:%M %p} at {view['office_name']}, {view['office_address']}.")

    # ------------------------------------------------------------------ tools

    @audited
    def find_patient(self, *, first_name: str, last_name: str, date_of_birth: str) -> dict[str, Any]:
        dob = _parse_date(date_of_birth, "date_of_birth")
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM patients WHERE lower(first_name) = lower(?) AND lower(last_name) = lower(?) "
                "AND date_of_birth = ?",
                (first_name.strip(), last_name.strip(), dob.isoformat()),
            ).fetchall()
            if len(rows) != 1:
                # Same message whether the name exists or not: do not leak who is a patient.
                return error(ErrorCode.PATIENT_NOT_FOUND, "No patient matches that name and date of birth.")
            patient = _to_patient(rows[0])
            upcoming = conn.execute(
                "SELECT * FROM appointments WHERE patient_id = ? AND status = 'booked' AND start >= ? ORDER BY start",
                (patient.id, _iso(self.now())),
            ).fetchall()
        return ok(
            patient={
                "patient_id": patient.id,
                "first_name": patient.first_name,
                "last_name": patient.last_name,
                "insurance_payer": patient.insurance_payer,
                "is_new_patient": patient.is_new_patient,
            },
            upcoming_appointments=[self._appointment_view(r) for r in upcoming],
        )

    @audited
    def search_slots(
        self,
        *,
        appointment_type_id: str,
        patient_id: str | None = None,
        provider_id: str | None = None,
        office_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        part_of_day: PartOfDay = "any",
        limit: int = 5,
    ) -> dict[str, Any]:
        appt_type = self._require_type(appointment_type_id)
        provider = self._require_provider(provider_id) if provider_id else None
        if office_id:
            self._require_office(office_id)
        day_from = _parse_date(date_from, "date_from") if date_from else None
        day_to = _parse_date(date_to, "date_to") if date_to else None
        limit = max(1, min(limit, 10))
        providers = [provider] if provider else [p for p in self.cfg.providers if appt_type.id in p.appointment_types]

        with self._db() as conn:
            patient = self._require_patient(conn, patient_id) if patient_id else None
            if provider:
                fit = check_provider_fit(self.cfg, provider, appt_type, patient, day_from or self.now().date())
                if fit:
                    start = datetime.combine(day_from or self.now().date(), datetime.min.time())
                    return self._rejection(conn, fit, appt_type, patient, provider, start)
            slots, excluded = self._find_slots(
                conn, appt_type, patient, providers, office_id, day_from, day_to, limit, part_of_day
            )
        result = ok(slots=slots, excluded_providers=excluded)
        if not slots:
            result["message"] = "No availability for these filters. Try a wider date range, another part of day or another provider."
        return result

    @audited
    def book_appointment(
        self,
        *,
        patient_id: str,
        provider_id: str,
        start: str,
        appointment_type_id: str,
        office_id: str | None = None,
    ) -> dict[str, Any]:
        provider = self._require_provider(provider_id)
        appt_type = self._require_type(appointment_type_id)
        start_dt = _parse_datetime(start, "start")
        office = self._infer_office(provider, start_dt, office_id)

        with self._db() as conn:
            patient = self._require_patient(conn, patient_id)
            try:
                with _transaction(conn):
                    violations = check_booking(
                        self.cfg, provider=provider, office_id=office, start=start_dt, appt_type=appt_type,
                        patient=patient, now=self.now(), busy=self._busy(conn, start_dt.date(), start_dt.date()),
                    )
                    if not violations:
                        appt_id = self._insert_appointment(conn, patient, provider, office, appt_type, start_dt)
            except sqlite3.IntegrityError:
                violations = [Violation(ErrorCode.SLOT_TAKEN, "That time was just booked by someone else.")]
            if violations:
                return self._rejection(conn, violations, appt_type, patient, provider, start_dt)
            view = self._appointment_view(self._get_appointment(conn, appt_id))
        return ok(appointment=view, confirmation=self._confirmation(view))

    @audited
    def reschedule_appointment(
        self,
        *,
        appointment_id: str,
        new_start: str,
        new_provider_id: str | None = None,
        new_office_id: str | None = None,
    ) -> dict[str, Any]:
        start_dt = _parse_datetime(new_start, "new_start")
        with self._db() as conn:
            old = self._get_appointment(conn, appointment_id)
            self._require_active(old)
            provider = self._require_provider(new_provider_id or old["provider_id"])
            office = self._infer_office(provider, start_dt, new_office_id)
            appt_type = self.cfg.appointment_type(old["appointment_type_id"])
            patient = self._require_patient(conn, old["patient_id"])
            try:
                with _transaction(conn):
                    violations = check_booking(
                        self.cfg, provider=provider, office_id=office, start=start_dt, appt_type=appt_type,
                        patient=patient, now=self.now(), busy=self._busy(conn, start_dt.date(), start_dt.date()),
                        ignore_appointment_id=appointment_id,
                    )
                    if not violations:
                        # Free the old slot first, then book the new one, in one transaction:
                        # either both happen or neither does.
                        conn.execute("UPDATE appointments SET status = 'rescheduled' WHERE id = ?", (appointment_id,))
                        new_id = self._insert_appointment(conn, patient, provider, office, appt_type, start_dt)
                        conn.execute("UPDATE appointments SET replaced_by = ? WHERE id = ?", (new_id, appointment_id))
            except sqlite3.IntegrityError:
                violations = [Violation(ErrorCode.SLOT_TAKEN, "That time was just booked by someone else.")]
            if violations:
                return self._rejection(conn, violations, appt_type, patient, provider, start_dt, appointment_id)
            view = self._appointment_view(self._get_appointment(conn, new_id))
        return ok(appointment=view, replaced_appointment_id=appointment_id, confirmation=self._confirmation(view))

    @audited
    def cancel_appointment(self, *, appointment_id: str, reason: str = "caller_request") -> dict[str, Any]:
        with self._db() as conn:
            r = self._get_appointment(conn, appointment_id)
            self._require_active(r)
            conn.execute(
                "UPDATE appointments SET status = 'cancelled', cancel_reason = ? WHERE id = ?", (reason, appointment_id)
            )
            view = self._appointment_view(self._get_appointment(conn, appointment_id))
        return ok(appointment=view)

    @audited
    def verify_insurance(self, *, patient_id: str, provider_id: str | None = None) -> dict[str, Any]:
        """Simulated eligibility check: deterministic, based on the seed data."""
        with self._db() as conn:
            patient = self._require_patient(conn, patient_id)
            member_id = conn.execute(
                "SELECT insurance_member_id FROM patients WHERE id = ?", (patient_id,)
            ).fetchone()[0]
        self_pay = patient.insurance_payer == SELF_PAY
        status = "self_pay" if self_pay else ("active" if patient.coverage_active else "inactive")
        in_network = [p.id for p in self.cfg.providers if self_pay or patient.insurance_payer in p.accepted_payers]
        eligibility: dict[str, Any] = {
            "patient_id": patient.id,
            "payer": patient.insurance_payer,
            "member_id_last4": member_id[-4:] if member_id else None,
            "status": status,
            "in_network_providers": in_network,
        }
        if provider_id:
            provider = self._require_provider(provider_id)
            eligibility["provider_id"] = provider.id
            eligibility["provider_in_network"] = provider.id in in_network
        if status == "inactive":
            eligibility["next_step"] = "Coverage is inactive: do not book. Offer escalate_to_human so staff can update insurance."
        return ok(eligibility=eligibility)

    @audited
    def escalate_to_human(
        self, *, reason: EscalationReason, summary: str, patient_id: str | None = None
    ) -> dict[str, Any]:
        # Escalation must never fail, so patient_id is stored as given, unverified.
        with self._db() as conn, _transaction(conn):
            count = conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
            ticket_id = f"ESC-{count + 1:04d}"
            conn.execute(
                "INSERT INTO escalations (id, created_at, reason, summary, patient_id) VALUES (?, ?, ?, ?, ?)",
                (ticket_id, _iso(self.now()), reason, summary, patient_id),
            )
        return ok(ticket_id=ticket_id, message=self.cfg.practice.escalation_callback)

    # ------------------------------------------------------------------ resources

    def directory(self) -> dict[str, Any]:
        now = self.now()
        p = self.cfg.practice
        return {
            "practice": p.name,
            "current_time": f"{now:%A %Y-%m-%d %H:%M}",
            # Computed by code so the LLM never does date arithmetic ("next Tuesday" -> look it up here).
            "calendar": [f"{d:%A} {d:%Y-%m-%d}" for d in (now.date() + timedelta(days=i) for i in range(21))],
            "timezone": p.timezone,
            "booking_policy": {
                "slot_minutes": p.slot_minutes,
                "min_notice_hours": p.min_notice_hours,
                "max_days_ahead": p.max_days_ahead,
            },
            "offices": [o.model_dump() for o in self.cfg.offices],
            "appointment_types": [t.model_dump() for t in self.cfg.appointment_types],
            "providers": [
                {
                    "provider_id": pr.id,
                    "name": pr.name,
                    "specialty": pr.specialty,
                    "appointment_types": pr.appointment_types,
                    "accepts_new_patients": pr.accepts_new_patients,
                    "schedule": pr.schedule_summary(),
                    "note": pr.note,
                }
                for pr in self.cfg.providers
            ],
        }

    def appointments(self) -> list[dict[str, Any]]:
        """Appointments from today on, for the dashboard."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT a.*, p.first_name || ' ' || p.last_name AS patient_name FROM appointments a "
                "JOIN patients p ON p.id = a.patient_id WHERE a.start >= ? ORDER BY a.start",
                (self.now().date().isoformat(),),
            ).fetchall()
        return [self._appointment_view(r) | {"patient_name": r["patient_name"]} for r in rows]

    def rules_markdown(self) -> str:
        p = self.cfg.practice
        lines = [
            f"# Booking rules: {p.name}",
            "",
            "These rules are enforced by server code on every booking; the agent cannot override them.",
            "",
            "## Practice-wide",
            f"- Appointments start every {p.slot_minutes} minutes inside a provider's schedule window.",
            f"- At least {p.min_notice_hours} hours notice; at most {p.max_days_ahead} days ahead.",
            f"- Active insurance coverage required: {'yes (self-pay always allowed)' if p.require_active_coverage else 'no'}.",
            "- No overlapping appointments for a provider or for a patient.",
            "",
            "## Providers",
        ]
        types = {t.id: t.name for t in self.cfg.appointment_types}
        for pr in self.cfg.providers:
            lines += [
                "",
                f"### {pr.name} ({pr.specialty}), `{pr.id}`",
                f"- Visit types: {', '.join(types[t] for t in pr.appointment_types)}",
                f"- Schedule: {pr.schedule_summary()}",
                f"- New patients: {'yes' if pr.accepts_new_patients else 'no'}",
                f"- Insurance accepted: {', '.join(pr.accepted_payers)}",
            ]
            if pr.patient_age:
                lines.append(f"- Patient age: {pr.patient_age.min or 0} to {pr.patient_age.max if pr.patient_age.max is not None else 'any'}")
            if pr.blocked_dates:
                lines.append(f"- Out of office: {', '.join(d.isoformat() for d in pr.blocked_dates)}")
            if pr.note:
                lines.append(f"- Note: {pr.note}")
        return "\n".join(lines) + "\n"
