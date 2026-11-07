import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import eval_local_voice_llm as probe


def test_local_cache_keeps_voice_and_translation(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "AUDIO", tmp_path)
    calls = []

    def fake_process(audio, mode, progress=None):
        calls.append((audio.name, mode))
        return {"transcript": [{"start": 1.0, "end": 2.0, "text": "Сәлем",
                                "source_language": "KZ", "translation_ru": "Привет",
                                "speaker": "speaker_2"}]}

    monkeypatch.setattr(probe, "process_audio", fake_process)
    first = probe.local_segments("p4-ru-001")
    assert first == [{"start": 1.0, "end": 2.0, "text": "Сәлем", "source_language": "KZ",
                      "translation_ru": "Привет", "voice": "speaker_2"}]
    assert probe.local_segments("p4-ru-001") == first
    assert calls == [("p4-ru-001.mp3", "local")]


def test_roles_only_local_uses_same_stt_and_speaker_steps(monkeypatch, tmp_path):
    from cqa import audio_pipeline

    monkeypatch.setattr(probe, "AUDIO", tmp_path)
    monkeypatch.setattr(audio_pipeline.LocalWhisperSTT, "transcribe",
                        lambda *args: [{"start": 1.0, "end": 2.0,
                                        "text": "Здравствуйте", "source_language": "RU"}])
    monkeypatch.setattr(audio_pipeline, "_speaker_ids", lambda *args: ["speaker_7"])
    segments = probe.local_segments("p4-ru-001", roles_only=True)
    assert segments[0]["voice"] == "speaker_7"
    assert segments[0]["text"] == "Здравствуйте"
    assert (tmp_path / "p4-ru-001.current_stt.json").is_file()
