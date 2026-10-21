"""Unit tests for the pure rule functions: no database, no MCP, no LLM."""

from datetime import date, datetime

import pytest
import yaml
from pydantic import ValidationError

from ehr_server.rules import ErrorCode, PracticeConfig
from ehr_server.rules import BusyBlock, age_on, candidate_starts, check_booking

from .conftest import NOW, RULES_PATH, make_patient

FRI = datetime(2026, 10, 30, 14, 0)  # a Friday with no seeded bookings


def codes(cfg, provider_id, office_id, start, type_id, patient=None, busy=(), ignore=None):
    patient = patient if patient is not None else make_patient()
    violations = check_booking(
        cfg, provider=cfg.provider(provider_id), office_id=office_id, start=start,
        appt_type=cfg.appointment_type(type_id), patient=patient, now=NOW, busy=list(busy),
        ignore_appointment_id=ignore,
    )
    return [v.code for v in violations]


# --- Harrison's example: "only Dr. Johnson on Fridays at 2 p.m. in this office, not that one"

def test_johnson_friday_2pm_office_a_is_allowed(cfg):
    assert codes(cfg, "DR-JOHNSON", "OFFICE-A", FRI, "skin_check") == []


def test_johnson_wrong_office_is_rejected(cfg):
    assert codes(cfg, "DR-JOHNSON", "OFFICE-B", FRI, "skin_check") == [ErrorCode.PROVIDER_NOT_AT_OFFICE]


def test_johnson_wrong_day_is_rejected(cfg):
    tuesday = datetime(2026, 10, 27, 14, 0)
    assert codes(cfg, "DR-JOHNSON", "OFFICE-A", tuesday, "skin_check") == [ErrorCode.PROVIDER_NOT_WORKING]


def test_johnson_friday_230_is_outside_window(cfg):
    assert codes(cfg, "DR-JOHNSON", "OFFICE-A", FRI.replace(minute=30), "skin_check") == [ErrorCode.PROVIDER_NOT_WORKING]


# --- Time rules

@pytest.mark.parametrize("start, expected", [
    (datetime(2026, 10, 19, 8, 0), ErrorCode.IN_THE_PAST),
    (datetime(2026, 10, 19, 10, 0), ErrorCode.TOO_SOON),          # 1h notice < 2h
    (datetime(2026, 12, 21, 9, 0), ErrorCode.TOO_FAR_AHEAD),      # > 60 days
    (datetime(2026, 10, 26, 9, 0), ErrorCode.PROVIDER_ON_LEAVE),  # Patel blocked date
    (datetime(2026, 10, 20, 9, 15), ErrorCode.INVALID_START_TIME),  # off the 30-min grid
    (datetime(2026, 10, 20, 16, 30), None),                      # last slot of the day fits exactly
])
def test_time_rules_for_patel(cfg, start, expected):
    result = codes(cfg, "DR-PATEL", "OFFICE-A", start, "sick_visit")
    assert result == ([expected] if expected else [])


def test_long_visit_must_fit_inside_window(cfg):
    # 60-minute physical starting 16:30 would end 17:30, after the 17:00 window end.
    assert codes(cfg, "DR-PATEL", "OFFICE-A", datetime(2026, 10, 20, 16, 30), "annual_physical") == [
        ErrorCode.PROVIDER_NOT_WORKING
    ]


def test_patel_works_office_b_only_on_fridays(cfg):
    assert codes(cfg, "DR-PATEL", "OFFICE-B", datetime(2026, 10, 30, 9, 0), "sick_visit") == []
    assert codes(cfg, "DR-PATEL", "OFFICE-B", datetime(2026, 10, 20, 9, 0), "sick_visit") == [ErrorCode.PROVIDER_NOT_WORKING]


# --- Provider / patient fit

def test_visit_type_must_be_offered(cfg):
    assert codes(cfg, "DR-JOHNSON", "OFFICE-A", FRI, "sick_visit") == [ErrorCode.TYPE_NOT_OFFERED_BY_PROVIDER]


def test_new_patients_rejected_when_provider_not_accepting(cfg):
    new = make_patient(is_new_patient=True)
    assert codes(cfg, "DR-MORALES", "OFFICE-B", datetime(2026, 10, 21, 9, 0), "sick_visit", new) == [
        ErrorCode.NOT_ACCEPTING_NEW_PATIENTS
    ]


