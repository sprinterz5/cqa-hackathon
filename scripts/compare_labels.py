"""Compare AI evaluations in the app database with a labels file.

Usage:
  python scripts/compare_labels.py                       # labels/dev_silver.json vs latest evaluations
  python scripts/compare_labels.py --version 2           # only evaluations made with checklist v2
  python scripts/compare_labels.py --sure-only           # skip labels marked unsure
  python scripts/compare_labels.py --json out.json       # also write the report as JSON
Metrics: per-criterion agreement (exact answer; scale must match the step), total-score MAE
(the label total is computed with the same scorer), breakdown by language/channel.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cqa.checklist import Checklist  # noqa: E402
from cqa.scoring import Verdict, score  # noqa: E402
from cqa.store import Store  # noqa: E402

LABEL = {"yes": "Да", "no": "Нет", "na": "N/A"}


def norm(a):
    """Label value -> (answer, value)."""
    if isinstance(a, (int, float)):
        return "score", float(a)
    return a, None


def show(ans, val):
    return f"{val:g}" if ans == "score" else LABEL.get(ans, str(ans))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=str(ROOT / "labels" / "dev_silver.json"))
    ap.add_argument("--db", default=str(ROOT / "data" / "cqa.db"))
    ap.add_argument("--checklist", default="freedom_default")
    ap.add_argument("--version", type=int)
    ap.add_argument("--sure-only", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args()

    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))["dialogs"]
    store = Store(args.db)

    per_crit = defaultdict(lambda: [0, 0])
    per_group = defaultdict(lambda: [0, 0])
    totals = []
    diffs = []
    missing = []
    for cid, lab in labels.items():
        rows = store._q("SELECT * FROM evaluations WHERE conversation_id = ? AND checklist_id = ? "
                        + ("AND checklist_version = ? " if args.version else "") + "ORDER BY id DESC LIMIT 1",
                        (cid, args.checklist, args.version) if args.version else (cid, args.checklist))
        if not rows:
            missing.append(cid)
            continue
        ev = store._eval_row(rows[0])
        dialog = store.get_dialog(cid)
        group = f"{dialog['language']}/{'звонок' if dialog['channel'] == 'voice call' else 'чат'}"
        cl = Checklist.model_validate(store.get_checklist(args.checklist, ev["checklist_version"]))
        ai = {c["criterion_id"]: c for c in ev["criteria"]}
        for crit, l in lab.items():
            if args.sure_only and l.get("unsure"):
                continue
            la, lv = norm(l["a"])
            c = ai.get(crit)
            if not c:
                continue
            ok = (c["answer"], c["value"]) == (la, lv)
            per_crit[crit][0] += ok
            per_crit[crit][1] += 1
            per_group[group][0] += ok
            per_group[group][1] += 1
            if not ok:
                diffs.append({"dialog": cid, "criterion": crit, "label": show(la, lv), "ai": show(c["answer"], c["value"]),
                              "unsure": bool(l.get("unsure")), "confidence": c.get("confidence"),
                              "label_note": l.get("note", ""), "ai_explanation": c.get("explanation", "")})
        try:
            lab_score = score(cl, [Verdict(criterion_id=k, answer=norm(v["a"])[0], value=norm(v["a"])[1])
                                   for k, v in lab.items()]).total
        except Exception as e:  # noqa: BLE001
            lab_score = None
            print(f"! {cid}: label total not computed: {e}")
        ai_total = (ev.get("score") or {}).get("total")
        if lab_score is not None and ai_total is not None:
            totals.append((cid, lab_score, ai_total, abs(lab_score - ai_total), ev["checklist_version"]))

    n_ok = sum(v[0] for v in per_crit.values())
    n = sum(v[1] for v in per_crit.values())
    print(f"Диалогов: {len(labels) - len(missing)} (нет оценки: {', '.join(missing) or '—'})")
    print(f"Совпадение по критериям: {n_ok}/{n} = {n_ok / n:.1%}" if n else "Нет данных")
    if totals:
        mae = sum(t[3] for t in totals) / len(totals)
        print(f"Итоговый балл: MAE = {mae:.2f}, точное совпадение итога: {sum(t[3] < 0.005 for t in totals)}/{len(totals)}")
    print("\nПо критериям:")
    for k in sorted(per_crit):
        ok, tot = per_crit[k]
        print(f"  {k}: {ok}/{tot} = {ok / tot:.0%}")
    print("\nПо языку / каналу:")
    for k in sorted(per_group):
        ok, tot = per_group[k]
        print(f"  {k}: {ok}/{tot} = {ok / tot:.0%}")
    print("\nИтоги (разметка → AI):")
    for cid, ls, ai_t, d, v in sorted(totals, key=lambda t: -t[3]):
        print(f"  {cid:10} {ls:7.2f} → {ai_t:7.2f}  Δ {d:6.2f}  (v{v})")
    print(f"\nРасхождения ({len(diffs)}):")
    for d in diffs:
        flag = " [спорная разметка]" if d["unsure"] else ""
        print(f"  {d['dialog']:10} {d['criterion']}: разметка {d['label']:>4} | AI {d['ai']:>4} "
              f"(увер. {d['confidence']}){flag}\n      AI: {d['ai_explanation'][:160]}")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "agreement": n_ok / n if n else None, "per_criterion": per_crit, "per_group": per_group,
            "totals": totals, "diffs": diffs}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
