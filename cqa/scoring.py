"""Deterministic scoring: turns per-criterion verdicts into points and a total.

The LLM only decides verdicts (Да / Нет / N/A / scale step). All arithmetic
lives here and follows the checklist's calculation order:

1. N/A only where allowed.
2. "Да" = full weight, "Нет" = 0; a scale step gives value / top_step of the weight.
3. Criteria with N/A are excluded and their weight is redistributed
   proportionally among applicable weighted criteria, so the maximum stays 100.
4. The total is rounded (half up) to `checklist.rounding` digits. If no
   weighted criterion is applicable, the total is not calculated.
5. Flag criteria (gross violations) carry no points and are reported separately.

Exact rational arithmetic (Fraction) is used so rounding is never affected by
float error.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel

from .checklist import Checklist, Criterion, CriterionType

MAX_TOTAL = Fraction(100)

Answer = Literal["yes", "no", "na", "score"]


class Verdict(BaseModel):
    criterion_id: str
    answer: Answer  # "score" is used for scale criteria together with `value`
    value: float | None = None


class ScoringError(ValueError):
    pass


class CriterionScore(BaseModel):
    criterion_id: str
    title: str
    section: str | None
    type: CriterionType
    answer: Answer
    value: float | None
    base_weight: float
    applicable: bool
    fraction: float | None          # share of the criterion's weight earned (0..1)
    effective_weight: float | None  # weight after N/A redistribution
    points: float | None            # rounded for display; totals use exact values


class SectionScore(BaseModel):
    section_id: str
    title: str
    points: float | None
    max_points: float | None
    percent: float | None


class ScoreResult(BaseModel):
    checklist_id: str
    checklist_version: int
    total: float | None
    max_total: float
    gross_violation: bool | None
    criteria: list[CriterionScore]
    sections: list[SectionScore]
    note: str | None = None


def _frac(x: float | int) -> Fraction:
    return Fraction(str(x))


def _round(x: Fraction, digits: int) -> float:
    q = Decimal(1).scaleb(-digits)
    d = (Decimal(x.numerator) / Decimal(x.denominator)).quantize(q, rounding=ROUND_HALF_UP)
    return float(d)


def validate_verdict(c: Criterion, v: Verdict) -> Fraction | None:
    """Validate a verdict and return the earned share of the weight (None for N/A)."""
    if v.answer == "na":
        if not c.na_allowed:
            raise ScoringError(f"{c.id}: N/A для этого критерия не допускается")
        return None
    if c.type == CriterionType.SCALE:
        if v.answer != "score" or v.value is None:
            raise ScoringError(f"{c.id}: для шкального критерия нужно выбрать ступень шкалы")
        steps = {_frac(s.value) for s in c.scale or []}
        if _frac(v.value) not in steps:
            allowed = ", ".join(f"{s.value:g}" for s in c.scale or [])
            raise ScoringError(f"{c.id}: {v.value:g} не является ступенью шкалы (допустимо: {allowed})")
        return _frac(v.value) / _frac(c.scale_max)
    if v.answer not in ("yes", "no"):
        raise ScoringError(f"{c.id}: ожидается «Да» или «Нет», получено «{v.answer}»")
    return Fraction(1) if v.answer == "yes" else Fraction(0)


def score(checklist: Checklist, verdicts: list[Verdict] | dict[str, Verdict]) -> ScoreResult:
    if isinstance(verdicts, list):
        by_id = {v.criterion_id: v for v in verdicts}
    else:
        by_id = dict(verdicts)

    criteria = checklist.active_criteria
    missing = [c.id for c in criteria if c.id not in by_id]
    if missing:
        raise ScoringError(f"нет ответа по критериям: {', '.join(missing)}")
    unknown = set(by_id) - {c.id for c in criteria}
    if unknown:
        raise ScoringError(f"ответы по неизвестным или отключённым критериям: {', '.join(sorted(unknown))}")

    earned: dict[str, Fraction | None] = {c.id: validate_verdict(c, by_id[c.id]) for c in criteria}

    weighted = [c for c in criteria if c.type != CriterionType.FLAG and c.weight > 0]
    applicable_weight = sum((_frac(c.weight) for c in weighted if earned[c.id] is not None), Fraction(0))
    factor = MAX_TOTAL / applicable_weight if applicable_weight > 0 else None

    rows: list[CriterionScore] = []
    exact_points: dict[str, Fraction] = {}
    exact_eff: dict[str, Fraction] = {}
    gross: bool | None = None
    digits = checklist.rounding

    for c in criteria:
        v = by_id[c.id]
        e = earned[c.id]
        eff = pts = None
        if c.type == CriterionType.FLAG:
            flag = v.answer == "yes"
            gross = flag if gross is None else (gross or flag)
        elif e is not None and factor is not None and c.weight > 0:
            eff = _frac(c.weight) * factor
            pts = e * eff
            exact_eff[c.id], exact_points[c.id] = eff, pts
        rows.append(CriterionScore(
            criterion_id=c.id,
            title=c.title,
            section=c.section,
            type=c.type,
            answer=v.answer,
            value=v.value,
            base_weight=c.weight,
            applicable=e is not None,
            fraction=float(e) if e is not None else None,
            effective_weight=_round(eff, digits) if eff is not None else None,
            points=_round(pts, digits) if pts is not None else None,
        ))

    sections: list[SectionScore] = []
    for s in checklist.sections:
        ids = [c.id for c in criteria if c.section == s.id and c.id in exact_eff]
        has_weighted = any(c.section == s.id and c in weighted for c in criteria)
        if not has_weighted:
            continue  # e.g. the gross-violation section: reported via gross_violation
        if ids:
            p = sum((exact_points[i] for i in ids), Fraction(0))
            m = sum((exact_eff[i] for i in ids), Fraction(0))
            sections.append(SectionScore(
                section_id=s.id, title=s.title,
                points=_round(p, digits), max_points=_round(m, digits),
                percent=_round(p / m * 100, digits) if m > 0 else None,
            ))
        else:
            sections.append(SectionScore(section_id=s.id, title=s.title,
                                         points=None, max_points=None, percent=None))

    total = None
    note = None
    if factor is None:
        note = "Нет применимых критериев с весом — итог не рассчитывается."
    else:
        total = _round(sum(exact_points.values(), Fraction(0)), digits)

    return ScoreResult(
        checklist_id=checklist.id,
        checklist_version=checklist.version,
        total=total,
        max_total=float(MAX_TOTAL),
        gross_violation=gross,
        criteria=rows,
        sections=sections,
        note=note,
    )
