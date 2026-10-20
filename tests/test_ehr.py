"""Tests for the EHR operations behind the tools (real SQLite, frozen clock)."""

from starlette.testclient import TestClient

from ehr_server.errors import ErrorCode
from ehr_server.server import create_server, http_app


def test_dashboard_api(ehr):
    ehr.cancel_appointment(appointment_id="APT-0003")
    with TestClient(http_app(create_server(ehr))) as client:
        state = client.get("/api/state").json()
    assert [(a["appointment_id"], a["status"]) for a in state["appointments"]] == [
        ("APT-0001", "booked"), ("APT-0003", "cancelled"), ("APT-0002", "booked")]
    assert state["appointments"][0]["patient_name"] == "Maria Lopez"
    assert state["audit"][0]["tool"] == "cancel_appointment"


def err(result):
    assert result["ok"] is False, result
    return result["error"]["code"]


# --- find_patient

def test_find_patient_matches_case_insensitively(ehr):
    r = ehr.find_patient(first_name="maria", last_name="LOPEZ", date_of_birth="1985-03-14")
    assert r["ok"]
    assert r["patient"]["patient_id"] == "P-1001"
    assert [a["appointment_id"] for a in r["upcoming_appointments"]] == ["APT-0001"]


def test_find_patient_wrong_dob_does_not_leak(ehr):
    r = ehr.find_patient(first_name="Maria", last_name="Lopez", date_of_birth="1985-03-15")
    assert err(r) == ErrorCode.PATIENT_NOT_FOUND
    unknown = ehr.find_patient(first_name="Nobody", last_name="Here", date_of_birth="1985-03-15")
    assert unknown["error"]["message"] == r["error"]["message"]


def test_same_name_patients_are_told_apart_by_dob(ehr):
    a = ehr.find_patient(first_name="John", last_name="Miller", date_of_birth="1980-04-04")
    b = ehr.find_patient(first_name="John", last_name="Miller", date_of_birth="1955-09-19")
    assert (a["patient"]["patient_id"], b["patient"]["patient_id"]) == ("P-1007", "P-1008")


def test_bad_date_format_is_invalid_input(ehr):
    assert err(ehr.find_patient(first_name="Maria", last_name="Lopez", date_of_birth="March 14")) == ErrorCode.INVALID_INPUT


# --- search_slots

def test_search_johnson_skips_taken_friday(ehr):
    r = ehr.search_slots(appointment_type_id="skin_check", provider_id="DR-JOHNSON", date_to="2026-11-30")
    starts = [s["start"] for s in r["slots"]]
    assert starts[0] == "2026-10-30T14:00"  # Oct 23 is taken by James
    assert all(s["day"] == "Friday" and s["start"].endswith("14:00") and s["office_id"] == "OFFICE-A" for s in r["slots"])


def test_search_explains_excluded_providers(ehr):
    # Aisha is a new Cigna patient: Morales does not take new patients, Okafor is pediatrics.
    r = ehr.search_slots(appointment_type_id="sick_visit", patient_id="P-1005")
    reasons = {e["provider_id"]: e["reason"] for e in r["excluded_providers"]}
    assert reasons == {"DR-MORALES": "NOT_ACCEPTING_NEW_PATIENTS", "DR-OKAFOR": "AGE_RESTRICTION"}
    assert {s["provider_id"] for s in r["slots"]} == {"DR-PATEL"}


def test_search_part_of_day(ehr):
    r = ehr.search_slots(appointment_type_id="sick_visit", provider_id="DR-PATEL", part_of_day="afternoon")
    assert r["slots"] and all(int(s["start"][11:13]) >= 12 for s in r["slots"])


def test_search_with_unfit_provider_returns_error_and_alternatives(ehr):
    r = ehr.search_slots(appointment_type_id="skin_check", patient_id="P-1005", provider_id="DR-JOHNSON")
    assert err(r) == ErrorCode.OUT_OF_NETWORK
    assert r["alternatives"] == []  # nobody else offers skin checks


def test_every_searched_slot_is_bookable(ehr):
    """Search and booking share one rule check, so search can never offer a slot booking refuses."""
    slots = ehr.search_slots(appointment_type_id="sick_visit", patient_id="P-1006", limit=10)["slots"]
    assert len(slots) == 10
    for s in slots[:5]:
        r = ehr.book_appointment(patient_id="P-1006", provider_id=s["provider_id"], office_id=s["office_id"],
                                 start=s["start"], appointment_type_id="sick_visit")
        # Booking the first slot makes overlapping ones invalid for the same patient; others must succeed.
        assert r["ok"] or r["error"]["code"] == ErrorCode.PATIENT_DOUBLE_BOOKED


# --- book_appointment

def test_book_happy_path(ehr):
    r = ehr.book_appointment(patient_id="P-1002", provider_id="DR-JOHNSON", start="2026-10-30T14:00",
                             appointment_type_id="skin_check")
    assert r["ok"]
    assert r["appointment"]["office_id"] == "OFFICE-A"  # inferred: Johnson only works at Office A
    assert "Friday, October 30 at 02:00 PM" in r["confirmation"]
    found = ehr.find_patient(first_name="James", last_name="Carter", date_of_birth="1972-11-02")
    assert len(found["upcoming_appointments"]) == 2


