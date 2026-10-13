# Deerstalker

A local file-investigation workspace: scan folders you choose, extract file content, ask a vision/language model about investigation leads, and collect matching evidence.

**Status:** Working according to the original author; live scans and model calls were not repeated for this publication.

## What is here

- `app/`: discovery, extraction, model clients, orchestration and API.
- `frontend/`: investigation interface.
- `tests/` and `test_assets/`: retained source checks and fixture guidance.
- `DEERSTALKER_PLAN*.md`: earlier design and cloud-worker expansion plans.

## Getting started

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.venv\Scripts\python main.py
```

Install Ollama and configure a compatible model as described in [the original setup guide](README.original.md). Local-only use needs no cloud credentials. For optional cloud workers, `api_keys.py` reads comma-separated `GOOGLE_API_KEYS` and `OLLAMA_API_KEYS` environment variables. Keep values in the untracked `.env` file or process environment.

## Current boundaries

Real investigation cases, copied matches, cached documents, logs and credentials were excluded. Start with a disposable test folder. Historical plans still describe the earlier key-file setup; the publication copy uses environment variables.

## Preservation and checks

This repository preserves a development snapshot, including incomplete work. See [PUBLICATION.md](PUBLICATION.md) for what was retained or excluded and [VALIDATION.md](VALIDATION.md) for the checks actually run. Original copyright and license notices take precedence over the repository’s display name.
