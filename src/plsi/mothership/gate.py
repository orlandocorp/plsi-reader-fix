"""The gate every file passes before it leaves for the Longevity Mothership (WIRE §3): a known message kind with
the current schema_version, the fields of its kind only; for contributions only consented sections of this contributor
and coded content only (plsi.mothership.content), free and cpg codes only when a map declares them; for withdrawals
the proof exactly when deleting; for professional messages the token's professional id; and nothing that identifies
the person (plsi.mothership.scan: exact dates, contacts, URLs, id numbers, internal ids, identifying fields, the
instance names), with fields exempt from a pattern only at their exact paths and in their strict formats (the Mother's
checks, made no looser). A violation is {path, rule}, never the text it found. Empty violations = safe to send."""

from __future__ import annotations

import re
from typing import Any

from plsi import codes, files, paths
from plsi.errors import GateFailed, ValidationError
from plsi.files import sha256_hex
from plsi.map import registry
from plsi.mothership import content, enroll, note, scan, stats
from plsi.mothership.relay import SCHEMA_VERSION
from plsi.mothership.scan import HEX64, flag
from plsi.vocab import (CodeSystem, ContributionKind, FeedbackSeverity, FeedbackStage, HypothesisDirection, MothershipMessage as M,
                        RegistryItemKind, SourceKind, UserRole, WithdrawalRequest)

FIELDS: dict[M, tuple[set[str], set[str]]] = {  # kind -> (required, optional) fields beyond kind and schema_version
    M.CONTRIBUTION: ({"contributor_id", "consent_version", "packed", "kinds", "subject"}, set(ContributionKind) | {"minimization"}),
    M.WITHDRAWAL: ({"contributor_id", "request"}, {"proof"}),
    M.RESEARCH_NOTE: ({"items", "method"}, {"report", "files"}),
    M.REGISTRY_UPDATE: ({"items"}, set()),
    M.PREREGISTRATION: ({"professional_id", "preregistration_id", "hypothesis", "release", "analysis", "outcome", "alpha",
                         "direction", "parents"}, set()),
    M.FINDING: ({"professional_id", "finding_id", "preregistration_id", "release", "analysis", "result", "summary"}, set()),
    M.FEEDBACK: ({"role", "stages", "frustrations", "suggestions"}, set()),
}
FEEDBACK = {"stages": {"stage", "minutes", "measured"}, "frustrations": {"stage", "what", "severity"},
            "suggestions": {"stage", "what"}}
MAX_FEEDBACK = 20
MAX_MINUTES = 1440
MAX_WHAT = 300
ITEM_FIELDS = {
    M.RESEARCH_NOTE: {"claim", "grade", "evidence_class", "sources", "falsified_by", "next_step", *note.FIELDS},
    M.REGISTRY_UPDATE: {"kind", "name", "specialty", "country", "public_url", "methods", "credentials", "source_urls"},
}
CONTRIBUTOR = frozenset({M.CONTRIBUTION, M.WITHDRAWAL, M.REGISTRY_UPDATE})  # install token only
PROFESSIONAL = frozenset({M.PREREGISTRATION, M.FINDING})  # professional token only; research notes go by email
# pattern exemptions by exact path template (`[]` any index), per rule and kind, each a field of strict format checked
# below; keys are never exempt. Contribution codes are exempt from the id-number rule only in code objects of loinc,
# rxnorm, snomed and icd10cm (exempt_codes), never free or cpg. Source ids are exempt from the dotted-date form too ("23.12.02").
EXEMPT = {
    "exact_date": {M.RESEARCH_NOTE: frozenset({"items[].sources[].id", *note.SOURCE_PATHS, *note.FILE_PATHS})},
    "id_number": {
        M.CONTRIBUTION: frozenset({"contributor_id", "states[].map_hash"}),
        M.WITHDRAWAL: frozenset({"contributor_id", "proof"}),
        M.RESEARCH_NOTE: frozenset({"items[].sources[].id", *note.SOURCE_PATHS, *note.FILE_PATHS}),
        M.PREREGISTRATION: frozenset({"professional_id", "preregistration_id", "release", "parents[]"}),
        M.FINDING: frozenset({"professional_id", "finding_id", "preregistration_id", "release"})},
    "url": {M.REGISTRY_UPDATE: frozenset({"items[].public_url", "items[].source_urls[]"}),
            M.RESEARCH_NOTE: frozenset({"files[].name"})},
}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
VERSION = re.compile(r"[0-9A-Za-z._-]{1,64}")  # a release version, as the relay's
IDS = {"preregistration_id": re.compile(r"pr_[0-9a-f]{16}"), "finding_id": re.compile(r"f_[0-9a-f]{16}")}
# research-note sources: exactly {kind, id}, a public identifier in its strict form (WIRE §3)
SOURCE_IDS = {SourceKind.PMID: re.compile(r"[1-9]\d{0,7}"), SourceKind.DOI: re.compile(r"10\.\d{4,9}/\S{1,200}"),
              SourceKind.NCT: re.compile(r"NCT\d{8}"), SourceKind.MODEL: re.compile(r"(?=.*[A-Za-z])[A-Za-z0-9._:/-]{1,80}")}
