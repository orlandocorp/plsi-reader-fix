"""Unit scaling into a coordinate's canonical unit: identity, a sourced map conversion, or a generic UCUM
prefix/volume scale (mg/dL -> g/L, 10*3/uL -> 10*9/L). Never a conversion between kinds of quantity."""

from __future__ import annotations

import re
from typing import Any

from plsi.map.models import MapCoordinate
from plsi.unit_alias import identity_alias

_PREFIX = {"": 1.0, "k": 1e3, "d": 1e-1, "c": 1e-2, "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}
_BASES = ("[IU]", "mol", "kat", "eq", "g", "L", "U")


def _sig(x: float) -> float:
    return float(f"{x:.12g}")


def _term(t: str) -> tuple[str, float] | None:
    """One side of a UCUM unit -> (base, scale): mg -> (g, 1e-3), 10*3 -> (#, 1e3), % -> ('', 0.01)."""
    if m := re.fullmatch(r"10\*(\d+)", t):
        return "#", 10.0 ** int(m.group(1))
    if t in ("", "1", "%"):
        return "", 0.01 if t == "%" else 1.0
    t = re.sub(r"(?<!\[)IU(?!\])", "[IU]", t)
    for base in _BASES:
        if t.endswith(base) and t[: -len(base)] in _PREFIX:
            return base, _PREFIX[t[: -len(base)]]
    return None


def _scale(unit: str) -> tuple[tuple[str, str], float] | None:
    num, _, den = unit.partition("/")
    a, b = _term(num), _term(den)
    return ((a[0], b[0]), a[1] / b[1]) if a and b and "/" not in den else None


def to_canonical(value: float, unit: str | None, c: MapCoordinate) -> tuple[float, dict[str, Any] | None]:
    """value in `unit` -> the coordinate's canonical unit: a sourced conversion, else a generic UCUM scale. ValueError otherwise.

    A documented identity alias (``mmHg`` / ``mm[Hg]``, ``mL/min·kg`` / ``mL/min/kg``) is the same unit:
    the value is not scaled and no conversion factor is recorded. Anything else still needs a sourced factor
    or a generic prefix/volume scale. This does not invent one."""
    if unit is not None and (aliased := identity_alias(unit)) is not None:
        unit = aliased
    if unit == c.unit:
        return value, None
    if unit is None:
        raise ValueError(f"no unit reported (canonical {c.unit})")
    for conv in c.conversions:
        if conv.from_unit == unit:
            return _sig(value * conv.factor), {"from_unit": unit, "factor": conv.factor}
    a, b = _scale(unit), (_scale(c.unit) if c.unit else None)
    if a and b and a[0] == b[0]:
        factor = _sig(a[1] / b[1])
        return _sig(value * factor), {"from_unit": unit, "factor": factor}
    raise ValueError(f"cannot convert {unit} to {c.unit} (no generic scale or sourced conversion)")
