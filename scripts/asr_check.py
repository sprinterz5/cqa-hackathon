"""Check the recording pipeline on DEV calls: our STT + roles vs. the organizers' transcripts.

For every call: download the mp3 → Alem STT → roles by the LLM → dialog "<id>-asr" (group ASRCHECK) →
evaluation by the checklist. Compared with the original dialog:
  - roles: each ASR phrase is matched to the most similar reference line; share of words with the same role
  - text: word error rate of our text against the reference text (the reference is ASR too: agreement, not truth)
  - verdicts: per-criterion agreement with the latest evaluation of the original transcript, and the total

Usage:  python scripts/asr_check.py p4-ru-001 p4-ru-002 ...   (default: all voice calls of DEV)
Report: data/ASRCHECK/report.json
"""
import argparse
import asyncio
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cqa.llm import LLMClient  # noqa: E402
from cqa.service import Service  # noqa: E402
from cqa.store import Store  # noqa: E402
from cqa.transcript import normalize  # noqa: E402


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold().replace("ё", "е"))


def wer(ref: list[str], hyp: list[str]) -> float:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, w in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != w))
        prev = cur
    return prev[-1] / max(1, len(ref))


def role_agreement(ref_lines: list[tuple[str, str]], asr_segments: list[tuple[str, str]]) -> dict:
    """Word-weighted share of ASR phrases whose role equals the role of the best-matching reference line."""
    ok = total = unmatched = 0
    confusions: dict[str, int] = {}
    for role, text in asr_segments:
        w = words(text)
        if not w:
            continue
        joined = " ".join(w)
        best, best_role = 0.0, None
        for ref_role, ref_text in ref_lines:
            ratio = SequenceMatcher(None, joined, ref_text).ratio()
            if ratio > best:
                best, best_role = ratio, ref_role
        if best < 0.5:
            unmatched += len(w)
            continue
        total += len(w)
        if best_role == role:
            ok += len(w)
        else:
            key = f"{best_role}→{role}"
            confusions[key] = confusions.get(key, 0) + len(w)
    return {"accuracy": round(ok / total, 3) if total else None, "words": total,
            "unmatched_words": unmatched, "confusions": confusions}


def reference_line_roles(gold, predicted) -> dict:
    """The metric of scripts/eval_asr_roles.py (branch islam), so both pipelines are compared the same way:
    words of both transcripts are aligned by exact token sequence; a reference voice line is correct when the
    ASR line holding most of its matched words has the same role."""
    from collections import Counter, defaultdict
    gw, go, aw, ao = [], [], [], []
    for i, m in enumerate(gold.messages):
        t = re.findall(r"\w+", m.text.casefold())
        gw += t
        go += [i] * len(t)
    for i, m in enumerate(predicted.messages):
        t = re.findall(r"\w+", m.text.casefold())
        aw += t
        ao += [i] * len(t)
    matches = defaultdict(Counter)
    for block in SequenceMatcher(None, gw, aw, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            matches[go[block.a + k]][ao[block.b + k]] += 1
    voice = [i for i, m in enumerate(gold.messages) if m.speaker != "system"]
    aligned = [(i, matches[i].most_common(1)[0][0]) for i in voice if matches[i]]
    correct = sum(gold.messages[i].speaker == predicted.messages[j].speaker for i, j in aligned)
    return {"gold_voice_replies": len(voice), "aligned": len(aligned), "correct": correct}


def verdict(c: dict) -> str:
    return f"{c['value']:g}" if c["answer"] == "score" else str(c["answer"])


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--checklist", default="freedom_default")
    ap.add_argument("--metrics-only", action="store_true", help="only recompute role metrics of existing <id>-asr dialogs")
    args = ap.parse_args()

    svc = Service(Store(ROOT / "data" / "cqa.db"))
    if args.metrics_only:
        ids = args.ids or [d["conversation_id"] for d in svc.store.list_dialogs("DEV") if d["channel"] == "voice call"]
        tot = {"gold_voice_replies": 0, "aligned": 0, "correct": 0}
        for cid in ids:
            asr = svc.store.get_dialog(f"{cid}-asr")
            if not asr:
                continue
            m = reference_line_roles(normalize(svc.store.get_dialog(cid)["raw"]), normalize(asr["raw"]))
            for k in tot:
                tot[k] += m[k]
            print(cid, m, flush=True)
        print("TOTAL", tot, f"accuracy {tot['correct'] / max(1, tot['aligned']):.1%}", flush=True)
        return
    checklist = svc.checklist(args.checklist)
    ids = args.ids or [d["conversation_id"] for d in svc.store.list_dialogs("DEV") if d["channel"] == "voice call"]
    chain = svc._chain("primary")
    report = {"checklist": f"{checklist.id} v{checklist.version}", "samples": args.samples, "calls": []}

    for cid in ids:
        orig = svc.store.get_dialog(cid)["raw"]
        src = await svc.audio_source(cid)
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as http:
            mp3 = (await http.get(src["src"])).content
        job_id = svc.start_audio_job(mp3, f"{cid}.mp3", language=None, conversation_id=f"{cid}-asr",
                                     group_id="ASRCHECK")
        job = svc.jobs[job_id]
        await job["task"]
        if job["status"] != "done":
            print(f"{cid}: ошибка {job.get('error')}", flush=True)
            report["calls"].append({"conversation_id": cid, "error": job.get("error")})
            continue
        asr_raw = svc.store.get_dialog(f"{cid}-asr")["raw"]

        ref = normalize(orig)
        ref_lines = [(m.speaker, " ".join(words(m.text))) for m in ref.messages if m.speaker in ("client", "operator", "bot")]
        asr_lines = [(m["speaker"], m["text"]) for m in asr_raw["transcript"]]
        roles = role_agreement(ref_lines, asr_lines)
        roles["by_reference_line"] = reference_line_roles(ref, normalize(asr_raw))
        ref_words = [w for _, t in ref_lines for w in t.split()]
        asr_words = [w for _, t in asr_lines for w in words(t)]

        async with LLMClient(chain) as client:
            ev = await svc.evaluate_dialog(client, chain, f"{cid}-asr", checklist, args.samples)
        base = svc.store.latest_evaluation(cid, checklist.id, checklist.version)
        base_v = {c["criterion_id"]: verdict(c) for c in (base or {}).get("criteria", [])}
        asr_v = {c["criterion_id"]: verdict(c) for c in ev["criteria"]}
        same = [k for k in asr_v if base_v.get(k) == asr_v[k]]
        row = {
            "conversation_id": cid, "duration_s": asr_raw["asr"]["duration_s"],
            "language": asr_raw["asr"]["language"], "role_source": asr_raw["asr"]["role_source"],
            "stt_s": job["stt_s"], "roles_s": job["roles_s"], "eval_s": ev["elapsed_s"],
            "ref_lines": len(ref_lines), "asr_lines": len(asr_lines),
            "wer_vs_reference": round(wer(ref_words, asr_words), 3), "roles": roles,
            "total_reference": (base or {}).get("score", {}).get("total") if base else None,
            "total_asr": (ev["score"] or {}).get("total"),
            "verdicts_same": f"{len(same)}/{len(asr_v)}",
            "verdicts_diff": {k: f"{base_v.get(k)}→{asr_v[k]}" for k in asr_v if k not in same},
        }
        report["calls"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    out = ROOT / "data" / "ASRCHECK"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
