"""Asleep sessions for one Apple Health source.

InBed and Awake are never minutes. Overlapping asleep intervals become their union.
A legacy ``Asleep`` interval that overlaps a stage (Core, Deep, REM, Unspecified) is
the coarse copy of those stages and is omitted, not added. Segments that touch the
same InBed record are one session even when an awake gap sits between them; the gap
is not added. With no InBed, an Awake record joins two blocks only when it covers a
gap of at most two hours, so a long daytime gap cannot glue two nights. The session
date is the local wake day: the calendar date of the latest end (asleep, or the
InBed end when that is later), in that timestamp's own offset. Duration uses
timezone-aware subtraction, so a DST fold is the elapsed time.
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


def _merge(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlap-union. Touching intervals (no gap) are one block. A gap stays a gap."""
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


def _minutes(block: dict[str, Any]) -> float:
    return (block["end"] - block["start"]).total_seconds() / 60


def _session(blocks: list[dict[str, Any]], anchor: dict[str, Any] | None, uncertain: bool,
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
        "minutes": sum(_minutes(b) for b in blocks), "segments": sum(b["segments"] for b in blocks),
        "offsets": sorted(offsets), "uncertain": uncertain, "inbed": anchor is not None,
        "awake": sum(1 for a in awake if _touch(first["start"], span_end, a["start"], a["end"])),
        "coarse_omitted": 0, "_start": first["start"], "_end": span_end,
    }


def _by_inbed(blocks: list[dict[str, Any]], inbed: list[dict[str, Any]], awake: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[int, list[dict[str, Any]]] = {}
    uncertain_group: set[int] = set()
    alone: list[dict[str, Any]] = []
    for block in blocks:
        hits = [i for i, bed in enumerate(inbed) if _touch(block["start"], block["end"], bed["start"], bed["end"])]
        if not hits:
            alone.append(_session([block], None, False, awake))
            continue
        best = max(hits, key=lambda i: (_overlap_s(block["start"], block["end"], inbed[i]["start"], inbed[i]["end"]), -i))
        groups.setdefault(best, []).append(block)
        if len(hits) > 1:
            uncertain_group.add(best)
    sessions = alone
    for idx, grouped in groups.items():
        sessions.append(_session(grouped, inbed[idx], idx in uncertain_group, awake))
    return sessions


def _bridged(left: dict[str, Any], right: dict[str, Any], awake: list[dict[str, Any]]) -> bool:
    gap = right["start"] - left["end"]
    if gap <= timedelta(0):
        return True
    if gap > MAX_AWAKE_BRIDGE:
        return False
    return any(a["start"] <= left["end"] and a["end"] >= right["start"] for a in awake)


def _by_awake(blocks: list[dict[str, Any]], awake: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = [[blocks[0]]]
    for nxt in blocks[1:]:
        if _bridged(groups[-1][-1], nxt, awake):
            groups[-1].append(nxt)
        else:
            groups.append([nxt])
    return [_session(group, None, False, awake) for group in groups]


def sessions(asleep: list[dict[str, Any]], inbed: list[dict[str, Any]], awake: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One source's asleep, InBed and Awake spans -> sessions. InBed and Awake add no minutes."""
    stages = [seg for seg in asleep if not seg.get("coarse")]
    omitted = [seg for seg in asleep if seg.get("coarse") and stages and any(
        _touch(seg["start"], seg["end"], stage["start"], stage["end"]) for stage in stages)]
    omitted_ids = {id(seg) for seg in omitted}
    kept = [seg for seg in asleep if id(seg) not in omitted_ids]
    blocks = _merge(kept)
    if not blocks:
        return []
    built = _by_inbed(blocks, inbed, awake) if inbed else _by_awake(blocks, awake)
    for leg in omitted:
        for sess in built:
            if _touch(leg["start"], leg["end"], sess["_start"], sess["_end"]):
                sess["coarse_omitted"] += 1
                break
    for sess in built:
        del sess["_start"], sess["_end"]
    return built