RESULT_KEYS = ("effect", "interval", "n", "statistic", "df", "p")
# every field name of the wire: the only key text a path ever shows
NAMES = frozenset({"kind", "schema_version", "id", *scan.FORBIDDEN_KEYS, *content.NAMES, *RESULT_KEYS, *note.NAMES,
                   *(k for required, optional in FIELDS.values() for k in required | optional),
                   *(k for fields in ITEM_FIELDS.values() for k in fields),
                   *(k for fields in FEEDBACK.values() for k in fields),
                   "op", "map_id", "coordinate", "group_a", "group_b", *stats.GROUP_KEYS})


def _exemptions(doc: dict[str, Any], kind: M) -> scan.Exempt:
    exempt_codes = {f"{at}.code" for at, (system, _) in content.code_objects(doc).items()
                    if isinstance(system, str) and system in tuple(CodeSystem) and system not in codes.LOCAL}
    templates = {rule: by_kind.get(kind, frozenset()) for rule, by_kind in EXEMPT.items()}
    return lambda rule, plain, shown: plain in templates.get(rule, ()) or (
        rule == "id_number" and kind == M.CONTRIBUTION and shown in exempt_codes)


def _match(doc: dict[str, Any], key: str, pattern: re.Pattern[str], rule: str) -> list[dict[str, str]]:
    value = doc.get(key)
    ok = isinstance(value, str) and pattern.fullmatch(value)
    return [] if ok or key not in doc else [flag(key, rule)]


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) <= scan.MAX_NUMBER


def _result(result: Any) -> list[dict[str, str]]:
    if not isinstance(result, dict):
        return [flag("result", "bad_result")]
    out = [flag(f"result.{k}", "missing_field") for k in RESULT_KEYS if k not in result]
    out += [flag(scan.name(k, n, NAMES, "result"), "unknown_field") for n, k in enumerate(result) if k not in RESULT_KEYS]
    out += [flag(f"result.{k}", "bad_result") for k in ("effect", "statistic", "df", "p")
            if k in result and not _number(result[k])]
    if _number(result.get("p")) and not 0 <= result["p"] <= 1:
        out.append(flag("result.p", "bad_result"))
    if "n" in result and not (_number(result["n"]) and isinstance(result["n"], int) and result["n"] >= 0):
        out.append(flag("result.n", "bad_result"))
    interval = result.get("interval")
    if "interval" in result and not (isinstance(interval, list) and len(interval) == 2 and all(map(_number, interval))):
        out.append(flag("result.interval", "bad_result"))
    return out


