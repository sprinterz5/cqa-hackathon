"""Transcript normalization: one format for chats and calls.

- every message gets a stable id m1..mN (evidence and UI links use it)
- timestamps become offsets in seconds from the start (chats: MM:SS, calls: ISO 8601);
  the original timestamp is kept
- consecutive messages of the same speaker are grouped into turns
"""
from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Speaker = Literal["client", "operator", "bot", "system"]
SPEAKERS = ("client", "operator", "bot", "system")
ROLE_LABEL = {"client": "CLIENT", "operator": "OPERATOR", "bot": "BOT", "system": "SYSTEM"}


class Message(BaseModel):
    id: str            # "m1"
    index: int         # 0-based position
    speaker: Speaker
    text: str
    timestamp: str     # original value
    t: float | None    # seconds from the start of the conversation
    turn: int          # turn number (consecutive messages of one speaker)
    speaker_id: str | None = None  # original ASR speaker label, if present
    end_t: float | None = None     # ASR segment end in seconds
    source_language: str | None = None
    translation_ru: str | None = None
    role_source: str = "transcript"  # transcript | speaker_map | reference | context | ai | reviewer
    role_needs_review: bool = False


class Turn(BaseModel):
    index: int
    speaker: Speaker
    message_ids: list[str]
    speaker_id: str | None = None


class Dialog(BaseModel):
    conversation_id: str
    channel: str               # "agent chat" | "voice call"
    language: str | None       # RU | KZ | EN
    audio_url: str | None = None
    messages: list[Message]
    turns: list[Turn]
    start_time: str | None = None  # absolute start for calls (ISO), used for audio offsets

    @property
    def is_call(self) -> bool:
        return self.channel == "voice call"

    def message(self, mid: str) -> Message:
        return self.messages[int(mid[1:]) - 1]

    def has_message(self, mid: str) -> bool:
        return bool(re.fullmatch(r"m\d+", mid)) and 1 <= int(mid[1:]) <= len(self.messages)


_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,3}):(\d{2})$")
_FRACTION = re.compile(r"(\.\d{6})\d+")


def _parse_iso(ts: str) -> datetime:
    # Python accepts at most 6 fractional digits; the calls have 7.
    return datetime.fromisoformat(_FRACTION.sub(r"\1", ts.replace("Z", "+00:00")))


def parse_offsets(timestamps: list[str]) -> list[float | None]:
    """Convert raw timestamps into seconds from the first one (None if unparsable)."""
    out: list[float | None] = []
    base: datetime | None = None
    for ts in timestamps:
        ts = (ts or "").strip()
        m = _CLOCK.match(ts)
        if m:
            h, mnt, s = m.groups()
            out.append(int(h or 0) * 3600 + int(mnt) * 60 + int(s))
            continue
        try:
            dt = _parse_iso(ts)
        except ValueError:
            out.append(None)
            continue
        if base is None:
            base = dt
        out.append(round((dt - base).total_seconds(), 3))
    return out


