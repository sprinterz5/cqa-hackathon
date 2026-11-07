"""Replaceable audio transcription backends and local speaker separation."""
from __future__ import annotations

import os
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol

from .facts import detect_lang
from .transcript import normalize

ROOT = Path(__file__).resolve().parent.parent
Progress = Callable[[str], None]


class STTBackend(Protocol):
    def transcribe(self, audio: Path, language: str | None) -> list[dict]: ...


def _audio_imports() -> None:
    # Local experiment dependencies are optional; importing the web API must stay cheap.
    for name in (".speaker_deps", ".asr_deps"):
        folder = ROOT / name
        if folder.exists() and str(folder) not in sys.path:
            sys.path.insert(0, str(folder))


@lru_cache(maxsize=1)
def _whisper():
    _audio_imports()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Не установлен faster-whisper для локального STT") from exc
    folder = Path(os.environ.get("LOCAL_STT_MODEL", str(ROOT / ".asr_models" / "large-v3")))
    if not folder.is_dir():
        raise RuntimeError("Локальная модель STT не найдена")
    return WhisperModel(str(folder), device="cpu", compute_type="int8",
                        cpu_threads=min(os.cpu_count() or 4, 8))


@lru_cache(maxsize=1)
def _speaker_model():
    _audio_imports()
    try:
        from speechbrain.inference.speaker import EncoderClassifier
        import torch
    except ImportError as exc:
        raise RuntimeError("Не установлены зависимости SpeechBrain для разделения голосов") from exc
    folder = Path(os.environ.get("LOCAL_SPEAKER_MODEL", str(ROOT / ".diarization_models" / "speechbrain-ecapa")))
    if not folder.is_dir():
        raise RuntimeError("Локальная модель разделения голосов не найдена")
    torch.set_num_threads(min(os.cpu_count() or 4, 6))
    return EncoderClassifier.from_hparams(
        source=str(folder), savedir=str(ROOT / ".speaker_cache"), run_opts={"device": "cpu"})


class LocalWhisperSTT:
    def transcribe(self, audio: Path, language: str | None) -> list[dict]:
        hint = {"RU": "ru", "KZ": "kk", "EN": "en"}.get((language or "").upper())
        segments, info = _whisper().transcribe(
            str(audio), language=hint, beam_size=1, best_of=1,
            condition_on_previous_text=False, vad_filter=True)
        fallback = {"ru": "RU", "kk": "KZ", "en": "EN"}.get(info.language, "RU")
        return [{"start": s.start, "end": s.end, "text": s.text.strip(),
                 "source_language": detect_lang(s.text) or fallback}
                for s in segments if s.text.strip()]


class AlemSTT:
    def transcribe(self, audio: Path, language: str | None) -> list[dict]:
        raise RuntimeError("Alem STT ожидает ключ и подтверждённый формат API")


def modes() -> dict:
    return {"local": {"available": (ROOT / ".asr_models" / "large-v3").is_dir()
             and (ROOT / ".diarization_models" / "speechbrain-ecapa").is_dir()},
            "cloud": {"available": False, "reason": "Ожидаются ключ и формат Alem STT"}}


def _speaker_ids(audio: Path, segments: list[dict]) -> list[str]:
    if len(segments) == 1:
        return ["speaker_1"]
    _audio_imports()
    import numpy as np
    import torch
    from faster_whisper.audio import decode_audio
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import cdist, pdist

    waveform = decode_audio(str(audio))
    model = _speaker_model()
    vectors = []
    for segment in segments:
        start, end = int(segment["start"] * 16000), int(segment["end"] * 16000)
        piece = waveform[start:end]
        if len(piece) < 16000:
            piece = np.pad(piece, (0, 16000 - len(piece)))
        with torch.no_grad():
            vectors.append(model.encode_batch(torch.from_numpy(piece).unsqueeze(0)).squeeze().numpy())
    embeddings = np.asarray(vectors)
    clusters = fcluster(linkage(pdist(embeddings, metric="cosine"), method="average"),
                        t=0.5, criterion="distance")
    counts = Counter(clusters)
    established = [cluster for cluster, count in counts.items() if count >= 2]
    if established:
        centers = np.asarray([embeddings[clusters == cluster].mean(axis=0) for cluster in established])
        for index, cluster in enumerate(clusters.copy()):
            if counts[cluster] == 1:
                nearest = int(np.argmin(cdist([embeddings[index]], centers, metric="cosine")))
                clusters[index] = established[nearest]
    return [f"speaker_{cluster}" for cluster in clusters]


def process_audio(audio: Path, mode: str = "local", language: str | None = None,
                  progress: Progress | None = None) -> dict:
    report = progress or (lambda _: None)
    backend: STTBackend
    if mode == "local":
        backend = LocalWhisperSTT()
    elif mode == "cloud":
        backend = AlemSTT()
    else:
        raise ValueError("Неизвестный режим STT")
    report("recognizing")
    segments = backend.transcribe(audio, language)
    if not segments:
        raise ValueError("Речь в записи не обнаружена")
    report("separating")
    speaker_ids = _speaker_ids(audio, segments)
    record = {"channel": "voice call", "language": language or "RU", "audio_url": None,
              "transcript": [{**segment, "speaker": speaker_ids[index]}
                             for index, segment in enumerate(segments)]}
    report("parsing")
    normalize(record)  # validate before storing; no labeled reference is passed here
    return record
