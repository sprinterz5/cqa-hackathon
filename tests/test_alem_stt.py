from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main as app_main
from cqa import audio_pipeline
from cqa.service import Service
from cqa.store import Store


def test_alem_stt_routes_kazakh_and_russian_chunks_without_real_requests(monkeypatch, tmp_path):
    audio_pipeline._audio_imports()
    import faster_whisper.audio
    import faster_whisper.vad
    import httpx

    monkeypatch.setenv("STT_API_KEY", "general-test-key")
    monkeypatch.setenv("STT_KZ_API_KEY", "kazakh-test-key")
    monkeypatch.setenv("STT_EN_API_KEY", "")
    monkeypatch.setattr(faster_whisper.audio, "decode_audio", lambda _: [0.0] * 64000)
    monkeypatch.setattr(faster_whisper.vad, "get_speech_timestamps", lambda *args: [
        {"start": 0, "end": 32000}, {"start": 48000, "end": 64000}])
    choices = iter(["KZ", "RU"])
    monkeypatch.setattr(audio_pipeline, "_clip_language", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(audio_pipeline, "_language_whisper", lambda: object())
    monkeypatch.setattr(audio_pipeline, "_whisper", lambda: object())
    requests = []

    class FakeHTTP:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, headers, files, data):
            requests.append((url, headers["Authorization"], data["model"], data["language"]))
            assert files["file"][2] == "audio/wav"
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"text": "Сәлеметсіз бе" if len(requests) == 1 else "Здравствуйте"})

    monkeypatch.setattr(httpx, "Client", FakeHTTP)
    result = audio_pipeline.AlemSTT().transcribe(tmp_path / "call.mp3", "RU")
    assert [s["source_language"] for s in result] == ["KZ", "RU"]
    assert requests == [
        ("https://llm.alem.ai/v1/audio/transcriptions", "Bearer kazakh-test-key", "speech-to-text-kk", "kk"),
        ("https://llm.alem.ai/v1/audio/transcriptions", "Bearer general-test-key", "speech-to-text", "ru")]


def test_cloud_upload_needs_explicit_audio_permission(monkeypatch):
    monkeypatch.setattr(app_main, "stt_modes", lambda: {"cloud": {"available": True}})
    service = Service(Store(":memory:"), providers={})
    with TestClient(app_main.create_app(service)) as client:
        response = client.post("/api/dialogs/upload-audio?mode=cloud", content=b"ID3test-mp3")
        assert response.status_code == 403