def _seconds(value: object) -> float | None:
    """Read a non-negative ASR offset without confusing it with a clock or ISO time."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


# labels other systems use for the same roles (Dynamics exports, other ASR tools, the plain-text upload)
SPEAKER_ALIASES = {
    "agent": "operator", "оператор": "operator", "operator_1": "operator", "support": "operator",
    "customer": "client", "клиент": "client", "user": "client", "caller": "client",
    "ivr": "bot", "бот": "bot", "assistant": "bot", "система": "system", "event": "system",
}


def _speaker_key(message: dict) -> str:
    return str(message.get("speaker_id") or message.get("speaker") or "").strip().casefold()


def _match_text(value: object) -> str:
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))


def _resolve_speakers(record: dict, raw: list[dict]) -> dict[str, tuple[str, str]]:
    """Resolve ASR speaker IDs from an explicit map, reference transcript, then context."""
    groups: dict[str, list[dict]] = {}
    resolved: dict[str, tuple[str, str]] = {}
    for message in raw:
        if str(message.get("speaker") or "").strip().lower() not in SPEAKERS:
            key = _speaker_key(message)
            if not key:  # no speaker at all: treat as a system line (as before ASR support)
                resolved[""] = ("system", "transcript")
                continue
            if key in SPEAKER_ALIASES:  # common labels from other systems map directly
                resolved[key] = (SPEAKER_ALIASES[key], "transcript")
                continue
            groups.setdefault(key, []).append(message)

    supplied = {str(key).strip().casefold(): str(role).strip().lower()
                for key, role in (record.get("speaker_map") or {}).items()}
    if any(role not in SPEAKERS for role in supplied.values()):
        raise ValueError("speaker_map contains an invalid role")
    resolved.update({key: (supplied[key], "speaker_map") for key in groups if key in supplied})

    reference = [(str(m.get("speaker") or "").strip().lower(), _match_text(m.get("text")))
                 for m in (record.get("reference_transcript") or [])]
    reference = [(role, text) for role, text in reference if role in ("client", "operator", "bot") and len(text) >= 8]
    for key, messages in groups.items():
        if key in resolved or not reference:
            continue
        scores = {"client": 0.0, "operator": 0.0, "bot": 0.0}
        for message in messages:
            text = _match_text(message.get("text"))
            if len(text) < 8:
                continue
            matches = {role: max((SequenceMatcher(None, text, ref).ratio()
                                  for ref_role, ref in reference if ref_role == role), default=0.0)
                       for role in scores}
            ranked_match = sorted(matches, key=matches.get, reverse=True)
            if matches[ranked_match[0]] >= 0.6 and matches[ranked_match[0]] - matches[ranked_match[1]] >= 0.15:
                scores[ranked_match[0]] += matches[ranked_match[0]]
        ranked = sorted(scores, key=scores.get, reverse=True)
        if scores[ranked[0]] >= 0.6 and scores[ranked[0]] - scores[ranked[1]] >= 0.25:
            resolved[key] = (ranked[0], "reference")

    # Baseline for new audio without a labeled reference. Keep the ID stable across turns.
    cues = {
        "operator": re.compile(r"меня зовут|чем могу помочь|как могу помочь|провер(им|ка) личност|my name is|how may i help|менің атым|қалай көмектес"),
        "client": re.compile(r"у меня|мне нужно|я хочу|не могу|мой сч[её]т|i need|i want|my account|менің шот|маған керек"),
        "bot": re.compile(r"нажмите|для выбора|press (one|two|[0-9])|select an option|басыңыз"),
    }
    context_scores = {key: {role: sum(bool(pattern.search(_match_text(m.get("text")))) for m in messages)
                            for role, pattern in cues.items()}
                      for key, messages in groups.items()}
    for key in groups:
        if key in resolved:
            continue
        scores = context_scores[key]
        if scores["bot"] > max(scores["operator"], scores["client"]):
            role = "bot"
        elif scores["operator"] > scores["client"]:
            role = "operator"
        elif scores["client"] > scores["operator"]:
            role = "client"
        else:
            other_roles = {role for other, (role, _) in resolved.items() if other != key and other in groups}
            role = "operator" if "client" in other_roles and "operator" not in other_roles else "client"
        resolved[key] = (role, "context")
    return resolved


def normalize(record: dict) -> Dialog:
    """Build a Dialog from DEV messages or timestamped ASR segments."""
    raw = record.get("transcript") or []
    timestamps = ["" if m.get("timestamp") is None else str(m["timestamp"]) for m in raw]
    offsets = parse_offsets(timestamps)
    speaker_roles = _resolve_speakers(record, raw)
    messages: list[Message] = []
    turns: list[Turn] = []
    for i, (m, timestamp, offset) in enumerate(zip(raw, timestamps, offsets)):
        original_speaker = str(m.get("speaker") or "").strip()
        speaker = original_speaker.lower()
        role_source = str(m.get("role_source") or "transcript")  # "ai" for uploaded recordings
        if speaker not in SPEAKERS:
            speaker, role_source = speaker_roles[_speaker_key(m)]
        speaker_id = m.get("speaker_id")
        speaker_id = str(speaker_id).strip() if speaker_id is not None else None
        if not speaker_id and original_speaker.lower() not in SPEAKERS:
            speaker_id = original_speaker or None
        t = _seconds(m.get("start"))
        if t is None:
            t = offset if offset is not None else _seconds(m.get("timestamp"))
        end_t = _seconds(m.get("end", m.get("end_t")))
        if not turns or turns[-1].speaker != speaker or turns[-1].speaker_id != speaker_id:
            turns.append(Turn(index=len(turns), speaker=speaker, message_ids=[], speaker_id=speaker_id))
        mid = f"m{i + 1}"
        turns[-1].message_ids.append(mid)
        messages.append(Message(
            id=mid, index=i, speaker=speaker, text=str(m.get("text") or "").strip(),
            timestamp=timestamp, t=t, turn=turns[-1].index,
            speaker_id=speaker_id, end_t=end_t,
            source_language=m.get("source_language") or m.get("language"),
            translation_ru=m.get("translation_ru"),
            role_source=role_source, role_needs_review=role_source in ("context", "ai"),
        ))
    channel = str(record.get("channel") or "agent chat").strip().lower()
    start = timestamps[0] if timestamps and channel == "voice call" and "T" in timestamps[0] else None
    return Dialog(
        conversation_id=str(record.get("conversation_id") or "uploaded"),
        channel=channel,
        language=(record.get("language") or None),
        audio_url=record.get("audio_url") or None,
        messages=messages,
        turns=turns,
        start_time=start,
    )


_PLAIN_LINE = re.compile(
    r"^\s*(?:\[?(?P<ts>[\d:.TZ+\-]+)\]?\s+)?(?P<sp>client|operator|bot|system|клиент|оператор|бот|система)\s*[:\-—]\s*(?P<text>.+)$",
    re.IGNORECASE,
)
_RU_ROLES = {"клиент": "client", "оператор": "operator", "бот": "bot", "система": "system"}


def parse_plain_text(text: str, conversation_id: str = "uploaded", channel: str = "agent chat",
                     language: str | None = None) -> dict:
    """Parse 'speaker: text' lines (optionally prefixed by a timestamp) into an API-format record.
    Lines that don't start with a speaker are appended to the previous message."""
    transcript: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _PLAIN_LINE.match(line)
        if m:
            sp = m.group("sp").lower()
            transcript.append({
                "timestamp": m.group("ts") or "",
                "speaker": _RU_ROLES.get(sp, sp),
                "text": m.group("text").strip(),
            })
        elif transcript:
            transcript[-1]["text"] += "\n" + line.strip()
    return {"conversation_id": conversation_id, "channel": channel, "language": language,
            "transcript": transcript, "audio_url": None}


def fmt_offset(t: float | None) -> str:
    if t is None:
        return "  ?  "
    t = int(t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"+{h}:{m:02d}:{s:02d}" if h else f"+{m:02d}:{s:02d}"
