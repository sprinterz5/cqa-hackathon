"""Offline Russian translations for non-Russian ASR segments."""
from __future__ import annotations

import sys
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KZ_FOLDER = ROOT / ".translation_models" / "kazrush-kk-ru"
M2M_FOLDER = ROOT / ".translation_models" / "m2m100_418M"


def available() -> bool:
    return (KZ_FOLDER / "model.safetensors").is_file() and (M2M_FOLDER / "pytorch_model.bin").is_file()


def _deps() -> None:
    folder = ROOT / ".translation_deps"
    if folder.exists() and str(folder) not in sys.path:
        sys.path.insert(0, str(folder))


@lru_cache(maxsize=1)
def _models(language: str):
    _deps()
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer, M2M100ForConditionalGeneration, M2M100Tokenizer

    torch.set_num_threads(6)
    if language == "KZ":
        tokenizer = AutoTokenizer.from_pretrained(KZ_FOLDER, local_files_only=True)
        model = AutoModelForSeq2SeqLM.from_pretrained(KZ_FOLDER, local_files_only=True)
    elif language == "EN":
        tokenizer = M2M100Tokenizer.from_pretrained(M2M_FOLDER, local_files_only=True)
        tokenizer.src_lang = "en"
        model = M2M100ForConditionalGeneration.from_pretrained(M2M_FOLDER, local_files_only=True)
    else:
        raise ValueError("Неподдерживаемый язык перевода")
    model.eval()
    return tokenizer, model


def translate_segments(segments: list[dict]) -> list[dict]:
    """Keep the original; add translation_ru only for KZ/EN speech."""
    if not any(s.get("source_language") in ("KZ", "EN") for s in segments):
        return segments
    if not available():
        raise RuntimeError("Локальные модели перевода не найдены")
    _deps()
    import torch

    for language in ("KZ", "EN"):
        indices = [i for i, s in enumerate(segments) if s.get("source_language") == language]
        if not indices:
            continue
        tokenizer, model = _models(language)
        for offset in range(0, len(indices), 8):
            batch = indices[offset:offset + 8]
            source, suffixes = [], []
            for i in batch:
                original = segments[i]["text"]
                boundary = re.search(r"\bдля выбора\b", original, re.IGNORECASE) if language == "KZ" else None
                source.append(original[:boundary.start()].strip() if boundary else original)
                suffixes.append(original[boundary.start():].strip() if boundary else "")
            inputs = tokenizer(source, return_tensors="pt",
                               padding=True, truncation=True, max_length=512)
            kwargs = {"num_beams": 4, "max_new_tokens": 128}
            if language == "EN":
                kwargs["forced_bos_token_id"] = tokenizer.get_lang_id("ru")
            with torch.inference_mode():
                output = model.generate(**inputs, **kwargs)
            for i, ids, suffix in zip(batch, output, suffixes):
                translated = tokenizer.decode(ids, skip_special_tokens=True).strip()
                if translated:
                    segments[i]["translation_ru"] = (translated.rstrip(". ") + ". " + suffix[0].upper()
                                                     + suffix[1:]) if suffix else translated
    return segments
