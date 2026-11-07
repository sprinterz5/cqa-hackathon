"""Export evaluations to the organizers' results.json format (schema 1.0)."""
from __future__ import annotations

from .checklist import Checklist, CriterionType

SCHEMA_VERSION = "1.0"
# value types required by the organizers for each code
ALLOWED = {
    "contact": {"bool"},
    "discovery": {"bool", "null"},
    "identification": {"bool", "null"},
    "consultation": {0, 5, 10, 15, 20, "null"},
    "empathy": {"bool"},
    "dialogue": {0, 4, 7, 10},
    "closure": {"bool", "null"},
    "critical": {"bool"},
}


# fallback for checklist versions saved before export_codes existed
DEFAULT_CODES = {"c01": "contact", "c02": "discovery", "c03": "identification", "c04": "consultation",
                 "c05": "empathy", "c06": "dialogue", "c07": "closure", "c08": "critical"}


def _value(c, ev: dict):
    answer = ev.get("answer")
    if answer == "na":
        return None
    if c.type == CriterionType.SCALE:
        v = ev.get("value")
        return int(v) if v is not None and float(v).is_integer() else v
    return answer == "yes"


def _turns(ev: dict) -> list[int]:
    out: set[int] = set()
    for e in ev.get("evidence") or []:
        a, b = int(e["from_id"][1:]), int(e["to_id"][1:])
        out.update(range(a, b + 1))
    return sorted(out)


def _model(evaluations: list[dict], providers: dict[str, str] | None = None) -> dict:
    """providers: provider name -> 'local' | 'external_api'."""
    providers = providers or {}
    used = sorted({m for e in evaluations for m in e.get("models") or []})
    kinds = {providers.get(m.split(":", 1)[0], "local" if m.startswith("local:") else "external_api") for m in used}
    mode = kinds.pop() if len(kinds) == 1 else ("hybrid" if kinds else "external_api")
    names = sorted({m.split(":", 1)[1] if ":" in m else m for m in used})
    return {"mode": mode, "name": ", ".join(names) or "unknown"}


def dialog_entry(checklist: Checklist, conversation_id: str, evaluation: dict | None) -> dict:
    if not evaluation:
        return {"conversation_id": conversation_id, "status": "error", "criteria": [], "score": None,
                "error": "Диалог не оценён."}
    by_id = {c["criterion_id"]: c for c in evaluation.get("criteria") or []}
    criteria, problems = [], []
    codes = checklist.export_codes or DEFAULT_CODES
    for c in checklist.active_criteria:
        code = codes.get(c.id)
        if not code:
            continue
        ev = by_id.get(c.id)
        if not ev or ev.get("status") != "ok":
            problems.append(f"{code}: нет корректного ответа модели")
            continue
        criteria.append({"criterion_id": code, "value": _value(c, ev),
                         "rationale": ev.get("explanation") or "", "evidence_turns": _turns(ev)})
    score = evaluation.get("score")
    if problems or not score:
        reason = "; ".join(problems) or evaluation.get("score_error") or "итог не рассчитан"
        return {"conversation_id": conversation_id, "status": "error", "criteria": [], "score": None,
                "error": reason}
    total = score.get("total")
    return {"conversation_id": conversation_id, "status": "ok", "criteria": criteria,
            "score": {"final_score": round(total, 2) if total is not None else None,
                      "critical_flag": bool(score.get("gross_violation"))}}


def build_results(checklist: Checklist, conversation_ids: list[str], evaluations: dict[str, dict],
                  team_name: str, providers: dict[str, str] | None = None) -> dict:
    used = [evaluations[i] for i in conversation_ids if i in evaluations]
    return {
        "schema_version": SCHEMA_VERSION,
        "team_name": team_name,
        "model": _model(used, providers),
        "results": [dialog_entry(checklist, cid, evaluations.get(cid)) for cid in conversation_ids],
    }


def validate_results(data: dict, expected_ids: list[str] | None = None) -> list[str]:
    """Return a list of problems (empty = file matches the format)."""
    errs: list[str] = []
    for k in ("schema_version", "team_name", "model", "results"):
        if k not in data:
            errs.append(f"нет поля {k}")
    if data.get("model", {}).get("mode") not in {"local", "external_api", "hybrid"}:
        errs.append("model.mode должен быть local / external_api / hybrid")
    ids = [r.get("conversation_id") for r in data.get("results", [])]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errs.append(f"повторяются ID: {', '.join(dupes)}")
    if expected_ids is not None:
        missing = sorted(set(expected_ids) - set(ids))
        extra = sorted(set(ids) - set(expected_ids))
        if missing:
            errs.append(f"нет диалогов: {', '.join(missing)}")
        if extra:
            errs.append(f"лишние диалоги: {', '.join(extra)}")
    for r in data.get("results", []):
        cid = r.get("conversation_id")
        if r.get("status") == "error":
            if r.get("criteria") != [] or r.get("score") is not None or not r.get("error"):
                errs.append(f"{cid}: для error нужны пустой criteria, score = null и причина")
            continue
        if r.get("status") != "ok":
            errs.append(f"{cid}: status должен быть ok или error")
            continue
        codes = [c.get("criterion_id") for c in r.get("criteria", [])]
        if sorted(codes) != sorted(ALLOWED):
            errs.append(f"{cid}: нужны все 8 критериев ровно по одному разу, есть {codes}")
        for c in r.get("criteria", []):
            code, v = c.get("criterion_id"), c.get("value")
            allowed = ALLOWED.get(code, set())
            kind = "null" if v is None else "bool" if isinstance(v, bool) else v
            if kind not in allowed:
                errs.append(f"{cid}/{code}: недопустимое значение {v!r}")
            turns = c.get("evidence_turns")
            if not isinstance(turns, list) or not all(isinstance(t, int) and t >= 1 for t in turns):
                errs.append(f"{cid}/{code}: evidence_turns должен быть списком номеров реплик с 1")
        s = r.get("score") or {}
        fs = s.get("final_score")
        if fs is not None and not (0 <= fs <= 100):
            errs.append(f"{cid}: final_score вне 0–100")
        crit = next((c.get("value") for c in r.get("criteria", []) if c.get("criterion_id") == "critical"), None)
        if s.get("critical_flag") != crit:
            errs.append(f"{cid}: critical_flag не совпадает с критерием critical")
    return errs