def _professional(doc: dict[str, Any], kind: M) -> list[dict[str, str]]:
    out = _match(doc, "professional_id", UUID, "bad_id") + _match(doc, "release", VERSION, "bad_release")
    for key, pattern in IDS.items():
        out += _match(doc, key, pattern, "bad_id")
    try:
        if "analysis" in doc:
            stats.check_spec(doc["analysis"])
    except ValidationError:
        out.append(flag("analysis", "bad_spec"))
    if kind == M.FINDING:
        return out + (_result(doc["result"]) if "result" in doc else [])
    alpha = doc.get("alpha")
    if "alpha" in doc and not (_number(alpha) and 0 < alpha < 1):
        out.append(flag("alpha", "bad_alpha"))
    if "direction" in doc and doc["direction"] not in tuple(HypothesisDirection):
        out.append(flag("direction", "bad_direction"))
    parents = doc.get("parents")
    if "parents" in doc and not (isinstance(parents, list)
                                 and all(isinstance(f, str) and IDS["finding_id"].fullmatch(f) for f in parents)):
        out.append(flag("parents", "bad_parents"))
    return out


def _formats(doc: dict[str, Any], kind: M) -> list[dict[str, str]]:
    """The strict formats of the fields exempt from the id-number rule, and the kinds' own field rules (as the Mother)."""
    out = _match(doc, "contributor_id", HEX64, "bad_contributor_id") if kind in CONTRIBUTOR else []
    out += _match(doc, "proof", HEX64, "bad_proof")
    if kind == M.CONTRIBUTION:
        out += content.violations(doc)
    return out + (_professional(doc, kind) if kind in PROFESSIONAL else [])


