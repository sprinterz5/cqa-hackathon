"""D25: current local audio pipeline + one LLM role pass with voice hints.

Predictions are saved before opening DEV references. All artifacts stay in data/.
Use --prepare-only to cache local ASR without calling Alem; --score-only never calls Alem.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from cqa.audio_pipeline import process_audio
from cqa.llm import LLMClient, providers_from_env
from cqa.stt import assign_roles, merge_segments
from cqa.transcript import normalize

AUDIO = ROOT / "data" / "DEV" / "audio"
IDS = tuple(f"p4-ru-{n:03}" for n in range(1, 6))


def local_segments(cid: str, recompute: bool = False, roles_only: bool = False) -> list[dict]:
    cache = AUDIO / f"{cid}.current_local.json"
    if cache.exists() and not recompute:
        return json.loads(cache.read_text(encoding="utf-8"))["segments"]
    if roles_only:
        # Translation only adds translation_ru; assign_roles reads text, time and voice.
        from cqa.audio_pipeline import LocalWhisperSTT, _speaker_ids

        stt_cache = AUDIO / f"{cid}.current_stt.json"
        if stt_cache.exists() and not recompute:
            recognized = json.loads(stt_cache.read_text(encoding="utf-8"))
        else:
            print("STAGE", cid, "recognizing", flush=True)
            recognized = LocalWhisperSTT().transcribe(AUDIO / f"{cid}.mp3", None)
            stt_cache.write_text(json.dumps(recognized, ensure_ascii=False, indent=2), encoding="utf-8")
        print("STAGE", cid, "separating", flush=True)
        voices = _speaker_ids(AUDIO / f"{cid}.mp3", recognized)
        record = {"transcript": [{**row, "speaker": voice}
                                 for row, voice in zip(recognized, voices)]}
    else:
        record = process_audio(AUDIO / f"{cid}.mp3", "local",
                               progress=lambda stage: print("STAGE", cid, stage, flush=True))
    segments = [{"start": row["start"], "end": row["end"], "text": row["text"],
                 "source_language": row.get("source_language"),
                 "translation_ru": row.get("translation_ru"), "voice": row["speaker"]}
                for row in record["transcript"]]
    cache.write_text(json.dumps({"source": "local STT + ECAPA on hybrid-audio",
                                 "segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8")
    return segments


def old_cached_segments(cid: str) -> list[dict]:
    """Fast diagnostic only: E17 ASR predates the current multilingual pipeline."""
    data = json.loads((AUDIO / f"{cid}.roles.json").read_text(encoding="utf-8"))
    return [{"start": row["t"], "end": row["end_t"], "text": row["text"],
             "voice": row["speaker_id"]} for row in data["messages"]]


async def predict(ids: list[str], cached_e17: bool, prepare_only: bool,
                  recompute_local: bool, roles_only: bool, rerun_roles: bool = False) -> None:
    segments_by_id = {}
    for cid in ids:
        segments_by_id[cid] = (old_cached_segments(cid) if cached_e17
                               else local_segments(cid, recompute_local, roles_only))
        print("LOCAL", cid, len(segments_by_id[cid]),
              len({row["voice"] for row in segments_by_id[cid]}), flush=True)
    if prepare_only:
        return
    providers = providers_from_env()
    chain = [providers[name] for name in ("primary", "fallback") if name in providers]
    if not chain:
        raise RuntimeError("LLM_* is not configured")
    async with LLMClient(chain, concurrency=1, timeout=180, retries=0) as client:
        for cid in ids:
            target = AUDIO / f"{cid}.voice_llm.json"
            source_name = "cached E17" if cached_e17 else "current local"
            if target.exists() and not rerun_roles:
                old = json.loads(target.read_text(encoding="utf-8"))
                if old.get("source") == source_name:
                    print("ROLES CACHED", cid, flush=True)
                    continue
            segs, roles, source, error = await assign_roles(client, segments_by_id[cid])
            if source != "ai":
                raise RuntimeError(f"{cid}: role LLM failed: {error}")
            output = {"conversation_id": cid, "source": source_name,
                      "role_source": source, "segments": segs, "roles": roles,
                      "split_parts": sum(bool(row.get("split")) for row in segs)}
            target.write_text(
                json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            print("ROLES", cid, len(roles), output["split_parts"], flush=True)


def score(ids: list[str]) -> dict:
    from eval_asr_roles import compare

    # Gold is opened only after the saved predictions exist for every requested call.
    predictions = [json.loads((AUDIO / f"{cid}.voice_llm.json").read_text(encoding="utf-8"))
                   for cid in ids]
    dataset = next(path for path in (ROOT / "data/dev_source.json", ROOT / "data/DEV/DEV_raw.json")
                   if path.exists())
    gold = {row["conversation_id"]: row for row in
            json.loads(dataset.read_text(encoding="utf-8"))["data"]["records"]}
    rows = []
    for item in predictions:
        cid = item["conversation_id"]
        transcript = merge_segments(item["segments"], item["roles"], item["role_source"])
        predicted = normalize({"conversation_id": cid, "channel": "voice call",
                               "language": "RU", "transcript": transcript})
        result = compare(normalize(gold[cid]), predicted)
        rows.append({"conversation_id": cid, "source": item["source"],
                     "correct": result["correct_gold_replies"],
                     "aligned": result["aligned_gold_replies"],
                     "gold": result["gold_voice_replies"],
                     "split_parts": item["split_parts"]})
        print("SCORE", cid, rows[-1]["correct"], "/", rows[-1]["aligned"], flush=True)
    total = {key: sum(row[key] for row in rows) for key in ("correct", "aligned", "gold")}
    report = {"method": "hybrid-audio process_audio(local) + cqa.stt.assign_roles with voice hints; exact eval_asr_roles.compare",
              "calls": rows, "totals": total}
    (AUDIO.parent / "local_voice_llm_metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TOTAL", total, f"{total['correct'] / max(1, total['aligned']):.1%}", flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ids", nargs="*", default=list(IDS))
    parser.add_argument("--cached-e17", action="store_true", help="diagnostic with old ASR, not D25")
    parser.add_argument("--prepare-only", action="store_true", help="local ASR only; no LLM call")
    parser.add_argument("--score-only", action="store_true", help="score saved predictions; no LLM call")
    parser.add_argument("--recompute-local", action="store_true")
    parser.add_argument("--roles-only-local", action="store_true",
                        help="same local STT and ECAPA; skip translation unused by role prompt")
    parser.add_argument("--rerun-roles", action="store_true", help="repeat LLM even when predictions are cached")
    args = parser.parse_args()
    if any(cid not in IDS for cid in args.ids):
        parser.error("expected DEV call IDs p4-ru-001..005")
    if not args.score_only:
        asyncio.run(predict(args.ids, args.cached_e17, args.prepare_only,
                            args.recompute_local, args.roles_only_local, args.rerun_roles))
    if not args.prepare_only:
        score(args.ids)


if __name__ == "__main__":
    main()
