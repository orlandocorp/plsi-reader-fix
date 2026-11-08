"""Stream ``export.xml`` out of an Apple Health ZIP. No member is extracted and no tree is kept.

A HealthKit DTD (the internal subset Apple writes) is accepted. An entity declaration or an external
DTD is refused. Records are handled one at a time. Step and exercise samples are summed without
keeping each identity: a provable duplicate in those series is not collapsed (see the sidecar limits).
HRV, resting heart rate and point readings are. The device attribute is used only to notice the words
"watch" and "phone", then dropped; it is never stored.
"""

from __future__ import annotations

import math
import re
import zipfile
import zlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.parsers import expat

from plsi.errors import ValidationError
from plsi.files import sha256_hex

SUMMED = {"HKQuantityTypeIdentifierStepCount": ("steps", "/d"), "HKQuantityTypeIdentifierAppleExerciseTime": ("exercise_minutes", "min")}
AVERAGED = {
    "HKQuantityTypeIdentifierRestingHeartRate": ("resting_heart_rate", "/min"),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": ("hrv_sdnn", "ms"),
    "HKQuantityTypeIdentifierHeartRateVariabilityRMSSD": ("hrv_rmssd", "ms"),
}
READINGS = {
    "HKQuantityTypeIdentifierVO2Max": "vo2_max", "HKQuantityTypeIdentifierBodyMass": "weight",
    "HKQuantityTypeIdentifierBloodPressureSystolic": "bp_systolic", "HKQuantityTypeIdentifierBloodPressureDiastolic": "bp_diastolic",
}
BP_CORR = "HKCorrelationTypeIdentifierBloodPressure"
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
LEGACY = "HKCategoryValueSleepAnalysisAsleep"
STAGES = {f"HKCategoryValueSleepAnalysis{name}" for name in ("AsleepCore", "AsleepDeep", "AsleepREM", "AsleepUnspecified")}
INBED, AWAKE = "HKCategoryValueSleepAnalysisInBed", "HKCategoryValueSleepAnalysisAwake"
ASLEEP = STAGES | {LEGACY}
METHOD = {"hrv_sdnn": "sdnn", "hrv_rmssd": "rmssd"}
POSITIVE = {"bp_systolic", "bp_diastolic", "resting_heart_rate", "hrv_sdnn", "hrv_rmssd", "vo2_max", "weight"}
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}$")


class _Blocked(Exception):
    pass