def _sources(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for n, item in enumerate(items):
        sources = item.get("sources", [])
        for m, s in enumerate(sources if isinstance(sources, list) else [None]):
            pattern = SOURCE_IDS.get(s["kind"]) if isinstance(s, dict) and isinstance(s.get("kind"), str) else None
            if not (pattern and set(s) == {"kind", "id"} and isinstance(s["id"], str) and pattern.fullmatch(s["id"])):
                out.append(flag(f"items[{n}].sources[{m}]", "bad_source"))
    return out


def _feedback(doc: dict[str, Any]) -> list[dict[str, str]]:
    """The plsi_feedback rules of WIRE §8, as the Mother."""
    out = [] if doc.get("role") in tuple(UserRole) else [flag("role", "unknown_role")]
    for key, fields in FEEDBACK.items():
        entries = doc.get(key)
        if not isinstance(entries, list) or len(entries) > MAX_FEEDBACK:
            out.append(flag(key, "bad_entries"))
            continue
        for n, e in enumerate(entries):
            ok = isinstance(e, dict) and set(e) == fields and e["stage"] in tuple(FeedbackStage)
            ok = ok and ("minutes" not in e or (type(e["minutes"]) is int and 0 <= e["minutes"] <= MAX_MINUTES
                                                and type(e["measured"]) is bool))
            ok = ok and ("what" not in e or (isinstance(e["what"], str) and 0 < len(e["what"].strip()) <= MAX_WHAT))
            ok = ok and ("severity" not in e or e["severity"] in tuple(FeedbackSeverity))
            if not ok:
                out.append(flag(f"{key}[{n}]", "bad_entry"))
    if not out and not (doc["frustrations"] or doc["suggestions"] or doc["stages"]):
        out.append(flag("$", "empty_feedback"))
    return out


def _keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    return {k for v in obj for k in _keys(v)} if isinstance(obj, list) else set()


def _shape(doc: dict[str, Any], kind: M) -> list[dict[str, str]]:
    required, optional = FIELDS[kind]
    out = [flag(k, "missing_field") for k in sorted(required - set(doc))]
    known = required | optional | {"kind", "schema_version"}
    out += [flag(scan.name(k, n, NAMES), "unknown_field") for n, k in enumerate(doc) if k not in known]
    if kind in ITEM_FIELDS:
        items = doc.get("items")
        if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
            return [*out, flag("items", "bad_items")]
        out += [flag(scan.name(k, m, NAMES, f"items[{n}]"), "unknown_field")
                for n, item in enumerate(items) for m, k in enumerate(item) if k not in ITEM_FIELDS[kind]]
        if kind == M.REGISTRY_UPDATE:
            out += [flag(f"items[{n}].kind", "unknown_kind")
                    for n, item in enumerate(items) if item.get("kind") not in tuple(RegistryItemKind)]
        if kind == M.RESEARCH_NOTE:
            out += _sources(items) + note.check(items, SOURCE_IDS[SourceKind.MODEL].fullmatch, doc.get("report")) + note.files(doc)
    separated = "professional_id" if kind in CONTRIBUTOR else "contributor_id" if kind in PROFESSIONAL else None
    if separated and separated in _keys(doc):
        out.append(flag(separated, "identity_mix"))
    if kind == M.RESEARCH_NOTE and {"contributor_id", "professional_id"} & _keys(doc):
        out.append(flag("items", "identity_mix"))
    if kind == M.FEEDBACK:
        out += _feedback(doc)
    return out


def _declared(doc: dict[str, Any], instance: str) -> list[dict[str, str]]:
    """PLSI's own names leave only as a shipped or installed map declares them: a free or cpg code that a map accepts,
    a known map_id and map_version, coordinate keys and gaps of that map (`bad_code`, `bad_id`, `bad_format`)."""
    declared, maps = registry.declared_codes(instance), registry.load_all(instance)
    out = [flag(at, "bad_code") for at, code in content.code_objects(doc).items()
           if code[0] in tuple(codes.LOCAL) and not (isinstance(code[1], str) and code in declared)]
    states = doc.get("states") if isinstance(doc.get("states"), list) else []
    every = {c.id for m in maps.values() for c in m.coordinates}
    for n, state in enumerate(states):
        if not isinstance(state, dict):
            continue
        m = maps.get(state["map_id"]) if isinstance(state.get("map_id"), str) else None
        if m is None:
            out += [flag(f"states[{n}].map_id", "bad_id")] if "map_id" in state else []
            continue
        if state.get("map_version", m.map_version) != m.map_version:
            out.append(flag(f"states[{n}].map_version", "bad_format"))
        ids = {c.id for c in m.coordinates}
        coordinates = state.get("coordinates") if isinstance(state.get("coordinates"), dict) else {}
        gaps = state.get("gaps") if isinstance(state.get("gaps"), list) else []
        out += [flag(f"states[{n}].coordinates.#key[{c}]", "bad_id") for c, k in enumerate(coordinates) if k not in ids]
        out += [flag(f"states[{n}].gaps[{g}]", "bad_id") for g, k in enumerate(gaps) if not (isinstance(k, str) and k in ids)]
    by = doc.get("scores", {}).get("by_coordinate") if isinstance(doc.get("scores"), dict) else None
    keys = by if isinstance(by, dict) else {}
    return out + [flag(f"scores.by_coordinate.#key[{c}]", "bad_id") for c, k in enumerate(keys) if k not in every]


def _contributor(doc: dict[str, Any], kind: M, enrollment: dict[str, Any], consent_version: str | None) -> list[dict[str, str]]:
    out = []
    # a delete_all whose proof hashes to its own contributor id needs no enrollment match: it stays sendable after a
    # later join made a new pseudonym (sha256(proof) == contributor_id already proves ownership)
    proven = (kind == M.WITHDRAWAL and doc.get("request") == WithdrawalRequest.DELETE_ALL and "proof" in doc
              and sha256_hex(str(doc["proof"])) == doc.get("contributor_id"))
    if not proven and doc.get("contributor_id") != enrollment.get("contributor_id"):
        out.append(flag("contributor_id", "contributor_mismatch"))
    if kind == M.CONTRIBUTION:
        if not enroll.consented(enrollment) or doc.get("consent_version") != consent_version:
            out.append(flag("consent_version", "consent"))
        kinds = doc.get("kinds") if isinstance(doc.get("kinds"), list) else []
        given = {k for k in kinds if isinstance(k, str)} | {k for k in ContributionKind if k in doc}
        out += [flag(k, "not_consented") for k in sorted(given - set(enrollment.get("kinds", [])))
                if k in tuple(ContributionKind)]
    if kind == M.WITHDRAWAL:
        deleting = doc.get("request") == WithdrawalRequest.DELETE_ALL
        if doc.get("request") not in tuple(WithdrawalRequest):
            out.append(flag("request", "bad_request"))
        elif not deleting and "proof" in doc:
            out.append(flag("proof", "unexpected_proof"))
        elif deleting and ("proof" not in doc or sha256_hex(str(doc["proof"])) != doc.get("contributor_id")):
            out.append(flag("proof", "bad_proof"))
    return out


def instance_names(instance: str | None) -> set[str]:
    """The names never sent: every instance in PLSI_HOME (and `instance`), lower-cased, at least 4 characters."""
    found = {p.name for p in paths.plsi_home().iterdir() if (p / "subject.json").is_file()} if paths.plsi_home().is_dir() else set()
    return {n.lower() for n in found | ({instance} if instance else set()) if len(n) >= 4}


def violations(doc: Any, instance: str | None = None, enrollment: dict[str, Any] | None = None,
               consent_version: str | None = None, pid: str | None = None) -> list[dict[str, str]]:
    """Everything that stops `doc` from leaving the computer. Contributor kinds need the instance (with its enrollment
    and the current consent); professional kinds the professional id of the token."""
    if scan.too_deep(doc):
        return [flag("$", "too_deep")]
    if not isinstance(doc, dict) or doc.get("kind") not in tuple(M):
        return [flag("kind", "unknown_kind")]
    kind = M(doc["kind"])
    version = doc.get("schema_version")
    ok = type(version) is int and version == SCHEMA_VERSION  # not a bool, not a float
    out = ([] if ok else [flag("schema_version", "schema_version")]) + _shape(doc, kind) + _formats(doc, kind)
    if kind in CONTRIBUTOR and instance is None:
        out.append(flag("kind", "needs_instance"))
    if kind in (M.CONTRIBUTION, M.WITHDRAWAL) and instance is not None:
        out += _contributor(doc, kind, enrollment or {}, consent_version)
    if kind == M.CONTRIBUTION and instance is not None:
        out += _declared(doc, instance)
    if kind in PROFESSIONAL and (pid is None or doc.get("professional_id") != pid):
        out.append(flag("professional_id", "professional_mismatch"))
    extra = (note.LIMITS, note.MASKS) if kind == M.RESEARCH_NOTE else (None, None)
    out += scan.violations(doc, _exemptions(doc, kind), NAMES, instance_names(instance), *extra)
    return [dict(t) for t in dict.fromkeys(tuple(v.items()) for v in out)]  # each {path, rule} once


def approved(doc: Any, expected: str | None) -> None:
    """Raise unless `doc` is the file the user approved (its package_hash)."""
    if expected is not None and files.hash_json(doc) != expected:
        raise ValidationError("the file changed since it was checked", code="HASH_MISMATCH",
                              hint="check the file again and ask the user to approve the new one")


def require(doc: Any, instance: str | None = None, pid: str | None = None, what: str = "file") -> None:
    """Raise GateFailed listing every reason `doc` may not leave the computer."""
    record = (enroll.enrollment(instance) or {}) if instance else None
    if found := violations(doc, instance, record, enroll.current_consent(), pid):
        raise GateFailed(found, f"the {what} is not safe to send")
