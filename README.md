# CQA — Conversation Quality Analytics

Freedom Broker CQA Hackathon prototype: configurable checklists, AI evaluation of operator chats and calls (RU / KZ / EN), evidence links, appeals and reviewer decisions.

## Setup

```bash
cp .env.example .env   # put the organizers' API URL (without group_id) into DATA_API_URL
python3 scripts/fetch_dataset.py DEV
```

Datasets are saved to `data/` and are not committed (they contain personal data).
