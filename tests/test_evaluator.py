import asyncio
import json
from pathlib import Path

from cqa.checklist import Checklist
from cqa.evaluator import Evaluator, Sample, parse_json, validate_sample, vote
from cqa.llm import LLMCall, LLMResponse
from cqa.prompts import build_messages
from cqa.facts import extract_facts
from cqa.transcript import normalize

from tests.test_transcript_facts import CHAT

CL = Checklist.load(Path(__file__).resolve().parent.parent / "checklists" / "freedom_default.json")
D = normalize(CHAT)


class FakeClient:
    """Returns scripted responses per criterion title (looked up in the prompt)."""

    def __init__(self, by_title: dict[str, list[str]]):
        self.by_title = {k: list(v) for k, v in by_title.items()}
        self.prompts: list[list[dict]] = []

    async def chat(self, messages, temperature=0.0, max_tokens=2000):
        self.prompts.append(messages)
        user = messages[1]["content"]
        for title, queue in self.by_title.items():
            if f"КРИТЕРИЙ: {title}" in user:
                content = queue.pop(0) if len(queue) > 1 else queue[0]
                return LLMResponse(content, [LLMCall("fake", "fake", 0.1, 100, 50, 0.0, True)])
        raise AssertionError("unexpected prompt")


def j(answer, value=None, ev=("m4", "m5"), expl="ок"):
    return json.dumps({"reasoning": "r", "answer": answer, "value": value,
                       "evidence": [{"from": ev[0], "to": ev[1]}], "explanation": expl})


ALL_OK = {c.title: [j("yes")] for c in CL.criteria}
ALL_OK["Корректная и полная консультация"] = [j("score", 20)]
ALL_OK["Управление диалогом, скорость и грамотность"] = [j("score", 10)]
ALL_OK["Грубые ошибки"] = [j("no")]


def test_prompt_is_built_from_config():
    msgs = build_messages(CL, CL.criterion("c04"), D, extract_facts(D))
    user = msgs[1]["content"]
    assert "КРИТЕРИЙ: Корректная и полная консультация" in user
    assert "20 — Все существенные вопросы закрыты" in user
    assert "N/A разрешён, только если" in user
    assert "[m4] +03:15 OPERATOR: Здравствуйте!" in user
    assert "Время до первого ответа оператора в оценку не входит" in user
    assert "Порог первой реакции в 3 минуты" in msgs[0]["content"]


def test_voice_wording_used_for_calls():
    call = normalize({**CHAT, "channel": "voice call"})
    user = build_messages(CL, CL.criterion("c06"), call, extract_facts(call))[1]["content"]
    assert "Управление разговором" in user and "артефакты распознавания" in user


def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Вот ответ: {"a": 2} конец') == {"a": 2}


def test_validation_rejects_bad_step_and_unknown_ids():
    c04 = CL.criterion("c04")
    s, errs = validate_sample(json.loads(j("score", 12, ev=("m4", "m99"))), c04, D)
    assert s is None
    assert any("ступенью" in e for e in errs) and any("m99" in e for e in errs)
    s, errs = validate_sample(json.loads(j("na")), CL.criterion("c01"), D)
    assert s is None and "недопустим" in errs[0]


def test_vote_majority_and_median():
    c04 = CL.criterion("c04")
    mk = lambda a, v=None: Sample(answer=a, value=v, evidence=[], explanation="x")
    rep, conf = vote([mk("score", 15), mk("score", 20), mk("score", 15)], c04)
    assert (rep.answer, rep.value, conf) == ("score", 15, 0.67)
    rep, conf = vote([mk("score", 10), mk("score", 20)], c04)  # median 15 -> step 15
    assert rep.value == 15 and conf == 0.0
    rep, _ = vote([mk("no"), mk("yes"), mk("no")], CL.criterion("c01"))
    assert rep.answer == "no"


def test_end_to_end_with_repair():
    script = dict(ALL_OK)
    # first answer for c04 is invalid (12 is not a step) -> repaired on the second call
    script["Корректная и полная консультация"] = [j("score", 12), j("score", 15)]
    client = FakeClient(script)
    res = asyncio.run(Evaluator(client, samples=1).evaluate(CL, D))
    c04 = next(c for c in res.criteria if c.criterion_id == "c04")
    assert c04.status == "ok" and c04.value == 15
    assert res.score.total == 95.0
    assert res.score.gross_violation is False
    repaired = [p for p in client.prompts if len(p) > 2]
    assert repaired and "не является ступенью" in repaired[0][-1]["content"]


def test_unrecoverable_criterion_needs_review():
    script = dict(ALL_OK)
    script["Завершение"] = ["не json вообще"]
    res = asyncio.run(Evaluator(FakeClient(script), samples=1, repairs=1).evaluate(CL, D))
    c07 = next(c for c in res.criteria if c.criterion_id == "c07")
    assert c07.status == "needs_review"
    assert res.score is None and "c07" in res.score_error
