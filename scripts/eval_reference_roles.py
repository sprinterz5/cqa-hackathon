"""Check role preservation when an official labeled transcript is supplied.

This is source fidelity, not an audio-only inference benchmark.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from cqa.audio_pipeline import process_audio
from cqa.transcript import normalize
from eval_asr_roles import compare


def main() -> None:
    records = json.loads((ROOT / "data" / "dev_source.json").read_text(encoding="utf-8"))["data"]["records"]
    totals = {"gold_voice_replies": 0, "aligned_gold_replies": 0, "correct_gold_replies": 0}
    for source in sorted(records, key=lambda record: record["conversation_id"]):
        if source.get("channel") != "voice call":
            continue
        cid = source["conversation_id"]
        record = process_audio(ROOT / "data" / "DEV" / "audio" / f"{cid}.mp3",
                               language=source.get("language"),
                               reference_transcript=source["transcript"])
        metric = compare(normalize(source), normalize(record))
        for key in totals:
            totals[key] += metric[key]
        print(cid, metric["correct_gold_replies"], "/", metric["gold_voice_replies"])
    print("TOTAL", totals)


if __name__ == "__main__":
    main()
