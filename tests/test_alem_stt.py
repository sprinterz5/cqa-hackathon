from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main as app_main
from cqa import audio_pipeline
from cqa.silence_audio import speech_regions
from cqa.service import Service
from cqa.store import Store


def test_alem_stt_routes_kazakh_and_russian_chunks_without_real_requests(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setenv("STT_API_KEY", "general-test-key")
    monkeypatch.setenv("STT_KZ_API_KEY", "kazakh-test-key")
    monkeypatch.setenv("STT_EN_API_KEY", "")
    monkeypatch.setattr(audio_pipeline.silence_audio, "decode_pcm16", lambda _: b"\0" * 128000)
    monkeypatch.setattr(audio_pipeline.silence_audio, "speech_regions", lambda _: [
        {"start": 0, "end": 32000}, {"start": 48000, "end": 64000}])
    requests = []

    class FakeHTTP:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, headers, files, data):
            requests.append((url, headers["Authorization"], data["model"], data.get("language")))
            assert files["file"][2] == "audio/wav"
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"text": "Здравствуйте"})

    monkeypatch.setattr(httpx, "Client", FakeHTTP)
    result = audio_pipeline.AlemSTT().transcribe(tmp_path / "call.mp3", "RU")
    assert [s["source_language"] for s in result] == ["RU", "RU"]
    assert [s["speaker_id"] for s in result] == ["chunk_1", "chunk_2"]
    assert requests == [
        ("https://llm.alem.ai/v1/audio/transcriptions", "Bearer general-test-key", "speech-to-text", "ru"),
        ("https://llm.alem.ai/v1/audio/transcriptions", "Bearer general-test-key", "speech-to-text", "ru")]

    requests.clear()
    result = audio_pipeline.AlemSTT().transcribe(tmp_path / "call.mp3", "KZ")
    assert [s["source_language"] for s in result] == ["KZ", "KZ"]
    assert all(row[1:] == ("Bearer kazakh-test-key", "speech-to-text-kk", "kk") for row in requests)


def test_cloud_upload_needs_explicit_audio_permission(monkeypatch):
    monkeypatch.setattr(app_main, "stt_modes", lambda: {"cloud": {"available": True}})
    service = Service(Store(":memory:"), providers={})
    with TestClient(app_main.create_app(service)) as client:
        response = client.post("/api/dialogs/upload-audio?mode=cloud", content=b"ID3test-mp3")
        assert response.status_code == 403


def test_cloud_processing_never_loads_local_models(monkeypatch, tmp_path):
    monkeypatch.setattr(audio_pipeline.AlemSTT, "transcribe", lambda *args: [
        {"start": 0, "end": 2, "text": "Здравствуйте, чем могу помочь?",
         "source_language": "RU", "speaker_id": "chunk_1"}])
    monkeypatch.setattr(audio_pipeline, "_speaker_ids", lambda *args: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(audio_pipeline, "translate_segments", lambda *args: (_ for _ in ()).throw(AssertionError()))
    record = audio_pipeline.process_audio(tmp_path / "call.mp3", mode="cloud", language="RU")
    assert record["transcript"][0]["speaker"] == "chunk_1"


def test_cloud_mode_available_without_local_models(monkeypatch):
    monkeypatch.setenv("STT_API_KEY", "test-key")
    monkeypatch.setattr(audio_pipeline, "translation_available", lambda: False)
    monkeypatch.setattr(audio_pipeline.silence_audio, "available", lambda: True)
    assert audio_pipeline.modes()["cloud"]["available"] is True
    assert audio_pipeline.modes()["local"]["available"] is False


def test_silence_splitter_caps_chunks_and_skips_silence():
    from array import array

    pcm = array("h", [0] * 16000 + [3000] * (16 * 16000) + [0] * 16000).tobytes()
    regions = speech_regions(pcm)
    assert len(regions) == 2
    assert all(0 < row["end"] - row["start"] <= 9 * 16000 for row in regions)
    assert speech_regions(b"\0" * 32000) == []
