import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from eval_hybrid_roles import grouped_prompt, parse_roles


def test_grouped_prompt_uses_all_lines_and_requires_complete_group_map():
    messages = [
        {"id": "m1", "speaker_id": "voice_a", "text": "Здравствуйте"},
        {"id": "m2", "speaker_id": "voice_b", "text": "Мне нужна помощь"},
        {"id": "m3", "speaker_id": "voice_a", "text": "Чем могу помочь?"},
    ]
    ids, prompt = grouped_prompt(messages)
    assert ids == ["voice_a", "voice_b"]
    assert prompt.index("m1:") < prompt.index("m3:") < prompt.index("m2:")
    assert parse_roles('{"groups":[{"group":2,"role":"client"},{"group":1,"role":"operator"}]}', ids) == {
        "voice_a": "operator", "voice_b": "client"}
    with pytest.raises(ValueError):
        parse_roles('{"groups":[{"group":1,"role":"operator"},{"group":1,"role":"client"}]}', ids)
