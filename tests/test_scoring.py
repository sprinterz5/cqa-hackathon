from pathlib import Path

import pytest

from cqa.checklist import Checklist
from cqa.scoring import ScoringError, Verdict, score

DEFAULT = Path(__file__).resolve().parent.parent / "checklists" / "freedom_default.json"


@pytest.fixture(scope="module")
def cl() -> Checklist:
    return Checklist.load(DEFAULT)


def v(**answers):
    """v(c01='yes', c04=15, c03='na') -> list[Verdict]"""
    out = []
    for cid, a in answers.items():
        if isinstance(a, (int, float)):
            out.append(Verdict(criterion_id=cid, answer="score", value=a))
        else:
            out.append(Verdict(criterion_id=cid, answer=a))
    return out


PERFECT = dict(c01="yes", c02="yes", c03="yes", c04=20, c05="yes", c06=10, c07="yes", c08="no")


def test_default_checklist_weights_sum_to_100(cl):
    assert sum(c.weight for c in cl.active_criteria) == 100


def test_perfect_dialog(cl):
    r = score(cl, v(**PERFECT))
    assert r.total == 100
    assert r.gross_violation is False


def test_scale_points_proportional_to_weight(cl):
    r = score(cl, v(**{**PERFECT, "c06": 7}))
    c06 = next(c for c in r.criteria if c.criterion_id == "c06")
    assert c06.points == 10.5
    assert r.total == 95.5


def test_na_redistributes_weight(cl):
    # c02, c03 N/A -> applicable weight 65; earned 10 + 15 + 15 + 15 + 5 = 60
    r = score(cl, v(**{**PERFECT, "c02": "na", "c03": "na", "c04": 15}))
    assert r.total == 92.31  # 60 / 65 * 100
    eff = sum(c.effective_weight or 0 for c in r.criteria)
    assert round(eff, 1) == 100.0
    start = next(s for s in r.sections if s.section_id == "start")
    assert start.points == 15.38 and start.percent == 100


def test_round_half_up_not_bankers(cl):
    # c03 N/A -> factor 100/80; raw 10 + 10.5 = 20.5 -> 25.625 exactly -> 25.63
    r = score(cl, v(c01="yes", c02="no", c03="na", c04=0, c05="no", c06=7, c07="no", c08="no"))
    assert r.total == 25.63


def test_gross_violation_is_separate_from_total(cl):
    r = score(cl, v(**{**PERFECT, "c08": "yes"}))
    assert r.gross_violation is True
    assert r.total == 100
    assert all(s.section_id != "gross" for s in r.sections)


def test_na_not_allowed(cl):
    with pytest.raises(ScoringError, match="c01"):
        score(cl, v(**{**PERFECT, "c01": "na"}))


def test_value_must_be_a_scale_step(cl):
    with pytest.raises(ScoringError, match="not a scale step"):
        score(cl, v(**{**PERFECT, "c04": 12}))


def test_missing_verdict(cl):
    answers = dict(PERFECT)
    answers.pop("c05")
    with pytest.raises(ScoringError, match="c05"):
        score(cl, v(**answers))


def test_no_applicable_weighted_criteria():
    cl = Checklist.model_validate({
        "id": "t", "name": "t",
        "criteria": [
            {"id": "a", "title": "A", "type": "binary", "weight": 50, "description": "d",
             "na_allowed": True, "na_condition": "n"},
            {"id": "g", "title": "G", "type": "flag", "weight": 0, "description": "d"},
        ],
    })
    r = score(cl, v(a="na", g="no"))
    assert r.total is None and r.note


def test_replaced_criterion_is_scored_from_config(cl):
    data = cl.model_dump(mode="json")
    data["criteria"] = [c for c in data["criteria"] if c["id"] != "c05"] + [{
        "id": "c09", "section": "conduct", "title": "Новый критерий", "type": "scale",
        "weight": 15, "description": "d",
        "scale": [{"value": 0, "description": "x"}, {"value": 3, "description": "y"},
                  {"value": 5, "description": "z"}],
    }]
    new = Checklist.model_validate(data)
    answers = {k: a for k, a in PERFECT.items() if k != "c05"}
    r = score(new, v(**answers, c09=3))
    assert r.total == 94.0  # 100 - 15 * 2/5


def test_voice_override_keeps_scale_values(cl):
    c06 = cl.criterion("c06")
    voice = c06.for_channel("voice call")
    assert voice.title != c06.title
    assert [s.value for s in voice.scale] == [s.value for s in c06.scale]
    assert voice.weight == c06.weight
    assert cl.criterion("c02").for_channel("voice call") is cl.criterion("c02")


def test_invalid_checklist_rejected():
    with pytest.raises(ValueError):
        Checklist.model_validate({
            "id": "t", "name": "t",
            "criteria": [{"id": "a", "title": "A", "type": "flag", "weight": 5, "description": "d"}],
        })
