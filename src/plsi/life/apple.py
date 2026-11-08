"""`plsi apple export`: an Apple Health export ZIP -> daily rows for wearables/import.

CSV columns stay ``date,metric,value,unit,source``. ``source`` is ``watch``, ``phone`` or ``other``
(the next app of a kind is ``other-2``). ``--provenance`` writes an opt-in local sidecar: the app
name, original timestamps and offsets, HRV method, and blood-pressure correlation ids. It is not
ingested and nothing is sent. Device serials, GPS, account ids and filenames are omitted from both.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from pathlib import Path
from typing import Any

from plsi.errors import NotFound, ValidationError
from plsi.files import atomic_write_json, atomic_write_text, sha256_file
from plsi.life.apple_sleep import sessions as sleep_sessions
from plsi.life.apple_xml import METHOD, POSITIVE, read_bag
from plsi.unit_alias import identity_alias

COLUMNS = ("date", "metric", "value", "unit", "source")
_LIMITS = (
    "Local only. Do not ingest, attach or send this file.",
    "app is the export sourceName. Device serials, GPS, account ids and filenames are omitted.",
    "HRV method is the HealthKit type (sdnn or rmssd), never inferred from the number.",
    "Blood-pressure correlation is set only from a Correlation element, never from the clock.",
    "copies > 1 is one record listed again. A different start is another reading, even with the same value.",
    "Step and exercise rows keep a count plus the first and last timestamps, not every sample.",
    "Sleep minutes are asleep only. InBed and Awake are not added. The date is the local wake day.",
    "abnormal means a non-physical number was kept. Outliers are not dropped and are not clinically classified.",
    "Without this file the CSV source is only watch, phone or other-N.",
)
_SIDE_KEYS = ("start", "end", "offset", "copies", "identity", "n", "first", "last", "offsets", "samples", "sessions")


def _public_app(name: str) -> tuple[str | None, bool]:
    """(label, redacted). An address is withheld. The device attribute is never a candidate."""
    if "@" in name:
        return None, True
    cleaned = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n\t").strip()
    return (cleaned[:200] or None), False


def _labels(kinds: dict[str, str]) -> dict[str, str]:
    """First app of a kind keeps the bare label; the next is kind-2. Order is the source name."""
    count: dict[str, int] = defaultdict(int)
    out = {}
    for src in sorted(kinds):
        count[kinds[src]] += 1
        out[src] = kinds[src] if count[kinds[src]] == 1 else f"{kinds[src]}-{count[kinds[src]]}"
    return out


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
        if n_sys == 1 and n_dia == 1:
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
    public = {src: _public_app(src) for src in bag["kinds"]}
    rows: list[dict[str, Any]] = list(bag["readings"].values())
    _bp_flags(rows)
    for (day, metric, unit, src), slot in bag["sums"].items():
        rows.append({"date": day, "metric": metric, "value": slot["value"], "unit": unit, "src": src, "flags": set(slot["flags"]),
                     "method": None, "n": slot["n"], "first": slot["first"][1] if slot["first"] else None,
                     "last": slot["last"][1] if slot["last"] else None, "offsets": sorted(slot["offsets"]),
                     "start": slot["first"][1] if slot["first"] else None})
    for (day, metric, unit, src), slot in bag["means"].items():
        samples = slot["samples"]
        flags = {"abnormal"} if metric in POSITIVE and any(sample["value"] <= 0 for sample in samples) else set()
        rows.append({"date": day, "metric": metric, "value": sum(sample["value"] for sample in samples) / len(samples),
                     "unit": unit, "src": src, "flags": flags, "method": METHOD.get(metric), "samples": samples,
                     "start": min(sample["start"] for sample in samples)})
    for src, spans in bag["asleep"].items():
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sess in sleep_sessions(spans, bag["inbed"].get(src, []), bag["awake"].get(src, [])):
            by_day[sess["date"]].append(sess)
        for day, group in by_day.items():
            flags = set()
            if len(group) > 1:
                flags.add("multi_session")
            if any(sess["uncertain"] for sess in group):
                flags.add("uncertain")
            rows.append({"date": day, "metric": "sleep_duration", "value": sum(sess["minutes"] for sess in group),
                         "unit": "min", "src": src, "flags": flags, "method": None, "sessions": group,
                         "start": min(sess["start"] for sess in group), "end": max(sess["end"] for sess in group)})
    for row in rows:
        row["source"] = labels[row.pop("src")]
    rows.sort(key=lambda r: (r["date"], r["metric"], r["source"], r.get("start") or "", r.get("end") or "", r.get("identity") or ""))
    sources = {lab: {"app": public[src][0], "kind": bag["kinds"][src], "redacted": public[src][1]} for src, lab in labels.items()}
    return rows, sources


def daily_rows(zip_path: Path) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    """(csv rows, skipped type counts, provenance body without the file hash)."""
    bag = read_bag(zip_path)
    rows, sources = _assemble(bag)
    body = {"kind": "plsi_apple_provenance", "schema": 1, "local_only": True, "limits": list(_LIMITS), "sources": sources,
            "rows": [_side(row, sources[row["source"]]["app"], sources[row["source"]]["redacted"]) for row in rows]}
    for index, side in enumerate(body["rows"]):
        side["i"] = index
    csv_rows = [{"date": r["date"], "metric": r["metric"], "value": r["value"], "unit": r["unit"], "source": r["source"]} for r in rows]
    return csv_rows, dict(bag["skipped"]), body


def export(file: str, out: str, provenance: str | None = None) -> dict[str, Any]:
    """Write daily rows to ``out``. With ``provenance``, also write the local sidecar.

    The returned summary has no app names and no timestamps. ``sha256`` is the ZIP, for ``origin.file_sha256``.
    ``provenance`` is included only when the sidecar was requested, so a caller that ignores unknown keys is unchanged
    and one that compares the old keys still sees them.
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
