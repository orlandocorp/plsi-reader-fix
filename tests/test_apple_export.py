"""Synthetic Apple Health exports only. No patient file, identifier, or real measurement is used."""

from __future__ import annotations

import csv
import json
import stat
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from plsi.errors import NotFound, ValidationError
from plsi.life.apple import export
from plsi.life.apple_sleep import sessions
from plsi.life.normalize import normalize_unit
from plsi.map.calc import convert

T_SYS = "HKQuantityTypeIdentifierBloodPressureSystolic"
T_DIA = "HKQuantityTypeIdentifierBloodPressureDiastolic"
T_BP = "HKCorrelationTypeIdentifierBloodPressure"
T_SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
T_STEPS = "HKQuantityTypeIdentifierStepCount"
T_SDNN = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
T_RMSSD = "HKQuantityTypeIdentifierHeartRateVariabilityRMSSD"
T_VO2 = "HKQuantityTypeIdentifierVO2Max"
T_HR = "HKQuantityTypeIdentifierHeartRate"
CORE = "HKCategoryValueSleepAnalysisAsleepCore"
DEEP = "HKCategoryValueSleepAnalysisAsleepDeep"
REM = "HKCategoryValueSleepAnalysisAsleepREM"
LEGACY = "HKCategoryValueSleepAnalysisAsleep"
INBED = "HKCategoryValueSleepAnalysisInBed"
AWAKE = "HKCategoryValueSleepAnalysisAwake"

_DTD = """<!DOCTYPE HealthData [
<!ELEMENT HealthData (ExportDate|Me|Record|Correlation|MetadataEntry)*>
<!ELEMENT ExportDate EMPTY>
<!ELEMENT Me EMPTY>
<!ATTLIST Me HKCharacteristicTypeIdentifierDateOfBirth CDATA #IMPLIED>
<!ELEMENT Record (MetadataEntry*)>
<!ATTLIST Record type CDATA #IMPLIED sourceName CDATA #IMPLIED unit CDATA #IMPLIED
  startDate CDATA #IMPLIED endDate CDATA #IMPLIED value CDATA #IMPLIED device CDATA #IMPLIED>
<!ELEMENT Correlation (Record|MetadataEntry)*>
<!ATTLIST Correlation type CDATA #IMPLIED sourceName CDATA #IMPLIED startDate CDATA #IMPLIED endDate CDATA #IMPLIED>
<!ELEMENT MetadataEntry EMPTY>
<!ATTLIST MetadataEntry key CDATA #IMPLIED value CDATA #IMPLIED>
]>"""


