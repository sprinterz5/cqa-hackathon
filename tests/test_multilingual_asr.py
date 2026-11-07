from types import SimpleNamespace

from cqa import audio_pipeline


def test_short_kazakh_segment_uses_large_language_check_and_kazakh_stt(monkeypatch, tmp_path):
    audio_pipeline._audio_imports()
    import faster_whisper.audio

    monkeypatch.setattr(faster_whisper.audio, "decode_audio", lambda _: [0.0] * 48000)
    calls = []

    class General:
        def transcribe(self, clip, **kwargs):
            calls.append(("general", kwargs["language"]))
            if len(clip) == 48000:
                return iter([
                    SimpleNamespace(start=0, end=1, text="Салимитсбе"),
                    SimpleNamespace(start=1, end=2, text="Здравствуйте"),
                    SimpleNamespace(start=2, end=3, text="Hello")]), SimpleNamespace(language="ru")
            return iter([SimpleNamespace(start=0, end=1, text="Hello")]), None

        def detect_language(self, **kwargs):
            calls.append(("large_detect", None))
            return "kk", 0.87, None

    class Tiny:
        answers = iter([("tr", 0.21, None), ("ru", 0.99, None), ("en", 0.99, None)])

        def detect_language(self, **kwargs):
            return next(self.answers)

    class Kazakh:
        def transcribe(self, clip, **kwargs):
            calls.append(("kazakh", kwargs["language"]))
            return iter([SimpleNamespace(start=0, end=1, text="Сәлеметсіз бе")]), None

    monkeypatch.setattr(audio_pipeline, "_whisper", General)
    monkeypatch.setattr(audio_pipeline, "_language_whisper", Tiny)
    monkeypatch.setattr(audio_pipeline, "_kazakh_whisper", Kazakh)
    result = audio_pipeline.LocalWhisperSTT().transcribe(tmp_path / "call.mp3", "RU")

    assert [s["source_language"] for s in result] == ["KZ", "RU", "EN"]
    assert result[0]["text"] == "Сәлеметсіз бе"
    assert calls == [("general", "ru"), ("large_detect", None),
                     ("kazakh", "kk"), ("general", "en")]
