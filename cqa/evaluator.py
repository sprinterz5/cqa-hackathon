"""LLM evaluation: one prompt per criterion, N samples, validation with repair, voting, scoring."""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from collections import Counter

from pydantic import BaseModel

from .checklist import Checklist, Criterion, CriterionType
from .facts import Facts, extract_facts
from .llm import LLMCall, LLMClient, LLMError
from .prompts import PROMPT_VERSION, build_messages
from .scoring import ScoreResult, ScoringError, Verdict, score
from .transcript import Dialog


class Evidence(BaseModel):
    from_id: str
    to_id: str


class Sample(BaseModel):
    answer: str
    value: float | None
    evidence: list[Evidence]
    explanation: str
    reasoning: str = ""


class CriterionEvaluation(BaseModel):
    criterion_id: str
    status: str                 # "ok" | "needs_review"
    answer: str | None
    value: float | None
    evidence: list[Evidence]
    explanation: str
    confidence: float           # share of valid samples agreeing with the final verdict
    samples: list[Sample]
    errors: list[str] = []


class DialogEvaluation(BaseModel):
    conversation_id: str
    channel: str
    checklist_id: str
    checklist_version: int
    prompt_version: str
    models: list[str]
    criteria: list[CriterionEvaluation]
    score: ScoreResult | None
    score_error: str | None = None
    calls: list[dict]
    total_tokens: int
    cost_usd: float
    elapsed_s: float


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(text)
        if not m:
            raise ValueError("ответ не содержит JSON-объекта")
        return json.loads(m.group(0))


def _norm_id(x) -> str:
    s = str(x).strip().lower().strip("[]")
    return s if s.startswith("m") else f"m{s}"


def validate_sample(raw: dict, c: Criterion, d: Dialog) -> tuple[Sample | None, list[str]]:
    """Return a Sample or a list of errors (in Russian, fed back to the model)."""
    errs: list[str] = []
    answer = str(raw.get("answer", "")).strip().lower()
    value = raw.get("value")
    allowed = {"yes", "no"} if c.type != CriterionType.SCALE else {"score"}
    if c.na_allowed:
        allowed.add("na")
    if c.type == CriterionType.SCALE and answer in {"yes", "no"}:
        errs.append('для шкального критерия answer должен быть "score"' + (' или "na"' if c.na_allowed else ""))
    elif answer not in allowed:
        errs.append(f'answer="{answer}" недопустим; допустимо: {", ".join(sorted(allowed))}')
    if answer == "score":
        steps = [s.value for s in c.scale or []]
        try:
            value = float(value)
        except (TypeError, ValueError):
            errs.append(f"value должен быть числом из ступеней шкалы: {steps}")
        else:
            if value not in steps:
                errs.append(f"value={value:g} не является ступенью шкалы; допустимо: {steps}")
    else:
        value = None
    evidence: list[Evidence] = []
    for e in raw.get("evidence") or []:
        if isinstance(e, dict):
            a, b = e.get("from") or e.get("id"), e.get("to") or e.get("from") or e.get("id")
        else:
            a = b = e
        if a is None:
            continue
        a, b = _norm_id(a), _norm_id(b)
        bad = [x for x in (a, b) if not d.has_message(x)]
        if bad:
            errs.append(f"в evidence указаны несуществующие реплики: {', '.join(bad)} (есть m1–m{len(d.messages)})")
            continue
        if int(a[1:]) > int(b[1:]):
            a, b = b, a
        evidence.append(Evidence(from_id=a, to_id=b))
    if not evidence and answer in allowed:
        errs.append("evidence пустой: укажи хотя бы одну реплику-основание")
    explanation = str(raw.get("explanation") or "").strip()
    if not explanation:
        errs.append("explanation пустой")
    if errs:
        return None, errs
    return Sample(answer=answer, value=value, evidence=evidence, explanation=explanation,
                  reasoning=str(raw.get("reasoning") or "")[:4000]), []


def _key(s: Sample) -> tuple:
    return (s.answer, s.value)


def vote(samples: list[Sample], c: Criterion) -> tuple[Sample, float]:
    """Majority for categorical answers; median (snapped to a step) for scale values."""
    cats = Counter(s.answer for s in samples)
    top = max(cats.values())
    tied = [a for a, n in cats.items() if n == top]
    # tie-break: the answer of the earliest sample among the tied ones
    category = next(s.answer for s in samples if s.answer in tied)
    if category == "score":
        values = sorted(s.value for s in samples if s.answer == "score")
        med = statistics.median(values)
        steps = sorted(st.value for st in c.scale or [])
        value = min(steps, key=lambda st: (abs(st - med), st))
        final_key = ("score", value)
    else:
        final_key = (category, None)
    rep = next((s for s in samples if _key(s) == final_key), None)
    if rep is None:  # median step that no sample chose exactly: take the closest sample's evidence
        rep = min((s for s in samples if s.answer == "score"), key=lambda s: abs(s.value - final_key[1]))
        rep = rep.model_copy(update={"value": final_key[1]})
    agree = sum(1 for s in samples if _key(s) == final_key) / len(samples)
    return rep, round(agree, 2)


