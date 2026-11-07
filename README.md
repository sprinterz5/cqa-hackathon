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

## ASR segments

`cqa.transcript.normalize()` accepts DEV transcripts and ASR segments with `speaker`, `start`, `end` (seconds), `source_language`, `text`, and `translation_ru`. The original text and Russian translation are stored separately.

For ASR speaker IDs, roles are assigned in this order: explicit `speaker_map`, labeled `reference_transcript`, then dialogue context. Common labels (`agent`, `customer`, `ivr`, …) map directly; a line without a speaker is a system line. The original ID stays in `speaker_id`; the output role is `client`, `operator`, `bot`, or `system`. Context-based assignments have `role_source="context"` and `role_needs_review=true` (shown in the UI). This contextual baseline still needs training and validation on labeled calls.

## Results for the organizers

`results/dev/results.json` and `results/prod/results.json` follow the organizers' format (schema 1.0): one record per dialog, 8 criteria (`contact`, `discovery`, `identification`, `consultation`, `empathy`, `dialogue`, `closure`, `critical`), `evidence_turns` numbered from 1 over the original transcript, `final_score` from the deterministic scorer, `status: "error"` with a reason when a dialog could not be evaluated. Criterion codes come from `export_codes` in the checklist config.

Control stage (10 chats, 1 hour):

```bash
.venv/bin/python scripts/fetch_dataset.py <GROUP>            # or "Загрузить набор" in the UI
# evaluate all dialogs of the group in the UI ("Оценить все показанные") or via POST /api/evaluations
.venv/bin/python scripts/export_results.py <GROUP> --folder prod --team "<team name>"
git add results/prod/results.json && git commit -m "Control results" && git push
```

The script validates the file (all IDs present, no duplicates, 8 criteria with allowed values, `critical_flag` = `critical`) and exits with code 1 on problems. The same export is available in the UI ("Выгрузить results.json") and at `GET /api/export/results?group=<GROUP>`.
