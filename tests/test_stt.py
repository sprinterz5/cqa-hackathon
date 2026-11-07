import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from cqa.llm import LLMCall, LLMResponse, Provider
from cqa.service import Service
from cqa.store import Store
from cqa.stt import STTConfig, assign_roles, guess_roles, merge_segments, parse_transcription
from cqa.transcript import normalize

# shape of Alem's verbose_json: phrases with timestamps, no speakers, ids restart every 30 s chunk
ALEM = {"text": "…", "language": "ru", "duration": "14.5", "segments": [
    {"id": 0, "start": 0.0, "end": 3.1, "text": "Здравствуйте, Фридом Брокер, меня зовут Дана."},
    {"id": 1, "start": 3.1, "end": 4.6, "text": " Чем могу помочь?"},
    {"id": 2, "start": 4.6, "end": 9.0, "text": " Не могу вывести деньги со счёта."},
    {"id": 0, "start": 9.0, "end": 9.2, "text": "  "},
    {"id": 1, "start": 9.2, "end": 14.5, "text": " Сейчас проверю заявку."},
]}
ROLES = ["operator", "operator", "client", "operator"]


class RolesClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def chat(self, messages, temperature=0.0, max_tokens=2000):
        self.prompts.append(messages)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return LLMResponse(a, [LLMCall("fake", "fake", 0.1, 10, 10, 0.0, True)])


def test_parse_transcription_drops_empty_phrases_and_maps_language():
    asr = parse_transcription(ALEM)
    assert [s["text"] for s in asr["segments"]] == [
        "Здравствуйте, Фридом Брокер, меня зовут Дана.", "Чем могу помочь?",
        "Не могу вывести деньги со счёта.", "Сейчас проверю заявку."]
    assert asr["segments"][3]["start"] == 9.2 and asr["language"] == "RU" and asr["duration"] == 14.5
    # no phrases (e.g. the Kazakh model): the whole text becomes one phrase
    only_text = parse_transcription({"text": "сәлеметсіз бе", "language": "kk", "duration": 2, "segments": []})
    assert only_text["segments"] == [{"start": 0.0, "end": 2.0, "text": "сәлеметсіз бе"}]
    assert only_text["language"] == "KZ"


def test_merge_segments_joins_one_speaker_and_keeps_times():
    lines = merge_segments(parse_transcription(ALEM)["segments"], ROLES, "ai")
    assert [(l["speaker"], l["start"], l["end"]) for l in lines] == [
        ("operator", 0.0, 4.6), ("client", 4.6, 9.0), ("operator", 9.2, 14.5)]
    assert lines[0]["text"] == "Здравствуйте, Фридом Брокер, меня зовут Дана. Чем могу помочь?"
    assert lines[2]["timestamp"] == "00:09"
    d = normalize({"conversation_id": "c", "channel": "voice call", "transcript": lines})
    assert [m.t for m in d.messages] == [0.0, 4.6, 9.2]
    assert all(m.role_needs_review and m.role_source == "ai" for m in d.messages)


def test_whisper_loops_and_silence_are_dropped():
    payload = {"language": "ru", "duration": 30, "segments": [
        {"start": 0, "end": 2, "text": "Алло, слушаю.", "compression_ratio": 0.8},
        {"start": 2, "end": 9, "text": "Я не могу ничего сказать. Я не могу ничего сказать.", "compression_ratio": 3.1},
        {"start": 9, "end": 12, "text": "Спасибо за ожидание.", "compression_ratio": 0.9},
        {"start": 12, "end": 15, "text": "спасибо за ожидание", "compression_ratio": 0.9},
        {"start": 15, "end": 20, "text": "Продолжение следует", "no_speech_prob": 0.9, "avg_logprob": -1.4},
    ]}
    asr = parse_transcription(payload)
    assert [s["text"] for s in asr["segments"]] == ["Алло, слушаю.", "Спасибо за ожидание."] and asr["dropped"] == 3


def test_assign_roles_uses_llm_then_falls_back_to_phrase_cues():
    segs = parse_transcription(ALEM)["segments"]
    out, roles, source, err = asyncio.run(assign_roles(RolesClient([json.dumps({"roles": ROLES})]), segs))
    assert roles == ROLES and source == "ai" and err is None and out == segs
    # numbered answer with an extra item (the model miscounted): nothing shifts
    numbered = [{"n": i + 1, "role": r} for i, r in enumerate(ROLES)] + [{"n": 5, "role": "client"}]
    _, roles, source, _ = asyncio.run(assign_roles(RolesClient([json.dumps({"roles": numbered})]), segs))
    assert roles == ROLES and source == "ai"
    # wrong length twice -> cues: "меня зовут" / "проверю" are the operator, "не могу" the client
    bad = RolesClient([json.dumps({"roles": ["client"]})])
    _, roles, source, err = asyncio.run(assign_roles(bad, segs))
    assert source == "context" and "ожидалось 4" in err and len(bad.prompts) == 2
    assert roles == guess_roles(segs) == ROLES