class Evaluator:
    def __init__(self, client: LLMClient, samples: int = 3, temperature: float = 0.5, repairs: int = 2,
                 extra_samples: int = 2):
        self.client = client
        self.samples = max(1, samples)
        self.extra_samples = max(0, extra_samples)  # added only when the first samples disagree
        self.temperature = temperature if self.samples > 1 else 0.0
        self.repairs = repairs
        self.calls: list[LLMCall] = []

    async def _one_sample(self, messages: list[dict], c: Criterion, d: Dialog) -> tuple[Sample | None, list[str]]:
        msgs = list(messages)
        errors: list[str] = []
        for _ in range(self.repairs + 1):
            try:
                resp = await self.client.chat(msgs, temperature=self.temperature)
            except LLMError as e:
                errors.append(str(e))
                return None, errors
            self.calls.extend(resp.calls)
            try:
                raw = parse_json(resp.content)
            except (ValueError, json.JSONDecodeError) as e:
                errs = [f"невалидный JSON: {e}"]
            else:
                sample, errs = validate_sample(raw, c, d)
                if sample:
                    return sample, errors
            errors.extend(errs)
            msgs = msgs + [
                {"role": "assistant", "content": resp.content[:6000]},
                {"role": "user", "content": "Ответ не прошёл проверку: " + "; ".join(errs)
                 + ". Исправь и верни только корректный JSON."},
            ]
        return None, errors

    async def evaluate_criterion(self, checklist: Checklist, c: Criterion, d: Dialog,
                                 f: Facts) -> CriterionEvaluation:
        messages = build_messages(checklist, c, d, f)
        results = await asyncio.gather(*(self._one_sample(messages, c, d) for _ in range(self.samples)))
        samples = [s for s, _ in results if s]
        errors = [e for _, errs in results for e in errs]
        if not samples:
            return CriterionEvaluation(criterion_id=c.id, status="needs_review", answer=None, value=None,
                                       evidence=[], explanation="Модель не вернула корректный ответ.",
                                       confidence=0.0, samples=[], errors=errors[-5:])
        rep, conf = vote(samples, c)
        if conf < 1 and self.extra_samples and self.samples > 1:
            # adaptive: the samples disagree -> ask for more votes before deciding
            more = await asyncio.gather(*(self._one_sample(messages, c, d) for _ in range(self.extra_samples)))
            samples += [s for s, _ in more if s]
            errors += [e for _, errs in more for e in errs]
            rep, conf = vote(samples, c)
        return CriterionEvaluation(
            criterion_id=c.id, status="ok", answer=rep.answer, value=rep.value, evidence=rep.evidence,
            explanation=rep.explanation, confidence=conf, samples=samples, errors=errors[-5:],
        )

    async def evaluate(self, checklist: Checklist, d: Dialog,
                       only: list[str] | None = None) -> DialogEvaluation:
        t0 = time.monotonic()
        self.calls = []
        f = extract_facts(d)
        criteria = [c for c in checklist.active_criteria if only is None or c.id in only]
        evals = await asyncio.gather(*(self.evaluate_criterion(checklist, c, d, f) for c in criteria))
        sc, err = None, None
        if only is None:
            sc, err = score_evaluations(checklist, evals)
        calls = [c.__dict__ for c in self.calls]
        return DialogEvaluation(
            conversation_id=d.conversation_id, channel=d.channel,
            checklist_id=checklist.id, checklist_version=checklist.version, prompt_version=PROMPT_VERSION,
            models=sorted({f"{c.provider}:{c.model}" for c in self.calls if c.ok}),
            criteria=list(evals), score=sc, score_error=err, calls=calls,
            total_tokens=sum(c.prompt_tokens + c.completion_tokens for c in self.calls),
            cost_usd=round(sum(c.cost_usd for c in self.calls), 6),
            elapsed_s=round(time.monotonic() - t0, 1),
        )


def score_evaluations(checklist: Checklist, evals: list[CriterionEvaluation]) -> tuple[ScoreResult | None, str | None]:
    pending = [e.criterion_id for e in evals if e.status != "ok"]
    if pending:
        return None, f"требуют ручной проверки: {', '.join(pending)}"
    try:
        return score(checklist, [Verdict(criterion_id=e.criterion_id, answer=e.answer, value=e.value)
                                 for e in evals]), None
    except ScoringError as e:
        return None, str(e)
