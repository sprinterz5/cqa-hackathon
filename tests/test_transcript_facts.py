from cqa.facts import classify_event, extract_facts, facts_to_prompt, transcript_to_prompt
from cqa.transcript import normalize, parse_offsets, parse_plain_text


def rec(transcript, channel="agent chat", language="RU"):
    return {"conversation_id": "t", "channel": channel, "language": language,
            "transcript": [{"timestamp": ts, "speaker": sp, "text": tx} for ts, sp, tx in transcript],
            "audio_url": None}


CHAT = rec([
    ("00:00", "client", "Где мой тариф?"),
    ("00:02", "bot", "Здравствуйте! Я AIM."),
    ("00:38", "system", "Эскалация на оператора."),
    ("03:15", "operator", "Здравствуйте!"),
    ("03:16", "operator", "Меня зовут Алина."),
    ("03:30", "client", "А комиссия?"),
    ("05:00", "operator", "Комиссия 0.1%."),
    ("05:10", "system", "Оператор закрыл чат."),
])


def test_offsets_chat_and_iso_with_7_fraction_digits():
    assert parse_offsets(["00:00", "01:15", "1:02:03"]) == [0, 75, 3723]
    iso = parse_offsets(["2026-11-03T13:10:00.6388245+00:00", "2026-11-03T13:11:17.2581733+00:00"])
    assert iso[0] == 0 and abs(iso[1] - 76.619) < 0.01


def test_ids_and_turns():
    d = normalize(CHAT)
    assert [m.id for m in d.messages][:2] == ["m1", "m2"]
    assert d.message("m4").speaker == "operator"
    assert [t.speaker for t in d.turns] == ["client", "bot", "system", "operator", "client", "operator", "system"]


def test_chat_facts():
    f = extract_facts(normalize(CHAT))
    assert f.operator_first_turn == ["m4", "m5"]     # consecutive messages = one response
    assert f.bot_before_operator and f.handoff_id == "m3"
    assert f.client_before_operator == ["m1"]
    assert f.end_type == "operator_closed" and f.end_id == "m8"
    assert "m4, m5" in facts_to_prompt(f)
    # the 2:37 wait before the first response is excluded; the 90 s wait after it is reported
    f60 = extract_facts(normalize(CHAT), chat_wait=60)
    assert [(g.from_id, g.to_id, g.seconds) for g in f60.waits] == [("m6", "m7", 90.0)]
    assert f.waits == []  # default threshold is 120 s


def test_wait_is_measured_from_the_latest_client_message():
    chat = rec([
        ("00:00", "operator", "Здравствуйте, я Дана."),
        ("00:10", "client", "Вопрос."),
        ("01:40", "client", "Ещё вопрос."),
        ("02:00", "operator", "Ответ."),
    ])
    f = extract_facts(normalize(chat), chat_wait=60)
    assert f.waits == []  # 20 s after the latest message, not 110 s after the first one


def test_event_classification_three_languages():
    assert classify_event("Клиенттің жеке басы расталды.") == "identity_verified"
    assert classify_event("Customer identity verified.") == "identity_verified"
    assert classify_event("Клиентпен байланыс үзілді.") == "connection_lost"
    assert classify_event("Agent closed the chat.") == "operator_closed"
    assert classify_event("Клиент больше не находится на удержании.") == "hold_end"
    assert classify_event("Клиент находится на удержании.") == "hold_start"
    assert classify_event("Началась консультация с агентом.") == "consultation"


def test_call_hold_lines_are_tagged():
    t = "2026-11-03T13:{}+00:00"
    call = rec([
        (t.format("10:00.0"), "operator", "Здравствуйте, меня зовут [Имя скрыто]."),
        (t.format("10:05.0"), "client", "Хочу купить бумагу."),
        (t.format("10:10.0"), "operator", "Ожидайте на линии."),
        (t.format("10:11.0"), "system", "Клиент находится на удержании."),
        (t.format("10:20.0"), "operator", "Коллега, подскажи по бумаге."),
        (t.format("12:00.0"), "system", "Клиент больше не находится на удержании."),
        (t.format("12:02.0"), "operator", "Спасибо за ожидание."),
    ], channel="voice call")
    d = normalize(call)
    f = extract_facts(d)
    assert f.on_hold_ids == ["m5"]
    assert [(g.from_id, g.to_id) for g in f.holds] == [("m4", "m6")]
    assert "OPERATOR [HOLD]: Коллега" in transcript_to_prompt(d, f)
    assert d.start_time.startswith("2026-11-03T13:10")


def test_plain_text_upload():
    r = parse_plain_text("00:01 Клиент: привет\n00:05 operator: Здравствуйте, я Дана\nпродолжение\n")
    assert [m["speaker"] for m in r["transcript"]] == ["client", "operator"]
    assert r["transcript"][1]["text"].endswith("продолжение")