def test_model_may_split_a_phrase_where_two_people_speak():
    segs = [{"start": 0.0, "end": 4.0, "text": "Да, верно. Хорошо, тогда смотрите заявку."},
            {"start": 4.0, "end": 6.0, "text": "Спасибо."}]
    answer = {"roles": [{"n": 1, "parts": [{"role": "client", "text": "Да, верно."},
                                           {"role": "operator", "text": "Хорошо, тогда смотрите заявку."}]},
                        {"n": 2, "role": "client"}]}
    out, roles, source, _ = asyncio.run(assign_roles(RolesClient([json.dumps(answer)]), segs))
    assert roles == ["client", "operator", "client"] and source == "ai"
    assert [s["text"] for s in out] == ["Да, верно.", "Хорошо, тогда смотрите заявку.", "Спасибо."]
    assert out[0]["start"] == 0.0 and 0 < out[0]["end"] < 4.0 and out[1]["end"] == 4.0 and out[1]["split"]
    # parts that do not reproduce the phrase word for word are ignored: the phrase keeps one role
    wrong = {"roles": [{"n": 1, "parts": [{"role": "client", "text": "Да."}, {"role": "operator", "text": "Смотрите."}]},
                       {"n": 2, "role": "client"}]}
    out, roles, _, _ = asyncio.run(assign_roles(RolesClient([json.dumps(wrong)]), segs))
    assert len(out) == 2 and roles[1] == "client"


@pytest.fixture()
def audio_env(tmp_path):
    seen = {}

    async def fake_transcribe(cfg, data, filename, language):
        seen.update(data=data, filename=filename, language=language)
        return parse_transcription(ALEM)

    roles = RolesClient([json.dumps({"roles": ROLES})])
    svc = Service(Store(":memory:"), providers={"primary": Provider("primary", "http://x", "k", "fake-model")},
                  default_samples=1, client_factory=lambda chain: roles,
                  stt=STTConfig("http://stt", "k", "speech-to-text"), transcriber=fake_transcribe,
                  audio_dir=tmp_path)
    with TestClient(create_app(svc)) as client:
        yield client, svc, seen


def wait(client, job_id):
    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_upload_audio_flow(audio_env, tmp_path):
    client, svc, seen = audio_env
    assert client.get("/api/health").json()["stt"]["model"] == "speech-to-text"
    r = client.post("/api/dialogs/upload-audio", params={"filename": "call 1.mp3", "conversation_id": "call-1", "approved_external_api": "true"},
                    content=b"ID3fake-mp3")
    assert r.status_code == 200, r.text
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "done" and job["lines"] == 3 and job["role_source"] == "ai"
    assert seen == {"data": b"ID3fake-mp3", "filename": "call 1.mp3", "language": None}

    d = client.get("/api/dialogs/call-1").json()
    assert d["group_id"] == "UPLOAD" and d["has_audio"] and d["dialog"]["channel"] == "voice call"
    assert d["dialog"]["language"] == "RU" and d["asr"]["language_source"] == "detected"
    assert [m["speaker"] for m in d["dialog"]["messages"]] == ["operator", "client", "operator"]
    src = client.get("/api/dialogs/call-1/audio").json()
    assert src == {"src": "dialogs/call-1/audio/file", "page": None}
    f = client.get("/api/dialogs/call-1/audio/file")
    assert f.content == b"ID3fake-mp3" and f.headers["content-type"] == "audio/mpeg"

    # the reviewer fixes a role: it is no longer marked for review
    r = client.post("/api/dialogs/call-1/speaker", json={"index": 1, "speaker": "operator"})
    m = r.json()["dialog"]["messages"][1]
    assert m["speaker"] == "operator" and m["role_source"] == "reviewer" and not m["role_needs_review"]
    assert r.json()["asr"]["roles_edited_at"]  # survives a reload: the card asks to re-evaluate
    assert client.post("/api/dialogs/call-1/speaker", json={"index": 9, "speaker": "client"}).status_code == 400


def test_upload_audio_rejects_bad_input(audio_env):
    client, svc, _ = audio_env
    r = client.post("/api/dialogs/upload-audio", params={"filename": "notes.txt", "approved_external_api": "true"}, content=b"x")
    assert r.status_code == 400 and "формат" in r.json()["detail"]
    r = client.post("/api/dialogs/upload-audio", params={"filename": "a.wav", "language": "DE", "approved_external_api": "true"}, content=b"x")
    assert r.status_code == 400 and "язык" in r.json()["detail"]
    svc.stt = None
    r = client.post("/api/dialogs/upload-audio", params={"filename": "a.wav", "approved_external_api": "true"}, content=b"x")
    assert r.status_code == 400 and "STT_API_KEY" in r.json()["detail"]
