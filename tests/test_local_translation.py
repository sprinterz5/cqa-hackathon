import sys
from contextlib import nullcontext
from types import SimpleNamespace

from cqa import translation


def test_local_translation_preserves_original_and_skips_russian(monkeypatch):
    class Tokenizer:
        def __call__(self, texts, **kwargs):
            return {"texts": texts}

        def get_lang_id(self, language):
            assert language == "ru"
            return 1

        def decode(self, ids, **kwargs):
            return ids

    class Model:
        def __init__(self, language):
            self.language = language

        def generate(self, texts, **kwargs):
            return [f"{self.language}: {text}" for text in texts]

    monkeypatch.setattr(translation, "available", lambda: True)
    monkeypatch.setattr(translation, "_deps", lambda: None)
    monkeypatch.setattr(translation, "_models", lambda language: (Tokenizer(), Model(language)))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext))
    segments = [{"text": "Сәлеметсіз бе", "source_language": "KZ"},
                {"text": "Здравствуйте", "source_language": "RU"},
                {"text": "Hello", "source_language": "EN"},
                {"text": "қазақ тілін таңдау үшін бірді басыңыз для выбора русского языка нажмите два",
                 "source_language": "KZ"}]
    originals = [s["text"] for s in segments]
    result = translation.translate_segments(segments)
    assert [s["text"] for s in result] == originals
    assert [s.get("translation_ru") for s in result] == [
        "KZ: Сәлеметсіз бе", None, "EN: Hello",
        "KZ: қазақ тілін таңдау үшін бірді басыңыз. Для выбора русского языка нажмите два"]
