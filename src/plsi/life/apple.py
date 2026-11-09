"""`plsi apple export`: an Apple Health export ZIP -> daily rows for wearables/import.

CSV columns stay ``date,metric,value,unit,source``. ``source`` is ``watch``, ``phone`` or ``other``
(the next of a kind is ``other-2``). Two device strings under one app name are two sources; the
sidecar ``device_index`` is a file-local ordinal, not a device id, and is not stable across files.

``--provenance`` is an opt-in local sidecar. It is not ingested and nothing is sent. There is no
record id in an Apple export, so identical same-time rows are kept and flagged ``attribute_match``
rather than collapsed as a proven duplicate. ``app`` is sourceName after withholding an address,
a path or filename, a UUID or long hex token, or a coordinate-like number. A personal name in
sourceName is not detected and is not claimed to be filtered.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from plsi.errors import NotFound, ValidationError
from plsi.files import atomic_write_json, atomic_write_text, sha256_file
from plsi.life.apple_sleep import sessions as sleep_sessions
from plsi.life.apple_xml import (
    MAX_ATTRIBUTE_CHARS, MAX_COMPRESSION_RATIO, MAX_SOURCE_CHARS, MAX_XML_BYTES, METHOD, POSITIVE, read_bag,
)
from plsi.unit_alias import identity_alias

COLUMNS = ("date", "metric", "value", "unit", "source")
_HEURISTIC = {"coarse_partial", "awake_bridge", "inbed_gap", "overlap_inbed"}
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"[0-9a-fA-F]{32,}")
_GPS = re.compile(r"-?\d{1,3}\.\d{4,}")
_PATH = re.compile(r"[\\/]|\.(?:gpx|xml|zip|csv|json|txt)\b", re.I)
_LIMITS = (
    "Local only. Do not ingest, attach or send this file.",
    "app is sourceName. Emails, paths, filenames, UUIDs, 32+ hex digits and coordinate-like numbers are withheld.",
    "A personal name in sourceName is not detected. This file does not claim to remove every identifier.",
    "device_index counts distinct device strings in this file, and a missing device attribute is its own string. It is not a device id and not stable across files.",
    "No record id exists. attribute_match means the same app, slot, time, value and unit occurred again; both rows are kept.",
    "HRV method is the HealthKit type (sdnn or rmssd), never inferred. A repeated sample stays in the daily mean.",
    "Blood-pressure correlation is only the Correlation element. Matching clocks are not a pair.",
    "Step and exercise rows are sums. A repeated step is not detected and is not removed.",
    "Sleep reasons (coarse_partial, coarse_covered, inbed_gap, awake_bridge, overlap_inbed) are heuristics, not a verified night.",
    "abnormal means a non-physical number was kept. Outliers are not dropped and are not clinically classified.",
    f"Refused whole, not truncated, past {MAX_XML_BYTES} uncompressed bytes, ratio {MAX_COMPRESSION_RATIO}, or an attribute over {MAX_ATTRIBUTE_CHARS} characters.",
)
_SIDE_KEYS = ("start", "end", "offset", "n", "first", "last", "offsets", "samples", "sessions", "device_index")


def _public_app(name: str) -> tuple[str | None, bool]:
    """(label, withheld). Withholds the patterns in ``_LIMITS`` only. Does not guess personal names."""
    if (not name.strip() or "@" in name or _UUID.search(name) or _HEX.search(name) or _GPS.search(name) or _PATH.search(name)
            or any(not ch.isprintable() or ch in "\r\n\t" for ch in name)):
        return None, True
    return name.strip(), False


def _labels(kinds: dict[tuple[str, int], str]) -> dict[tuple[str, int], str]:
    """First of a kind keeps the bare label; the next is kind-2. Order is kind, app name, device slot."""
    count: dict[str, int] = defaultdict(int)
    out = {}
    for key in sorted(kinds, key=lambda k: (kinds[k], k[0], k[1])):
        count[kinds[key]] += 1
        out[key] = kinds[key] if count[kinds[key]] == 1 else f"{kinds[key]}-{count[kinds[key]]}"
    return out


def _mark_matches(rows: list[dict[str, Any]]) -> None:
    """Identical attributes are not a record id. Keep every row and mark the repeat uncertain."""
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["src"], row["metric"], row["start"], row["end"], row["value"], row["unit"])].append(row)
    for items in groups.values():
        if len(items) > 1:
            for item in items:
                item["flags"].update(("uncertain", "attribute_match"))


def _bp_flags(rows: list[dict[str, Any]]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["metric"] not in ("bp_systolic", "bp_diastolic"):
            continue
        if not row.get("correlation"):
            row["flags"].add("unpaired")
        else:
            grouped[row["correlation"]].append(row)
    for items in grouped.values():
        n_sys = sum(item["metric"] == "bp_systolic" for item in items)
        n_dia = sum(item["metric"] == "bp_diastolic" for item in items)
        disagree = len({item["start"] for item in items}) > 1
        if n_sys == 1 and n_dia == 1 and not disagree:
            systolic = next(item for item in items if item["metric"] == "bp_systolic")
            diastolic = next(item for item in items if item["metric"] == "bp_diastolic")
            if systolic["value"] <= diastolic["value"]:
                systolic["flags"].add("uncertain")
                diastolic["flags"].add("uncertain")
            continue
        for item in items:
            item["flags"].add("uncertain")
            if n_sys == 0 or n_dia == 0:
                item["flags"].add("unpaired")


def _samples(samples: list[dict[str, Any]], metric: str) -> tuple[float, set[str], list[dict[str, Any]]]:
    flags: set[str] = set()
    seen: dict[tuple, dict[str, Any]] = {}
    for sample in samples:
        key = (sample["start"], sample["end"], sample["value"])
        if key in seen:
            flags.update(("uncertain", "attribute_match"))
            sample["attribute_match"] = True
            seen[key]["attribute_match"] = True
        else:
            seen[key] = sample
        if metric in POSITIVE and sample["value"] <= 0:
            flags.add("abnormal")
    return sum(sample["value"] for sample in samples) / len(samples), flags, samples


def _side(row: dict[str, Any], app: str | None, redacted: bool) -> dict[str, Any]:
    if redacted:
        row["flags"].add("app_redacted")
    side: dict[str, Any] = {
        "date": row["date"], "metric": row["metric"], "value": row["value"], "unit": row["unit"], "source": row["source"],
        "app": app, "method": row.get("method"), "flags": sorted(row["flags"]),
    }
    aliased = identity_alias(row["unit"])
    if aliased and aliased != row["unit"]:
        side["unit_alias"] = aliased
    for key in _SIDE_KEYS:
        if row.get(key) is not None:
            side[key] = row[key]
    if row["metric"] in ("bp_systolic", "bp_diastolic"):
        side["correlation"] = row.get("correlation")
    return side


def _assemble(bag: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    labels = _labels(bag["kinds"])
    multi = bag["multi_device"]
    rows: list[dict[str, Any]] = list(bag["readings"])
    _mark_matches(rows)
    _bp_flags(rows)
    for (day, metric, unit, src), slot in bag["sums"].items():
        rows.append({"date": day, "metric": metric, "value": slot["value"], "unit": unit, "src": src, "flags": set(slot["flags"]),
                     "method": None, "seq": slot["seq"], "n": slot["n"], "first": slot["first"][1] if slot["first"] else None,
                     "last": slot["last"][1] if slot["last"] else None, "offsets": sorted(slot["offsets"]),
                     "start": slot["first"][1] if slot["first"] else None})
    for (day, metric, unit, src), samples in bag["means"].items():
        value, flags, kept = _samples(samples, metric)
        rows.append({"date": day, "metric": metric, "value": value, "unit": unit, "src": src, "flags": flags,
                     "method": METHOD.get(metric), "samples": [{k: s[k] for k in ("start", "end", "offset", "value") if k in s}
                                                                | ({"attribute_match": True} if s.get("attribute_match") else {})
                                                                for s in kept],
                     "seq": min(s["seq"] for s in kept), "start": min(s["start"] for s in kept)})
    for src, spans in bag["asleep"].items():
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sess in sleep_sessions(spans, bag["inbed"].get(src, []), bag["awake"].get(src, [])):
            by_day[sess["date"]].append(sess)
        for day, group in by_day.items():
            flags = {reason for sess in group for reason in sess["reasons"] if reason != "coarse_covered"}
            if any(sess["reasons"] for sess in group if "coarse_covered" in sess["reasons"]):
                flags.add("coarse_covered")
            if len(group) > 1:
                flags.add("multi_session")
            if flags & (_HEURISTIC | {"coarse_partial"}):
                flags.add("uncertain")
            rows.append({"date": day, "metric": "sleep_duration", "value": sum(sess["minutes"] for sess in group),
                         "unit": "min", "src": src, "flags": flags, "method": None, "sessions": group,
                         "seq": 0, "start": min(sess["start"] for sess in group), "end": max(sess["end"] for sess in group)})
    for row in rows:
        src = row.pop("src")
        row["source"] = labels[src]
        if src[0] in multi:
            row["flags"].update(("uncertain", "multi_device"))
            row["device_index"] = src[1]
    rows.sort(key=lambda r: (r["date"], r["metric"], r["source"], r.get("start") or "", r.get("end") or "", r.get("seq") or 0))
    sources = {}
    for src, lab in labels.items():
        app, redacted = _public_app(src[0])
        entry: dict[str, Any] = {"app": app, "kind": bag["kinds"][src], "redacted": redacted}
        if src[0] in multi:
            entry["device_index"] = src[1]
            entry["multi_device"] = True
        sources[lab] = entry
    return rows, sources


def _bounds() -> dict[str, int]:
    return {"max_xml_bytes": MAX_XML_BYTES, "max_compression_ratio": MAX_COMPRESSION_RATIO,
            "max_attribute_chars": MAX_ATTRIBUTE_CHARS, "max_source_chars": MAX_SOURCE_CHARS}


def daily_rows(zip_path: Path) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    """(csv rows, skipped type counts, provenance body without the file hash)."""
    bag = read_bag(zip_path)
    rows, sources = _assemble(bag)
    body = {"kind": "plsi_apple_provenance", "schema": 1, "local_only": True, "limits": list(_LIMITS), "bounds": _bounds(),
            "sources": sources, "rows": [_side(row, sources[row["source"]]["app"], sources[row["source"]]["redacted"]) for row in rows]}
    for index, side in enumerate(body["rows"]):
        side["i"] = index
    csv_rows = [{"date": r["date"], "metric": r["metric"], "value": r["value"], "unit": r["unit"], "source": r["source"]} for r in rows]
    return csv_rows, dict(bag["skipped"]), body


def export(file: str, out: str, provenance: str | None = None) -> dict[str, Any]:
    """Write daily rows to ``out``. With ``provenance``, also write the local sidecar.

    The summary has no app names and no timestamps. ``sha256`` is the ZIP. ``provenance`` is present
    only when requested. A file over a limit raises before either file is written.
    """
    path = Path(file)
    if not path.is_file():
        raise NotFound(f"input file not found: {path.name}", code="FILE_NOT_FOUND")
    if provenance and Path(provenance).resolve() == Path(out).resolve():
        raise ValidationError("provenance path and csv path are the same file", code="APPLE_EXPORT_FORMAT")
    rows, skipped, body = daily_rows(path)
    sha = sha256_file(path)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows({**row, "value": f"{row['value']:.6g}"} for row in rows)
    atomic_write_text(Path(out), buf.getvalue())
    if provenance:
        atomic_write_json(Path(provenance), {**body, "file_sha256": sha}, private=True)
    metrics: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        metrics[row["metric"]][row["source"]] += 1
    result: dict[str, Any] = {
        "out": out, "sha256": sha, "rows": len(rows), "metrics": {m: dict(counts) for m, counts in metrics.items()},
        "first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None, "skipped_types": skipped,
    }
    if provenance:
        result["provenance"] = provenance
    return result
