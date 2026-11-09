"""Synthetic Oura API v2 pages only. Shapes follow the public v2 docs. No real account or ring data."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from plsi.errors import ValidationError
from plsi.life.oura import export

TOKEN = "NEXT-TOKEN-SECRET"
EMAIL = "synth@example.com"
DEVICE = "RING-SECRET"
SERIES = "HR-SERIES-SECRET"


def _page(rows: list[dict]) -> str:
    return json.dumps({"data": rows, "next_token": TOKEN})


def _night(day: str, ident: str, hours: float | None = 8, hr: float | None = 50, hrv: float | None = 40,
           kind: str = "long_sleep") -> dict:
    return {"id": ident, "day": day, "type": kind, "total_sleep_duration": None if hours is None else int(round(hours * 3600)),
            "lowest_heart_rate": hr, "average_hrv": hrv, "heart_rate": {"items": [SERIES]}, "device": DEVICE}


def _steps(day: str, ident: str, steps: float | None = 1000) -> dict:
    return {"id": ident, "day": day, "steps": steps}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(root: Path, as_of: str | None = "2026-03-15") -> tuple[dict, list[dict], dict]:
    out, side = root / "monthly.csv", root / "oura-provenance.json"
    kwargs = {"as_of": __import__("datetime").date.fromisoformat(as_of)} if as_of else {}
    result = export(str(root / "raw"), str(out), str(side), **kwargs)
    rows = list(csv.DictReader(out.read_text(encoding="utf-8").splitlines()))
    doc = json.loads(side.read_text(encoding="utf-8"))
    return result, rows, doc


def _fill(root: Path) -> None:
    raw = root / "raw"
    jan = []
    feb = []
    mar = []
    for day in range(1, 16):
        jan.append(_night(f"2026-01-{day:02d}", f"sleep-jan-{day:02d}"))
        jan[-1] if day < 15 else None
    jan.append(_night("2026-01-01", "sleep-jan-01"))  # same id, same metrics: pagination overlap
    jan.append(_night("2026-01-01", "nap-jan-01", hours=1, kind="late_nap"))
    jan.append(_night("2026-01-02", "rest-jan-02", hours=2, kind="rest"))
    jan.append(_night("2026-01-03", "deleted-jan-03", hours=9, kind="deleted"))
    jan[14] = _night("2026-01-15", "sleep-jan-15", hours=17, hr=20, hrv=0)
    for day in range(1, 15):
        feb.append(_night(f"2026-02-{day:02d}", f"sleep-feb-{day:02d}"))
    for day in range(1, 17):
        mar.append(_night(f"2026-03-{day:02d}", f"sleep-mar-{day:02d}"))
    _write(raw, "sleep/page-a.json", _page(jan[:10]))
    _write(raw, "sleep/page-b.json", _page(jan[10:] + feb + mar))
    activity = [_steps(f"2026-01-{day:02d}", f"act-jan-{day:02d}") for day in range(1, 16)]
    activity += [_steps(f"2026-02-{day:02d}", f"act-feb-{day:02d}") for day in range(1, 15)]
    _write(raw, "daily_activity/page.json", _page(activity))
    _write(raw, "daily_sleep/score.json", _page([
        {"id": "score-1", "day": "2026-01-01", "score": 90, "total_sleep_duration": 999999},
    ]))
    _write(raw, "personal_info.json", json.dumps({"email": EMAIL, "device_id": DEVICE}))


def test_closed_months_naps_duplicates_and_bounds(tmp_path: Path) -> None:
    _fill(tmp_path)
    result, rows, doc = _run(tmp_path)
    header = (tmp_path / "raw").parent.joinpath("monthly.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == "date,month,metric,value,unit,n,source"
    by_key = {(row["month"], row["metric"]): row for row in rows}
    sleep = by_key[("2026-01", "sleep_duration")]
    assert sleep["date"] == "2026-01-31" and sleep["n"] == "15" and sleep["unit"] == "h" and sleep["source"] == "oura"
    assert float(sleep["value"]) == pytest.approx((14 * 8 + 17) / 15)
    assert by_key[("2026-01", "resting_heart_rate")]["unit"] == "/min"
    assert float(by_key[("2026-01", "resting_heart_rate")]["value"]) == pytest.approx((14 * 50 + 20) / 15)
    assert by_key[("2026-01", "hrv_rmssd")]["unit"] == "ms"
    assert float(by_key[("2026-01", "hrv_rmssd")]["value"]) == pytest.approx((14 * 40) / 15)
    assert by_key[("2026-01", "steps")]["unit"] == "{steps}/d" and by_key[("2026-01", "steps")]["value"] == "1000"
    assert all(row["metric"] != "hrv_sdnn" for row in rows)
    assert ("2026-02", "sleep_duration") not in by_key and ("2026-03", "sleep_duration") not in by_key
    reasons = {(item["month"], item["metric"]): item["reason"] for item in doc["excluded_months"]}
    assert reasons[("2026-02", "sleep_duration")] == "fewer_than_15_days"
    assert reasons[("2026-02", "sleep_duration")] and doc["excluded_months"]
    feb = next(item for item in doc["excluded_months"] if item["month"] == "2026-02" and item["metric"] == "sleep_duration")
    assert feb["n"] == 14
    march = next(item for item in doc["excluded_months"] if item["month"] == "2026-03" and item["metric"] == "sleep_duration")
    assert march["reason"] == "open_month" and march["n"] == 16
    assert doc["endpoints"]["sleep"]["duplicate_ids"] == 1
    assert doc["endpoints"]["sleep"]["skipped_types"]["late_nap"] == 1
    assert doc["endpoints"]["sleep"]["skipped_types"]["rest"] == 1
    assert doc["endpoints"]["sleep"]["skipped_types"]["deleted"] == 1
    assert doc["endpoints"]["daily_sleep"]["used_for_metrics"] is False
    flagged = {(item["day"], item.get("metric")) for item in doc["flags"] if item["flag"] == "bounds"}
    assert ("2026-01-15", "sleep_duration") in flagged
    assert ("2026-01-15", "resting_heart_rate") in flagged
    assert ("2026-01-15", "hrv_rmssd") in flagged
    blob = json.dumps(doc) + json.dumps(result) + (tmp_path / "monthly.csv").read_text(encoding="utf-8")
    for secret in (TOKEN, EMAIL, DEVICE, SERIES, "999999"):
        assert secret not in blob
    assert doc["endpoints"]["skipped_files"] == 1
    assert {item["endpoint"] for item in doc["files"]} == {"sleep", "daily_activity", "daily_sleep"}
    assert all(len(item["sha256"]) == 64 for item in doc["files"])


def test_long_sleep_beats_a_longer_other_session_and_nulls_are_per_field(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    nights = [_night(f"2026-01-{day:02d}", f"ok-{day}", hours=8, hr=50, hrv=40) for day in range(1, 15)]
    nights.append(_night("2026-01-01", "short-sleep", hours=6, hr=80, hrv=10, kind="sleep"))
    nights.append(_night("2026-01-16", "hr-only", hours=None, hr=55, hrv=None))
    nights.append(_night("2026-01-17", "pair-a", hours=2, hr=48, hrv=30, kind="sleep"))
    nights.append(_night("2026-01-17", "pair-b", hours=5, hr=47, hrv=33, kind="sleep"))
    nights.append(_night("2026-01-17", "pair-nap", hours=3, kind="late_nap"))
    _write(raw, "sleep/nights.json", _page(nights))
    _result, rows, doc = _run(tmp_path, as_of="2026-02-01")
    sleep = next(row for row in rows if row["metric"] == "sleep_duration")
    # Days 1-14 stay 8 h (long_sleep beats the 6 h session). Day 16 has no duration. Day 17 uses 5 h, not 2+5+nap.
    assert sleep["n"] == "15"
    assert float(sleep["value"]) == pytest.approx((14 * 8 + 5) / 15)
    hr = next(row for row in rows if row["metric"] == "resting_heart_rate")
    assert hr["n"] == "16"  # day 16 contributes heart rate only
    assert doc["endpoints"]["sleep"]["null_fields"]["total_sleep_duration"] == 1
    assert doc["endpoints"]["sleep"]["null_fields"]["average_hrv"] == 1
    assert any(item["flag"] == "multiple_main" and item["day"] == "2026-01-17" for item in doc["flags"])
    assert not any(item["flag"] == "multiple_main" and item["day"] == "2026-01-01" for item in doc["flags"])


def test_conflicting_ids_and_two_step_documents_are_not_summed(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    nights = [_night(f"2026-01-{day:02d}", f"s-{day}") for day in range(1, 17)]
    _write(raw, "sleep/a.json", _page(nights))
    _write(raw, "sleep/b.json", _page([_night("2026-01-02", "s-2", hours=4)]))
    activity = [_steps(f"2026-01-{day:02d}", f"a-{day}", 1000) for day in range(1, 17)]
    activity.append(_steps("2026-01-03", "other-id", 5000))
    _write(raw, "daily_activity/a.json", _page(activity))
    _result, rows, doc = _run(tmp_path, as_of="2026-02-01")
    assert doc["endpoints"]["sleep"]["id_conflicts"] == 1
    sleep = next(row for row in rows if row["metric"] == "sleep_duration")
    assert sleep["n"] == "15" and sleep["value"] == "8"  # the conflicting 4 h copy is not used
    steps = next(row for row in rows if row["metric"] == "steps")
    assert steps["n"] == "15" and steps["value"] == "1000"
    assert doc["endpoints"]["daily_activity"]["excluded_days"] == 1
    assert any(item["flag"] == "multiple_activity" and item["day"] == "2026-01-03" for item in doc["flags"])


def test_directory_limits_refuse_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import plsi.life.oura as oura

    raw = tmp_path / "raw" / "sleep"
    raw.mkdir(parents=True)
    (raw / "page.json").write_text(_page([_night("2026-01-01", "s")]), encoding="utf-8")
    out = tmp_path / "monthly.csv"
    original_bytes = oura.MAX_XML_BYTES
    monkeypatch.setattr(oura, "MAX_XML_BYTES", 40)
    with pytest.raises(ValidationError) as caught:
        export(str(tmp_path / "raw"), str(out), str(tmp_path / "side.json"), as_of=__import__("datetime").date(2026, 2, 1))
    assert caught.value.code == "OURA_EXPORT_LIMIT" and not out.exists()
    monkeypatch.setattr(oura, "MAX_XML_BYTES", original_bytes)
    monkeypatch.setattr(oura, "MAX_ATTRIBUTE_CHARS", 12)
    with pytest.raises(ValidationError) as caught:
        export(str(tmp_path / "raw"), str(out), str(tmp_path / "side.json"), as_of=__import__("datetime").date(2026, 2, 1))
    assert caught.value.code == "OURA_EXPORT_LIMIT" and SERIES not in str(caught.value) and not out.exists()
    monkeypatch.setattr(oura, "MAX_ATTRIBUTE_CHARS", oura.MAX_ATTRIBUTE_CHARS)
    (raw / "page.json").write_bytes(b"\x1f\x8b" + b"not-json")
    with pytest.raises(ValidationError) as caught:
        export(str(tmp_path / "raw"), str(out), str(tmp_path / "side.json"), as_of=__import__("datetime").date(2026, 2, 1))
    assert caught.value.code == "OURA_EXPORT_LIMIT"


def test_default_as_of_uses_the_test_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "raw" / "sleep"
    raw.mkdir(parents=True)
    nights = [_night(f"2026-01-{day:02d}", f"s-{day}") for day in range(1, 16)]
    (raw / "page.json").write_text(_page(nights), encoding="utf-8")
    monkeypatch.setenv("PLSI_TEST_MODE", "1")
    monkeypatch.setenv("PLSI_NOW", "2026-01-20T00:00:00+00:00")
    _result, rows, doc = _run(tmp_path, as_of=None)
    assert rows == []
    assert doc["as_of"] == "2026-01-20"
    assert any(item["reason"] == "open_month" and item["month"] == "2026-01" for item in doc["excluded_months"])
