# CQA hackathon — notes for Claude

- Record every hypothesis or assumption the team makes (about the checklist interpretation, the ground truth, the data, the model, infrastructure) in `notes/hypotheses.md`: add a row with an ID, a status (✅ / 🟡 / ❓ / ❌), the evidence and how to verify it. Update the status when something is checked; never delete refuted rows — mark them ❌. Open questions for the organizers go to the list at the end of that file. Log every check of a hypothesis as an entry in `notes/experiments.md` (what was checked, data, result, conclusion) and update the status in `hypotheses.md`.
- Communicate with the team in Russian.
- Secrets live in `.env` (never commit it, never print key values). Datasets live in `data/` (not committed: real calls contain personal data).
- The LLM only returns verdicts; all scoring arithmetic is in `cqa/scoring.py`. Nothing criterion-specific is hard-coded: prompts are built from the checklist config.
- Alem API key allows 2 parallel requests (`LLM_MAX_PARALLEL=2`).
- Run tests: `.venv/bin/python -m pytest -q`. Run the app: `.venv/bin/uvicorn app.main:app --port 8000`.
- Preserve ASR speaker IDs, segment start/end times, source language, original text, and Russian translation separately. Resolve roles from `speaker_map`, then the labeled reference transcript, then dialogue context. Flag context-based roles for review; train and validate this attribution on labeled calls before relying on it.
