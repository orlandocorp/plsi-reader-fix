"""Synthetic mothership packages only. No real subject, and nothing is sent."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from plsi.errors import GateFailed, NotFound, ValidationError
from plsi.files import atomic_write_json, hash_json, sha256_hex
from plsi.life.store import init_instance
from plsi.map import registry
from plsi.map.models import CoordinateValue, SeriesPoint, SetItem, StateDocument
from plsi.mothership.enroll import current_consent, pseudonym
from plsi.mothership import gate
from plsi.mothership.pack import pack, record_path, recorded
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


# the root keys of a contribution the Mother accepts (no `minimization`: it refused one with unknown_field)
WIRE_ROOT = {"kind", "schema_version", "contributor_id", "consent_version", "packed", "kinds", "subject",
             "states", "scores", "life_events"}


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
    assert "minimization" not in doc and set(doc) <= WIRE_ROOT
    assert recorded(INSTANCE, result["package_hash"]) == {"maps": ["cardiometabolic", "sleep_activity"], "kinds": ["states"],
                                                          "omitted_subject_fields": ["ethnicity"], "decimals": 0}
    assert result["minimization_record"] == str(record_path(INSTANCE, result["package_hash"]))
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
    assert "minimization" not in doc
    assert recorded(INSTANCE, hash_json(doc))["decimals"] == 1 and recorded(INSTANCE, hash_json(doc))["maps"] is None
    _, excluded = _pack(tmp_path, exclude_maps=["care_context"])
    assert "care_context" not in {state["map_id"] for state in excluded["states"]}
    assert "cardiometabolic" in recorded(INSTANCE, hash_json(excluded))["maps"]
    assert "care_context" not in recorded(INSTANCE, hash_json(excluded))["maps"]
    assert excluded["subject"]["ethnicity"] == "european"
    plain = pack(INSTANCE, out=str(tmp_path / "plain.json"))
    plain_doc = json.loads((tmp_path / "plain.json").read_text(encoding="utf-8"))
    assert _value(plain_doc, "cardiometabolic", "sbp")["value"] == 120.6
    assert _value(plain_doc, "sleep_activity", "resting_heart_rate")["series"][0]["value"] == 55.13
    assert "minimization" not in plain_doc and plain["package_hash"] == hash_json(plain_doc)
    assert recorded(INSTANCE, plain["package_hash"])["decimals"] is None


def test_unknown_subject_field_and_unknown_map_are_refused(home, tmp_path):
    with pytest.raises(ValidationError) as caught:
        pack(INSTANCE, out=str(tmp_path / "no.json"), omit_subject_fields=["email"])
    assert caught.value.code == "UNKNOWN_SUBJECT_FIELD"
    with pytest.raises(NotFound) as missing:
        pack(INSTANCE, out=str(tmp_path / "no.json"), maps="not-a-map")
    assert missing.value.code == "MAP_NOT_FOUND"
    assert sha256_hex(SECRET) == pseudonym(SECRET)
    assert "PLSI_HOME" in os.environ


def test_every_pack_option_keeps_minimization_off_the_wire(home, tmp_path):
    variants = [{}, {"maps": "cardiometabolic"}, {"exclude_maps": ["care_context"]}, {"kinds": "states"},
                {"omit_subject_fields": ["ethnicity", "sex"]}, {"decimals": 2}, {"precision": "int"}]
    for n, options in enumerate(variants):
        out = tmp_path / f"variant-{n}.json"
        result = pack(INSTANCE, out=str(out), **options)
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert "minimization" not in doc and set(doc) <= WIRE_ROOT
        assert {"kind", "schema_version", "contributor_id", "consent_version", "packed", "kinds", "subject"} <= set(doc)
        assert gate.violations(doc, INSTANCE, json.loads((instance_dir(INSTANCE) / "mothership" / "enrollment.json")
                                                         .read_text(encoding="utf-8")), doc["consent_version"]) == []
        assert check(str(out), INSTANCE, result["package_hash"])["ok"] is True


def test_a_package_carrying_minimization_fails_the_local_gate_like_the_mother(home, tmp_path):
    result, doc = _pack(tmp_path, kinds="states")
    doc["minimization"] = recorded(INSTANCE, result["package_hash"])
    (tmp_path / "old-style.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(GateFailed) as caught:
        check(str(tmp_path / "old-style.json"), INSTANCE)
    position = list(doc).index("minimization")
    assert {"path": f"#unknown[{position}]", "rule": "unknown_field"} in caught.value.violations


def test_check_still_enforces_the_local_minimization_record(home, tmp_path):
    result, doc = _pack(tmp_path, maps="cardiometabolic", kinds="states", omit_subject_fields=["ethnicity"])
    assert check(str(tmp_path / "package.json"), INSTANCE)["ok"] is True
    record = record_path(INSTANCE, result["package_hash"])
    entry = json.loads(record.read_text(encoding="utf-8"))
    # a record that asks for less than the file carries: the rules name what is not minimized
    entry["minimization"] = {"maps": ["sleep_activity"], "kinds": ["scores"], "omitted_subject_fields": ["sex"],
                             "decimals": None}
    record.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(GateFailed) as caught:
        check(str(tmp_path / "package.json"), INSTANCE)
    rules = {(v["path"], v["rule"]) for v in caught.value.violations}
    assert {("states[0].map_id", "not_in_maps"), ("kinds", "not_in_kinds"), ("subject.sex", "not_omitted")} <= rules
    # a malformed record fails too
    entry["minimization"] = {"maps": "cardiometabolic"}
    record.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(GateFailed):
        check(str(tmp_path / "package.json"), INSTANCE)
    # a record of another package is refused
    entry = {"package_hash": "sha256:" + "0" * 64, "minimization": None}
    record.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(ValidationError) as wrong:
        check(str(tmp_path / "package.json"), INSTANCE)
    assert wrong.value.code == "BAD_MINIMIZATION"
    # no record at all: the minimized file is not the unminimized pack, so it is a mismatch
    record.unlink()
    with pytest.raises(ValidationError) as missing:
        check(str(tmp_path / "package.json"), INSTANCE)
    assert missing.value.code == "PACKAGE_MISMATCH"


def test_hash_is_stable_and_covers_only_the_wire_document(home, tmp_path):
    first = pack(INSTANCE, out=str(tmp_path / "a.json"), maps="cardiometabolic,sleep_activity", decimals=1)
    second = pack(INSTANCE, out=str(tmp_path / "b.json"), maps="sleep_activity,cardiometabolic", precision="1dp")
    assert first["package_hash"] == second["package_hash"]
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
    doc = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    assert first["package_hash"] == hash_json(doc)
    assert check(str(tmp_path / "a.json"), INSTANCE, first["package_hash"])["package_hash"] == first["package_hash"]
    other = pack(INSTANCE, out=str(tmp_path / "c.json"), maps="cardiometabolic,sleep_activity", decimals=0)
    assert other["package_hash"] != first["package_hash"]
    with pytest.raises(ValidationError) as changed:
        check(str(tmp_path / "a.json"), INSTANCE, other["package_hash"])
    assert changed.value.code == "HASH_MISMATCH"