def _time(raw: str | None) -> datetime | None:
    """Apple's '2026-01-15 22:30:00 +0400'. The day is the local date as written, never shifted to UTC."""
    try:
        return datetime.strptime(raw or "", "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def _kind(src: str, device: str) -> str:
    text = f"{src} {device[:500]}".lower()
    return "watch" if "watch" in text else "phone" if "iphone" in text or "phone" in text else "other"


def _member(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if n.replace("\\", "/").rsplit("/", 1)[-1] == "export.xml"]
    if len(names) != 1:
        raise ValidationError("not an Apple Health export: the ZIP needs exactly one export.xml", code="APPLE_EXPORT_FORMAT",
                              hint="share the export.zip from Health -> profile -> Export All Health Data")
    return names[0]


def _span(attrs: dict[str, str]) -> dict[str, Any] | None:
    start, end = _time(attrs.get("startDate")), _time(attrs.get("endDate"))
    if start is None or end is None or end <= start:
        return None
    return {"start": start, "end": end, "start_raw": attrs.get("startDate") or "", "end_raw": attrs.get("endDate") or "",
            "offsets": {start.strftime("%z"), end.strftime("%z")}}


def _skip(bag: dict[str, Any], key: str) -> None:
    bag["skipped"][key.removeprefix("HKQuantityTypeIdentifier") or "unknown"] += 1


def _note_time(slot: dict[str, Any], start_raw: str, end_raw: str) -> None:
    start, end = _time(start_raw), _time(end_raw)
    if start is not None and (slot["first"] is None or start < slot["first"][0]):
        slot["first"] = (start, start_raw)
    if end is not None and (slot["last"] is None or end > slot["last"][0]):
        slot["last"] = (end, end_raw)
    for moment in (start, end):
        if moment is not None:
            slot["offsets"].add(moment.strftime("%z"))


def _ident(rtype: str, src: str, start: str, end: str, value: str, unit: str, device: str) -> str:
    """Identity of one record. The device string is hashed so two devices stay distinct without being stored."""
    return sha256_hex("\n".join((rtype, src, start, end, value, unit, sha256_hex(device))))[:16]


def _record(bag: dict[str, Any], attrs: dict[str, str], corr: str | None) -> None:
    rtype, src, device = attrs.get("type", ""), (attrs.get("sourceName") or "")[:500], attrs.get("device") or ""
    if rtype == SLEEP:
        _sleep(bag, attrs, src, device)
        return
    try:
        value = float(attrs.get("value", ""))
    except ValueError:
        value = None
    if value is None or not math.isfinite(value):
        _skip(bag, rtype)
        return
    raw, end_raw, unit = attrs.get("startDate") or "", attrs.get("endDate") or "", attrs.get("unit") or ""
    day = raw[:10]
    if not _DAY.match(day):
        _skip(bag, rtype)
        return
    if not (rtype in SUMMED or rtype in AVERAGED or rtype in READINGS):
        _skip(bag, rtype)
        return
    bag["kinds"].setdefault(src, _kind(src, device))
    if rtype in SUMMED:
        metric, summed_unit = SUMMED[rtype]
        slot = bag["sums"].setdefault((day, metric, summed_unit, src),
                                      {"value": 0.0, "n": 0, "first": None, "last": None, "offsets": set(), "flags": set()})
        slot["value"] += value
        slot["n"] += 1
        if value < 0:
            slot["flags"].add("abnormal")
        _note_time(slot, raw, end_raw)
        return
    ident = _ident(rtype, src, raw, end_raw, attrs.get("value", ""), unit, device)
    parsed = _time(raw)
    if rtype in AVERAGED:
        metric, averaged_unit = AVERAGED[rtype]
        slot = bag["means"].setdefault((day, metric, averaged_unit, src), {"samples": [], "index": {}})
        if ident in slot["index"]:
            slot["samples"][slot["index"][ident]]["copies"] += 1
            return
        slot["index"][ident] = len(slot["samples"])
        slot["samples"].append({"start": raw, "end": end_raw, "offset": parsed.strftime("%z") if parsed else "",
                                 "value": value, "copies": 1, "identity": ident})
        return
    prior = bag["readings"].get(ident)
    if prior is not None:
        prior["copies"] += 1
        if corr and str(prior["metric"]).startswith("bp_") and not prior["correlation"]:
            prior["correlation"] = corr
        return
    metric = READINGS[rtype]
    bag["readings"][ident] = {
        "date": day, "metric": metric, "value": value, "unit": unit, "src": src, "start": raw, "end": end_raw,
        "offset": parsed.strftime("%z") if parsed else "", "correlation": corr if metric.startswith("bp_") else None,
        "copies": 1, "identity": ident,
        "flags": {"abnormal"} if metric in POSITIVE and value <= 0 else set(), "method": METHOD.get(metric),
    }


def _sleep(bag: dict[str, Any], attrs: dict[str, str], src: str, device: str) -> None:
    value = attrs.get("value") or ""
    label = "sleep:" + value.removeprefix("HKCategoryValueSleepAnalysis")
    if value not in ASLEEP and value not in (INBED, AWAKE):
        bag["skipped"][label or "sleep:unknown"] += 1
        return
    if value in (INBED, AWAKE):
        bag["skipped"][label] += 1
    span = _span(attrs)
    if span is None:
        if value in ASLEEP:
            bag["skipped"]["sleep:unparsed"] += 1
        return
    if value in ASLEEP:
        bag["kinds"].setdefault(src, _kind(src, device))
        span["coarse"] = value == LEGACY
        bag["asleep"][src].append(span)
    elif value == INBED:
        bag["inbed"][src].append(span)
    else:
        bag["awake"][src].append(span)


def _forbid_doctype(_name: str, system: str | None, public: str | None, _internal: bool) -> None:
    if system or public:
        raise _Blocked("export.xml references an external DTD")


def _forbid_entity(*_args: Any) -> None:
    raise _Blocked("export.xml contains a forbidden entity declaration")


def _forbid_external(*_args: Any) -> bool:
    raise _Blocked("export.xml references an external entity")


def _parse(stream: Any) -> dict[str, Any]:
    bag: dict[str, Any] = {
        "sums": {}, "means": {}, "readings": {}, "asleep": defaultdict(list), "inbed": defaultdict(list),
        "awake": defaultdict(list), "skipped": defaultdict(int), "kinds": {},
    }
    stack: list[str | None] = []
    seq = 0
    saw = False

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal saw, seq
        if not saw:
            if name != "HealthData":
                raise _Blocked("export.xml is not an Apple Health export")
            saw = True
            return
        if name == "Correlation":
            if attrs.get("type") == BP_CORR:
                seq += 1
                stack.append(f"bp-{seq}")
            else:
                stack.append(None)
            return
        if name == "Record":
            _record(bag, attrs, next((item for item in reversed(stack) if item), None))

    def end(name: str) -> None:
        if name == "Correlation" and stack:
            stack.pop()

    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.StartDoctypeDeclHandler = _forbid_doctype
    parser.EntityDeclHandler = _forbid_entity
    parser.ExternalEntityRefHandler = _forbid_external
    if hasattr(parser, "SetParamEntityParsing"):
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        while chunk := stream.read(1 << 16):
            parser.Parse(chunk, False)
        parser.Parse(b"", True)
    except _Blocked as exc:
        raise ValidationError(str(exc), code="APPLE_EXPORT_FORMAT") from None
    except expat.ExpatError as exc:
        raise ValidationError(f"export.xml is not valid XML: {exc}", code="APPLE_EXPORT_FORMAT") from None
    if not saw:
        raise ValidationError("export.xml is not an Apple Health export", code="APPLE_EXPORT_FORMAT")
    return bag


def read_bag(zip_path: Path) -> dict[str, Any]:
    """Parse the one ``export.xml`` in the ZIP. Other members (routes, GPX) are not opened."""
    try:
        with zipfile.ZipFile(zip_path) as zf, zf.open(_member(zf)) as xml:
            return _parse(xml)
    except ValidationError:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError, OSError) as exc:
        raise ValidationError(f"the ZIP cannot be read to the end: {exc}", code="APPLE_EXPORT_UNREADABLE",
                              hint="download or export it again; an encrypted ZIP is not supported") from None
