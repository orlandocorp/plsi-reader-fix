"""Equivalent spellings of one unit. The number is never scaled.

These are identity aliases, not conversions. ``mmHg`` and ``mm[Hg]`` are the same
pressure unit. Apple Health writes VO2max as ``mL/min·kg`` (U+00B7, millilitres per
minute per kilogram), the same quantity as the map unit ``mL/min/kg``. A factor
(lb to kg, mmol/L to mg/dL, L to mL) is not an alias and is not invented here.
``plsi.life.normalize`` and ``plsi.map.units`` both use this table, so the two paths
agree. The original spelling stays on the observation (``unit_original``) and in the
Apple export CSV.
"""

from __future__ import annotations

_BP = "mm[Hg]"
_VO2 = "mL/min/kg"

# Space-free, lower-cased spelling -> canonical spelling. Middle dot is U+00B7.
_ALIAS = {
    "mmhg": _BP,
    "mm[hg]": _BP,
    "ml/min·kg": _VO2,
    "ml/min⋅kg": _VO2,  # U+22C5 dot operator, same Apple compound unit
    "ml/min/kg": _VO2,
    "ml/kg/min": _VO2,
    "ml/kg*min": _VO2,  # HealthKit unit string ml/kg*min
    "ml/(kg·min)": _VO2,
    "ml/(kg⋅min)": _VO2,
    "ml/(kg*min)": _VO2,
    "ml/(kg.min)": _VO2,  # UCUM
}


def _key(unit: str) -> str:
    return unit.strip().replace(" ", "").replace("⋅", "·").lower()


def identity_alias(unit: str | None) -> str | None:
    """Canonical spelling when ``unit`` is a documented equivalent, else None.

    The numeric value is unchanged. None or blank is None. An unknown unit is None
    (the caller keeps its own spelling rules).
    """
    if unit is None or not str(unit).strip():
        return None
    return _ALIAS.get(_key(str(unit)))