def test_book_rule_violation_returns_valid_alternatives(ehr):
    r = ehr.book_appointment(patient_id="P-1002", provider_id="DR-JOHNSON", office_id="OFFICE-B",
                             start="2026-10-23T14:00", appointment_type_id="skin_check")
    assert err(r) == ErrorCode.PROVIDER_NOT_AT_OFFICE
    assert r["error"]["how_to_fix"]
    alt = r["alternatives"][0]
    assert (alt["start"], alt["office_id"]) == ("2026-10-30T14:00", "OFFICE-A")
    again = ehr.book_appointment(patient_id="P-1002", provider_id=alt["provider_id"], office_id=alt["office_id"],
                                 start=alt["start"], appointment_type_id="skin_check")
    assert again["ok"]


def test_out_of_network_suggests_other_providers(ehr):
    # Aisha (Cigna, new) asks for Dr. Morales: not accepting new patients -> alternatives from Patel.
    r = ehr.book_appointment(patient_id="P-1005", provider_id="DR-MORALES", start="2026-10-21T09:00",
                             appointment_type_id="sick_visit")
    assert err(r) == ErrorCode.NOT_ACCEPTING_NEW_PATIENTS
    assert r["alternatives"] and {a["provider_id"] for a in r["alternatives"]} == {"DR-PATEL"}


def test_inactive_coverage_blocks_booking_without_alternatives(ehr):
    r = ehr.book_appointment(patient_id="P-1004", provider_id="DR-PATEL", start="2026-10-20T11:00",
                             appointment_type_id="sick_visit")
    assert err(r) == ErrorCode.COVERAGE_INACTIVE
    assert r["alternatives"] == []


def test_double_booking_same_slot_is_rejected(ehr):
    args = dict(provider_id="DR-PATEL", start="2026-10-21T11:00", appointment_type_id="sick_visit")
    assert ehr.book_appointment(patient_id="P-1001", **args)["ok"]
    assert err(ehr.book_appointment(patient_id="P-1006", **args)) == ErrorCode.SLOT_TAKEN


def test_office_required_when_ambiguous(ehr, cfg):
    # Give Patel two offices on the same weekday to make the office ambiguous.
    patel = cfg.provider("DR-PATEL")
    patel.availability[1].days.append("tue")
    r = ehr.book_appointment(patient_id="P-1001", provider_id="DR-PATEL", start="2026-10-20T11:00",
                             appointment_type_id="sick_visit")
    assert err(r) == ErrorCode.INVALID_INPUT


# --- reschedule / cancel

def test_reschedule_moves_appointment_atomically(ehr):
    r = ehr.reschedule_appointment(appointment_id="APT-0001", new_start="2026-10-21T15:00")
    assert r["ok"] and r["replaced_appointment_id"] == "APT-0001"
    found = ehr.find_patient(first_name="Maria", last_name="Lopez", date_of_birth="1985-03-14")
    assert [a["start"] for a in found["upcoming_appointments"]] == ["2026-10-21T15:00"]


def test_reschedule_into_own_overlapping_slot(ehr):
    # Moving 10:00 -> 10:00 the next week, or to an overlapping time, must not conflict with itself.
    r = ehr.reschedule_appointment(appointment_id="APT-0001", new_start="2026-10-20T10:30")
    assert r["ok"]


def test_failed_reschedule_keeps_original(ehr):
    r = ehr.reschedule_appointment(appointment_id="APT-0001", new_start="2026-10-26T10:00")  # Patel on leave
    assert err(r) == ErrorCode.PROVIDER_ON_LEAVE
    assert r["alternatives"]
    found = ehr.find_patient(first_name="Maria", last_name="Lopez", date_of_birth="1985-03-14")
    assert [a["appointment_id"] for a in found["upcoming_appointments"]] == ["APT-0001"]


def test_cannot_reschedule_twice(ehr):
    assert ehr.reschedule_appointment(appointment_id="APT-0001", new_start="2026-10-21T15:00")["ok"]
    assert err(ehr.reschedule_appointment(appointment_id="APT-0001", new_start="2026-10-21T16:00")) == ErrorCode.APPOINTMENT_NOT_ACTIVE


def test_cancel_then_cancel_again(ehr):
    assert ehr.cancel_appointment(appointment_id="APT-0003")["appointment"]["status"] == "cancelled"
    assert err(ehr.cancel_appointment(appointment_id="APT-0003")) == ErrorCode.APPOINTMENT_NOT_ACTIVE
    assert err(ehr.cancel_appointment(appointment_id="APT-9999")) == ErrorCode.APPOINTMENT_NOT_FOUND


# --- insurance / escalation

def test_verify_insurance(ehr):
    inactive = ehr.verify_insurance(patient_id="P-1004")["eligibility"]
    assert inactive["status"] == "inactive" and "next_step" in inactive
    aisha = ehr.verify_insurance(patient_id="P-1005", provider_id="DR-JOHNSON")["eligibility"]
    assert aisha["provider_in_network"] is False and "DR-JOHNSON" not in aisha["in_network_providers"]
    assert aisha["member_id_last4"] == "1144"
    assert ehr.verify_insurance(patient_id="P-1006")["eligibility"]["status"] == "self_pay"


def test_escalation_creates_ticket(ehr):
    r = ehr.escalate_to_human(reason="insurance_issue", summary="Robert Smith's coverage shows inactive.", patient_id="P-1004")
    assert r["ok"] and r["ticket_id"] == "ESC-0001" and "call you back" in r["message"]
