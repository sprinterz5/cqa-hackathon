import json
import time

from fastapi.testclient import TestClient

from app import main as app_main
from cqa import audio_pipeline
from cqa.llm import LLMCall, LLMResponse, Provider
from cqa.service import Service
from cqa.store import Store


class VoiceRoles:
    """Fake LLM: answers the role prompt and remembers it."""

    def __init__(self):
        self.prompts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def chat(self, messages, temperature=0.0, max_tokens=2000):
        self.prompts.append(messages)
        roles = [{"n": 1, "role": "operator"}, {"n": 2, "role": "client"}]
        return LLMResponse(json.dumps({"roles": roles}), [LLMCall("fake", "fake", 0.1, 10, 10, 0.0, True)])


def wait(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_local_mp3_upload_creates_playable_dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_pipeline, "modes", lambda: {
        "local": {"available": True}, "cloud": {"available": False, "reason": "pending"}})
    stages = []

    def fake_audio(audio, mode, language, progress):
        assert audio.read_bytes() == b"ID3test-mp3"
        assert mode == "local" and language == "RU"
        for stage in ("recognizing", "separating", "parsing"):
            progress(stage)
            stages.append(stage)
        return {"channel": "voice call", "language": "RU", "transcript": [
            {"speaker": "speaker_1", "start": 0.0, "end": 2.0, "source_language": "RU",
             "text": "Здравствуйте, меня зовут Дана."},
            {"speaker": "speaker_2", "start": 2.0, "end": 4.0, "source_language": "KZ",
             "text": "Сәлеметсіз бе", "translation_ru": "Здравствуйте"}]}

    monkeypatch.setattr(audio_pipeline, "process_audio", fake_audio)
    llm = VoiceRoles()
    service = Service(Store(":memory:"), providers={"primary": Provider("primary", "http://x", "k", "fake")},
                      client_factory=lambda chain: llm, audio_dir=tmp_path)
    with TestClient(app_main.create_app(service)) as client:
        response = client.post("/api/dialogs/upload-audio?filename=a.mp3&conversation_id=audio-1&mode=local&language=RU",
                               content=b"ID3test-mp3", headers={"Content-Type": "audio/mpeg"})
        assert response.status_code == 200, response.text
        job = wait(client, response.json()["job_id"])
        assert job["status"] == "done", job
        assert stages == ["recognizing", "separating", "parsing"]
        # the role prompt carries the voice group of each phrase
        assert "[голос 1]" in llm.prompts[0][1]["content"] and "[голос 2]" in llm.prompts[0][1]["content"]
        d = client.get("/api/dialogs/audio-1").json()
        messages = d["dialog"]["messages"]
        assert [m["speaker"] for m in messages] == ["operator", "client"]
        assert messages[0]["role_needs_review"] is True and messages[1]["translation_ru"] == "Здравствуйте"
        assert d["asr"]["mode"] == "local" and d["asr"]["voice_groups"] == 2
        assert client.get("/api/dialogs/audio-1/audio").json()["src"] == "dialogs/audio-1/audio/file"
        assert client.get("/api/dialogs/audio-1/audio/file").content == b"ID3test-mp3"
        # the same ID again is refused; cloud mode is not configured here
        again = client.post("/api/dialogs/upload-audio?filename=a.mp3&conversation_id=audio-1&mode=local", content=b"ID3")
        assert again.status_code == 400 and "уже есть" in again.json()["detail"]
        cloud = client.post("/api/dialogs/upload-audio?mode=cloud&approved_external_api=true", content=b"ID3test-mp3")
        assert cloud.status_code == 400 and "STT_API_KEY" in cloud.json()["detail"]


def test_local_mode_without_any_llm_falls_back_to_phrase_cues(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_pipeline, "modes", lambda: {"local": {"available": True}})
    monkeypatch.setattr(audio_pipeline, "process_audio", lambda audio, mode, language, progress: {"transcript": [
        {"speaker": "speaker_1", "start": 0.0, "end": 2.0, "text": "Меня зовут Дана, чем могу помочь?"}]})
    service = Service(Store(":memory:"), providers={}, audio_dir=tmp_path)
    with TestClient(app_main.create_app(service)) as client:
        r = client.post("/api/dialogs/upload-audio?filename=b.mp3&conversation_id=audio-2&mode=local", content=b"ID3x")
        assert wait(client, r.json()["job_id"])["status"] == "done"
        d = client.get("/api/dialogs/audio-2").json()
        assert d["asr"]["role_source"] == "context" and d["dialog"]["messages"][0]["speaker"] == "operator"
