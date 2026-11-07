"""Evaluate dialogs from a downloaded dataset.

Usage:
  python scripts/evaluate.py DEV p1-ru-001 p4-ru-002     # specific dialogs
  python scripts/evaluate.py DEV --limit 5 --samples 1   # first 5 dialogs, one sample per criterion
  python scripts/evaluate.py DEV --provider local        # use LOCAL_* (Ollama) instead of LLM_*
Results go to data/<group>/evals/<conversation_id>.json
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cqa.checklist import Checklist  # noqa: E402
from cqa.evaluator import Evaluator  # noqa: E402
from cqa.llm import LLMClient, providers_from_env  # noqa: E402
from cqa.transcript import normalize  # noqa: E402

LABEL = {"yes": "Да", "no": "Нет", "na": "N/A"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("group")
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--provider", choices=["primary", "local"], default="primary")
    ap.add_argument("--checklist", default=str(ROOT / "checklists" / "freedom_default.json"))
    args = ap.parse_args()

    records = json.loads((ROOT / "data" / args.group / f"{args.group}_raw.json").read_text("utf-8"))
    records = records["data"]["records"]
    if args.ids:
        records = [r for r in records if r["conversation_id"] in args.ids]
    if args.limit:
        records = records[: args.limit]
    checklist = Checklist.load(args.checklist)

    provs = providers_from_env()
    chain = [provs[args.provider]] if args.provider in provs else []
    if args.provider == "primary" and "fallback" in provs:
        chain.append(provs["fallback"])
    out_dir = ROOT / "data" / args.group / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)

    async with LLMClient(chain, concurrency=args.concurrency) as client:
        ev = Evaluator(client, samples=args.samples)
        for r in records:
            d = normalize(r)
            res = await ev.evaluate(checklist, d)
            (out_dir / f"{d.conversation_id}.json").write_text(
                res.model_dump_json(indent=1), encoding="utf-8")
            parts = []
            for c in res.criteria:
                a = f"{c.value:g}" if c.answer == "score" else LABEL.get(c.answer or "", "??")
                parts.append(f"{c.criterion_id}={a}" + ("" if c.confidence == 1 else f"({c.confidence:.0%})"))
            total = res.score.total if res.score else res.score_error
            gross = " ГРУБАЯ" if res.score and res.score.gross_violation else ""
            print(f"{d.conversation_id:10} {total!s:>7}{gross}  {' '.join(parts)}  "
                  f"[{res.elapsed_s}s, {res.total_tokens} tok, ${res.cost_usd}]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
