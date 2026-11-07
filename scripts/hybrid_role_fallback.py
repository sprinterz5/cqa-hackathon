"""Leave-one-call-out text fallback for calls without an IVR or operator intro."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict


def features(text: str) -> Counter:
    tokens = re.findall(r"\w+", text.casefold())
    result = Counter("w:" + word for word in tokens if len(word) >= 3)
    line = " ".join(tokens)
    for size in (3, 4):
        result.update("c:" + line[i:i + size] for i in range(max(0, len(line) - size + 1)))
    return result


def vector(feats: Counter, idf: dict[str, float]) -> dict[str, float]:
    values = {key: math.log1p(count) * idf.get(key, 0.0) for key, count in feats.items()}
    norm = math.sqrt(sum(value * value for value in values.values())) or 1.0
    return {key: value / norm for key, value in values.items()}


def fit(train: list[tuple[str, str]]) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    docs = [(features(text), role) for text, role in train]
    df = Counter(key for feat, _ in docs for key in feat)
    idf = {key: math.log((len(docs) + 1) / (count + 1)) + 1 for key, count in df.items()}
    sums: dict[str, Counter] = defaultdict(Counter)
    sizes = Counter()
    for feat, role in docs:
        sums[role].update(vector(feat, idf))
        sizes[role] += 1
    centers = {}
    for role, counts in sums.items():
        norm = math.sqrt(sum(value * value for value in counts.values())) or 1.0
        centers[role] = {key: value / norm for key, value in counts.items()}
    return centers, idf


def predict_groups(messages: list[dict], train: list[tuple[str, str]]) -> dict[str, str]:
    centers, idf = fit(train)
    grouped: dict[str, list[str]] = defaultdict(list)
    for message in messages:
        grouped[message["speaker_id"]].append(message["text"])
    result = {}
    for speaker_id, texts in grouped.items():
        merged = Counter()
        for text in texts:
            merged.update(vector(features(text), idf))
        norm = math.sqrt(sum(value * value for value in merged.values())) or 1.0
        result[speaker_id] = max(
            centers, key=lambda role: sum(value * centers[role].get(key, 0.0)
                                          for key, value in merged.items()) / norm)
    return result
