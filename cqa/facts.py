"""Deterministic facts about a dialog — things an LLM is bad at computing itself.

Facts are given to the evaluator as context, never as verdicts. Content
judgments (was it rude? was identity actually checked by voice?) stay with the LLM.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from .transcript import Dialog, Message, fmt_offset

# --- system events (RU / KZ / EN). Order matters: more specific patterns first. ---
EVENT_PATTERNS: list[tuple[str, str]] = [
    ("hold_end", r"больше не находится на удержании|no longer on hold|ұстаудан шықты"),
    ("hold_start", r"находится на удержании|placed on hold|is on hold|ұстауда"),
    ("identity_verified", r"личность клиента подтверждена|жеке басы расталды|identity (?:was )?verified"),
    ("operator_closed", r"оператор закрыл чат|оператор чатты жапты|agent closed the chat"),
    ("client_left", r"клиент покинул чат|клиент завершил разговор|клиент чаттан шықты|customer left|customer ended"),
    ("connection_lost", r"соединение с клиентом прервано|байланыс үзілді|connection to customer lost"),
    ("agent_left", r"агент вышел из разговора|agent left"),
    ("escalation", r"эскалация на оператора|операторға жіберілді|transferred to an agent|переведен от агента на представителя"),
    ("consultation", r"консультация с агентом|consultation"),
    ("operator_joined", r"агент присоединился|agent joined"),
    ("recording_paused", r"приостановлен|recording paused|тоқтатылды"),
    ("recording_resumed", r"возобновлен|recording resumed|жалғастырылды"),
    ("recording_started", r"запись и транскрибирование запущен|жазу және транскрипция басталды|recording started"),
    ("language_changed", r"languages have been updated|language changed"),
]
_EVENT_RE = [(name, re.compile(p, re.IGNORECASE)) for name, p in EVENT_PATTERNS]

CLOSING = ("operator_closed", "client_left", "connection_lost")

EVENT_LABEL = {
    "hold_start": "клиент поставлен на удержание",
    "hold_end": "удержание снято",
    "identity_verified": "личность клиента подтверждена системой",
    "operator_closed": "оператор закрыл чат",
    "client_left": "клиент завершил диалог",
    "connection_lost": "соединение с клиентом прервано",
    "agent_left": "агент вышел из разговора",
    "escalation": "передача на оператора",
    "operator_joined": "оператор подключился",
    "consultation": "консультация с другим агентом",
    "recording_paused": "запись приостановлена",
    "recording_resumed": "запись возобновлена",
    "recording_started": "запись начата",
    "language_changed": "смена языка IVR",
}

# --- personal data patterns (UI highlighting / masking only, not given to the LLM as hints) ---
PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("card", re.compile(r"(?<!\d)(?:\d{4}[ \-]?){3}\d{4}(?!\d)")),
    ("iin", re.compile(r"(?<!\d)\d{12}(?!\d)")),
    ("phone", re.compile(r"(?<!\d)(?:\+7|8)[ \-(]*\d{3}[ \-)]*\d{3}[ \-]*\d{2}[ \-]*\d{2}(?!\d)")),
    ("email", re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")),
]

CHAT_WAIT_THRESHOLD = 120.0   # s: client waited this long for an operator reply
CALL_SILENCE_THRESHOLD = 20.0  # s: pause before an operator utterance in a call


class SystemEvent(BaseModel):
    message_id: str
    t: float | None
    type: str
    text: str


class Gap(BaseModel):
    from_id: str
    to_id: str
    seconds: float
    kind: str  # "response_wait" | "silence" | "hold"


class PiiHit(BaseModel):
    message_id: str
    speaker: str
    kind: str
    span: tuple[int, int]


class Facts(BaseModel):
    channel: str
    language: str | None
    duration_s: float | None
    counts: dict[str, int]
    operator_present: bool
    handoff_id: str | None                     # escalation / transfer to a human
    operator_first_turn: list[str]             # ids of the operator's first response (consecutive messages)
    bot_before_operator: bool                  # a bot / IVR talked to the client first
    client_before_operator: list[str]          # what the client said before the operator's first response
    identity_verified_id: str | None           # system event, if any
    events: list[SystemEvent]
    end_type: str                              # operator_closed | client_left | connection_lost | open
    end_id: str | None
    last_operator_id: str | None
    waits: list[Gap]                           # chats: long waits for an operator reply (after the first one)
    holds: list[Gap]                           # calls: hold periods
    silences: list[Gap]                        # calls: long pauses before operator speech
    pii: list[PiiHit]
    on_hold_ids: list[str] = []                # calls: messages spoken while the client was on hold


def classify_event(text: str) -> str:
    for name, rx in _EVENT_RE:
        if rx.search(text):
            return name
    return "other"


def find_pii(m: Message) -> list[PiiHit]:
    hits: list[PiiHit] = []
    for kind, rx in PII_PATTERNS:
        for x in rx.finditer(m.text):
            hits.append(PiiHit(message_id=m.id, speaker=m.speaker, kind=kind, span=(x.start(), x.end())))
    return hits


def _gap(a: Message, b: Message) -> float | None:
    if a.t is None or b.t is None:
        return None
    return round(b.t - a.t, 1)


def extract_facts(d: Dialog, chat_wait: float = CHAT_WAIT_THRESHOLD,
                  call_silence: float = CALL_SILENCE_THRESHOLD) -> Facts:
    msgs = d.messages
    counts = {s: sum(1 for m in msgs if m.speaker == s) for s in ("client", "operator", "bot", "system")}

    events = [SystemEvent(message_id=m.id, t=m.t, type=classify_event(m.text), text=m.text)
              for m in msgs if m.speaker == "system"]

    op_msgs = [m for m in msgs if m.speaker == "operator"]
    first_op = op_msgs[0] if op_msgs else None
    first_turn = next((t.message_ids for t in d.turns if t.speaker == "operator"), [])

    handoff = next((e.message_id for e in events if e.type in ("escalation", "operator_joined")), None)
    before = [m.id for m in msgs if m.speaker == "client" and (first_op is None or m.index < first_op.index)]
    ident = next((e.message_id for e in events if e.type == "identity_verified"), None)

    closing = [e for e in events if e.type in CLOSING]
    end_type, end_id = (closing[-1].type, closing[-1].message_id) if closing else ("open", None)

    # chats: the client's LAST message before an operator reply -> that reply (after the first response)
    waits: list[Gap] = []
    silences: list[Gap] = []
    holds: list[Gap] = []
    if first_op is not None:
        after_first = first_op.index + len(first_turn)
        if not d.is_call:
            pending: Message | None = None
            for m in msgs[after_first:]:
                if m.speaker == "client":
                    pending = m  # measure from the latest client message, not the first unanswered one
                elif m.speaker == "operator" and pending is not None:
                    g = _gap(pending, m)
                    if g is not None and g >= chat_wait:
                        waits.append(Gap(from_id=pending.id, to_id=m.id, seconds=g, kind="response_wait"))
                    pending = None
        else:
            paused = False
            for prev, m in zip(msgs[first_op.index:], msgs[first_op.index + 1:]):
                if prev.speaker == "system":
                    t = classify_event(prev.text)
                    paused = t == "recording_paused" or (paused and t != "recording_resumed")
                if m.speaker == "operator" and prev.speaker == "client" and not paused:
                    g = _gap(prev, m)
                    if g is not None and g >= call_silence:
                        silences.append(Gap(from_id=prev.id, to_id=m.id, seconds=g, kind="silence"))
            start: SystemEvent | None = None
            for e in events:
                if e.type == "hold_start":
                    start = e
                elif e.type == "hold_end" and start is not None:
                    secs = round(e.t - start.t, 1) if e.t is not None and start.t is not None else 0.0
                    holds.append(Gap(from_id=start.message_id, to_id=e.message_id, seconds=secs, kind="hold"))
                    start = None

    on_hold: list[str] = []
    for g in holds:
        a, b = int(g.from_id[1:]), int(g.to_id[1:])
        on_hold += [m.id for m in msgs[a:b - 1] if m.speaker != "system"]
    if on_hold:
        silences = [g for g in silences if g.to_id not in on_hold]

    times = [m.t for m in msgs if m.t is not None]
    return Facts(
        channel=d.channel,
        language=d.language,
        duration_s=round(max(times) - min(times), 1) if times else None,
        counts=counts,
        operator_present=bool(op_msgs),
        handoff_id=handoff,
        operator_first_turn=first_turn,
        bot_before_operator=any(m.speaker == "bot" and (first_op is None or m.index < first_op.index)
                                for m in msgs),
        client_before_operator=before,
        identity_verified_id=ident,
        events=events,
        end_type=end_type,
        end_id=end_id,
        last_operator_id=op_msgs[-1].id if op_msgs else None,
        waits=waits,
        holds=holds,
        silences=silences,
        pii=[h for m in msgs for h in find_pii(m)],
        on_hold_ids=on_hold,
    )


def _ids(ids: list[str]) -> str:
    if not ids:
        return "нет"
    return f"{ids[0]}–{ids[-1]}" if len(ids) > 2 else ", ".join(ids)


def facts_to_prompt(f: Facts) -> str:
    """Neutral, factual summary for the evaluator prompt (no verdicts, no PII hints)."""
    kind = "звонок (транскрипт автоматического распознавания речи)" if f.channel == "voice call" else "чат"
    lines = [f"- Канал: {kind}; язык набора: {f.language or 'не указан'}."]
    if not f.operator_present:
        lines.append("- Оператор в диалоге не участвовал.")
    else:
        if f.bot_before_operator:
            lines.append("- До оператора с клиентом общался бот" + (f"; передача на оператора — {f.handoff_id}."
                                                                   if f.handoff_id else "."))
        if f.client_before_operator:
            lines.append("- Сообщения клиента до первого ответа оператора (известные оператору детали): "
                         f"{', '.join(f.client_before_operator)}.")
        lines.append(f"- Первый ответ оператора: {_ids(f.operator_first_turn)}.")
    lines.append(
        f"- Системное подтверждение личности: {f.identity_verified_id}." if f.identity_verified_id
        else "- Системного события о подтверждении личности нет."
    )
    end = {"operator_closed": "оператор закрыл чат", "client_left": "клиент завершил диалог",
           "connection_lost": "соединение прервано"}.get(f.end_type)
    lines.append(f"- Завершение: {end} ({f.end_id})." if end else "- Системного события о завершении нет.")
    if f.last_operator_id:
        lines.append(f"- Последняя реплика оператора: {f.last_operator_id}.")
    for g in f.waits:
        lines.append(f"- Пауза {int(g.seconds)} с между сообщением клиента {g.from_id} и ответом оператора {g.to_id}. "
                     "Сама по себе пауза не нарушение: учитывайте её, только если оператор брал время на проверку и не предупредил клиента.")
    for g in f.holds:
        lines.append(f"- Клиент на удержании {int(g.seconds)} с: {g.from_id}–{g.to_id}. Реплики в этом "
                     "интервале помечены [HOLD]: это консультация оператора с коллегами, клиент их не слышит; "
                     "не оценивайте их как общение с клиентом.")
    for g in f.silences:
        lines.append(f"- Пауза {int(g.seconds)} с перед репликой оператора {g.to_id}.")
    other = [e for e in f.events if e.type not in CLOSING and e.type != "identity_verified"]
    if other:
        lines.append("- Системные события: " + "; ".join(
            f"{e.message_id} {fmt_offset(e.t)} {EVENT_LABEL.get(e.type, e.text)}" for e in other) + ".")
    lines.append("- Время до первого ответа оператора в оценку не входит.")
    return "\n".join(lines)


def transcript_to_prompt(d: Dialog, f: Facts | None = None) -> str:
    """Numbered transcript: [m14] +02:49 OPERATOR: text  (hold-time lines tagged [HOLD])"""
    hold = set(f.on_hold_ids) if f else set()
    return "\n".join(
        f"[{m.id}] {fmt_offset(m.t)} {m.speaker.upper()}{' [HOLD]' if m.id in hold else ''}: {m.text}"
        for m in d.messages
    )
