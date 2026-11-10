"""The deidentified package for the Longevity Mothership, built from published states, recorded life events and
prediction scores, gate-checked (plsi.mothership.gate) into the instance's outbox; plsi.mothership.send sends it.
Coded fields only (WIRE §2): set items and life events go as their code, never their text, and a free or cpg code
only when a shipped or installed map declares it, never an ethnicity, race or occupation code (plsi.codes.NEVER_SHARED:
the subject carries the ethnicity group, sex and a five-year age band); a unit only when it is a map's canonical unit or a form of the
unit table (plsi.life.normalize), else the value stays too. A structured dose goes as {value, unit} only when its
unit is in DOSE_UNITS or a map's canonical unit; the dose as written never leaves.

No age leaves (consent: an age band only). A coordinate computed from chronological age stays on this computer: every
coordinate plsi.map.derive.age_based finds in the state's map (a derive given the age, any value in years such as a
clock's age or age acceleration, and anything derived from those), plus LOCAL_COORDINATES, which give the age back
with another coordinate.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from plsi import codes, files
from plsi.errors import GateFailed, NotFound, ValidationError
from plsi.clock import now, ts_filename
from plsi.life.store import EventStore
from plsi.life.normalize import known_unit
from plsi.map import derive, mapper, registry
from plsi.map.models import CoordinateValue
from plsi.models import Code
from plsi.mothership import content, gate
from plsi.mothership.enroll import active, folder
from plsi.mothership.relay import SCHEMA_VERSION, address
from plsi.policy.score import calibration
from plsi.vocab import ContributionKind, EventType, MothershipMessage, ObservationKind

LIFE_KINDS = frozenset({ObservationKind.CONDITION, ObservationKind.INTERVENTION, ObservationKind.LIFESTYLE,
                        ObservationKind.SYMPTOM, ObservationKind.MEDICATION, ObservationKind.ALLERGY})
LOCAL_COORDINATES = frozenset({"months_since_final_period"})  # with age_at_menopause it gives the age
MIN_SCORES = 5
# UCUM symbols a dose may carry on the wire (annotations for counted forms)
DOSE_UNITS = frozenset({"mg", "g", "ug", "ng", "mL", "L", "uL", "[iU]", "[IU]", "U", "mmol", "umol", "meq", "%",
                        "{tablet}", "{capsule}", "{puff}", "{drop}", "{spray}", "{patch}", "{dose}"})


def _month(t: datetime | None) -> str | None:
    return t.strftime("%Y-%m") if t else None


def _round(value: float, decimals: int | None) -> float:
    """`None` leaves the value. Otherwise round half even to that many decimal places."""
    return value if decimals is None else float(f"{value:.{decimals}f}")


def _monthly(series: list[Any], decimals: int | None) -> list[dict[str, Any]]:
    """One point per month: its mean and `n`. Default means keep six significant figures."""
    by: dict[str, list[float]] = {}
    for p in series:
        by.setdefault(str(_month(p.t)), []).append(p.value)
    out = []
    for month, vals in sorted(by.items()):
        mean = fmean(vals)
        value = float(f"{mean:.6g}") if decimals is None else _round(mean, decimals)
        out.append({"t": month, "value": value, "n": len(vals)})
    return out


class _Wire:
    """What may leave for this instance: the declared free and cpg codes and the known units."""

    def __init__(self, instance: str, decimals: int | None = None) -> None:
        self.declared = registry.declared_codes(instance)
        self.units = registry.canonical_units(instance)
        self.decimals = decimals

    def code(self, code: Code | None) -> dict[str, str] | None:
        """{system, code} of a code that may leave (plsi.codes.shareable); the display never leaves."""
        return {"system": code.system.value, "code": code.code} if code and codes.shareable(
            code.system.value, code.code, self.declared) else None

    def unit(self, unit: str | None) -> bool:
        return unit is None or unit in self.units or known_unit(unit)

    def dose(self, quantity: float | None, unit: str | None) -> dict[str, Any]:
        """{"dose": {value, unit}} of a structured dose whose unit may leave, else {}."""
        ok = quantity is not None and unit is not None and (unit in DOSE_UNITS or unit in self.units)
        return {"dose": {"value": quantity, "unit": unit}} if ok else {}

    def coordinate(self, v: CoordinateValue) -> dict[str, Any]:
        out: dict[str, Any] = {"status": v.status.value}
        if isinstance(v.value, list):
            out["items"] = [{"code": code} | ({"clinical_status": i.clinical_status.value} if i.clinical_status else {})
                            | self.dose(i.dose_quantity, i.dose_unit) for i in v.value if (code := self.code(i.code))]
        else:
            out |= {k: val for k, val in {"value": v.value, "unit": v.unit,
                                          "comparator": v.comparator.value if v.comparator else None,
                                          "observed": _month(v.observed_at)}.items() if val is not None}
            if self.decimals is not None and isinstance(out.get("value"), (int, float)) and not isinstance(out.get("value"), bool):
                out["value"] = _round(float(out["value"]), self.decimals)
        if v.series:
            out["series"] = _monthly(v.series, self.decimals)
            month = [p["value"] for p in out["series"] if p["t"] == out.get("observed")]
            if "value" in out and v.comparator is None and month:  # the headline value is that month's mean too
                out["value"] = month[0]
        return out

    def states(self, store: EventStore, allowed: set[str] | None = None) -> list[dict[str, Any]]:
        out = []
        for doc in mapper.latest_states(store.instance):
            if doc.map_id in mapper.LOCAL_MAPS or (allowed is not None and doc.map_id not in allowed):
                continue
            local = LOCAL_COORDINATES | derive.age_based(registry.load_snapshot(store.instance, doc.map_id, doc.map_hash).coordinates)
            coordinates = {c: self.coordinate(v) for c, v in doc.coordinates.items()
                           if v.value is not None and c not in local and (isinstance(v.value, list) or self.unit(v.unit))}
            if not coordinates:
                continue
            out.append({"map_id": doc.map_id, "map_version": doc.map_version, "map_hash": doc.map_hash.removeprefix("sha256:"),
                        "as_of": _month(doc.as_of), "gaps": sorted(g.coordinate for g in doc.gaps),
                        "coordinates": coordinates})
        return out

    def life_events(self, store: EventStore) -> list[dict[str, Any]]:
        out = []
        for e in store.events(EventType.OBSERVATION):
            o = e.payload
            if o.kind in LIFE_KINDS and (code := self.code(o.code)):
                measured = o.value_quantity is not None and self.unit(o.unit)
                item = {"kind": o.kind.value, "year": o.effective_at.year,
                        "until": o.effective_end.year if o.effective_end else None, "basis": o.effective_basis.value,
                        "code": code, "value": o.value_quantity if measured else None, "unit": o.unit if measured else None,
                        "clinical_status": o.clinical_status.value if o.clinical_status else None}
                out.append({k: v for k, v in item.items() if v is not None} | self.dose(o.dose_quantity, o.dose_unit))
        return sorted(out, key=lambda i: (i["year"], i["kind"], i["code"]["system"], i["code"]["code"]))


def _scores(store: EventStore) -> dict[str, Any]:
    cal = calibration(store.instance)
    cells = {c: s for c, s in cal["by_coordinate"].items() if s["n_scored"] >= MIN_SCORES}
    return {"overall": cal["overall"], "by_coordinate": cells} if cal["overall"]["n_scored"] >= MIN_SCORES else {}


def _age_band(birth_year: int | None, age_group: str | None) -> str | None:
    if age_group:
        return age_group
    if birth_year is None:
        return None
    low = (now().year - birth_year) // 5 * 5
    return f"{low}-{low + 4}"


SUBJECT_FIELDS = frozenset({"sex", "age_band", "ethnicity"})
_PRECISION = {"int": 0, "1dp": 1, "2dp": 2}


def _id_list(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    if not ids:
        raise ValidationError("name at least one map id", code="MAP_NOT_FOUND")
    return sorted(set(ids))


def _decimals(decimals: int | None, precision: str | None) -> int | None:
    if precision is not None and precision not in _PRECISION:
        raise ValidationError("precision is int, 1dp or 2dp", code="BAD_PRECISION")
    chosen = _PRECISION[precision] if precision is not None else decimals
    if decimals is not None and precision is not None and decimals != _PRECISION[precision]:
        raise ValidationError("decimals and precision disagree", code="BAD_PRECISION")
    if chosen is not None and chosen not in (0, 1, 2):
        raise ValidationError("decimals is 0, 1 or 2", code="BAD_PRECISION")
    return chosen


def _requested_kinds(raw: str | None, consented: set[str]) -> list[str] | None:
    if raw is None:
        return None
    kinds = sorted({part.strip() for part in raw.split(",") if part.strip()})
    unknown = [kind for kind in kinds if kind not in tuple(ContributionKind)]
    if unknown:
        raise ValidationError(f"unknown kind {unknown[0]!r}", code="UNKNOWN_KIND")
    extra = [kind for kind in kinds if kind not in consented]
    if extra:
        raise ValidationError(f"{extra[0]} is not a consented kind", code="NOT_CONSENTED")
    return kinds


def _omitted(fields: list[str] | None) -> list[str]:
    names = sorted(set(fields or []))
    unknown = [field for field in names if field not in SUBJECT_FIELDS]
    if unknown:
        raise ValidationError(f"cannot omit {unknown[0]!r}; subject fields are sex, age_band and ethnicity",
                              code="UNKNOWN_SUBJECT_FIELD")
    return names


DEFAULT_MINIMIZATION: dict[str, Any] = {"maps": None, "kinds": None, "omitted_subject_fields": [], "decimals": None}


def record_path(instance: str, package_hash: str) -> Path:
    """The local minimization record of the package with this package_hash. It never leaves the computer."""
    return folder(instance) / "minimization" / f"{package_hash.removeprefix('sha256:')}.json"


def recorded(instance: str, package_hash: str) -> dict[str, Any] | None:
    """The minimization the package with this package_hash was packed with, or None when there is no local record."""
    path = record_path(instance, package_hash)
    if not path.is_file():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        entry = None
    if not isinstance(entry, dict) or entry.get("package_hash") != package_hash:
        raise ValidationError("the local minimization record does not belong to this package", code="BAD_MINIMIZATION")
    return entry.get("minimization")


def document(instance: str, minimization: dict[str, Any] | None = None) -> dict[str, Any]:
    """The contribution for these minimization options. Writes nothing and does not send.

    The options are not part of the document: the Mother refuses an unknown root key (`minimization` was one), so the
    record of them stays local (`record_path`) and `mothership check` rebuilds the package from it."""
    record = active(instance)
    store = EventStore(instance)
    opts = minimization or {}
    consented = set(record["kinds"])
    requested = opts.get("kinds")
    use = set(requested) if isinstance(requested, list) else consented
    omit = list(opts.get("omitted_subject_fields") or [])
    decimals = opts.get("decimals")
    recorded_maps = opts.get("maps")
    allowed = set(recorded_maps) if isinstance(recorded_maps, list) else None
    subject = {k: v for k, v in {"sex": store.subject.sex.value,
                                 "age_band": _age_band(store.subject.birth_year, store.subject.age_group),
                                 "ethnicity": store.subject.ethnicity.value if store.subject.ethnicity else None}.items()
               if v and k not in omit}
    wire = _Wire(instance, decimals if isinstance(decimals, int) else None)
    sections: dict[str, Any] = {}
    if ContributionKind.STATES in use:
        states = wire.states(store, allowed)
        if states:
            sections[ContributionKind.STATES] = states
    if ContributionKind.LIFE_EVENTS in use:
        events = wire.life_events(store)
        if events:
            sections[ContributionKind.LIFE_EVENTS] = events
    if ContributionKind.SCORES in use:
        scores = _scores(store)
        if scores:
            sections[ContributionKind.SCORES] = scores
    doc: dict[str, Any] = {"kind": MothershipMessage.CONTRIBUTION, "schema_version": SCHEMA_VERSION,
                           "contributor_id": record["contributor_id"], "consent_version": record["consent_version"],
                           "packed": _month(now()), "kinds": sorted(sections), "subject": subject}
    doc |= sections
    return doc


def pack(instance: str, out: str | None = None, maps: str | None = None, exclude_maps: list[str] | None = None,
         kinds: str | None = None, omit_subject_fields: list[str] | None = None, decimals: int | None = None,
         precision: str | None = None) -> dict[str, Any]:
    """`mothership pack`: the deidentified package of the consented kinds, gate-checked; nothing is sent.

    Minimization only removes maps, kinds, subject fields or precision. It cannot add an identifying field.
    """
    record = active(instance)
    known = set(registry.load_all(instance))
    chosen = _id_list(maps)
    excluded = sorted(set(exclude_maps or []))
    for map_id in (chosen or []) + excluded:
        if map_id not in known:
            raise NotFound(f"unknown map {map_id!r}", code="MAP_NOT_FOUND", hint="plsi maps list")
    if chosen is None and not excluded:
        recorded_maps = None
    else:
        base = chosen if chosen is not None else sorted(known - set(mapper.LOCAL_MAPS))
        recorded_maps = sorted(set(base) - set(excluded) - set(mapper.LOCAL_MAPS))
    minimization = {"maps": recorded_maps, "kinds": _requested_kinds(kinds, set(record["kinds"])),
                    "omitted_subject_fields": _omitted(omit_subject_fields), "decimals": _decimals(decimals, precision)}
    doc = document(instance, minimization)
    gate.require(doc, instance, what="package")
    if found := content.minimized(doc, minimization):
        raise GateFailed(found, "the package is not minimized as asked")
    digest = files.hash_json(doc)
    path = Path(out) if out else folder(instance) / "outbox" / f"contribution-{ts_filename()}.json"
    record = record_path(instance, digest)
    files.atomic_write_json(record, {"package_hash": digest, "minimization": minimization})
    files.atomic_write_json(path, doc)
    counts = {"states": len(doc.get("states", [])), "life_events": len(doc.get("life_events", [])),
              "scored_predictions": doc.get("scores", {}).get("overall", {}).get("n_scored", 0)}
    return {"file": str(path), "to": address(), "package_hash": digest, "minimization_record": str(record),
            "counts": counts}

