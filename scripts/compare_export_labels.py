"""Audit exported DEV verdicts against provisional labels without a database."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL_VALUE = {"yes": True, "no": False, "na": None}


def compare_export(results: dict, labels: dict, records: list[dict], codes: dict) -> dict:
    source = {row["conversation_id"]: row for row in records}
    exported = {row["conversation_id"]: row for row in results["results"] if row["status"] == "ok"}
    totals = Counter()
    by_criterion = defaultdict(Counter)
    by_group = defaultdict(Counter)
    disagreements = []
    missing = []
    for cid, expected in labels["dialogs"].items():
        if cid not in exported or cid not in source:
            missing.append(cid)
            continue
        actual = {row["criterion_id"]: row["value"] for row in exported[cid]["criteria"]}
        group = f"{source[cid]['language']}/{source[cid]['channel']}"
        for criterion, label in expected.items():
            value = LABEL_VALUE.get(label["a"], label["a"])
            matched = actual.get(codes[criterion], object()) == value
            sure = not label.get("unsure", False)
            for counter in (totals, by_criterion[criterion], by_group[group]):
                counter["total"] += 1
                counter["correct"] += matched
                if sure:
                    counter["sure_total"] += 1
                    counter["sure_correct"] += matched
            if not matched:
                disagreements.append({"conversation_id": cid, "criterion": criterion,
                                      "expected": value, "actual": actual.get(codes[criterion]),
                                      "unsure": not sure})
    return {"totals": dict(totals),
            "by_criterion": {key: dict(value) for key, value in sorted(by_criterion.items())},
            "by_group": {key: dict(value) for key, value in sorted(by_group.items())},
            "disagreements": disagreements, "missing": missing}


def main() -> None:
    results = json.loads((ROOT / "results/dev/results.json").read_text(encoding="utf-8"))
    labels = json.loads((ROOT / "labels/dev_silver.json").read_text(encoding="utf-8"))
    records = json.loads((ROOT / "data/dev_source.json").read_text(encoding="utf-8"))["data"]["records"]
    codes = json.loads((ROOT / "checklists/freedom_default.json").read_text(encoding="utf-8"))["export_codes"]
    report = compare_export(results, labels, records, codes)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
