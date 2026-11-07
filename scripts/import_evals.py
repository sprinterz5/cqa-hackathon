"""Import results of scripts/evaluate.py (data/<group>/evals/*.json) into the app database.

Usage: python scripts/import_evals.py DEV
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import bootstrap  # noqa: E402

if __name__ == "__main__":
    group = sys.argv[1] if len(sys.argv) > 1 else "DEV"
    svc = bootstrap()
    print(f"imported {svc.import_cli_evals(group)} evaluations from data/{group}/evals")
