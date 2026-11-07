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


def test_asr_segments_preserve_speakers_times_and_translation():
    d = normalize({
        "conversation_id": "asr-1", "channel": "voice call", "language": "KZ",
        "reference_transcript": [
            {"speaker": "client", "text": "Сәлеметсіз бе"},
            {"speaker": "client", "text": "Менің сұрағым бар"},
            {"speaker": "operator", "text": "Тыңдап тұрмын"},
        ],
        "transcript": [
            {"speaker": "speaker_1", "start": 1.25, "end": 2.5,
             "source_language": "kk", "text": "Сәлеметсіз бе",
             "translation_ru": "Здравствуйте"},
            {"speaker": "speaker_1", "start": 2.5, "end_t": 3.0, "text": "Менің сұрағым бар"},
            {"speaker": "speaker_2", "start": 3.1, "end": 4.2, "text": "Тыңдап тұрмын"},
        ],
    })
    assert [m.speaker for m in d.messages] == ["client", "client", "operator"]
    assert [m.speaker_id for m in d.messages] == ["speaker_1", "speaker_1", "speaker_2"]
    assert [m.role_source for m in d.messages] == ["reference", "reference", "reference"]
    assert not any(m.role_needs_review for m in d.messages)
    assert [t.message_ids for t in d.turns] == [["m1", "m2"], ["m3"]]
    assert [t.speaker_id for t in d.turns] == ["speaker_1", "speaker_2"]
    assert (d.messages[0].t, d.messages[0].end_t) == (1.25, 2.5)
    assert d.messages[1].end_t == 3.0
    assert d.messages[0].source_language == "kk"
    assert d.messages[0].translation_ru == "Здравствуйте"
    assert d.messages[0].text == "Сәлеметсіз бе"
    assert d.start_time is None


def test_context_role_fallback_flags_review():
    d = normalize({"conversation_id": "asr-2", "channel": "voice call",
                   "transcript": [
                       {"speaker": "speaker_1", "start": 0, "text": "Здравствуйте, меня зовут Алина. Чем могу помочь?"},
                       {"speaker": "speaker_2", "start": 3, "text": "Мне нужно узнать баланс моего счета."},
                   ]})
    assert [m.speaker for m in d.messages] == ["operator", "client"]
    assert all(m.role_source == "context" and m.role_needs_review for m in d.messages)


def test_context_fallback_labels_bot_client_and_operator():
    d = normalize({"conversation_id": "asr-ivr", "channel": "voice call",
                   "transcript": [
                       {"speaker": "speaker_1", "start": 0, "text": "Для выбора русского языка нажмите один."},
                       {"speaker": "speaker_2", "start": 4, "text": "У меня вопрос по брокерскому счёту."},
                       {"speaker": "speaker_3", "start": 8, "text": "Здравствуйте, меня зовут Алина. Чем могу помочь?"},
                   ]})
    assert [m.speaker for m in d.messages] == ["bot", "client", "operator"]
    assert all(m.role_needs_review for m in d.messages)


def test_explicit_speaker_map_takes_precedence():
    d = normalize({"conversation_id": "asr-3", "speaker_map": {"speaker_1": "client"},
                   "transcript": [{"speaker": "speaker_1", "start": 0, "text": "Здравствуйте"}]})
    assert d.messages[0].speaker == "client"
    assert d.messages[0].role_source == "speaker_map"
    assert not d.messages[0].role_needs_review


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


def test_kazakh_call_events():
    from cqa.facts import classify_event
    cases = {
        "Жазу және транскрипция басталды.": "recording_started",
        "Агент клиентпен сөйлесуге қосылды.": "operator_joined",
        "Клиент ұстауда.": "hold_start",
        "Клиент енді ұстауда емес.": "hold_end",
        "Агентпен кеңес басталды.": "consultation",
        "Клиент сөйлесуді аяқтады.": "client_left",
    }
    for text, kind in cases.items():
        assert classify_event(text) == kind, text


def test_mixed_language_tags_only_for_mixed_dialogs():
    from cqa.facts import detect_lang, extract_facts, facts_to_prompt, transcript_to_prompt
    from cqa.transcript import normalize
    assert detect_lang("Сәлеметсіз бе, мен Саматпын.") == "KZ"
    assert detect_lang("Жарайды, растаймын.") == "KZ"
    assert detect_lang("Hi, I'm Leo from support") == "EN"
    assert detect_lang("Здравствуйте, меня зовут Алина") == "RU"
    assert detect_lang("да") is None
    mono = normalize(CHAT)
    f = extract_facts(mono)
    assert f.line_languages == {} and "[RU]" not in transcript_to_prompt(mono, f)
    mixed = {**CHAT, "transcript": [
        {"timestamp": "00:00", "speaker": "client", "text": "Сәлеметсіз бе, шотқа ақша түспеді"},
        {"timestamp": "00:30", "speaker": "operator", "text": "Здравствуйте, меня зовут Алина. Сейчас проверю"},
    ]}
    d = normalize(mixed)
    f = extract_facts(d)
    assert f.line_languages == {"m1": "KZ", "m2": "RU"}
    assert "CLIENT [KZ]" in transcript_to_prompt(d, f) and "OPERATOR [RU]" in transcript_to_prompt(d, f)
    assert "первый ответ оператора: RU (m2)" in facts_to_prompt(f)


def test_empty_and_alias_speakers():
    d = normalize({"conversation_id": "x", "transcript": [
        {"speaker": "customer", "text": "Где тариф?"},
        {"speaker": "Agent", "text": "Здравствуйте"},
        {"speaker": "", "text": "Чат закрыт"},
    ]})
    assert [m.speaker for m in d.messages] == ["client", "operator", "system"]
    assert not any(m.role_needs_review for m in d.messages)
    assert d.messages[1].speaker_id == "Agent"
