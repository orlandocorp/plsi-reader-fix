"""`plsi oura export`: saved Oura API v2 JSON pages -> calendar-month rows.

Offline. No token and no network. Pages are ``{"data": [...], "next_token": ...}`` under
``sleep``, ``daily_activity``, or ``daily_sleep``. The rules, units, and refusal ceilings are the
``_LIMITS`` written into the sidecar. ``next_token``, sample series, emails and device fields are
not copied.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from calendar import monthrange
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from plsi.clock import now
from plsi.errors import NotFound, ValidationError
from plsi.files import atomic_write_json, atomic_write_text, sha256_file
from plsi.life.apple_xml import MAX_ATTRIBUTE_CHARS, MAX_XML_BYTES

ENDPOINTS = ("daily_activity", "daily_sleep", "sleep")  # longest token first
NAP_TYPES = {"late_nap", "rest", "deleted"}
MIN_DAYS = 15
COLUMNS = ("date", "month", "metric", "value", "unit", "n", "source")
METRICS = {
    "sleep_duration": "h",
    "resting_heart_rate": "/min",
    "hrv_rmssd": "ms",
    "steps": "{steps}/d",
}
_EP = re.compile(r"(^|[^a-z0-9])(daily_activity|daily_sleep|sleep)([^a-z0-9]|$)")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}$")
_LIMITS = (
    "Local only. Do not ingest this CSV: sleep is in hours, while sleep_activity.sleep_duration is minutes.",
    "No network and no token. next_token is not stored.",
    "daily_sleep is a score page and is not sleep duration, HRV, or steps.",
    "One main night per day: a long_sleep if any exist, otherwise the longest non-nap session. Naps and rest are not added.",
    "lowest_heart_rate is a proxy for resting heart rate. average_hrv is RMSSD and is never labeled SDNN.",
    "Hours are total_sleep_duration seconds / 3600. That scale is exact.",
    "Bounds flags do not remove a day: heart rate under 25 or over 200, RMSSD at or under 0 or over 300, sleep over 16 h or negative.",
    "A closed month is one whose last day is before --as-of. Fewer than 15 days excludes the metric.",
    "Identical document ids are one session. The same id with different metric fields is a conflict and is not used.",
    "Emails, device fields, sample series and personal_info are not copied. A personal name in a free-text field is not scanned.",
    f"A file over {MAX_XML_BYTES} bytes, or a string over {MAX_ATTRIBUTE_CHARS} characters, refuses the directory. Nothing is truncated.",
)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _endpoint(path: Path, root: Path) -> str | None:
    parts = [p.lower() for p in path.relative_to(root).parts[:-1]]
    for name in ENDPOINTS:
        if name in parts:
            return name
    found = _EP.findall(path.name.lower())
    return found[0][1] if found else None


def _too_long(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) > MAX_ATTRIBUTE_CHARS
    if isinstance(value, dict):
        return any(_too_long(k) or _too_long(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_too_long(item) for item in value)
    return False


def _pages(root: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in ENDPOINTS}
    counts: dict[str, Any] = {name: {"files": 0, "records": 0} for name in ENDPOINTS}
    files: list[dict[str, str]] = []
    skipped = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValidationError("a page path leaves the directory", code="OURA_EXPORT_FORMAT")
            endpoint = _endpoint(path, root)
            if endpoint is None:
                skipped += 1
                continue
            size = path.stat().st_size
            total += size
            if size > MAX_XML_BYTES or total > MAX_XML_BYTES:
                raise ValidationError(f"Oura pages exceed {MAX_XML_BYTES} bytes", code="OURA_EXPORT_LIMIT")
            with path.open("rb") as raw:
                if raw.read(2) == b"\x1f\x8b":
                    raise ValidationError("Oura pages must be raw JSON, not compressed", code="OURA_EXPORT_LIMIT")
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                raise ValidationError(f"a {endpoint} page is not valid JSON", code="OURA_EXPORT_FORMAT") from None
            if _too_long(doc):
                raise ValidationError(f"a {endpoint} page has a string longer than {MAX_ATTRIBUTE_CHARS} characters",
                                      code="OURA_EXPORT_LIMIT")
            rows = doc.get("data") if isinstance(doc, dict) else None
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValidationError(f"a {endpoint} page needs a data list of objects", code="OURA_EXPORT_FORMAT")
            grouped[endpoint].extend(rows)
            counts[endpoint]["files"] += 1
            counts[endpoint]["records"] += len(rows)
            files.append({"endpoint": endpoint, "sha256": sha256_file(path)})
    counts["skipped_files"] = skipped
    files.sort(key=lambda item: (item["endpoint"], item["sha256"]))
    return grouped, counts, files


def _dedupe(rows: list[dict[str, Any]], fields: tuple[str, ...], stats: dict[str, int]) -> list[dict[str, Any]]:
    kept: dict[str, tuple[tuple, dict[str, Any]]] = {}
    blocked: set[str] = set()
    for row in rows:
        ident = row.get("id")
        if not isinstance(ident, str) or not ident:
            stats["missing_id"] += 1
            continue
        sig = tuple(row.get(field) for field in fields)
        if ident in blocked:
            continue
        if ident in kept:
            if kept[ident][0] == sig:
                stats["duplicate_ids"] += 1
            else:
                stats["id_conflicts"] += 1
                blocked.add(ident)
                del kept[ident]
            continue
        kept[ident] = (sig, row)
    return [row for _, row in kept.values()]


def _length(row: dict[str, Any]) -> tuple[int, float, str]:
    duration = _num(row.get("total_sleep_duration"))
    if duration is not None:
        return (1, duration, str(row.get("id")))
    return (0, _num(row.get("time_in_bed")) or -1.0, str(row.get("id")))


def _nights(rows: list[dict[str, Any]], stats: dict[str, Any]) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    stats.update({"duplicate_ids": 0, "id_conflicts": 0, "missing_id": 0, "null_fields": defaultdict(int), "skipped_types": defaultdict(int)})
    chosen: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _dedupe(rows, ("day", "type", "total_sleep_duration", "lowest_heart_rate", "average_hrv", "time_in_bed"), stats):
        kind = str(row.get("type") or "")
        if kind in NAP_TYPES:
            stats["skipped_types"][kind] += 1
            continue
        day = str(row.get("day") or "")
        if not _DAY.match(day):
            stats["bad_day"] = stats.get("bad_day", 0) + 1
            continue
        chosen[day].append(row)
    days: dict[str, dict[str, float]] = {}
    flags: list[dict[str, Any]] = []
    for day, pool in chosen.items():
        longs = [row for row in pool if row.get("type") == "long_sleep"]
        group = longs or pool
        main = max(group, key=_length)
        if len(group) > 1:
            flags.append({"day": day, "flag": "multiple_main"})
        values: dict[str, float] = {}
        hours = _num(main.get("total_sleep_duration"))
        if hours is None:
            stats["null_fields"]["total_sleep_duration"] += 1
        else:
            hours /= 3600.0
            values["sleep_duration"] = hours
            if hours > 16 or hours < 0:
                flags.append({"day": day, "metric": "sleep_duration", "flag": "bounds", "value": hours})
        hr = _num(main.get("lowest_heart_rate"))
        if hr is None:
            stats["null_fields"]["lowest_heart_rate"] += 1
        else:
            values["resting_heart_rate"] = hr
            if hr < 25 or hr > 200:
                flags.append({"day": day, "metric": "resting_heart_rate", "flag": "bounds", "value": hr})
        hrv = _num(main.get("average_hrv"))
        if hrv is None:
            stats["null_fields"]["average_hrv"] += 1
        else:
            values["hrv_rmssd"] = hrv
            if hrv <= 0 or hrv > 300:
                flags.append({"day": day, "metric": "hrv_rmssd", "flag": "bounds", "value": hrv})
        if values:
            days[day] = values
    stats["null_fields"] = dict(stats["null_fields"])
    stats["skipped_types"] = dict(stats["skipped_types"])
    return days, flags


def _steps(rows: list[dict[str, Any]], stats: dict[str, Any]) -> tuple[dict[str, float], list[dict[str, Any]]]:
    stats.update({"duplicate_ids": 0, "id_conflicts": 0, "missing_id": 0, "null_fields": {"steps": 0}, "excluded_days": 0})
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in _dedupe(rows, ("day", "steps"), stats):
        day = str(row.get("day") or "")
        steps = _num(row.get("steps"))
        if not _DAY.match(day):
            stats["bad_day"] = stats.get("bad_day", 0) + 1
            continue
        if steps is None:
            stats["null_fields"]["steps"] += 1
            continue
        by_day[day].append(steps)
    out, flags = {}, []
    for day, values in by_day.items():
        if len(values) > 1:
            stats["excluded_days"] += 1
            flags.append({"day": day, "metric": "steps", "flag": "multiple_activity"})
            continue
        out[day] = values[0]
    return out, flags


def _closed(month: str, as_of: date) -> bool:
    year, mon = (int(part) for part in month.split("-"))
    return as_of > date(year, mon, monthrange(year, mon)[1])


def _aggregate(sleep: dict[str, dict[str, float]], steps: dict[str, float], as_of: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_metric: dict[str, dict[str, list[float]]] = {metric: defaultdict(list) for metric in METRICS}
    for day, values in sleep.items():
        for metric, value in values.items():
            by_metric[metric][day[:7]].append(value)
    for day, value in steps.items():
        by_metric["steps"][day[:7]].append(value)
    months = sorted({month for metric in by_metric.values() for month in metric})
    rows, excluded = [], []
    for month in months:
        year, mon = (int(part) for part in month.split("-"))
        last = date(year, mon, monthrange(year, mon)[1]).isoformat()
        for metric, unit in METRICS.items():
            values = by_metric[metric].get(month, [])
            item = {"month": month, "metric": metric, "n": len(values)}
            if not _closed(month, as_of):
                excluded.append({**item, "reason": "open_month"})
            elif len(values) < MIN_DAYS:
                excluded.append({**item, "reason": "fewer_than_15_days"})
            else:
                rows.append({"date": last, "month": month, "metric": metric, "value": sum(values) / len(values),
                             "unit": unit, "n": len(values), "source": "oura"})
    rows.sort(key=lambda row: (row["month"], row["metric"]))
    excluded.sort(key=lambda row: (row["month"], row["metric"]))
    return rows, excluded


def export(raw_dir: str, out: str, provenance: str, as_of: date | None = None) -> dict[str, Any]:
    """Write closed-month means and the local sidecar. Reads only files under ``raw_dir``."""
    root = Path(raw_dir)
    if not root.is_dir():
        raise NotFound(f"input directory not found: {root.name}", code="FILE_NOT_FOUND")
    if Path(provenance).resolve() == Path(out).resolve():
        raise ValidationError("provenance path and csv path are the same file", code="OURA_EXPORT_FORMAT")
    as_of = as_of or now().date()
    grouped, counts, files = _pages(root)
    sleep_stats: dict[str, Any] = {}
    activity_stats: dict[str, Any] = {}
    nights, flags = _nights(grouped["sleep"], sleep_stats)
    steps, step_flags = _steps(grouped["daily_activity"], activity_stats)
    rows, excluded = _aggregate(nights, steps, as_of)
    counts["sleep"].update(sleep_stats)
    counts["daily_activity"].update(activity_stats)
    counts["daily_sleep"]["used_for_metrics"] = False
    flag_rows = sorted(flags + step_flags, key=lambda item: (item["day"], item.get("metric") or "", item["flag"], item.get("value") or 0))
    written: dict[str, int] = defaultdict(int)
    for row in rows:
        written[row["metric"]] += 1
    body = {
        "kind": "plsi_oura_provenance", "schema": 1, "local_only": True, "as_of": as_of.isoformat(),
        "limits": list(_LIMITS), "bounds": {"max_bytes": MAX_XML_BYTES, "max_string_chars": MAX_ATTRIBUTE_CHARS},
        "endpoints": counts, "files": files, "excluded_months": excluded, "flags": flag_rows,
        "metrics": {
            "sleep_duration": {"unit": "h", "from": "total_sleep_duration", "scale": "1/3600"},
            "resting_heart_rate": {"unit": "/min", "from": "lowest_heart_rate", "proxy": True},
            "hrv_rmssd": {"unit": "ms", "from": "average_hrv", "method": "rmssd"},
            "steps": {"unit": "{steps}/d", "from": "daily_activity.steps"},
        },
    }
    buf = io.StringIO()
    writer = csv.DictWriter(buf, COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows({**row, "value": f"{row['value']:.6g}", "n": str(row["n"])} for row in rows)
    atomic_write_text(Path(out), buf.getvalue())
    atomic_write_json(Path(provenance), body, private=True)
    return {"out": out, "provenance": provenance, "as_of": as_of.isoformat(), "rows": len(rows),
            "excluded_months": len(excluded), "metrics": dict(written)}