def _xml(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{_DTD}\n<HealthData locale="en_US">\n{body}\n</HealthData>\n'


def _zip(tmp: Path, body: str, extra: dict[str, str] | None = None, name: str = "synthetic-export.zip") -> Path:
    path = tmp / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("apple_health_export/export.xml", _xml(body))
        for member, text in (extra or {}).items():
            zf.writestr(member, text)
    return path


def _rec(rtype: str, value: str, start: str, end: str | None = None, source: str = "Synth Watch",
         unit: str = "", device: str = "", inner: str = "") -> str:
    end = end or start
    attrs = f'type="{rtype}" sourceName="{source}" startDate="{start}" endDate="{end}" value="{value}"'
    if unit:
        attrs += f' unit="{unit}"'
    if device:
        attrs += f' device="{device}"'
    return f"<Record {attrs}>{inner}</Record>"


def _bp(systolic: str, diastolic: str, start: str, source: str = "Cuff Alpha", device: str = "", repeat: bool = False) -> str:
    sys = _rec(T_SYS, systolic, start, source=source, unit="mmHg", device=device)
    dia = _rec(T_DIA, diastolic, start, source=source, unit="mmHg", device=device)
    block = (f'<Correlation type="{T_BP}" sourceName="{source}" startDate="{start}" endDate="{start}">\n'
             f"{sys}\n{dia}\n</Correlation>\n")
    return block + (f"{sys}\n{dia}\n" if repeat else "")


def _run(tmp: Path, body: str, **kw) -> tuple[dict, str, dict | None]:
    path = _zip(tmp, body, kw.get("extra"))
    out, side = tmp / "daily.csv", tmp / "apple-provenance.json"
    result = export(str(path), str(out), provenance=str(side) if kw.get("provenance", True) else None)
    text = out.read_text(encoding="utf-8")
    doc = json.loads(side.read_text(encoding="utf-8")) if kw.get("provenance", True) else None
    return result, text, doc


def _rows(doc: dict, metric: str) -> list[dict]:
    return [row for row in doc["rows"] if row["metric"] == metric]


def _span(start: str, end: str, coarse: bool = False) -> dict:
    a = datetime.strptime(start, "%Y-%m-%d %H:%M:%S %z")
    b = datetime.strptime(end, "%Y-%m-%d %H:%M:%S %z")
    return {"start": a, "end": b, "start_raw": start, "end_raw": end, "offsets": {a.strftime("%z"), b.strftime("%z")},
            "coarse": coarse}


def test_csv_shape_kinds_and_no_provenance_key(tmp_path: Path) -> None:
    body = "\n".join([
        _rec(T_STEPS, "100", "2026-02-01 08:00:00 -0500", "2026-02-01 09:00:00 -0500", unit="count"),
        _rec(T_STEPS, "100", "2026-02-01 08:00:00 -0500", "2026-02-01 09:00:00 -0500", unit="count"),
        _rec(T_STEPS, "50", "2026-02-01 09:00:00 -0500", "2026-02-01 10:00:00 -0500", unit="count"),
        _rec(T_SDNN, "40", "2026-02-01 07:00:00 -0500", "2026-02-01 07:01:00 -0500", unit="ms"),
        _rec(T_SDNN, "60", "2026-02-01 08:00:00 -0500", "2026-02-01 08:01:00 -0500", unit="ms"),
        _rec(T_STEPS, "7", "2026-02-01 08:00:00 -0500", "2026-02-01 08:30:00 -0500", source="Synth iPhone", unit="count"),
        _rec(T_STEPS, "9", "2026-02-01 11:00:00 +0000", "2026-02-01 12:00:00 +0000", source="Step Logger", unit="count"),
    ])
    path = _zip(tmp_path, body)
    result = export(str(path), str(tmp_path / "daily.csv"))
    text = (tmp_path / "daily.csv").read_text(encoding="utf-8")
    assert text.splitlines()[0] == "date,metric,value,unit,source"
    assert list(csv.DictReader(text.splitlines()))
    assert set(result) == {"out", "sha256", "rows", "metrics", "first", "last", "skipped_types"}
    assert not (tmp_path / "apple-provenance.json").exists()
    by_source = {row["source"]: row for row in csv.DictReader(text.splitlines()) if row["metric"] == "steps"}
    assert by_source["watch"]["value"] == "250"  # identical step samples are summed; point readings are the ones de-duplicated
    assert by_source["phone"]["value"] == "7"
    assert by_source["other"]["value"] == "9"  # a high step count does not become a watch
    assert "Synth" not in text and "Step Logger" not in text
    sdnn = next(row for row in csv.DictReader(text.splitlines()) if row["metric"] == "hrv_sdnn")
    assert sdnn["value"] == "50" and sdnn["unit"] == "ms" and sdnn["source"] == "watch"


def test_apps_hrv_methods_units_and_duplicate_bp(tmp_path: Path) -> None:
    body = "\n".join([
        _rec(T_SDNN, "40", "2026-02-02 07:00:00 -0500", "2026-02-02 07:01:00 -0500", unit="ms"),
        _rec(T_SDNN, "40", "2026-02-02 07:00:00 -0500", "2026-02-02 07:01:00 -0500", unit="ms"),
        _rec(T_SDNN, "80", "2026-02-02 08:00:00 -0500", "2026-02-02 08:01:00 -0500", unit="ms"),
        _rec(T_RMSSD, "40", "2026-02-02 07:05:00 -0500", "2026-02-02 07:06:00 -0500", unit="ms"),
        _rec(T_VO2, "47.5", "2026-02-02 18:00:00 -0500", unit="mL/min·kg"),
        _rec(T_HR, "70", "2026-02-02 09:00:00 -0500", "2026-02-02 09:01:00 -0500", unit="count/min"),
        _bp("120", "80", "2026-02-02 08:15:00 -0500", repeat=True),
        _bp("120", "80", "2026-02-02 21:00:00 -0500"),
        _rec(T_SYS, "119", "2026-02-02 22:00:00 -0500", source="Cuff Alpha", unit="mmHg"),
        _rec(T_DIA, "79", "2026-02-02 22:00:00 -0500", source="Cuff Alpha", unit="mmHg"),
        (f'<Correlation type="{T_BP}" sourceName="Cuff Alpha" startDate="2026-02-02 22:00:00 -0500" endDate="2026-02-02 22:00:00 -0500">'
         + _rec(T_SYS, "119", "2026-02-02 22:00:00 -0500", source="Cuff Alpha", unit="mmHg")
         + _rec(T_DIA, "79", "2026-02-02 22:00:00 -0500", source="Cuff Alpha", unit="mmHg")
         + "</Correlation>"),
        _rec(T_SYS, "120", "2026-02-02 08:15:00 -0500", source="Cuff Beta", unit="mmHg"),
        _rec(T_DIA, "80", "2026-02-02 08:15:00 -0500", source="Cuff Beta", unit="mmHg"),
    ])
    result, text, doc = _run(tmp_path, body)
    assert "HeartRateVariabilityRMSSD" not in result["skipped_types"]
    assert result["skipped_types"]["HeartRate"] == 1
    assert doc is not None and doc["local_only"] is True and doc["file_sha256"] == result["sha256"]
    assert "Cuff Alpha" not in text and "Cuff Beta" not in text
    assert doc["sources"]["other"]["app"] == "Cuff Alpha"
    assert doc["sources"]["other-2"]["app"] == "Cuff Beta"
    assert "Cuff" not in json.dumps(result)
    sdnn, rmssd = _rows(doc, "hrv_sdnn")[0], _rows(doc, "hrv_rmssd")[0]
    assert sdnn["method"] == "sdnn" and sdnn["value"] == 60  # the repeated 40 is one sample, then 80
    assert [sample["copies"] for sample in sdnn["samples"] if sample["value"] == 40] == [2]
    assert rmssd["method"] == "rmssd" and rmssd["value"] == 40
    assert sdnn["value"] != rmssd["value"] or sdnn["metric"] != rmssd["metric"]
    vo2 = _rows(doc, "vo2_max")[0]
    assert vo2["unit"] == "mL/min·kg" and vo2["unit_alias"] == "mL/min/kg" and vo2["value"] == 47.5
    morning = [row for row in _rows(doc, "bp_systolic") if row["app"] == "Cuff Alpha" and row["start"].endswith("08:15:00 -0500")]
    evening = [row for row in _rows(doc, "bp_systolic") if row["start"].endswith("21:00:00 -0500")]
    assert len(morning) == 1 and morning[0]["copies"] == 2 and morning[0]["correlation"] == "bp-1"
    assert morning[0]["flags"] == [] and evening[0]["copies"] == 1 and evening[0]["correlation"] == "bp-2"
    assert morning[0]["value"] == evening[0]["value"] == 120
    listed_first = [row for row in _rows(doc, "bp_systolic") if row["value"] == 119]
    assert len(listed_first) == 1 and listed_first[0]["copies"] == 2 and listed_first[0]["correlation"]
    beta = [row for row in doc["rows"] if row["app"] == "Cuff Beta"]
    assert {row["correlation"] for row in beta} == {None}
    assert all("unpaired" in row["flags"] for row in beta)
    paired = [row for row in doc["rows"] if row.get("correlation") == "bp-1"]
    assert {row["metric"] for row in paired} == {"bp_systolic", "bp_diastolic"}


def test_distinct_devices_ambiguous_unpaired_and_abnormal_bp(tmp_path: Path) -> None:
    secret_a = "&lt;&lt;HKDevice: serial:DEVICE-SECRET-A; hardware:Watch9,9; name:Apple Watch&gt;&gt;"
    secret_b = "&lt;&lt;HKDevice: serial:DEVICE-SECRET-B; hardware:Watch9,9; name:Apple Watch&gt;&gt;"
    meta = '<MetadataEntry key="HKMetadataKeyLatitude" value="12.345"/>' \
           '<MetadataEntry key="HKMetadataKeySyncIdentifier" value="sync-secret-value"/>' \
           '<MetadataEntry key="HKMetadataKeyInstantaneousBeats" value="beat-secret-12:03:04"/>'
    body = "\n".join([
        '<Me HKCharacteristicTypeIdentifierDateOfBirth="1900-01-01"/>',
        _rec(T_SYS, "120", "2026-03-03 08:00:00 +0000", source="Health", unit="mmHg", device=secret_a, inner=meta),
        _rec(T_SYS, "120", "2026-03-03 08:00:00 +0000", source="Health", unit="mmHg", device=secret_b),
        (f'<Correlation type="{T_BP}" sourceName="Cuff Alpha" startDate="2026-03-03 09:00:00 +0000" endDate="2026-03-03 09:00:00 +0000">'
         + _rec(T_SYS, "121", "2026-03-03 09:00:00 +0000", source="Cuff Alpha", unit="mmHg")
         + _rec(T_SYS, "122", "2026-03-03 09:00:00 +0000", source="Cuff Alpha", unit="mmHg")
         + _rec(T_DIA, "70", "2026-03-03 09:00:00 +0000", source="Cuff Alpha", unit="mmHg")
         + "</Correlation>"),
        _bp("90", "100", "2026-03-03 10:00:00 +0000"),
        _bp("0", "0", "2026-03-03 11:00:00 +0000"),
        _bp("240", "140", "2026-03-03 12:00:00 +0000"),
        _rec(T_STEPS, "-5", "2026-03-03 08:00:00 +0000", "2026-03-03 09:00:00 +0000", source="Step Logger", unit="count"),
    ])
    _result, text, doc = _run(tmp_path, body, extra={"workout-routes/route.gpx": "<gpx>LAT-99 LON-99</gpx>", "../slip-marker.txt": "SLIP-MARKER"})
    assert doc is not None
    blob = text + json.dumps(doc) + json.dumps(_result)
    for secret in ("DEVICE-SECRET-A", "DEVICE-SECRET-B", "Watch9,9", "12.345", "sync-secret-value",
                   "beat-secret", "1900-01-01", "LAT-99", "SLIP-MARKER", "synthetic-export.zip", "route.gpx"):
        assert secret not in blob
    assert not (tmp_path.parent / "slip-marker.txt").exists()
    same_time = [row for row in _rows(doc, "bp_systolic") if row["app"] == "Health"]
    assert len(same_time) == 2 and same_time[0]["identity"] != same_time[1]["identity"]
    assert all(row["copies"] == 1 and row["correlation"] is None and "unpaired" in row["flags"] for row in same_time)
    assert doc["sources"]["watch"]["app"] == "Health"  # the word watch is on the device text, not guessed from 120
    ambiguous = [row for row in doc["rows"] if row.get("correlation") == "bp-1"]
    assert len(ambiguous) == 3 and {row["value"] for row in ambiguous} == {121, 122, 70}
    assert all("uncertain" in row["flags"] for row in ambiguous)
    inconsistent = [row for row in doc["rows"] if row.get("correlation") == "bp-2"]
    assert {row["value"] for row in inconsistent} == {90, 100}
    assert all("uncertain" in row["flags"] for row in inconsistent)
    zero = [row for row in doc["rows"] if row.get("correlation") == "bp-3"]
    high = [row for row in doc["rows"] if row.get("correlation") == "bp-4"]
    assert {row["value"] for row in zero} == {0, 0} and all("abnormal" in row["flags"] for row in zero)
    assert {row["value"] for row in high} == {240, 140} and all(row["flags"] == [] for row in high)
    steps = _rows(doc, "steps")[0]
    assert steps["value"] == -5 and "abnormal" in steps["flags"] and steps["app"] == "Step Logger" and steps["source"] == "other-2"


def test_sleep_wake_day_overlap_dst_and_sources(tmp_path: Path) -> None:
    body = "\n".join([
        # stages end before midnight; InBed ends the next morning, so the wake day is the 2nd
        _rec(T_SLEEP, CORE, "2026-05-01 22:00:00 +0000", "2026-05-01 23:30:00 +0000"),
        _rec(T_SLEEP, INBED, "2026-05-01 22:00:00 +0000", "2026-05-02 07:00:00 +0000"),
        # overlap union, not a sum: 9h outer contains a 2h inner
        _rec(T_SLEEP, CORE, "2026-04-01 22:00:00 +0000", "2026-04-02 07:00:00 +0000", source="Overlap Watch"),
        _rec(T_SLEEP, DEEP, "2026-04-01 23:00:00 +0000", "2026-04-02 01:00:00 +0000", source="Overlap Watch"),
        # legacy coarse copy plus stages and a short awake gap
        _rec(T_SLEEP, LEGACY, "2026-04-10 22:00:00 +0000", "2026-04-11 07:00:00 +0000", source="Stage Watch"),
        _rec(T_SLEEP, CORE, "2026-04-10 22:00:00 +0000", "2026-04-10 23:00:00 +0000", source="Stage Watch"),
        _rec(T_SLEEP, AWAKE, "2026-04-10 23:00:00 +0000", "2026-04-10 23:20:00 +0000", source="Stage Watch"),
        _rec(T_SLEEP, REM, "2026-04-10 23:20:00 +0000", "2026-04-11 07:00:00 +0000", source="Stage Watch"),
        _rec(T_SLEEP, INBED, "2026-04-10 22:00:00 +0000", "2026-04-11 07:00:00 +0000", source="Stage Watch"),
        # three-hour gap stays two sessions unless one InBed contains both
        _rec(T_SLEEP, CORE, "2026-06-01 22:00:00 +0000", "2026-06-01 23:00:00 +0000", source="Gap Watch"),
        _rec(T_SLEEP, AWAKE, "2026-06-01 23:00:00 +0000", "2026-06-02 02:00:00 +0000", source="Gap Watch"),
        _rec(T_SLEEP, REM, "2026-06-02 02:00:00 +0000", "2026-06-02 07:00:00 +0000", source="Gap Watch"),
        _rec(T_SLEEP, CORE, "2026-06-10 22:00:00 +0000", "2026-06-10 23:00:00 +0000", source="Held Watch"),
        _rec(T_SLEEP, AWAKE, "2026-06-10 23:00:00 +0000", "2026-06-11 02:00:00 +0000", source="Held Watch"),
        _rec(T_SLEEP, REM, "2026-06-11 02:00:00 +0000", "2026-06-11 07:00:00 +0000", source="Held Watch"),
        _rec(T_SLEEP, INBED, "2026-06-10 22:00:00 +0000", "2026-06-11 07:00:00 +0000", source="Held Watch"),
        # nap and night share a wake date
        _rec(T_SLEEP, CORE, "2026-07-01 23:00:00 +0000", "2026-07-02 06:00:00 +0000", source="Nap Watch"),
        _rec(T_SLEEP, CORE, "2026-07-02 13:00:00 +0000", "2026-07-02 14:00:00 +0000", source="Nap Watch"),
        # two sources are not added
        _rec(T_SLEEP, CORE, "2026-08-01 22:00:00 +0000", "2026-08-02 06:00:00 +0000", source="Night Watch"),
        _rec(T_SLEEP, CORE, "2026-08-01 22:00:00 +0000", "2026-08-02 06:00:00 +0000", source="Night Phone"),
        # DST spring-forward is one elapsed hour, not two clock hours
        _rec(T_SLEEP, LEGACY, "2026-03-08 01:30:00 -0500", "2026-03-08 03:30:00 -0400", source="DST Watch"),
        # DST fall-back is four elapsed hours, not the three the clock shows
        _rec(T_SLEEP, LEGACY, "2026-11-01 00:30:00 -0400", "2026-11-01 03:30:00 -0500", source="Fold Watch"),
        # local wake date, not the UTC date
        _rec(T_SLEEP, LEGACY, "2026-01-15 22:00:00 +0400", "2026-01-16 00:30:00 +0400", source="Offset Watch"),
    ])
    result, _text, doc = _run(tmp_path, body)
    assert doc is not None
    assert result["skipped_types"]["sleep:InBed"] >= 1 and result["skipped_types"]["sleep:Awake"] >= 1

    def one(source_app: str, metric: str = "sleep_duration") -> dict:
        found = [row for row in doc["rows"] if row["metric"] == metric and row["app"] == source_app]
        assert len(found) == 1, (source_app, found)
        return found[0]

    wake = one("Synth Watch")
    assert wake["date"] == "2026-05-02" and wake["value"] == 90  # 22:00-23:30 only; InBed is not minutes
    assert one("Overlap Watch")["value"] == 540
    staged = one("Stage Watch")
    assert staged["date"] == "2026-04-11" and staged["value"] == 520 and staged["sessions"][0]["coarse_omitted"] == 1
    gap = [row for row in doc["rows"] if row["app"] == "Gap Watch"]
    assert sorted((row["date"], row["value"]) for row in gap) == [("2026-06-01", 60), ("2026-06-02", 300)]
    held = one("Held Watch")
    assert held["date"] == "2026-06-11" and held["value"] == 360  # 60 + 300, not the 9h InBed
    nap = one("Nap Watch")
    assert nap["value"] == 480 and "multi_session" in nap["flags"] and len(nap["sessions"]) == 2
    assert one("Night Watch")["value"] == 480 and one("Night Phone")["value"] == 480
    assert one("DST Watch")["value"] == 60 and one("DST Watch")["date"] == "2026-03-08"
    assert one("Fold Watch")["value"] == 240
    offset = one("Offset Watch")
    assert offset["date"] == "2026-01-16" and offset["end"] == "2026-01-16 00:30:00 +0400"
    assert "InBed" not in json.dumps([row["value"] for row in doc["rows"] if row["metric"] == "sleep_duration"])


def test_direct_sleep_sessions_do_not_mix_or_fill_gaps() -> None:
    night = [_span("2026-04-10 22:00:00 +0000", "2026-04-10 23:00:00 +0000"),
             _span("2026-04-10 23:20:00 +0000", "2026-04-11 07:00:00 +0000")]
    awake = [_span("2026-04-10 23:00:00 +0000", "2026-04-10 23:20:00 +0000")]
    built = sessions(night, [], awake)
    assert len(built) == 1 and built[0]["minutes"] == 520 and built[0]["date"] == "2026-04-11"
    split = sessions(night, [], [])
    assert len(split) == 2
    coarse = sessions(night + [_span("2026-04-10 22:00:00 +0000", "2026-04-11 07:00:00 +0000", coarse=True)], [], awake)
    assert coarse[0]["minutes"] == 520 and coarse[0]["coarse_omitted"] == 1


def test_unit_aliases_are_identity_and_real_factors_stay_sourced() -> None:
    for spelling in ("mmHg", "mm Hg", "mm[Hg]"):
        assert normalize_unit(spelling) == "mm[Hg]"
        got = convert("cardiometabolic", "sbp", 120, spelling)
        assert got == {"value": 120, "unit": "mm[Hg]", "conversion": None}
        assert convert("cardiometabolic", "dbp", 80, spelling)["value"] == 80
    for spelling in ("mL/min·kg", "mL/min/kg", "mL/kg/min", "ml/kg*min", "mL/(kg·min)"):
        assert normalize_unit(spelling) == "mL/min/kg"
        got = convert("sleep_activity", "vo2_max", 47.5, spelling)
        assert got["value"] == 47.5 and got["unit"] == "mL/min/kg" and got["conversion"] is None
    assert normalize_unit("mg/dl") == "mg/dL"
    assert normalize_unit("kg/m^2") == "kg/m2"
    assert normalize_unit("K/uL") == "10*3/uL"
    weight = convert("cardiometabolic", "weight", 10, "lb")
    assert weight["value"] != 10 and weight["conversion"]["factor"] == 0.45359237
    with pytest.raises(ValidationError) as caught:
        convert("sleep_activity", "vo2_max", 47.5, "L/min/kg")
    assert caught.value.code == "NO_CONVERSION"


def test_email_is_withheld_and_malformed_input_is_refused(tmp_path: Path) -> None:
    result, text, doc = _run(tmp_path, _rec(T_STEPS, "3", "2026-02-03 08:00:00 +0000", "2026-02-03 09:00:00 +0000",
                                             source="synth@example.com", unit="count"))
    assert doc is not None and "synth@example.com" not in text + json.dumps(doc) + json.dumps(result)
    assert doc["sources"]["other"]["app"] is None and doc["sources"]["other"]["redacted"] is True
    assert "app_redacted" in _rows(doc, "steps")[0]["flags"]
    mode = stat.S_IMODE((tmp_path / "apple-provenance.json").stat().st_mode)
    assert mode == 0o600

    def refused(raw: bytes | str, code: str) -> None:
        path = tmp_path / "bad.zip"
        if isinstance(raw, bytes):
            path.write_bytes(raw)
        else:
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("export.xml", raw)
        with pytest.raises(ValidationError) as caught:
            export(str(path), str(tmp_path / "nope.csv"), provenance=str(tmp_path / "nope.json"))
        assert caught.value.code == code
        assert "SECRET-ENTITY-VALUE" not in str(caught.value)

    refused(b"not a zip", "APPLE_EXPORT_UNREADABLE")
    refused("<Nope/>", "APPLE_EXPORT_FORMAT")
    refused("<HealthData><Record", "APPLE_EXPORT_FORMAT")
    refused('<!DOCTYPE HealthData SYSTEM "http://example.invalid/h.dtd"><HealthData/>', "APPLE_EXPORT_FORMAT")
    refused('<!DOCTYPE HealthData [<!ENTITY secret "SECRET-ENTITY-VALUE">]><HealthData>&secret;</HealthData>', "APPLE_EXPORT_FORMAT")
    with zipfile.ZipFile(tmp_path / "two.zip", "w") as zf:
        zf.writestr("a/export.xml", _xml(""))
        zf.writestr("b/export.xml", _xml(""))
    with pytest.raises(ValidationError) as caught:
        export(str(tmp_path / "two.zip"), str(tmp_path / "nope.csv"))
    assert caught.value.code == "APPLE_EXPORT_FORMAT"
    with pytest.raises(NotFound):
        export(str(tmp_path / "missing.zip"), str(tmp_path / "nope.csv"))
    with pytest.raises(ValidationError):
        export(str(_zip(tmp_path, "")), str(tmp_path / "same.json"), provenance=str(tmp_path / "same.json"))
    empty = export(str(_zip(tmp_path, "")), str(tmp_path / "empty.csv"))
    assert empty["rows"] == 0 and (tmp_path / "empty.csv").read_text(encoding="utf-8") == "date,metric,value,unit,source\n"


def test_cli_help_and_invocation(tmp_path: Path) -> None:
    import subprocess
    import sys

    help_run = subprocess.run([sys.executable, "-m", "plsi", "apple", "export", "--help"], capture_output=True, text=True, check=False)
    assert help_run.returncode == 0 and "--provenance" in help_run.stdout
    path = _zip(tmp_path, _rec(T_STEPS, "4", "2026-02-04 08:00:00 +0000", "2026-02-04 09:00:00 +0000", unit="count"))
    out, side = tmp_path / "cli.csv", tmp_path / "cli.json"
    run = subprocess.run([sys.executable, "-m", "plsi", "apple", "export", "--file", str(path), "--out", str(out),
                          "--provenance", str(side)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload["ok"] is True and payload["data"]["rows"] == 1 and payload["data"]["provenance"] == str(side)
    assert "Synth Watch" not in run.stdout
    assert json.loads(side.read_text(encoding="utf-8"))["sources"]["watch"]["app"] == "Synth Watch"
