"""Code and unit normalization (`normalize observations`).

LOINC via NLM Clinical Tables, RxNorm via RxNav, units via a local UCUM alias table. Base URLs come from
plsi.references.api_base("nlm_clinical_tables_loinc" | "rxnav"); HTTP only through plsi.net.
Must not import plsi.map: shipped map accepts/reject_codes are read as raw YAML from MAPS_DIR.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from plsi import net, references
from plsi.life.importers import observation_items
from plsi.unit_alias import identity_alias
from plsi.life.models import ObservationInput
from plsi.models import Code, LOINC_RE, parse
from plsi.files import atomic_write_json, load_json
from plsi.paths import REPO_ROOT
from plsi.vocab import CodeSystem, ObservationKind

MAPS_DIR = REPO_ROOT / "maps"
MAX_AMBIGUOUS_CANDIDATES = 5
RXNORM_MIN_SCORE = 2.0  # approximateTerm false hits score < 1 (e.g. an abbreviation matching a brand), true hits ~8-19

# ---------------------------------------------------------------- units

# Lower-cased, space-free, mu -> u, mcg -> ug spellings -> UCUM.
_UNIT_ALIASES = {
    "k/ul": "10*3/uL", "thou/ul": "10*3/uL", "m/ul": "10*6/uL", "mill/ul": "10*6/uL",
    "u/l": "U/L", "iu/l": "U/L", "fl": "fL", "%": "%", "percent": "%",
    "kg/m2": "kg/m2", "kg/m^2": "kg/m2",  # mmHg and VO2 spellings: plsi.unit_alias (shared with calc)
    "uiu/ml": "u[IU]/mL", "miu/l": "m[IU]/L", "miu/ml": "m[IU]/mL",
}
_COUNT_RE = re.compile(r"x?10[\^e*](\d+)/(ul|l)")
_CONC_RE = re.compile(r"([kmunp]?)(g|mol|eq)(?:/([dmu]?)l)?")


def normalize_unit(unit: str | None) -> str | None:
    """UCUM-syntax normalization of a reported unit string via a local alias table (no network, no value change).

    "K/uL", "x10E3/uL", "10^3/uL" -> "10*3/uL"; "mg/dl" -> "mg/dL"; "mcg/mL" -> "ug/mL"; "mEq/L" -> "meq/L".
    ``mmHg`` -> ``mm[Hg]`` and Apple VO2 ``mL/min·kg`` -> ``mL/min/kg`` via plsi.unit_alias (the value is not scaled).
    Unknown strings are returned stripped and unchanged; None or blank -> None.
    """
    if unit is None or not unit.strip():
        return None
    if (aliased := identity_alias(unit)) is not None:
        return aliased
    raw = unit.strip()
    low = raw.replace("µ", "u").replace("μ", "u").replace(" ", "").lower().replace("mcg", "ug")
    if low in _UNIT_ALIASES:
        return _UNIT_ALIASES[low]
    if m := _COUNT_RE.fullmatch(low):
        return f"10*{m[1]}/{'uL' if m[2] == 'ul' else 'L'}"
    if m := _CONC_RE.fullmatch(low):
        prefix, base, volume = m.groups()
        return f"{prefix}{base}" + (f"/{volume}L" if volume is not None else "")
    return raw


def known_unit(unit: str | None) -> bool:
    """`unit` is a form the table above produces: an alias target, a count (10*3/uL) or a concentration (mg/dL)."""
    return bool(unit) and (unit in _UNIT_ALIASES.values() or bool(re.fullmatch(r"10\*\d+/u?L|[kmunp]?(g|mol|eq)(/[dmu]?L)?", unit)))


def _family(unit: str | None) -> str | None:
    """Dimension family of a unit; units in one family are inter-convertible by generic scaling."""
    u = normalize_unit(unit)
    if u is None:
        return None
    if u in ("%", "1"):
        return "fraction"
    if re.fullmatch(r"(10\*\d+)?/[mu]?L", u):
        return "count"
    if m := re.fullmatch(r"[kmunp]?(g|mol|eq)/[dmu]?L", u):
        return "mass" if m[1] == "g" else "molar"
    if re.fullmatch(r"[kmu]?U/[dmu]?L", u):
        return "catalytic"
    if "[IU]/" in u:
        return "iu"
    return u


_PROPERTY_FAMILIES = {
    "MCnc": {"mass"}, "SCnc": {"molar"}, "MSCnc": {"mass", "molar"}, "NCnc": {"count"}, "CCnc": {"catalytic"},
    "ACnc": {"iu"}, "NFr": {"fraction"}, "MFr": {"fraction"}, "VFr": {"fraction"}, "SFr": {"fraction"},
}

# ---------------------------------------------------------------- LOINC

_SPECIMEN_RE = re.compile(r"\bin ([A-Z].*?)(?: by |\s--|$)")  # LOINC system names are capitalized
_DROP_RE = re.compile(r"^deprecated\b|\s--|challenge|\b\d+\s*hours?\b", re.I)  # deprecated, timed, challenge


def _map_codes() -> tuple[set[str], set[str]]:
    """(accepted, rejected) LOINC codes across shipped maps. A code accepted anywhere is never treated as rejected."""
    accepted: set[str] = set()
    rejected: set[str] = set()
    for path in sorted(MAPS_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for coord in doc.get("coordinates") or []:
            accepts = coord.get("accepts") or {}
            for key, into in (("codes", accepted), ("reject_codes", rejected)):
                into |= {str(c["code"]) for c in accepts.get(key) or [] if c.get("system") == CodeSystem.LOINC}
    return accepted, rejected - accepted


def loinc_candidates(text: str, unit: str | None = None, specimen: str | None = None) -> list[dict[str, Any]]:
    """Ranked LOINC candidates for a lab label (network).

    Drops deprecated, timed and challenge codes and any shipped-map reject_code. Rank: shipped-map accepted codes
    first, then unit/property compatibility (convertible counts as compatible), then specimen match, then API order.
    Returns [{code, display, specimen, property, units, map_accepted, compatible}].
    """
    reply = net.get_json(
        references.api_base("nlm_clinical_tables_loinc") + "/api/loinc_items/v3/search",
        {"terms": text, "type": "question", "maxList": 200, "df": "LOINC_NUM,LONG_COMMON_NAME",
         "ef": "PROPERTY,units,COMPONENT,SHORTNAME"},
    )
    codes, extra, display = reply[1] or [], reply[2] or {}, reply[3] or []
    accepted, rejected = _map_codes()
    obs_family = _family(unit)
    ranked = []
    for i, code in enumerate(codes):
        name = display[i][1] if i < len(display) else ""
        if code in rejected or not LOINC_RE.match(code) or (code not in accepted and _DROP_RE.search(name)):
            continue
        prop = (extra.get("PROPERTY") or [None] * len(codes))[i]
        units = [u["unit"] for u in (extra.get("units") or [None] * len(codes))[i] or [] if u.get("unit")]
        spec = m[1] if (m := _SPECIMEN_RE.search(name)) else None
        families = {_family(u) for u in units} | _PROPERTY_FAMILIES.get(prop or "", set())
        compatible = obs_family is None or obs_family in families  # no reported unit: nothing to contradict
        spec_match = bool(specimen and spec and specimen.lower() in spec.lower())
        ranked.append(((code not in accepted, not compatible, not spec_match, i), {
            "code": code, "display": name, "specimen": spec, "property": prop, "units": units,
            "map_accepted": code in accepted, "compatible": compatible,
        }))
    return [c for _, c in sorted(ranked, key=lambda r: r[0])]


# ---------------------------------------------------------------- RxNorm

def _rxnorm_groups(text: str) -> list[list[Code]]:
    """Ingredients per approximateTerm candidate (deduped by rxcui, best first)."""
    base = references.api_base("rxnav")
    reply = net.get_json(f"{base}/approximateTerm.json", {"term": text, "maxEntries": 10, "option": 1})
    seen: list[str] = []
    for cand in (reply.get("approximateGroup") or {}).get("candidate") or []:
        if cand.get("rxcui") and float(cand.get("score") or 0) >= RXNORM_MIN_SCORE and cand["rxcui"] not in seen:
            seen.append(cand["rxcui"])
    groups = []
    for rxcui in seen:
        related = net.get_json(f"{base}/rxcui/{rxcui}/related.json", {"tty": "IN PIN"})  # urlencode -> IN+PIN
        ingredients = [
            Code(system=CodeSystem.RXNORM, code=p["rxcui"], display=p.get("name"))
            for g in (related.get("relatedGroup") or {}).get("conceptGroup") or [] if g.get("tty") == "IN"
            for p in g.get("conceptProperties") or []
        ]
        groups.append(ingredients)
    return groups


def _flatten(groups: list[list[Code]]) -> list[Code]:
    out: dict[str, Code] = {}
    for c in (c for g in groups for c in g):
        out.setdefault(c.code, c)
    return list(out.values())


# ---------------------------------------------------------------- command

def _load_inputs(in_path: str) -> list[ObservationInput]:
    return [parse(ObservationInput, item, f"observation {i}") for i, item in enumerate(observation_items(load_json(in_path)))]


def normalize_observations(in_path: str, out_path: str) -> dict[str, Any]:
    """`plsi normalize observations --in F --out F` (network).

    Uncoded measurements get LOINC (auto-pick only when the top candidate is map-accepted AND compatible);
    uncoded medications get the RxNorm ingredient when the best match resolves to exactly one. Others pass through.
    Returns {out, counts: {total, coded, ambiguous, unchanged}, ambiguous: [{index, original_text, candidates}]}.
    """
    observations = _load_inputs(in_path)
    out, ambiguous, coded = [], [], 0
    for i, obs in enumerate(observations):
        code, candidates = None, None
        if obs.code is None and obs.kind == ObservationKind.MEASUREMENT:
            ranked = loinc_candidates(obs.original_text, obs.unit, obs.specimen)
            if ranked and ranked[0]["map_accepted"] and ranked[0]["compatible"]:
                code = Code(system=CodeSystem.LOINC, code=ranked[0]["code"], display=ranked[0]["display"])
            else:
                candidates = ranked[:MAX_AMBIGUOUS_CANDIDATES]
        elif obs.code is None and obs.kind == ObservationKind.MEDICATION:
            groups = _rxnorm_groups(obs.original_text)
            if groups and len(groups[0]) == 1:
                code = groups[0][0]
            else:
                candidates = [c.dump() for c in _flatten(groups)[:MAX_AMBIGUOUS_CANDIDATES]]
        if code is not None:
            obs, coded = obs.model_copy(update={"code": code}), coded + 1
        elif candidates is not None:
            ambiguous.append({"index": i, "original_text": obs.original_text, "candidates": candidates})
        out.append(parse(ObservationInput, obs.dump()).dump())
    target = Path(out_path).resolve()
    atomic_write_json(target, out)
    return {
        "out": str(target),
        "counts": {"total": len(out), "coded": coded, "ambiguous": len(ambiguous), "unchanged": len(out) - coded - len(ambiguous)},
        "ambiguous": ambiguous,
    }
