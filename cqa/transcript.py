"""Transcript normalization: one format for chats and calls.

- every message gets a stable id m1..mN (evidence and UI links use it)
- timestamps become offsets in seconds from the start (chats: MM:SS, calls: ISO 8601);
  the original timestamp is kept
- consecutive messages of the same speaker are grouped into turns
"""
from __future__ import annotations

import re
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


class Turn(BaseModel):
    index: int
    speaker: Speaker
    message_ids: list[str]


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


def normalize(record: dict) -> Dialog:
    """Build a Dialog from a record in the organizers' API format."""
    raw = record.get("transcript") or []
    offsets = parse_offsets([str(m.get("timestamp", "")) for m in raw])
    messages: list[Message] = []
    turns: list[Turn] = []
    for i, (m, t) in enumerate(zip(raw, offsets)):
        speaker = str(m.get("speaker", "")).strip().lower()
        if speaker not in SPEAKERS:
            speaker = "system"
        if not turns or turns[-1].speaker != speaker:
            turns.append(Turn(index=len(turns), speaker=speaker, message_ids=[]))
        mid = f"m{i + 1}"
        turns[-1].message_ids.append(mid)
        messages.append(Message(
            id=mid, index=i, speaker=speaker, text=str(m.get("text", "")).strip(),
            timestamp=str(m.get("timestamp", "")), t=t, turn=turns[-1].index,
        ))
    channel = str(record.get("channel") or "agent chat").strip().lower()
    start = raw[0].get("timestamp") if raw and channel == "voice call" else None
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
