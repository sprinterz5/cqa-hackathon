"""Leave-one-call-out phrase classifier probe for E17; no target-call labels in training."""
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "scripts"))
from cqa.transcript import normalize
from eval_asr_roles import compare

records = {d["conversation_id"]: d for d in json.loads((root / "data/dev_source.json").read_text(encoding="utf-8"))["data"]["records"] if d.get("channel") == "voice call"}


def features(text):
    tokens = re.findall(r"\w+", text.casefold())
    out = Counter("w:" + word for word in tokens if len(word) >= 3)
    line = " ".join(tokens)
    for n in (3, 4):
        out.update("c:" + line[i:i+n] for i in range(max(0, len(line)-n+1)))
    return out


def vector(feats, idf):
    values = {key: math.log1p(count) * idf.get(key, 0) for key, count in feats.items()}
    norm = math.sqrt(sum(value*value for value in values.values())) or 1
    return {key: value / norm for key, value in values.items()}


def dot(a, b):
    return sum(value*b.get(key, 0) for key, value in a.items())


def fit(train):
    docs = [(features(m.text), m.speaker) for d in train for m in normalize(d).messages if m.speaker != "system"]
    df = Counter(key for feat, _ in docs for key in feat)
    idf = {key: math.log((len(docs)+1)/(count+1))+1 for key,count in df.items()}
    sums = defaultdict(Counter)
    sizes = Counter()
    for feat, role in docs:
        sums[role].update(vector(feat, idf))
        sizes[role] += 1
    centers = {}
    for role, counts in sums.items():
        mean = {key: value/sizes[role] for key,value in counts.items()}
        norm = math.sqrt(sum(value*value for value in mean.values())) or 1
        centers[role] = {key: value/norm for key,value in mean.items()}
    return centers, idf


totals = Counter()
for cid, record in sorted(records.items()):
    centers, idf = fit([d for other,d in records.items() if other != cid])
    raw = json.loads((root / "data/DEV/audio" / f"{cid}.roles.json").read_text(encoding="utf-8"))
    from cqa.transcript import Dialog
    pred = Dialog.model_validate(raw)
    groups = defaultdict(list)
    for message in pred.messages:
        groups[message.speaker_id].append(message.text)
    mapping = {}
    all_scores = {}
    for sid,texts in groups.items():
        rows = [vector(features(t),idf) for t in texts]
        avg = Counter()
        for row in rows:
            avg.update(row)
        norm = math.sqrt(sum(value*value for value in avg.values())) or 1
        avg = {key: value/norm for key,value in avg.items()}
        scores = {role: round(dot(avg,center),3) for role,center in centers.items()}
        all_scores[sid] = scores
        mapping[sid] = max(scores,key=scores.get)
    for message in pred.messages:
        message.speaker = mapping[message.speaker_id]
    metrics = compare(normalize(record), pred)
    print(cid, mapping, all_scores, metrics["correct_gold_replies"], "/", metrics["aligned_gold_replies"], flush=True)
    totals.update({key: metrics[key] for key in ("gold_voice_replies","aligned_gold_replies","correct_gold_replies")})
print("TOTAL", dict(totals), flush=True)
