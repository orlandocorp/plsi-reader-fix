"""Stream ``export.xml`` out of an Apple Health ZIP. No member is extracted and no tree is kept.

A HealthKit internal DTD is accepted. An entity declaration or an external DTD is refused.
One attribute over ``MAX_ATTRIBUTE_CHARS``, a sourceName over ``MAX_SOURCE_CHARS``, a declared
or actual decompressed ``export.xml`` over ``MAX_XML_BYTES``, or a compression ratio over
``MAX_COMPRESSION_RATIO``, refuses the file. Nothing is truncated into a row. The parser
still allocates an attribute before that check, so one value inside the byte cap can spike
memory; the cap then stops the file.

Records are not collapsed. The same app name with two device strings is two slots (file-local
ordinals). The device string is dropped before the bag is returned. Step and exercise samples
are summed without retaining each one, so a repeated step is not flagged and is not removed.
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
MAX_XML_BYTES = 32 * 1024 ** 3
MAX_COMPRESSION_RATIO = 1000
MAX_ATTRIBUTE_CHARS = 1024 * 1024
MAX_SOURCE_CHARS = 1024
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}$")
SourceKey = tuple[str, int]


class _Blocked(Exception):
    def __init__(self, message: str, code: str = "APPLE_EXPORT_FORMAT") -> None:
        super().__init__(message)
        self.code = code


def _time(raw: str | None) -> datetime | None:
    """Apple's '2026-01-15 22:30:00 +0400'. The day is the local date as written, never shifted to UTC."""
    try:
        return datetime.strptime(raw or "", "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def _kind(src: str, device: str) -> str:
    text = f"{src} {device}".lower()
    return "watch" if "watch" in text else "phone" if "iphone" in text or "phone" in text else "other"


def _member(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if n.replace("\\", "/").rsplit("/", 1)[-1] == "export.xml"]
    if len(names) != 1:
        raise ValidationError("not an Apple Health export: the ZIP needs exactly one export.xml", code="APPLE_EXPORT_FORMAT",
                              hint="share the export.zip from Health -> profile -> Export All Health Data")
    return names[0]


def _limits(info: zipfile.ZipInfo) -> None:
    if info.file_size > MAX_XML_BYTES:
        raise ValidationError(f"export.xml declares more than {MAX_XML_BYTES} uncompressed bytes", code="APPLE_EXPORT_LIMIT")
    if info.compress_size > 0 and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
        raise ValidationError(f"export.xml compression ratio is over {MAX_COMPRESSION_RATIO}", code="APPLE_EXPORT_LIMIT")


def _slot(bag: dict[str, Any], src: str, device: str) -> SourceKey:
    """File-local ordinal for this device string. The string itself is not a returned field."""
    table: dict[str, int] = bag["devices"].setdefault(src, {})
    if device not in table:
        table[device] = len(table) + 1
    return src, table[device]


def _span(attrs: dict[str, str]) -> dict[str, Any] | None:
    start, end = _time(attrs.get("startDate")), _time(attrs.get("endDate"))
    if start is None or end is None or end <= start:
        return None
    return {"start": start, "end": end, "start_raw": attrs.get("startDate") or "", "end_raw": attrs.get("endDate") or "",
            "offsets": {start.strftime("%z"), end.strftime("%z")}}


def _skip(bag: dict[str, Any], key: str) -> None:
    bag["skipped"][key.removeprefix("HKQuantityTypeIdentifier") or "unknown"] += 1


def _check_attrs(attrs: dict[str, str]) -> str:
    for value in attrs.values():
        if len(value) > MAX_ATTRIBUTE_CHARS:
            raise _Blocked(f"export.xml has an attribute longer than {MAX_ATTRIBUTE_CHARS} characters", "APPLE_EXPORT_LIMIT")
    src = attrs.get("sourceName") or ""
    if len(src) > MAX_SOURCE_CHARS:
        raise _Blocked(f"export.xml has a sourceName longer than {MAX_SOURCE_CHARS} characters", "APPLE_EXPORT_LIMIT")
    return src


def _record(bag: dict[str, Any], attrs: dict[str, str], corr: str | None, seq: int) -> None:
    rtype, device = attrs.get("type", ""), attrs.get("device") or ""
    src = _check_attrs(attrs)
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
    if not _DAY.match(day) or not (rtype in SUMMED or rtype in AVERAGED or rtype in READINGS):
        _skip(bag, rtype)
        return
    key = _slot(bag, src, device)
    bag["kinds"].setdefault(key, _kind(src, device))
    parsed = _time(raw)
    offset = parsed.strftime("%z") if parsed else ""
    if rtype in SUMMED:
        metric, summed_unit = SUMMED[rtype]
        slot = bag["sums"].setdefault((day, metric, summed_unit, key),
                                      {"value": 0.0, "n": 0, "first": None, "last": None, "offsets": set(), "flags": set(), "seq": seq})
        slot["value"] += value
        slot["n"] += 1
        if value < 0:
            slot["flags"].add("abnormal")
        start, end = parsed, _time(end_raw)
        if start is not None and (slot["first"] is None or start < slot["first"][0]):
            slot["first"] = (start, raw)
        if end is not None and (slot["last"] is None or end > slot["last"][0]):
            slot["last"] = (end, end_raw)
        for moment in (start, end):
            if moment is not None:
                slot["offsets"].add(moment.strftime("%z"))
        return
    if rtype in AVERAGED:
        metric, averaged_unit = AVERAGED[rtype]
        bag["means"].setdefault((day, metric, averaged_unit, key), []).append(
            {"start": raw, "end": end_raw, "offset": offset, "value": value, "seq": seq})
        return
    metric = READINGS[rtype]
    bag["readings"].append({
        "date": day, "metric": metric, "value": value, "unit": unit, "src": key, "start": raw, "end": end_raw,
        "offset": offset, "correlation": corr if metric.startswith("bp_") else None, "seq": seq,
        "flags": {"abnormal"} if metric in POSITIVE and value <= 0 else set(), "method": METHOD.get(metric),
    })


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
    key = _slot(bag, src, device)
    if value in ASLEEP:
        bag["kinds"].setdefault(key, _kind(src, device))
        span["coarse"] = value == LEGACY
        bag["asleep"][key].append(span)
    elif value == INBED:
        bag["inbed"][key].append(span)
    else:
        bag["awake"][key].append(span)


def _forbid_doctype(_name: str, system: str | None, public: str | None, _internal: bool) -> None:
    if system or public:
        raise _Blocked("export.xml references an external DTD")


def _forbid_entity(*_args: Any) -> None:
    raise _Blocked("export.xml contains a forbidden entity declaration")


def _parse(stream: Any) -> dict[str, Any]:
    bag: dict[str, Any] = {
        "sums": {}, "means": defaultdict(list), "readings": [], "asleep": defaultdict(list), "inbed": defaultdict(list),
        "awake": defaultdict(list), "skipped": defaultdict(int), "kinds": {}, "devices": {},
    }
    stack: list[str | None] = []
    seq = 0
    bp_seq = 0
    seen = 0
    saw = False

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal saw, seq, bp_seq
        if not saw:
            if name != "HealthData":
                raise _Blocked("export.xml is not an Apple Health export")
            saw = True
            return
        if name == "Correlation":
            if attrs.get("type") == BP_CORR:
                bp_seq += 1
                stack.append(f"bp-{bp_seq}")
            else:
                stack.append(None)
            _check_attrs(attrs)
            return
        if name == "Record":
            seq += 1
            _record(bag, attrs, next((item for item in reversed(stack) if item), None), seq)
        elif len(attrs) > 0:
            _check_attrs(attrs)

    def end(name: str) -> None:
        if name == "Correlation" and stack:
            stack.pop()

    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.StartDoctypeDeclHandler = _forbid_doctype
    parser.EntityDeclHandler = _forbid_entity
    parser.ExternalEntityRefHandler = lambda *_a: (_ for _ in ()).throw(_Blocked("export.xml references an external entity"))
    if hasattr(parser, "SetParamEntityParsing"):
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        while chunk := stream.read(1 << 16):
            seen += len(chunk)
            if seen > MAX_XML_BYTES:
                raise _Blocked(f"export.xml is larger than {MAX_XML_BYTES} uncompressed bytes", "APPLE_EXPORT_LIMIT")
            parser.Parse(chunk, False)
        parser.Parse(b"", True)
    except _Blocked as exc:
        raise ValidationError(str(exc), code=exc.code) from None
    except expat.ExpatError as exc:
        raise ValidationError(f"export.xml is not valid XML: {exc}", code="APPLE_EXPORT_FORMAT") from None
    if not saw:
        raise ValidationError("export.xml is not an Apple Health export", code="APPLE_EXPORT_FORMAT")
    counts: dict[str, int] = defaultdict(int)
    for name, _slot_index in bag["kinds"]:
        counts[name] += 1
    bag["multi_device"] = {name for name, n in counts.items() if n > 1}
    del bag["devices"]
    return bag


def read_bag(zip_path: Path) -> dict[str, Any]:
    """Parse the one ``export.xml``. Other members are not opened. Over-limit files are refused whole."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            name = _member(zf)
            _limits(zf.getinfo(name))
            with zf.open(name) as xml:
                return _parse(xml)
    except ValidationError:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError, OSError) as exc:
        raise ValidationError(f"the ZIP cannot be read to the end: {exc}", code="APPLE_EXPORT_UNREADABLE",
                              hint="download or export it again; an encrypted ZIP is not supported") from None
