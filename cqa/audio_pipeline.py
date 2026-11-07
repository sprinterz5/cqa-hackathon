"""Replaceable audio transcription backends and local speaker separation."""
from __future__ import annotations

import os
import sys
import io
import wave
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol

from .facts import detect_lang
from .llm import load_env
from .transcript import normalize
from .translation import available as translation_available, translate_segments

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
def _kazakh_whisper():
    _audio_imports()
    from faster_whisper import WhisperModel
    folder = Path(os.environ.get("LOCAL_KAZAKH_STT_MODEL",
                                 str(ROOT / ".asr_models" / "kazakh-turbo-ct2")))
    if not (folder / "model.bin").is_file():
        raise RuntimeError("Казахская модель STT не найдена")
    return WhisperModel(str(folder), device="cpu", compute_type="int8",
                        cpu_threads=min(os.cpu_count() or 4, 8))


@lru_cache(maxsize=1)
def _language_whisper():
    _audio_imports()
    from faster_whisper import WhisperModel
    folder = Path(os.environ.get("LOCAL_LANGUAGE_MODEL",
                                 str(ROOT / ".asr_models" / "tiny-langid")))
    if not (folder / "model.bin").is_file():
        raise RuntimeError("Локальная модель определения языка не найдена")
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
        _audio_imports()
        from faster_whisper.audio import decode_audio
        waveform = decode_audio(str(audio))
        model = _whisper()
        detector = _language_whisper()
        hint = {"RU": "ru", "KZ": "kk", "EN": "en"}.get((language or "").upper())
        segments, info = model.transcribe(
            waveform, language=hint, beam_size=1, best_of=1,
            condition_on_previous_text=False, vad_filter=True)
        initial = list(segments)
        fallback = {"ru": "RU", "kk": "KZ", "en": "EN"}.get(info.language, "RU")
        output = []
        previous = fallback
        for segment in initial:
            start, end = max(0, int(segment.start * 16000)), min(len(waveform), int(segment.end * 16000))
            clip = waveform[start:end]
            chosen = _clip_language(clip, text_language=detect_lang(segment.text),
                                    previous=previous, detector=detector, fallback_model=model)
            previous = chosen
            rerun = chosen == "KZ" or (chosen != fallback and chosen in ("RU", "EN"))
            if rerun and len(clip) >= 8000:
                specialist = _kazakh_whisper() if chosen == "KZ" else model
                code = {"RU": "ru", "KZ": "kk", "EN": "en"}[chosen]
                better, _ = specialist.transcribe(
                    clip, language=code, beam_size=1, best_of=1,
                    condition_on_previous_text=False, vad_filter=False)
                replacements = [s for s in better if s.text.strip()]
            else:
                replacements = []
            if replacements:
                output.extend({"start": segment.start + s.start, "end": segment.start + s.end,
                               "text": s.text.strip(), "source_language": chosen}
                              for s in replacements)
            elif segment.text.strip():
                output.append({"start": segment.start, "end": segment.end,
                               "text": segment.text.strip(), "source_language": chosen})
        return output


def _clip_language(clip, text_language: str | None, previous: str,
                   detector, fallback_model) -> str:
    chosen = text_language or previous
    if len(clip) >= 16000:
        acoustic, probability, _ = detector.detect_language(audio=clip)
        if probability < 0.9 or acoustic not in ("ru", "kk", "en"):
            acoustic, probability, _ = fallback_model.detect_language(audio=clip)
        if acoustic in ("ru", "kk", "en") and probability >= 0.55:
            chosen = {"ru": "RU", "kk": "KZ", "en": "EN"}[acoustic]
    return chosen


class AlemSTT:
    def transcribe(self, audio: Path, language: str | None) -> list[dict]:
        _audio_imports()
        load_env()
        import httpx
        import numpy as np
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        waveform = decode_audio(str(audio))
        regions = get_speech_timestamps(waveform, VadOptions(
            max_speech_duration_s=9, min_silence_duration_ms=350, speech_pad_ms=100))
        merged = []
        for region in regions:
            if merged and region["start"] - merged[-1]["end"] <= 12000 \
                    and region["end"] - merged[-1]["start"] <= 9 * 16000:
                merged[-1]["end"] = region["end"]
            else:
                merged.append(dict(region))
        if not merged:
            return []
        detector, fallback_model = _language_whisper(), _whisper()
        base = os.environ.get("STT_BASE_URL", "https://llm.alem.ai/v1").rstrip("/")
        result = []
        previous = language or "RU"
        with httpx.Client(timeout=120) as client:
            for region in merged:
                clip = waveform[region["start"]:region["end"]]
                chosen = _clip_language(clip, None, previous, detector, fallback_model)
                previous = chosen
                key = (os.environ.get("STT_KZ_API_KEY") if chosen == "KZ" else
                       os.environ.get("STT_EN_API_KEY") if chosen == "EN" and os.environ.get("STT_EN_API_KEY") else
                       os.environ.get("STT_API_KEY"))
                if not key:
                    raise RuntimeError(f"Нет ключа Alem STT для {chosen}")
                model = "speech-to-text-kk" if chosen == "KZ" else "speech-to-text"
                wav = io.BytesIO()
                with wave.open(wav, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(16000)
                    output.writeframes((np.clip(clip, -1, 1) * 32767).astype("<i2").tobytes())
                response = client.post(base + "/audio/transcriptions",
                                       headers={"Authorization": "Bearer " + key},
                                       files={"file": ("segment.wav", wav.getvalue(), "audio/wav")},
                                       data={"model": model, "language": {"RU": "ru", "KZ": "kk", "EN": "en"}[chosen]})
                response.raise_for_status()
                body = response.json()
                start, end = region["start"] / 16000, region["end"] / 16000
                if isinstance(body, dict) and isinstance(body.get("segments"), list) and body["segments"]:
                    for part in body["segments"]:
                        if isinstance(part, dict) and str(part.get("text") or "").strip():
                            result.append({"start": start + max(0, float(part.get("start", 0))),
                                           "end": min(end, start + float(part.get("end", end - start))),
                                           "text": part["text"].strip(), "source_language": chosen})
                elif isinstance(body, dict) and str(body.get("text") or "").strip():
                    result.append({"start": start, "end": end, "text": body["text"].strip(),
                                   "source_language": chosen})
                else:
                    raise RuntimeError("Alem STT вернул ответ без текста")
        return result


def modes() -> dict:
    load_env()
    kazakh = (ROOT / ".asr_models" / "kazakh-turbo-ct2" / "model.bin").is_file()
    language_model = (ROOT / ".asr_models" / "tiny-langid" / "model.bin").is_file()
    support = ((ROOT / ".asr_models" / "large-v3").is_dir()
               and (ROOT / ".diarization_models" / "speechbrain-ecapa").is_dir()
               and language_model and translation_available())
    return {"local": {"available": support and kazakh,
             "kazakh_available": kazakh, "language_model_available": language_model,
             "translation_available": translation_available()},
            "cloud": {"available": bool(support and os.environ.get("STT_API_KEY")
                                        and os.environ.get("STT_KZ_API_KEY")),
                      "reason": "Нужны STT_API_KEY, STT_KZ_API_KEY и локальные модули обработки"}}


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
    report("translating")
    segments = translate_segments(segments)
    record = {"channel": "voice call", "language": language or "RU", "audio_url": None,
              "transcript": [{**segment, "speaker": speaker_ids[index]}
                             for index, segment in enumerate(segments)]}
    report("parsing")
    normalize(record)  # validate before storing; no labeled reference is passed here
    return record
