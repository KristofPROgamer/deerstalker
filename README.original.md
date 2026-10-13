# Deerstalker

Deerstalker is a local file-investigation tool. It scans only folders you choose, asks a local Ollama vision model to identify files matching your investigation leads, and writes the resulting report and copied matches to a local case folder.

## Prerequisites

- Python 3.11 or newer
- [Ollama](https://ollama.com/) running locally
- A vision-capable Ollama model, for example `llama3.2-vision`

Start Ollama and pull a model before opening Deerstalker:

```powershell
ollama serve
ollama pull llama3.2-vision
```

## Install and run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

On Windows, `starter.bat` is the simplest option after cloning the project. It first requests Administrator approval, then creates `.venv` if needed, installs the declared dependencies, starts Ollama when it is not already running, verifies the already-pulled `gemma3:12b-it-qat` model, and opens Deerstalker with that model selected for both analysis passes.

Administrator access lets Deerstalker inspect protected folders and attached storage Windows mounts with a drive letter, including USB storage. An unlocked Android or iPhone connected through MTP also appears as an **MTP** search location. Deerstalker temporarily stages accessible phone files locally for that scan only, then removes the staging area after report and match copying finish. Keep the phone connected and unlocked until its case completes; scanning a whole phone can require corresponding temporary local disk space.

The desktop window opens at a local-only address. Use **Add a folder or drive** to open the operating-system picker, select exactly what Deerstalker may inspect, choose one or more investigation leads, and begin the case. Original files are never moved. Completed reports and copied matches are stored under `APP_DATA_DIR/output/`.

If port 8731 is busy, change `APP_PORT` in `.env` and start the app again. `starter.bat` deliberately overrides the model settings for its own process to use `gemma3:12b-it-qat`; use `python main.py` directly if you want the model values from `.env` instead.

## How investigation leads work

Each selected lead is a separate question asked of each discovered file. The fast pass reviews all reachable files; likely matches receive a detailed second pass. **Interesting Things** is a special lead for anomalies, contradictions, and items that merit a closer look. A custom description can be used alone or with the preset leads.

Deerstalker supports text documents, common Office formats, PDFs, images, archives, and metadata-only handling for unknown files. Missing optional readers never stop a scan; the file is recorded with the information that is available.

## Multi-provider worker pool (optional)

By default Deerstalker talks to one local Ollama server, one call at a
time (or `OLLAMA_MAX_CONCURRENCY` at a time). If you have Ollama Cloud
and/or Google Gemini API keys, Deerstalker can fan work out across all of
them at once instead — useful for large scans.

This is entirely optional and additive: leave it unconfigured and nothing
changes.

To enable it, create `api_keys.py` in the project root (next to
`main.py`) — a plain Python file with two lists:

```python
google_apikeys = ["key-1", "key-2", "..."]     # one Gemini worker slot per entry
ollama_apikeys = ["key-1", "key-2", "..."]     # one Ollama Cloud worker slot per entry
```

`api_keys.py` is gitignored and should never be committed — it holds
plaintext secrets. Both lists are optional and independent: a missing
file, or either list left out or empty, just means zero worker slots for
that provider (logged once at startup, not an error). Everything else
about Ollama Cloud and Gemini (host, model, timeouts, an optional hard
cap on total slots) is configured normally in `.env` — see
`.env.example`.

**The number of worker slots is never a fixed number anywhere in the
app.** It's always `len(ollama_cloud_api_keys) + len(google_api_keys)`,
computed fresh from `api_keys.py` at startup — whether that's 0 keys (the
default, plain-local-Ollama behavior), 17, or 60. Add or remove keys and
restart to change it; there's no other setting to keep in sync.

If a worker slot's key stops working mid-scan (revoked, rate-limited,
temporarily unreachable), Deerstalker logs it, retries the same request
on a different slot, and keeps going — one bad key never stops a scan
that has other working slots available.

**Known limitation:** an Ollama Cloud slot always uses the plain
`OLLAMA_MODEL`, not `OLLAMA_FAST_MODEL`/`OLLAMA_DETAILED_MODEL`, since a
pooled slot has no way to know which pass is calling it. Local Ollama and
Gemini slots are unaffected.



```powershell
python -m unittest tests.test_end_to_end
```

The test creates its own mixed fixture set in `test_assets/`, uses a deterministic in-process model stub rather than Ollama, runs the complete scan pipeline, and asserts that a report is written.

## Optional packaging stretch goal

PyInstaller can package this as a double-clickable app, but it is intentionally not part of the build yet. A packaging configuration would need to bundle the `frontend/` directory as data files and declare pywebview's platform-specific GUI imports as hidden imports, then be tested on each target operating system.
