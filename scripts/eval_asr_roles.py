"""Local ASR and speaker-role measurement on the five DEV calls.

Run from the repository root with Python 3.12 and the local dependency folders:
    python scripts/eval_asr_roles.py

Reference transcripts are loaded only after speaker clustering and role inference.
Artifacts with call text and audio stay under the git-ignored data/ directory.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (".speaker_deps", ".asr_deps"):
    local = ROOT / folder
    if local.exists():
        sys.path.insert(0, str(local))
sys.path.insert(0, str(ROOT))

import av
import numpy as np
import torch
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import cdist, pdist
from speechbrain.inference.speaker import EncoderClassifier

from cqa.facts import detect_lang
from cqa.transcript import normalize

AUDIO = ROOT / "data" / "DEV" / "audio"
ASR_MODEL = ROOT / ".asr_models" / "large-v3"
SPEAKER_MODEL = ROOT / ".diarization_models" / "speechbrain-ecapa"
THRESHOLD = 0.5


def download(record: dict) -> Path:
    AUDIO.mkdir(parents=True, exist_ok=True)
    target = AUDIO / f"{record['conversation_id']}.mp3"
    if target.exists():
        return target
    page_url = record["audio_url"]
    with urllib.request.urlopen(page_url, timeout=30) as response:
        page = response.read().decode("utf-8")
    match = re.search(r'<source[^>]+src="([^"]+)"', page) or re.search(r'<audio[^>]+src="([^"]+)"', page)
    if not match:
        raise RuntimeError(f"No mp3 source for {record['conversation_id']}")
    url = urllib.parse.urljoin(page_url, html.unescape(match.group(1)))
    with urllib.request.urlopen(url, timeout=90) as response:
        target.write_bytes(response.read())
    return target


def transcribe(cid: str, audio: Path, model: WhisperModel) -> list[dict]:
    cache = AUDIO / f"{cid}.asr.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    segments, _ = model.transcribe(
        str(audio), language="ru", beam_size=1, best_of=1,
        condition_on_previous_text=False, vad_filter=True,
    )
    out = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
    cache.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def speaker_ids(cid: str, audio: Path, segments: list[dict], model: EncoderClassifier) -> list[str]:
    cache = AUDIO / f"{cid}.embeddings.npy"
    if cache.exists():
        embeddings = np.load(cache)
    else:
        waveform = decode_audio(str(audio))
        vectors = []
        for segment in segments:
            start, end = int(segment["start"] * 16000), int(segment["end"] * 16000)
            piece = waveform[start:end]
            if len(piece) < 16000:
                piece = np.pad(piece, (0, 16000 - len(piece)))
            with torch.no_grad():
                vector = model.encode_batch(torch.from_numpy(piece).unsqueeze(0)).squeeze().numpy()
            vectors.append(vector)
        embeddings = np.asarray(vectors)
        np.save(cache, embeddings)
    if len(embeddings) <= 1:
        return ["speaker_1"] * len(embeddings)
    clusters = fcluster(linkage(pdist(embeddings, metric="cosine"), method="average"),
                        t=THRESHOLD, criterion="distance")
    counts = Counter(clusters)
    established = [cluster for cluster, count in counts.items() if count >= 2]
    if established:
        centers = np.asarray([embeddings[clusters == cluster].mean(axis=0) for cluster in established])
        for index, cluster in enumerate(clusters.copy()):
            if counts[cluster] == 1:
                nearest = int(np.argmin(cdist([embeddings[index]], centers, metric="cosine")))
                clusters[index] = established[nearest]
    return [f"speaker_{cluster}" for cluster in clusters]


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def compare(gold, predicted) -> dict:
    gold_words, gold_owner, asr_words, asr_owner = [], [], [], []
    for index, message in enumerate(gold.messages):
        tokens = words(message.text)
        gold_words.extend(tokens)
        gold_owner.extend([index] * len(tokens))
    for index, message in enumerate(predicted.messages):
        tokens = words(message.text)
        asr_words.extend(tokens)
        asr_owner.extend([index] * len(tokens))
    matches = defaultdict(Counter)
    for block in SequenceMatcher(None, gold_words, asr_words, autojunk=False).get_matching_blocks():
        for offset in range(block.size):
            matches[gold_owner[block.a + offset]][asr_owner[block.b + offset]] += 1
    voice_indices = [index for index, message in enumerate(gold.messages) if message.speaker != "system"]
    aligned = []
    for index in voice_indices:
        if matches[index]:
            asr_index, count = matches[index].most_common(1)[0]
            aligned.append({"gold_index": index, "asr_index": asr_index, "matched_words": count,
                            "correct": gold.messages[index].speaker == predicted.messages[asr_index].speaker})
    asr_gold = defaultdict(Counter)
    for gold_index, votes in matches.items():
        if gold.messages[gold_index].speaker == "system":
            continue
        for asr_index, count in votes.items():
            asr_gold[asr_index][gold.messages[gold_index].speaker] += count
    wrong_reviewed = sum(
        predicted.messages[index].role_needs_review and
        predicted.messages[index].speaker != votes.most_common(1)[0][0]
        for index, votes in asr_gold.items()
    )
    return {
        "gold_voice_replies": len(voice_indices),
        "aligned_gold_replies": len(aligned),
        "correct_gold_replies": sum(row["correct"] for row in aligned),
        "wrong_reviewed_gold_replies": sum(not row["correct"] for row in aligned),
        "asr_segments": len(predicted.messages),
        "review_segments": sum(message.role_needs_review for message in predicted.messages),
        "aligned_review_segments": sum(predicted.messages[index].role_needs_review for index in asr_gold),
        "wrong_review_segments": wrong_reviewed,
        "speaker_ids": len({message.speaker_id for message in predicted.messages}),
    }


def main() -> None:
    records = json.loads(next(p for p in (ROOT / "data" / "dev_source.json", ROOT / "data" / "DEV" / "DEV_raw.json") if p.exists()).read_text(encoding="utf-8"))["data"]["records"]
    calls = sorted((record for record in records if record.get("channel") == "voice call"),
                   key=lambda record: record["conversation_id"])
    torch.set_num_threads(6)
    start = time.monotonic()
    asr = WhisperModel(str(ASR_MODEL), device="cpu", compute_type="int8", cpu_threads=8)
    speaker = EncoderClassifier.from_hparams(
        source=str(SPEAKER_MODEL), savedir=str(ROOT / ".speaker_cache"),
        run_opts={"device": "cpu"},
    )
    results = []
    for record in calls:
        cid = record["conversation_id"]
        t0 = time.monotonic()
        print("START", cid, flush=True)
        audio = download(record)
        with av.open(str(audio)) as container:
            duration = container.duration / 1_000_000
        segments = transcribe(cid, audio, asr)
        print("ASR", cid, len(segments), round(time.monotonic() - t0, 1), flush=True)
        ids = speaker_ids(cid, audio, segments, speaker)
        asr_record = {"conversation_id": cid, "channel": "voice call", "language": "RU",
                      "transcript": [{**segment, "speaker": ids[index],
                                      "source_language": detect_lang(segment["text"]) or "RU"}
                                     for index, segment in enumerate(segments)]}
        predicted = normalize(asr_record)  # no speaker_map or reference_transcript
        gold = normalize(record)
        metrics = compare(gold, predicted)
        metrics.update({"conversation_id": cid, "audio_seconds": round(duration, 1),
                        "wall_seconds": round(time.monotonic() - t0, 1)})
        results.append(metrics)
        (AUDIO / f"{cid}.roles.json").write_text(
            json.dumps(predicted.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(metrics, ensure_ascii=False), flush=True)
        (AUDIO.parent / "asr_role_partial.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    totals = {key: sum(row[key] for row in results) for key in (
        "gold_voice_replies", "aligned_gold_replies", "correct_gold_replies", "wrong_reviewed_gold_replies",
        "asr_segments", "review_segments", "aligned_review_segments", "wrong_review_segments",
        "audio_seconds", "wall_seconds")}
    totals["total_wall_seconds"] = round(time.monotonic() - start, 1)
    output = {"method": "Whisper large-v3 int8 CPU beam=1 + SpeechBrain ECAPA cosine clustering t=0.5; roles from phrases only; exact-token sequence alignment for measurement",
              "calls": results, "totals": totals}
    (AUDIO.parent / "asr_role_metrics.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TOTAL", json.dumps(totals, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
