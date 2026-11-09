"""Synthetic mothership packages only. No real subject, and nothing is sent."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from plsi.errors import NotFound, ValidationError
from plsi.files import atomic_write_json, hash_json, sha256_hex
from plsi.life.store import init_instance
from plsi.map import registry
from plsi.map.models import CoordinateValue, SeriesPoint, SetItem, StateDocument
from plsi.mothership.enroll import current_consent, pseudonym
from plsi.mothership.pack import pack
from plsi.mothership.send import check
from plsi.paths import instance_dir
from plsi.vocab import Ethnicity, Relation, Sex, ValueStatus

INSTANCE = "packdemo"
SECRET = "synthetic-contributor-secret"


def _state(map_id: str, coordinates: dict) -> None:
    contract = registry.get(map_id)
    registry.snapshot(INSTANCE, contract)
    subject_id = json.loads((instance_dir(INSTANCE) / "subject.json").read_text(encoding="utf-8"))["subject_id"]
    doc = StateDocument(
        subject_id=subject_id, map_id=map_id, map_version=contract.map_version, map_hash=contract.compute_hash(),
        as_of=datetime(2026, 6, 15, tzinfo=UTC), generated_at=datetime(2026, 6, 15, 12, tzinfo=UTC),
        coordinates=coordinates,
    ).sealed()
    folder = instance_dir(INSTANCE) / "states" / map_id
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_json(folder / "latest.json", doc.dump())


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PLSI_HOME", str(tmp_path))
    monkeypatch.setenv("PLSI_TEST_MODE", "1")
    monkeypatch.setenv("PLSI_NOW", "2026-06-15T12:00:00+00:00")
    init_instance(INSTANCE, birth_year=1970, sex=Sex.M, relation=Relation.SELF, ethnicity=Ethnicity.EUROPEAN)
    folder = instance_dir(INSTANCE) / "mothership"
    folder.mkdir(parents=True)
    atomic_write_json(folder / "install.json", {"token": "synthetic-install-token", "contributor_secret": SECRET,
                                                "install_id": "synthetic-install"}, private=True)
    atomic_write_json(folder / "enrollment.json", {
        "contributor_id": pseudonym(SECRET), "kinds": ["states", "scores", "life_events"],
        "consent_version": current_consent(), "joined_at": "2026-06-15T12:00:00+00:00", "status": "active"})
    _state("cardiometabolic", {"sbp": CoordinateValue(value=120.6, unit="mm[Hg]", status=ValueStatus.PRESENT,
                                                      observed_at=datetime(2026, 6, 1, tzinfo=UTC))})
    _state("sleep_activity", {"resting_heart_rate": CoordinateValue(
        value=55.13, unit="/min", status=ValueStatus.PRESENT, observed_at=datetime(2026, 6, 2, tzinfo=UTC),
        series=[SeriesPoint(t=datetime(2026, 6, 1, tzinfo=UTC), value=55.12),
                SeriesPoint(t=datetime(2026, 6, 20, tzinfo=UTC), value=55.14)])})
    _state("care_context", {"active_medications": CoordinateValue(
        value=[SetItem(text="test-drug-a", event_id="evt_0123abcd")], status=ValueStatus.PRESENT)})
    _state("diet", {"energy_intake": CoordinateValue(value=None, unit="kcal/d", status=ValueStatus.PRESENT)})
    return tmp_path


def _pack(tmp_path, **kwargs):
    out = tmp_path / "package.json"
    result = pack(INSTANCE, out=str(out), **kwargs)
    doc = json.loads(out.read_text(encoding="utf-8"))
    return result, doc


def _value(doc, map_id, coordinate):
    state = next(item for item in doc["states"] if item["map_id"] == map_id)
    return state["coordinates"][coordinate]


def test_minimized_package_is_what_check_rebuilds(home, tmp_path):
    result, doc = _pack(tmp_path, maps="sleep_activity,cardiometabolic", kinds="states",
                        omit_subject_fields=["ethnicity"], decimals=0)
    assert doc["kinds"] == ["states"] and "scores" not in doc and "life_events" not in doc
    assert [state["map_id"] for state in doc["states"]] == ["cardiometabolic", "sleep_activity"]
    assert "care_context" not in {state["map_id"] for state in doc["states"]}
    assert "diet" not in {state["map_id"] for state in doc["states"]}
    assert set(doc["subject"]) == {"sex", "age_band"} and doc["subject"]["age_band"] == "55-59" and doc["subject"]["sex"] == "M"
    assert _value(doc, "cardiometabolic", "sbp")["value"] == 121
    heart = _value(doc, "sleep_activity", "resting_heart_rate")
    assert heart["value"] == 55 and heart["series"] == [{"t": "2026-06", "value": 55, "n": 2}]
    assert doc["minimization"] == {"maps": ["cardiometabolic", "sleep_activity"], "kinds": ["states"],
                                    "omitted_subject_fields": ["ethnicity"], "decimals": 0}
    assert result["package_hash"] == hash_json(doc)
    again = pack(INSTANCE, out=str(tmp_path / "package-again.json"), maps="cardiometabolic,sleep_activity",
                 kinds="states", omit_subject_fields=["ethnicity"], precision="int")
    assert again["package_hash"] == result["package_hash"]
    assert (tmp_path / "package-again.json").read_bytes() == (tmp_path / "package.json").read_bytes()
    checked = check(str(tmp_path / "package.json"), INSTANCE)
    assert checked["ok"] is True and checked["package_hash"] == result["package_hash"]
    doc["states"][0]["coordinates"]["sbp"]["value"] = 120
    (tmp_path / "package.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        check(str(tmp_path / "package.json"), INSTANCE)
    assert caught.value.code == "PACKAGE_MISMATCH"


def test_one_decimal_exclude_map_and_default_precision(home, tmp_path):
    _, doc = _pack(tmp_path, precision="1dp")
    assert _value(doc, "cardiometabolic", "sbp")["value"] == 120.6
    heart = _value(doc, "sleep_activity", "resting_heart_rate")
    assert heart["value"] == 55.1 and heart["series"][0]["value"] == 55.1
    assert "care_context" in {state["map_id"] for state in doc["states"]}
    assert "diet" not in {state["map_id"] for state in doc["states"]}
    assert doc["minimization"]["decimals"] == 1 and doc["minimization"]["maps"] is None
    _, excluded = _pack(tmp_path, exclude_maps=["care_context"])
    assert "care_context" not in {state["map_id"] for state in excluded["states"]}
    assert "cardiometabolic" in excluded["minimization"]["maps"]
    assert "care_context" not in excluded["minimization"]["maps"]
    assert excluded["subject"]["ethnicity"] == "european"
    plain = pack(INSTANCE, out=str(tmp_path / "plain.json"))
    plain_doc = json.loads((tmp_path / "plain.json").read_text(encoding="utf-8"))
    assert _value(plain_doc, "cardiometabolic", "sbp")["value"] == 120.6
    assert _value(plain_doc, "sleep_activity", "resting_heart_rate")["series"][0]["value"] == 55.13
    assert plain_doc["minimization"]["decimals"] is None and plain["package_hash"] == hash_json(plain_doc)


def test_unknown_subject_field_and_unknown_map_are_refused(home, tmp_path):
    with pytest.raises(ValidationError) as caught:
        pack(INSTANCE, out=str(tmp_path / "no.json"), omit_subject_fields=["email"])
    assert caught.value.code == "UNKNOWN_SUBJECT_FIELD"
    with pytest.raises(NotFound) as missing:
        pack(INSTANCE, out=str(tmp_path / "no.json"), maps="not-a-map")
    assert missing.value.code == "MAP_NOT_FOUND"
    assert sha256_hex(SECRET) == pseudonym(SECRET)
    assert "PLSI_HOME" in os.environ
