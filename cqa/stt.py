"""Speech-to-text for uploaded call recordings.

Alem STT (OpenAI-compatible /audio/transcriptions) returns timestamped phrases but no speakers,
so roles are assigned by the LLM from the dialogue content and marked for review in the UI.

The Kazakh model (speech-to-text-kk) returns plain text without timestamps or punctuation, so
phrases with timestamps always come from the general model; the language is passed to it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import httpx

from .llm import load_env

LANG_CODES = {"RU": "ru", "KZ": "kk", "EN": "en"}
_DETECTED = {"ru": "RU", "russian": "RU", "kk": "KZ", "kazakh": "KZ", "en": "EN", "english": "EN"}
ROLES = ("operator", "client", "bot")


class STTError(RuntimeError):
    pass


@dataclass
class STTConfig:
    base_url: str
    api_key: str
    model: str

    def public(self) -> dict:
        return {"model": self.model, "base_url": self.base_url}


def stt_from_env() -> STTConfig | None:
    load_env()
    key = os.environ.get("STT_API_KEY")
    if not key:
        return None
    return STTConfig(os.environ.get("STT_BASE_URL", "https://llm.alem.ai/v1").rstrip("/"), key,
                     os.environ.get("STT_MODEL", "speech-to-text"))


async def transcribe(cfg: STTConfig, data: bytes, filename: str, language: str | None = None,
                     timeout: float = 600.0) -> dict:
    """Return {"segments": [{"start", "end", "text"}], "language": RU|KZ|EN|None, "duration"}."""
    form = {"model": cfg.model, "response_format": "verbose_json", "timestamp_granularities[]": "segment"}
    if language:
        form["language"] = LANG_CODES[language]
    async with httpx.AsyncClient(timeout=timeout) as http:
        r = await http.post(f"{cfg.base_url}/audio/transcriptions", data=form,
                            files={"file": (filename, data)},
                            headers={"Authorization": f"Bearer {cfg.api_key}"})
    if r.status_code >= 400:
        raise STTError(f"распознавание: HTTP {r.status_code}: {r.text[:300]}")
    return parse_transcription(r.json())


def _hallucination(s: dict) -> bool:
    """Whisper's usual filters: text that compresses too well is a loop ("Я не могу ничего сказать" × 20
    over hold music), very low confidence on silence is invented speech."""
    if float(s.get("compression_ratio") or 0) > 2.4:
        return True
    return float(s.get("no_speech_prob") or 0) > 0.6 and float(s.get("avg_logprob") or 0) < -1.0


def parse_transcription(payload: dict) -> dict:
    segments, dropped = [], 0
    for s in payload.get("segments") or []:
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        key = " ".join(re.findall(r"\w+", text.casefold()))
        if _hallucination(s) or (segments and key and key == segments[-1]["_key"]):  # loops repeat the phrase
            dropped += 1
            continue
        segments.append({"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2),
                         "text": text, "_key": key})
    for s in segments:
        del s["_key"]
    duration = float(payload.get("duration") or 0)
    if not segments and str(payload.get("text") or "").strip():
        segments = [{"start": 0.0, "end": duration, "text": payload["text"].strip()}]
    return {"segments": segments, "duration": duration, "dropped": dropped,
            "language": _DETECTED.get(str(payload.get("language") or "").strip().lower())}


ROLE_PROMPT = """Ты размечаешь расшифровку телефонного звонка в службу поддержки Freedom Broker.
Распознавание речи разбило звонок на фразы, но не различает, кто говорит.
Для каждой фразы определи говорящего:
- "operator" — сотрудник поддержки: приветствует от имени компании, представляется, проверяет личность, объясняет, предлагает помощь, прощается;
- "client" — клиент: описывает проблему, отвечает на вопросы оператора, называет свои данные;
- "bot" — автоответчик (IVR): меню, «нажмите», «ваш звонок важен».
Соседние фразы часто принадлежат одному человеку; ответ на вопрос обычно произносит другой.
Распознавание иногда склеивает в одну фразу слова двух людей («Да, верно. Хорошо, тогда смотрите…»). Только в таком явном случае верни для фразы вместо "role" список "parts": [{"role": "client", "text": "Да, верно."}, {"role": "operator", "text": "Хорошо, тогда смотрите…"}] — тексты частей подряд должны дать исходную фразу слово в слово. Без явной причины фразу не дели.
Ответь только JSON: {"roles": [{"n": 1, "role": "operator"}, {"n": 2, "parts": [...]}, ...]} — по одному элементу на каждую фразу, n — номер фразы."""

_CUES = {
    "operator": re.compile(r"меня зовут|чем могу помочь|могу ещё|могу еще|провер|идентифицир|my name is|how (can|may) i help|менің атым|көмектес"),
    "client": re.compile(r"у меня|мне нужно|я хочу|не могу|мой сч[её]т|i need|i want|my account|менің шот|маған керек"),
    "bot": re.compile(r"нажмите|для выбора|press (one|two|[0-9])|select an option|басыңыз"),
}


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold().replace("ё", "е"))


def _parts(x: dict, text: str) -> list[tuple[str, str]] | None:
    """Accept a split only if the parts reproduce the phrase word for word and have valid roles."""
    parts = x.get("parts")
    if not isinstance(parts, list) or len(parts) < 2:
        return None
    out = [(str(p.get("role") or "").strip().lower(), str(p.get("text") or "").strip())
           for p in parts if isinstance(p, dict)]
    if len(out) != len(parts) or any(r not in ROLES or not t for r, t in out):
        return None
    if [w for _, t in out for w in _words(t)] != _words(text) or len({r for r, _ in out}) < 2:
        return None
    return out


def _parse_roles(content: str, n: int, texts: list[str] | None = None) -> tuple[list[str], dict[int, list]]:
    """Roles by phrase number, so an extra or missing item does not shift the rest.
    A few gaps take the previous phrase's role; many gaps mean the answer is unusable.
    -> (role per phrase, {phrase index: [(role, text), ...]} for phrases the model split)."""
    m = re.search(r"\{.*\}", content, re.S)
    items = json.loads(m.group(0) if m else content).get("roles")
    if not isinstance(items, list):
        raise ValueError(f"нет списка roles: {content[:200]!r}")
    by_n: dict[int, str] = {}
    splits: dict[int, list] = {}
    if all(isinstance(x, str) for x in items):  # plain list: only trustworthy when the length matches
        if len(items) != n:
            raise ValueError(f"ожидалось {n} ролей, получено {len(items)}")
        by_n = {i + 1: x for i, x in enumerate(items)}
    else:
        for x in items:
            try:
                k = int(x.get("n"))
            except (AttributeError, TypeError, ValueError):
                continue
            parts = _parts(x, texts[k - 1]) if texts and 1 <= k <= n else None
            if parts:
                splits.setdefault(k - 1, parts)
            # an inexact split keeps the phrase whole with the role of its longest part
            given = [p for p in x.get("parts") or [] if isinstance(p, dict) and p.get("role")]
            role = x.get("role") or (max(given, key=lambda p: len(str(p.get("text") or "")))["role"] if given else None)
            by_n.setdefault(k, role)
    by_n = {k: str(v).strip().lower() for k, v in by_n.items() if 1 <= k <= n and str(v).strip().lower() in ROLES}
    if len(by_n) < n * 0.9:
        raise ValueError(f"роли есть только у {len(by_n)} из {n} фраз")
    roles: list[str] = []
    for i in range(1, n + 1):
        roles.append(by_n.get(i) or (roles[-1] if roles else next(iter(by_n.values()))))
    return roles, splits


def apply_splits(segments: list[dict], roles: list[str], splits: dict[int, list]) -> tuple[list[dict], list[str]]:
    """Replace split phrases by their parts; part times are proportional to text length."""
    segs, out_roles = [], []
    for i, (s, role) in enumerate(zip(segments, roles)):
        if i not in splits:
            segs.append(s)
            out_roles.append(role)
            continue
        total = sum(len(t) for _, t in splits[i]) or 1
        t, span = s["start"], s["end"] - s["start"]
        for r, text in splits[i]:
            end = round(t + span * len(text) / total, 2)
            segs.append({**s, "start": round(t, 2), "end": end, "text": text, "split": True})
            out_roles.append(r)
            t = end
    return segs, out_roles


def guess_roles(segments: list[dict]) -> list[str]:
    """Fallback when the LLM is unavailable: phrase cues, otherwise the previous speaker."""
    roles: list[str] = []
    for s in segments:
        text = s["text"].casefold()
        hit = next((role for role in ("bot", "operator", "client") if _CUES[role].search(text)), None)
        roles.append(hit or (roles[-1] if roles else "operator"))
    return roles


VOICE_HINT = """
У каждой фразы есть метка [голос N] — группа по тембру из автоматического разделения голосов. Обычно один голос — один человек, поэтому фразы одного голоса чаще всего имеют одну роль. Но разделение ошибается (короткие фразы, перебивания, музыка): если смысл фразы явно противоречит голосу, доверяй смыслу."""


async def assign_roles(client, segments: list[dict], split: bool = True) -> tuple[list[dict], list[str], str, str | None]:
    """-> (phrases, role per phrase, source, error); source is "ai" or "context" (fallback).
    A phrase with "voice" (voice group from speaker separation, local mode) shows it to the model as a hint.
    With split=True the model may cut a phrase where two people speak; the parts replace it."""
    voices = [s.get("voice") for s in segments] if all(s.get("voice") for s in segments) else None
    voice_no = {v: i + 1 for i, v in enumerate(dict.fromkeys(voices))} if voices else {}
    lines = "\n".join(f"{i + 1}. [{s['start']:.1f}–{s['end']:.1f} c]"
                      + (f" [голос {voice_no[voices[i]]}]" if voices else "") + f" {s['text']}"
                      for i, s in enumerate(segments))
    messages = [{"role": "system", "content": ROLE_PROMPT + (VOICE_HINT if voices else "")},
                {"role": "user", "content": f"Фраз: {len(segments)}\n\n{lines}"}]
    error = None
    for _ in range(2):
        try:
            resp = await client.chat(messages, temperature=0.0, max_tokens=8000)
            roles, splits = _parse_roles(resp.content, len(segments), [s["text"] for s in segments])
            segs, roles = apply_splits(segments, roles, splits if split else {})
            return segs, roles, "ai", None
        except Exception as e:  # LLM down or malformed answer: retry once, then fall back to cues
            error = str(e)[:300]
    return segments, guess_roles(segments), "context", error


def merge_segments(segments: list[dict], roles: list[str], role_source: str) -> list[dict]:
    """Consecutive phrases of one speaker (and one language) become one transcript line in the organizers'
    format. Language, Russian translation and voice group of local recognition are kept."""
    out: list[dict] = []
    for i, (s, role) in enumerate(zip(segments, roles)):
        lang = s.get("source_language")
        if out and out[-1]["speaker"] == role and out[-1].get("source_language") == lang:
            out[-1]["text"] += " " + s["text"]
            out[-1]["end"] = s["end"]
            if s.get("translation_ru"):
                out[-1]["translation_ru"] = " ".join(filter(None, [out[-1].get("translation_ru"), s["translation_ru"]]))
            continue
        mm, ss = divmod(int(s["start"]), 60)
        line = {"timestamp": f"{mm:02d}:{ss:02d}", "start": s["start"], "end": s["end"],
                "speaker": role, "text": s["text"], "role_source": role_source}
        if lang:
            line["source_language"] = lang
        if s.get("translation_ru"):
            line["translation_ru"] = s["translation_ru"]
        if s.get("voice"):
            line["speaker_id"] = s["voice"]
        out.append(line)
    return out
