"""Write results/<dev|prod>/results.json in the organizers' format (schema 1.0) and validate it.

Usage: python scripts/export_results.py DEV [--folder dev] [--version N] [--team "Team name"]
Uses the latest evaluation of each dialog in the group by the given checklist version (default: latest version).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import bootstrap  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("group")
    ap.add_argument("--folder", help="dev or prod (default: dev for DEV, otherwise prod)")
    ap.add_argument("--checklist", default="freedom_default")
    ap.add_argument("--version", type=int)
    ap.add_argument("--team")
    a = ap.parse_args()
    svc = bootstrap()
    out = svc.export_results(a.group, a.checklist, a.version, a.team)
    folder = a.folder or ("dev" if a.group.upper() == "DEV" else "prod")
    path = ROOT / "results" / folder / "results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out["results"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok = sum(1 for r in out["results"]["results"] if r["status"] == "ok")
    print(f"{path.relative_to(ROOT)}: {len(out['results']['results'])} dialogs, ok {ok}, "
          f"checklist v{out['checklist_version']}")
    for p in out["problems"]:
        print("PROBLEM:", p)
    sys.exit(1 if out["problems"] else 0)
