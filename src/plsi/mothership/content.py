"""What a contribution may carry (WIRE §2): coded fields only, no free text. Every section is an object or list of
the fields below and nothing else; codes are {system, code}, valid in their system (plsi.codes); identifiers match
ID, units UNIT, months YYYY-MM, years YEARS; numbers are finite, not bool and at most MAX_NUMBER in size. The same
rules, rule names and paths as the Mother's gate: a coordinate key is `#key[<n>]`, an unknown key `#unknown[<n>]`
(n = its position), never the key text. Violations are {path, rule}."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from plsi import codes
from plsi.mothership.scan import HEX64, ID, MAX_NUMBER, MONTH, flag
from plsi.vocab import ClinicalStatus, Comparator, ContributionKind, Ethnicity, Sex, ValueStatus

CONSENT_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
MAP_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,32}")
UNIT = re.compile(r"[A-Za-z0-9%/.*^()\[\]{}'_-]{1,32}")  # UCUM-like, no whitespace
AGE_BANDS = frozenset({*(f"{lo}-{lo + 4}" for lo in range(0, 116, 5)), "90+"})
YEARS = range(1900, 2101)
SCORE_KEYS = frozenset({"n_scored", "n_missed", "within_interval_rate", "nominal_level", "mean_abs_error",
                        "beat_persistence_rate", "direction_accuracy"})
Check = Callable[[Any, str], list[dict[str, str]]]


def number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) <= MAX_NUMBER  # False for NaN, inf


def code_ok(code: Any) -> bool:
    """Exactly {system, code}, a valid code of its system."""
    return isinstance(code, dict) and set(code) == {"system", "code"} and codes.valid(code["system"], code["code"])


def _num(v: Any, at: str) -> list[dict[str, str]]:
    return [] if number(v) else [flag(at, "bad_number")]


def _year(v: Any, at: str) -> list[dict[str, str]]:
    return [] if number(v) and isinstance(v, int) and v in YEARS else [flag(at, "bad_number")]


def _positive(v: Any, at: str) -> list[dict[str, str]]:
    return [] if number(v) and v > 0 else [flag(at, "bad_number")]


def _rate(v: Any, at: str) -> list[dict[str, str]]:
    return [] if v is None else _num(v, at)


def _match(pattern: re.Pattern[str], rule: str = "bad_format") -> Check:
    return lambda v, at: [] if isinstance(v, str) and pattern.fullmatch(v) else [flag(at, rule)]


def _one_of(allowed: frozenset[str]) -> Check:
    return lambda v, at: [] if isinstance(v, str) and v in allowed else [flag(at, "bad_format")]


def _code(v: Any, at: str) -> list[dict[str, str]]:
    return [] if code_ok(v) else [flag(at, "bad_code")]


def _list(check: Check) -> Check:
    def run(v: Any, at: str) -> list[dict[str, str]]:
        if not isinstance(v, list):
            return [flag(at, "bad_section")]
        return [x for n, item in enumerate(v) for x in check(item, f"{at}[{n}]")]
    return run


def _object(required: set[str], fields: dict[str, Check]) -> Check:
    """An object of these fields only (`unknown_field`), the required ones present (`missing_field`)."""
    def run(v: Any, at: str) -> list[dict[str, str]]:
        if not isinstance(v, dict):
            return [flag(at, "bad_section")]
        out = [flag(f"{at}.{k}", "missing_field") for k in sorted(required - set(v))]
        for n, (k, item) in enumerate(v.items()):
            out += fields[k](item, f"{at}.{k}") if k in fields else [flag(f"{at}.#unknown[{n}]", "unknown_field")]
        return out
    return run


def _keyed(check: Check) -> Check:
    """An object keyed by identifiers (`bad_id`), each key shown as `#key[<n>]`, each value checked."""
    def run(v: Any, at: str) -> list[dict[str, str]]:
        if not isinstance(v, dict):
            return [flag(at, "bad_section")]
        out = []
        for n, (k, item) in enumerate(v.items()):
            sub = f"{at}.#key[{n}]"
            out += ([] if isinstance(k, str) and ID.fullmatch(k) else [flag(sub, "bad_id")]) + check(item, sub)
        return out
    return run


_id = _match(ID, "bad_id")
_month = _match(MONTH)
_unit = _match(UNIT)
_summary = _object(set(), dict.fromkeys(SCORE_KEYS, _rate))
DOSE = _object({"value", "unit"}, {"value": _positive, "unit": _unit})
def _count(v: Any, at: str) -> list[dict[str, str]]:
    """How many values a mean covers: an integer of at least 1."""
    return [] if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else [flag(at, "bad_number")]


def _series(v: Any, at: str) -> list[dict[str, str]]:
    """Points of {t, value}, at most one per month: a month is the finest grain a package carries."""
    out = _list(_object({"t", "value"}, {"t": _month, "value": _num, "n": _count}))(v, at)
    months = [p.get("t") for p in v if isinstance(p, dict)] if isinstance(v, list) else []
    return out + [flag(f"{at}[{n}]", "repeat_month") for n, m in enumerate(months) if m in months[:n]]


COORDINATE = _object(set(), {
    "status": _one_of(frozenset(ValueStatus)), "value": _num, "unit": _unit, "comparator": _one_of(frozenset(Comparator)), "observed": _month,
    "series": _series,
    "items": _list(_object({"code"}, {"code": _code, "clinical_status": _one_of(frozenset(ClinicalStatus)), "dose": DOSE}))})
STATE = _object({"map_id", "coordinates"}, {
    "map_id": _id, "map_version": _match(MAP_VERSION), "map_hash": _match(HEX64), "as_of": _month,
    "gaps": _list(_id), "coordinates": _keyed(COORDINATE)})
EVENT = _object({"kind", "year", "basis", "code"}, {
    "kind": _id, "year": _year, "until": _year, "basis": _id, "code": _code, "value": _num, "unit": _unit,
    "clinical_status": _one_of(frozenset(ClinicalStatus)), "dose": DOSE})
SECTIONS = {ContributionKind.STATES: _list(STATE), ContributionKind.LIFE_EVENTS: _list(EVENT),
            ContributionKind.SCORES: _object(set(), {"overall": _summary, "by_coordinate": _keyed(_summary)})}
SUBJECT_FIELDS = frozenset({"sex", "age_band", "ethnicity"})
SUBJECT = _object(set(), {"sex": _one_of(frozenset(Sex)), "age_band": _one_of(AGE_BANDS),
                          "ethnicity": _one_of(frozenset(Ethnicity))})


def _id_list_or_null(v: Any, at: str) -> list[dict[str, str]]:
    return [] if v is None else _list(_id)(v, at)


def _kind_list_or_null(v: Any, at: str) -> list[dict[str, str]]:
    return [] if v is None else _list(_one_of(frozenset(ContributionKind)))(v, at)


def _decimals(v: Any, at: str) -> list[dict[str, str]]:
    return [] if v is None or (isinstance(v, int) and not isinstance(v, bool) and v in (0, 1, 2)) else [flag(at, "bad_format")]


# the local minimization record of a package (plsi.mothership.pack.record): never on the wire, the Mother does not
# accept it; `mothership check` reads it from the instance and checks the package against it
MINIMIZATION = _object({"maps", "kinds", "omitted_subject_fields", "decimals"}, {
    "maps": _id_list_or_null, "kinds": _kind_list_or_null,
    "omitted_subject_fields": _list(_one_of(SUBJECT_FIELDS)), "decimals": _decimals})
# every field name above: the only key text a path shows
NAMES = frozenset({"map_id", "map_version", "map_hash", "as_of", "gaps", "coordinates", "status", "value", "unit",
                   "comparator", "observed", "series", "t", "n", "items", "code", "system", "clinical_status", "kind",
                   "year", "until", "basis", "dose", "overall", "by_coordinate", "sex", "age_band", "ethnicity",
                   *SCORE_KEYS})


def code_objects(doc: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """{path: (system, code)} of every code object (a dict) of the set items and life events."""
    found = {}
    states = doc.get("states") if isinstance(doc.get("states"), list) else []
    for n, state in enumerate(states):
        coordinates = state.get("coordinates") if isinstance(state, dict) else None
        for c, value in enumerate(coordinates.values() if isinstance(coordinates, dict) else ()):
            items = value.get("items") if isinstance(value, dict) else None
            for j, item in enumerate(items if isinstance(items, list) else ()):
                found[f"states[{n}].coordinates.#key[{c}].items[{j}].code"] = item.get("code") if isinstance(item, dict) else None
    events = doc.get("life_events") if isinstance(doc.get("life_events"), list) else []
    found |= {f"life_events[{n}].code": e.get("code") if isinstance(e, dict) else None for n, e in enumerate(events)}
    return {at: (code.get("system"), code.get("code")) for at, code in found.items() if isinstance(code, dict)}


def violations(doc: dict[str, Any]) -> list[dict[str, str]]:
    """Everything wrong with a contribution's own fields and sections beyond the string rules (plsi.mothership.scan)."""
    out = []
    kinds = doc.get("kinds")
    if not isinstance(kinds, list) or not all(isinstance(k, str) and k in tuple(ContributionKind) for k in kinds):
        out.append(flag("kinds", "unknown_kind"))
    else:
        out += [flag(s, "not_in_kinds") for s in ContributionKind if s in doc and s not in kinds]
    checks = {"packed": _month, "consent_version": _match(CONSENT_VERSION), "subject": SUBJECT, **SECTIONS}
    out += [x for field, check in checks.items() if field in doc for x in check(doc[field], field)]
    return out


def minimized(doc: dict[str, Any], minimization: Any) -> list[dict[str, str]]:
    """Everything in a contribution that its local minimization record left out, and a malformed record (paths under
    `minimization`). The record stays on this computer; the package must not carry it."""
    if not isinstance(minimization, dict):
        return [flag("minimization", "bad_minimization")]
    out = MINIMIZATION(minimization, "minimization")
    mini = minimization
    omitted = mini.get("omitted_subject_fields") if isinstance(mini.get("omitted_subject_fields"), list) else []
    subject = doc.get("subject") if isinstance(doc.get("subject"), dict) else {}
    out += [flag(f"subject.{field}", "not_omitted") for field in omitted if field in subject]
    allowed = mini.get("maps")
    if isinstance(allowed, list):
        states = doc.get("states") if isinstance(doc.get("states"), list) else []
        out += [flag(f"states[{n}].map_id", "not_in_maps") for n, state in enumerate(states)
                if isinstance(state, dict) and state.get("map_id") not in allowed]
    requested = mini.get("kinds")
    packed = doc.get("kinds") if isinstance(doc.get("kinds"), list) else []
    if isinstance(requested, list):
        out += [flag("kinds", "not_in_kinds") for kind in packed if kind not in requested]
    return out
