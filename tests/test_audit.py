import sqlite3

import pytest

from ehr_server.db import connect


def test_every_call_is_audited_including_failures(ehr):
    ehr.find_patient(first_name="Maria", last_name="Lopez", date_of_birth="1985-03-14")
    ehr.book_appointment(patient_id="P-1002", provider_id="DR-JOHNSON", office_id="OFFICE-B",
                         start="2026-10-23T14:00", appointment_type_id="skin_check")
    entries = ehr.audit.recent()
    assert [(e["tool"], e["ok"], e["error_code"]) for e in entries] == [
        ("book_appointment", False, "PROVIDER_NOT_AT_OFFICE"),
        ("find_patient", True, None),
    ]
    assert entries[0]["args"]["office_id"] == "OFFICE-B"


def test_audit_log_is_append_only(ehr):
    ehr.find_patient(first_name="Maria", last_name="Lopez", date_of_birth="1985-03-14")
    conn = connect(ehr.db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE audit_log SET ok = 0")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM audit_log")
    finally:
        conn.close()


def test_appointment_history_follows_reschedule(ehr):
    new_id = ehr.reschedule_appointment(appointment_id="APT-0001", new_start="2026-10-21T15:00")["appointment"]["appointment_id"]
    ehr.cancel_appointment(appointment_id=new_id)
    history = ehr.audit.for_appointment("APT-0001")
    assert [e["tool"] for e in history] == ["reschedule_appointment"]
    assert [e["tool"] for e in ehr.audit.for_appointment(new_id)] == ["reschedule_appointment", "cancel_appointment"]


def test_unexpected_errors_become_structured_and_audited(ehr, monkeypatch):
    monkeypatch.setattr(ehr, "_busy", lambda *a, **k: 1 / 0)
    r = ehr.book_appointment(patient_id="P-1002", provider_id="DR-JOHNSON", start="2026-10-30T14:00",
                             appointment_type_id="skin_check")
    assert r["error"]["code"] == "INTERNAL_ERROR"
    assert ehr.audit.recent(1)[0]["error_code"] == "INTERNAL_ERROR"
