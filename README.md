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

## Backend

```bash
.venv/bin/uvicorn app.main:app --port 8000
```

- `cqa/store.py` — SQLite (`data/cqa.db`): checklist versions, dialogs, evaluations, per-criterion cache, appeals, decisions.
- `cqa/service.py` — dataset fetch by `group_id`, upload (API JSON or `Клиент: …` text), evaluation jobs with a per-criterion cache (replacing one criterion re-runs only that criterion), appeals and reviewer decisions (original vs. final score), audio link extraction, quality metrics.
- `app/main.py` — REST API under `/api`, static UI from `web/`.

## UI

Open http://localhost:8000 after starting the backend. Plain HTML/CSS/JS in `web/` (no build, no external dependencies) so it can be uploaded as a Dynamics 365 HTML web resource:

- `?api=https://host` — API base URL when the page is served elsewhere
- `?conversation_id=ID` — open one dialog card directly (e.g. from a CRM record)
- `?embed=1` — hide the navigation; `?role=reviewer` — start as reviewer

Screens: dialogs (fetch by `group_id`, upload, batch evaluation with progress), dialog card (transcript with ids, evidence links that scroll to the message and seek the call recording, per-criterion verdicts/points/confidence, sections, gross-violation flag), appeals (operator files, reviewer confirms or changes with a reason; original and final score are both kept), checklist editor (criteria, weights, scales, N/A rules, voice-call wording, versions, JSON), AI quality.

Import results of a CLI run into the app: `.venv/bin/python scripts/import_evals.py DEV`.
