"""Asleep sessions for one Apple Health source.

InBed and Awake are never minutes. These rules are heuristics, not a verified night.
Each session carries ``reasons`` for the rules that fired.

- Touching or overlapping asleep intervals become their union (no reason flag).
- A legacy ``Asleep`` interval that lies entirely inside stages plus Awake adds no
  minutes (``coarse_covered``). One that overlaps stages but also extends outside
  them keeps the uncovered remainder, so that tail is not discarded, and is marked
  ``coarse_partial``. The union can fill a hole the stages left; that is not proof
  the hole was sleep. Awake is removed from that remainder and is not counted.
- A legacy interval with no stage overlap is kept (awake inside it is removed).
- Blocks that share one InBed record but do not touch are one session (``inbed_gap``).
  The gap is not added. Two InBed records touching one block is ``overlap_inbed``;
  the minutes are counted once.
- With no InBed, an Awake record joins a gap of at most two hours (``awake_bridge``).
  A longer gap stays two sessions.
- The date is the local calendar date of the latest end (asleep, or the InBed end
  when that is later), in that timestamp's own offset. Duration is timezone-aware.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

MAX_AWAKE_BRIDGE = timedelta(hours=2)


def _touch(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return a0 <= b1 and b0 <= a1


def _overlap_s(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    start, end = max(a0, b0), min(a1, b1)
    return (end - start).total_seconds() if end > start else 0.0


def _merge_cuts(cuts: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(cuts):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(span: dict[str, Any], covers: list[dict[str, Any]]) -> list[tuple[datetime, datetime]]:
    """Parts of ``span`` that no cover overlaps. Empty when the span is fully covered."""
    cuts = [(max(span["start"], c["start"]), min(span["end"], c["end"])) for c in covers
            if _touch(span["start"], span["end"], c["start"], c["end"])]
    if not cuts:
        return [(span["start"], span["end"])]
    cursor, out = span["start"], []
    for start, end in _merge_cuts(cuts):
        if cursor < start:
            out.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < span["end"]:
        out.append((cursor, span["end"]))
    return out


def _piece(origin: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    raw = origin["start_raw"] if start == origin["start"] else start.strftime("%Y-%m-%d %H:%M:%S %z")
    end_raw = origin["end_raw"] if end == origin["end"] else end.strftime("%Y-%m-%d %H:%M:%S %z")
    return {"start": start, "end": end, "start_raw": raw, "end_raw": end_raw,
            "offsets": {start.strftime("%z"), end.strftime("%z")}, "coarse": False}


def _coarse_kept(asleep: list[dict[str, Any]], awake: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Stages plus any legacy remainder. Notes record covered and partial coarse intervals."""
    stages = [seg for seg in asleep if not seg.get("coarse")]
    notes: list[dict[str, Any]] = []
    kept = list(stages)
    for leg in (seg for seg in asleep if seg.get("coarse")):
        hits = [seg for seg in stages if _touch(leg["start"], leg["end"], seg["start"], seg["end"])]
        leftovers = _subtract(leg, (stages + awake) if hits else awake)
        if hits and not leftovers:
            notes.append({"start": leg["start"], "end": leg["end"], "reason": "coarse_covered"})
        elif hits:
            notes.append({"start": leg["start"], "end": leg["end"], "reason": "coarse_partial"})
            kept.extend(_piece(leg, start, end) for start, end in leftovers)
        else:
            kept.extend(_piece(leg, start, end) for start, end in leftovers)
    return kept, notes


