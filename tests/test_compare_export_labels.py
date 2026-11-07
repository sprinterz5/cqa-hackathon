import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from compare_export_labels import compare_export


def test_export_audit_separates_sure_and_disputed_labels():
    results = {"results": [{"conversation_id": "a", "status": "ok", "criteria": [
        {"criterion_id": "contact", "value": True},
        {"criterion_id": "closure", "value": False}]}]}
    labels = {"dialogs": {"a": {"c01": {"a": "yes"},
                                "c07": {"a": "yes", "unsure": True}}}}
    records = [{"conversation_id": "a", "language": "RU", "channel": "agent chat"}]
    report = compare_export(results, labels, records, {"c01": "contact", "c07": "closure"})
    assert report["totals"] == {"total": 2, "correct": 1,
                                "sure_total": 1, "sure_correct": 1}
    assert report["by_group"]["RU/agent chat"]["correct"] == 1
    assert report["disagreements"] == [{"conversation_id": "a", "criterion": "c07",
                                        "expected": True, "actual": False, "unsure": True}]