def test_pediatrician_rejects_adults_and_accepts_children(cfg):
    tue = datetime(2026, 10, 20, 11, 0)
    assert codes(cfg, "DR-OKAFOR", "OFFICE-B", tue, "sick_visit") == [ErrorCode.AGE_RESTRICTION]
    child = make_patient(date_of_birth=date(2015, 6, 21))
    assert codes(cfg, "DR-OKAFOR", "OFFICE-B", tue, "sick_visit", child) == []


def test_age_is_computed_on_the_appointment_day():
    assert age_on(date(2008, 10, 25), date(2026, 10, 24)) == 17
    assert age_on(date(2008, 10, 25), date(2026, 10, 25)) == 18


def test_out_of_network_payer_rejected(cfg):
    cigna = make_patient(insurance_payer="Cigna")
    assert codes(cfg, "DR-JOHNSON", "OFFICE-A", FRI, "skin_check", cigna) == [ErrorCode.OUT_OF_NETWORK]


def test_inactive_coverage_rejected_but_self_pay_allowed(cfg):
    inactive = make_patient(coverage_active=False)
    assert codes(cfg, "DR-JOHNSON", "OFFICE-A", FRI, "skin_check", inactive) == [ErrorCode.COVERAGE_INACTIVE]
    self_pay = make_patient(insurance_payer="self_pay", coverage_active=False)
    assert codes(cfg, "DR-JOHNSON", "OFFICE-A", FRI, "skin_check", self_pay) == []


def test_all_violations_are_reported(cfg):
    new_cigna = make_patient(is_new_patient=True, insurance_payer="Cigna")
    result = codes(cfg, "DR-JOHNSON", "OFFICE-B", FRI, "sick_visit", new_cigna)
    assert result == [ErrorCode.TYPE_NOT_OFFERED_BY_PROVIDER, ErrorCode.OUT_OF_NETWORK, ErrorCode.PROVIDER_NOT_AT_OFFICE]


# --- Conflicts

def busy(appt_id, provider, patient, start, minutes):
    from datetime import timedelta
    return BusyBlock(appt_id, provider, patient, start, start + timedelta(minutes=minutes))


def test_overlap_with_longer_visit_is_slot_taken(cfg):
    # A 60-min physical at 09:00 blocks a 09:30 request even though start times differ.
    taken = busy("APT-X", "DR-PATEL", "P-OTHER", datetime(2026, 10, 20, 9, 0), 60)
    assert codes(cfg, "DR-PATEL", "OFFICE-A", datetime(2026, 10, 20, 9, 30), "sick_visit", busy=[taken]) == [
        ErrorCode.SLOT_TAKEN
    ]
    assert codes(cfg, "DR-PATEL", "OFFICE-A", datetime(2026, 10, 20, 10, 0), "sick_visit", busy=[taken]) == []


def test_patient_cannot_be_in_two_places(cfg):
    other = busy("APT-X", "DR-JOHNSON", "P-TEST", FRI, 30)
    assert codes(cfg, "DR-PATEL", "OFFICE-B", FRI.replace(hour=9), "sick_visit") == []
    assert codes(cfg, "DR-MORALES", "OFFICE-B", FRI.replace(hour=9), "sick_visit") == []
    clash = busy("APT-Y", "DR-MORALES", "P-TEST", FRI.replace(hour=9), 30)
    assert codes(cfg, "DR-PATEL", "OFFICE-B", FRI.replace(hour=9), "sick_visit", busy=[other, clash]) == [
        ErrorCode.PATIENT_DOUBLE_BOOKED
    ]


def test_reschedule_can_ignore_its_own_appointment(cfg):
    own = busy("APT-OWN", "DR-PATEL", "P-TEST", datetime(2026, 10, 20, 9, 0), 60)
    start = datetime(2026, 10, 20, 9, 30)
    assert codes(cfg, "DR-PATEL", "OFFICE-A", start, "sick_visit", busy=[own]) == [ErrorCode.SLOT_TAKEN]
    assert codes(cfg, "DR-PATEL", "OFFICE-A", start, "sick_visit", busy=[own], ignore="APT-OWN") == []


# --- Candidate generation and config validation

def test_candidates_for_johnson_are_exactly_friday_2pm(cfg):
    johnson, skin = cfg.provider("DR-JOHNSON"), cfg.appointment_type("skin_check")
    assert candidate_starts(cfg, johnson, date(2026, 10, 30), skin) == [("OFFICE-A", FRI)]
    assert candidate_starts(cfg, johnson, date(2026, 10, 29), skin) == []


def test_config_rejects_unknown_office():
    raw = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    raw["providers"][0]["availability"][0]["office"] = "OFFICE-Z"
    with pytest.raises(ValidationError, match="unknown office"):
        PracticeConfig.model_validate(raw)
