"""Download a dataset group from the organizers' API.

Usage: python scripts/fetch_dataset.py DEV
Reads DATA_API_URL from .env (or the environment) and saves data/<group>/<group>_raw.json.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    group = sys.argv[1] if len(sys.argv) > 1 else "DEV"
    load_env()
    base = os.environ.get("DATA_API_URL")
    if not base:
        sys.exit("DATA_API_URL is not set (see .env.example)")
    url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"group_id": group})
    with urllib.request.urlopen(url, timeout=120) as resp:
        payload = json.load(resp)
    if not payload.get("isSuccess"):
        sys.exit(f"API error: {payload.get('error')}")
    out = ROOT / "data" / group / f"{group}_raw.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{payload['data']['total_records']} records -> {out}")


if __name__ == "__main__":
    main()
