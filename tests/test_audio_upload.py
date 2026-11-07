import time

from fastapi.testclient import TestClient

from app import main as app_main
from cqa.service import Service
from cqa.store import Store


def test_local_mp3_upload_creates_playable_dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(app_main, "UPLOADS", tmp_path)
    monkeypatch.setattr(app_main, "stt_modes", lambda: {
        "local": {"available": True}, "cloud": {"available": False, "reason": "pending"}})
    stages = []

    def fake_audio(audio, mode, language, progress):
        assert audio.read_bytes() == b"ID3test-mp3"
        assert mode == "local" and language == "RU"
        for stage in ("recognizing", "separating", "parsing"):
            progress(stage)
            stages.append(stage)
        return {"channel": "voice call", "language": "RU", "transcript": [
            {"speaker": "speaker_1", "start": 0.0, "end": 2.0,
             "source_language": "RU", "text": "Здравствуйте, меня зовут Дана."}]}

    monkeypatch.setattr(app_main, "process_audio", fake_audio)
    service = Service(Store(":memory:"), providers={})
    with TestClient(app_main.create_app(service)) as client:
        response = client.post("/api/dialogs/upload-audio?conversation_id=audio-1&mode=local&language=RU",
                               content=b"ID3test-mp3", headers={"Content-Type": "audio/mpeg"})
        assert response.status_code == 200, response.text
        job_id = response.json()["job_id"]
        for _ in range(50):
            job = client.get(f"/api/audio-jobs/{job_id}").json()
            if job["status"] != "running":
                break
            time.sleep(0.02)
        assert job["status"] == "done", job
        assert stages == ["recognizing", "separating", "parsing"]
        dialog = client.get("/api/dialogs/audio-1").json()["dialog"]
        assert dialog["messages"][0]["speaker"] == "operator"
        assert dialog["messages"][0]["role_needs_review"] is True
        source = client.get("/api/dialogs/audio-1/audio").json()["src"]
        assert source.endswith("/api/dialogs/audio-1/audio-file")
        assert client.get("/api/dialogs/audio-1/audio-file").content == b"ID3test-mp3"
        assert client.post("/api/dialogs/upload-audio?mode=cloud", content=b"ID3test-mp3").status_code == 503
