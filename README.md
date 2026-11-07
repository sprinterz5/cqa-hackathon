# CQA — Conversation Quality Analytics

Freedom Broker CQA Hackathon prototype: configurable checklists, AI evaluation of operator chats and calls (RU / KZ / EN), evidence links, appeals and reviewer decisions.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # put the organizers' API URL (without group_id) into DATA_API_URL
.venv/bin/python scripts/fetch_dataset.py DEV
.venv/bin/python -m pytest -q
```

Datasets are saved to `data/` and are not committed (they contain personal data).

## Structure

- `checklists/freedom_default.json` — the 8 criteria from the organizers' checklist; criteria 1, 4, 6, 7 have `voice call` wording overrides.
- `cqa/checklist.py` — checklist schema and validation (criteria types: `binary`, `scale`, `flag`; N/A rules; sections; channel overrides).
- `cqa/scoring.py` — deterministic scoring: weights, N/A redistribution, half-up rounding, gross-violation flag. The LLM only returns verdicts.
- `cqa/transcript.py` — normalization: message ids `m1..mN`, offsets in seconds (chat `MM:SS`, call ISO 8601), turns, plain-text upload parser.
- `cqa/facts.py` — deterministic facts for the evaluator: system events (RU/KZ/EN), operator's first response, end type, long waits (chats), holds and pauses (calls); lines spoken while the client is on hold are tagged `[HOLD]`.
- `cqa/llm.py` — OpenAI-compatible client (Alem Plus / DeepSeek / Ollama) with retries, concurrency limit and fallback.
- `cqa/prompts.py` — one prompt per criterion, assembled from the checklist config (channel wording, scale, N/A rule, facts, numbered transcript).
- `cqa/evaluator.py` — N samples per criterion, JSON validation with repair, voting (majority / median step), confidence, scoring.

```bash
.venv/bin/python scripts/evaluate.py DEV p1-ru-001 --samples 1
```