def _merge(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for seg in sorted(spans, key=lambda s: (s["start"], s["end"])):
        if blocks and seg["start"] <= blocks[-1]["end"]:
            block = blocks[-1]
            if seg["end"] > block["end"]:
                block["end"], block["end_raw"] = seg["end"], seg["end_raw"]
            block["segments"] += 1
            block["offsets"].update(seg["offsets"])
        else:
            blocks.append({
                "start": seg["start"], "end": seg["end"], "start_raw": seg["start_raw"], "end_raw": seg["end_raw"],
                "segments": 1, "offsets": set(seg["offsets"]),
            })
    return blocks


def _session(blocks: list[dict[str, Any]], anchor: dict[str, Any] | None, reasons: set[str],
             awake: list[dict[str, Any]]) -> dict[str, Any]:
    first = min(blocks, key=lambda b: b["start"])
    candidates = [(b["end"], b["end_raw"]) for b in blocks]
    if anchor is not None:
        candidates.append((anchor["end"], anchor["end_raw"]))
    wake_at, wake_raw = max(candidates)
    offsets: set[str] = set()
    for block in blocks:
        offsets.update(block["offsets"])
    if anchor is not None:
        offsets.update(anchor["offsets"])
    span_end = max(b["end"] for b in blocks)
    return {
        "start": first["start_raw"], "end": wake_raw, "date": wake_at.date().isoformat(),
        "minutes": sum((b["end"] - b["start"]).total_seconds() for b in blocks) / 60,
        "segments": sum(b["segments"] for b in blocks), "offsets": sorted(offsets),
        "reasons": sorted(reasons), "inbed": anchor is not None,
        "awake": sum(1 for a in awake if _touch(first["start"], span_end, a["start"], a["end"])),
        "coarse_covered": 0, "_start": first["start"], "_end": span_end,
    }


def _by_inbed(blocks: list[dict[str, Any]], inbed: list[dict[str, Any]], awake: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[int, list[dict[str, Any]]] = {}
    overlap: set[int] = set()
    alone: list[dict[str, Any]] = []
    for block in blocks:
        hits = [i for i, bed in enumerate(inbed) if _touch(block["start"], block["end"], bed["start"], bed["end"])]
        if not hits:
            alone.append(_session([block], None, set(), awake))
            continue
        best = max(hits, key=lambda i: (_overlap_s(block["start"], block["end"], inbed[i]["start"], inbed[i]["end"]), -i))
        groups.setdefault(best, []).append(block)
        if len(hits) > 1:
            overlap.add(best)
    sessions = alone
    for idx, grouped in groups.items():
        ordered = sorted(grouped, key=lambda b: b["start"])
        reasons = {"overlap_inbed"} if idx in overlap else set()
        if any(b["start"] > a["end"] for a, b in zip(ordered, ordered[1:])):
            reasons.add("inbed_gap")
        sessions.append(_session(ordered, inbed[idx], reasons, awake))
    return sessions


def _by_awake(blocks: list[dict[str, Any]], awake: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = [[blocks[0]]]
    reasons: list[set[str]] = [set()]
    for nxt in blocks[1:]:
        left = groups[-1][-1]
        gap = nxt["start"] - left["end"]
        covered = any(a["start"] <= left["end"] and a["end"] >= nxt["start"] for a in awake)
        if gap <= timedelta(0) or (gap <= MAX_AWAKE_BRIDGE and covered):
            groups[-1].append(nxt)
            if gap > timedelta(0):
                reasons[-1].add("awake_bridge")
        else:
            groups.append([nxt])
            reasons.append(set())
    return [_session(group, None, reason, awake) for group, reason in zip(groups, reasons)]


def sessions(asleep: list[dict[str, Any]], inbed: list[dict[str, Any]], awake: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One source's asleep, InBed and Awake spans. InBed and Awake add no minutes."""
    kept, notes = _coarse_kept(asleep, awake)
    blocks = _merge(kept)
    if not blocks:
        return []
    built = _by_inbed(blocks, inbed, awake) if inbed else _by_awake(blocks, awake)
    for note in notes:
        counted = False
        for sess in built:
            if not _touch(note["start"], note["end"], sess["_start"], sess["_end"]):
                continue
            if note["reason"] not in sess["reasons"]:
                sess["reasons"] = sorted([*sess["reasons"], note["reason"]])
            if note["reason"] == "coarse_covered" and not counted:
                sess["coarse_covered"] += 1
                counted = True
    for sess in built:
        del sess["_start"], sess["_end"]
    return built
