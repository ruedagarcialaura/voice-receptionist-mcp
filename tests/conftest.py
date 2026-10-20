from datetime import date, datetime
from pathlib import Path

import pytest

from ehr_server.config import load_config
from ehr_server.ehr import EHR
from ehr_server.rules import Patient

RULES_PATH = Path(__file__).resolve().parent.parent / "ehr_server" / "practice_rules.yaml"

# Frozen clock: Monday 2026-10-19 09:00. With the seed data this means:
#   APT-0001 Maria  / Dr. Patel   Tue 2026-10-20 10:00 OFFICE-A
#   APT-0002 James  / Dr. Johnson Fri 2026-10-23 14:00 OFFICE-A  (Johnson's only slot that week)
#   APT-0003 Lily   / Dr. Okafor  Thu 2026-10-22 10:00 OFFICE-B
NOW = datetime(2026, 10, 19, 9, 0)


@pytest.fixture
def cfg():
    return load_config(RULES_PATH)


@pytest.fixture
def ehr(tmp_path, cfg):
    e = EHR(tmp_path / "ehr.db", cfg, now_fn=lambda: NOW)
    e.reset()
    return e


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_patient(**overrides) -> Patient:
    fields = dict(
        id="P-TEST", first_name="Test", last_name="Patient", date_of_birth=date(1985, 3, 14),
        insurance_payer="BlueCross", coverage_active=True, is_new_patient=False,
    )
    fields.update(overrides)
    return Patient(**fields)
