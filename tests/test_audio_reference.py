import pytest

from cqa import audio_pipeline
from cqa.transcript import normalize


def test_available_reference_preserves_roles_and_skips_asr(monkeypatch, tmp_path):
    monkeypatch.setattr(audio_pipeline.LocalWhisperSTT, "transcribe",
                        lambda *args: (_ for _ in ()).throw(AssertionError("ASR called")))
    source = [
        {"timestamp": "2026-11-03T13:10:00+00:00", "speaker": "bot", "text": "Добро пожаловать"},
        {"timestamp": "2026-11-03T13:10:05+00:00", "speaker": "operator", "text": "Чем могу помочь?"},
        {"timestamp": "2026-11-03T13:10:09+00:00", "speaker": "client", "text": "Нужна помощь"},
    ]
    record = audio_pipeline.process_audio(tmp_path / "call.mp3", reference_transcript=source)
    assert record["transcript"] == source
    assert [(m.speaker, m.role_source, m.role_needs_review) for m in normalize(record).messages] == [
        ("bot", "transcript", False), ("operator", "transcript", False),
        ("client", "transcript", False)]


def test_reference_rejects_unlabeled_line(tmp_path):
    with pytest.raises(ValueError, match="роль каждой реплики"):
        audio_pipeline.process_audio(tmp_path / "call.mp3", reference_transcript=[
            {"speaker": "unknown", "text": "Здравствуйте"}])
