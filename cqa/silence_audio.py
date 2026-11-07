"""Small MP3 decoder and energy-based silence splitter for cloud STT."""
from __future__ import annotations

import io
import math
import sys
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RATE = 16000


def _av():
    try:
        import av
    except ImportError:
        local = ROOT / ".asr_deps"
        if local.is_dir() and str(local) not in sys.path:
            sys.path.insert(0, str(local))
        try:
            import av
        except ImportError as exc:
            raise RuntimeError("Для облачного STT нужен пакет av (PyAV)") from exc
    return av


def available() -> bool:
    try:
        _av()
    except RuntimeError:
        return False
    return True


def decode_pcm16(audio: Path) -> bytes:
    """Decode any PyAV-supported audio to 16 kHz mono PCM16 without numpy."""
    av = _av()
    chunks = []
    with av.open(str(audio)) as container:
        if not container.streams.audio:
            raise ValueError("В записи нет аудиопотока")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                chunks.append(bytes(converted.planes[0])[:converted.samples * 2])
        for converted in resampler.resample(None):
            chunks.append(bytes(converted.planes[0])[:converted.samples * 2])
    return b"".join(chunks)


def speech_regions(pcm: bytes, max_seconds: float = 9.0) -> list[dict[str, int]]:
    """Detect speech by 20 ms RMS windows; merge short gaps into upload chunks."""
    window = 320  # 20 ms at 16 kHz
    samples = array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    levels = []
    for start in range(0, len(samples), window):
        piece = samples[start:start + window]
        levels.append(math.sqrt(sum(value * value for value in piece) / len(piece)))
    if not levels:
        return []
    peak = sorted(levels)[int(0.9 * (len(levels) - 1))]
    threshold = max(180.0, peak * 0.08)
    active = [level >= threshold for level in levels]
    spans = []
    start = None
    last_active = None
    silence_windows = 18  # 360 ms
    for index, speech in enumerate(active):
        if speech:
            if start is None:
                start = index
            last_active = index
        elif start is not None and index - last_active >= silence_windows:
            if last_active - start + 1 >= 10:  # 200 ms minimum speech
                spans.append((max(0, start - 5), min(len(samples), (last_active + 6) * window)))
            start = last_active = None
    if start is not None and last_active - start + 1 >= 10:
        spans.append((max(0, start - 5), len(samples)))
    regions = []
    max_samples = int(max_seconds * RATE)
    for start_window, end in spans:
        start_sample = start_window * window
        if regions and start_sample - regions[-1]["end"] <= 12000 \
                and end - regions[-1]["start"] <= max_samples:
            regions[-1]["end"] = end
        else:
            while end - start_sample > max_samples:
                regions.append({"start": start_sample, "end": start_sample + max_samples})
                start_sample += max_samples
            regions.append({"start": start_sample, "end": end})
    return regions


def wav_bytes(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    return output.getvalue()
