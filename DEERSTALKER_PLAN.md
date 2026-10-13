# Deerstalker — Local File Investigation Tool

A small local app: pick drives/folders, pick what you're looking for (or describe it yourself), and a vision-capable Ollama model reads through *every file* — text, images, PDFs, office docs, archives, unknown binaries, all of it — tags what matters, does a cheap first pass then a deeper second pass on the promising stuff, then hands you a folder of copies + a written case report.

This document is the single source of truth across sessions. Upload it (as a file, not pasted text) in every new conversation, along with `PROJECT_STATE.md`, and use the base prompt at the bottom to build one segment at a time.

---

## 0. Assumptions (redirect me if you want something else)

- **Language/backend:** Python 3.11+, FastAPI for the local HTTP API.
- **App shell:** `pywebview` for the native window, not Electron — no Node toolchain, no bundler, keeps every segment small and dependency-light.
- **Frontend:** plain HTML/CSS/JS, no framework, no build step. Served as static files by FastAPI. This matters for the token-budget goal: React/Vue segments need extra scaffolding and state-management boilerplate that plain JS doesn't.
- **Storage:** SQLite for scan results/jobs (one file, zero setup).
- **Ollama transport:** plain HTTP via `requests` to `/api/generate` (not `/api/chat` — picking one and documenting it so segments don't disagree).
- **Model:** one vision-capable model handles both text and images (e.g. `llama3.2-vision`, `qwen2.5vl`, `gemma3`, `llava` — whatever the user has pulled). Model name is an env var, never hardcoded.

## 1. How to use this plan (read this first)

Each numbered segment below is meant to be built in its **own, fresh Claude conversation**, so a free-tier session's thinking budget only ever has to hold one small, well-specified piece.

**Workflow per segment:**
1. Start a new chat.
2. Attach (as files, not pasted inline — saves your input tokens) `PLAN.md` (this doc) and `PROJECT_STATE.md`.
3. Attach any files listed in that segment's "Depends on" that already exist.
4. Send the base prompt below with the segment number filled in.
5. When Claude finishes, save the code files it produced, and save the updated `PROJECT_STATE.md` it gives you back — that's what you attach next time.
6. If a single segment still runs long / gets cut off, tell the next session to split it further (e.g. "13a" / "13b") — the interfaces are specific enough that sub-splitting is safe.

**Base prompt (copy, fill in `{N}`):**

```
I'm building Deerstalker — a local file-investigation tool — in independent
segments, following an existing plan. Attached:
- PLAN.md — full project plan/spec (read the section for Segment {N} carefully;
  it's the source of truth for interfaces/contracts)
- PROJECT_STATE.md — what already exists (files, signatures, deviations from plan)
- [any files listed as "Depends on" for Segment {N}, if they exist yet]

Please build ONLY Segment {N} from PLAN.md.
- Follow the interfaces/contracts in PLAN.md exactly so this connects cleanly
  with the other segments.
- If a dependency file isn't attached, assume it matches PLAN.md's documented
  interface — don't rebuild it, just call it as specified.
- Don't refactor or rewrite other segments.
- Create the files in /mnt/user-data/outputs at the paths PLAN.md specifies.
- When done, give me a short updated PROJECT_STATE.md (new files, signatures,
  any deviations from PLAN.md) and flag anything a later segment now needs to
  know about.
```

---

## 2. Project file tree (target end state)

```
deerstalker/
  app/
    __init__.py
    config.py                 # Segment 1
    models.py                 # Segment 1
    db.py                     # Segment 1
    log.py                    # Segment 1
    ollama_client.py           # Segment 2
    scanner.py                  # Segment 3
    archives.py                  # Segment 4
    extractors/
      __init__.py
      text_docs.py                # Segment 5
      media_binary.py              # Segment 6
    intents.py                       # Segment 7
    prompts.py                        # Segment 7
    pipeline/
      __init__.py
      fast_pass.py                     # Segment 8
      detailed_pass.py                  # Segment 9
      orchestrator.py                    # Segment 10
    output.py                             # Segment 11
    api.py                                 # Segment 12
  frontend/
    index.html                              # Segment 13
    progress.html                            # Segment 14
    results.html                              # Segment 15
    static/
      css/detective.css                       # Segment 13 (shared theme)
      js/setup.js                              # Segment 13
      js/progress.js                            # Segment 14
      js/results.js                              # Segment 15
  test_assets/                                    # Segment 17
  main.py                                          # Segment 16
  .env.example                                      # Segment 1 → finalized Seg 16
  requirements.txt                                   # grows every segment
  README.md                                           # Segment 16 → finalized Seg 17
  PLAN.md                                              # this file
  PROJECT_STATE.md                                      # running log, you keep this
```

---

## 3. Core data contracts

Every segment must respect these field names/types exactly — this is what lets segments built in isolation connect. Segment 1 implements these as real Pydantic models; everyone else just imports them.

```python
class FileRecord:
    id: str                      # stable hash of the absolute path
    path: str                    # real filesystem path (readable right now)
    display_path: str            # human path, shows archive lineage: "backup.zip/photos/img1.jpg"
    source_archive_id: str | None
    ext: str
    size_bytes: int
    mime_type: str | None
    mtime: float
    is_from_archive: bool
    status: Literal["pending","fast_done","detailed_done","error","skipped"]
    error_message: str | None

class ContentPayload:
    file_id: str
    kind: Literal["text","image","mixed","metadata_only"]
    text_excerpt: str | None
    images_b64: list[str]        # resized, no data-uri prefix
    metadata: dict                # exif / page count / duration / whatever's relevant
    truncated: bool

class SearchIntent:
    id: str
    label: str
    description: str
    icon: str                     # emoji or icon key, frontend uses this
    fast_prompt_template: str
    detailed_prompt_template: str

class AnalysisResult:
    id: str
    file_id: str
    job_id: str
    pass_type: Literal["fast","detailed"]
    intent_id: str | None          # None for "interesting" or free-text custom
    tags: list[str]
    relevance_score: float          # 0..1
    summary: str
    reasoning: str
    flagged_interesting: bool
    model_used: str
    created_at: float

class ScanJob:
    id: str
    roots: list[str]
    intent_ids: list[str]
    custom_query: str | None
    status: Literal["pending","scanning","fast_pass","detailed_pass","done","cancelled","error"]
    counts: dict                   # total, scanned, matched, errored
    output_folder: str
    report_path: str | None
    created_at: float
```

## 4. Env vars (`.env.example`)

```ini
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2-vision        # must be a vision-capable model you've pulled
OLLAMA_FAST_MODEL=                  # optional override for fast pass; blank = use OLLAMA_MODEL
OLLAMA_DETAILED_MODEL=              # optional override for detailed pass; blank = use OLLAMA_MODEL
OLLAMA_TIMEOUT_SECONDS=120
OLLAMA_MAX_CONCURRENCY=2

APP_DATA_DIR=./data                 # sqlite db, archive temp extraction, output folders
MAX_ARCHIVE_DEPTH=3
MAX_ARCHIVE_TOTAL_MB=2000           # zip-bomb guard
MAX_FILE_READ_MB=25                 # cap on how much of one file gets read for text extraction

APP_PORT=8731
```

## 5. Predefined search intents

The ~5–10 "well-crafted" presets, plus the two special modes:

| id | label | what fast-pass looks for |
|---|---|---|
| `sensitive_docs` | Sensitive & Personal Documents | IDs, passports, SSNs/tax IDs, medical records, insurance paperwork |
| `financial` | Financial Records | bank/credit statements, invoices, receipts, crypto wallet info, budgets |
| `credentials` | Credentials & Security Risks | plaintext passwords, API keys/tokens, secrets in config files, exported browser credentials |
| `people_photos` | Photos of People | images containing faces/people — portraits, group shots, ID photos |
| `legal` | Legal & Contracts | contracts, NDAs, agreements, court docs, legal correspondence |
| `work_docs` | Work & Professional | resumes/CVs, business plans, reports, presentations |
| `memories` | Media & Memories | photos/videos of specific events, screenshots, personal creative work |
| `cleanup` | Duplicates & Reclaimable Space | exact/near duplicates, cache/temp junk, large forgotten files |
| `forgotten` | Old & Forgotten | untouched for years, orphaned from deleted apps |
| `interesting` | **Interesting Things** (special) | model's own judgment — anything anomalous, out of place, contradictory, or surprising given everything else seen in this scan |
| *(free text)* | Custom | user-typed query, turned into its own prompt at runtime |

Segment 7 finalizes the actual prompt wording; this table is its starting brief.

---

## 6. Segment map

| # | Name | Depends on | Phase |
|---|---|---|---|
| 1 | Foundation & Contracts | — | A |
| 2 | Ollama Client | 1 | A |
| 3 | Drive/Folder Discovery & File Walker | 1 | A |
| 4 | Archive Handling | 1, 3 | A |
| 5 | Content Extraction — Text & Documents | 1 | A |
| 6 | Content Extraction — Media/Image/Binary | 1 | A |
| 7 | Search Intents & Prompt Library | 1 | A |
| 8 | Fast-Pass Engine | 1,2,5,6,7 | B |
| 9 | Detailed-Pass Engine | 1,2,5,6,7,8 | B |
| 10 | Pipeline Orchestrator | 1,3,4,8,9,11 | B |
| 11 | Output Stage (copy + report) | 1 | B |
| 12 | Backend API (FastAPI) | 1,3,10 | C |
| 13 | Frontend — Setup/Selection screen + shared theme | 12 | C |
| 14 | Frontend — Progress screen | 12,13 | C |
| 15 | Frontend — Results screen | 12,13 | C |
| 16 | App Shell (pywebview) & Packaging | 12,13,14,15 | D |
| 17 | Integration Test Pass & Docs | everything | D |

Build roughly in order within a phase; segments in the same phase with no direct dependency on each other (e.g. 5 & 6, or 13/14/15 after 13's theme exists) can be done in any order.

---

## 7. Segment specs

### Segment 1 — Foundation & Contracts
**Goal:** the shared vocabulary everything else relies on.
**Depends on:** none
**Files:** `app/config.py`, `app/models.py`, `app/db.py`, `app/log.py`, `app/__init__.py`, `requirements.txt` (start), `.env.example`
**Must implement:**
- `config.py`: loads env vars (python-dotenv), exposes a `Settings` object with every var from §4, creates `APP_DATA_DIR` if missing.
- `models.py`: the four dataclasses/Pydantic models from §3, exactly as named/typed, with serialization helpers.
- `db.py`: plain `sqlite3` (no ORM) with tables for files/results/jobs, plus: `init_db()`, `upsert_file()`, `get_file(id)`, `upsert_result()`, `get_results(job_id, intent_id=None)`, `create_job()`, `update_job()`, `get_job(id)`.
- `log.py`: basic console+file logger, `from app.log import logger`.
**Acceptance test:** `python -m app.db --selftest` — creates a temp DB, round-trips one of each model, prints OK.
**Watch out for:** don't add an ORM or extra dependencies here; every later segment imports these names verbatim, so get the field names right the first time.

### Segment 2 — Ollama Client
**Goal:** one module every analysis segment calls into.
**Depends on:** 1
**Files:** `app/ollama_client.py`
**Must implement:** calls to `{OLLAMA_HOST}/api/generate` via `requests`, `stream: false`, `format: "json"` when `json_mode=True`.
```python
class OllamaClient:
    def __init__(self, settings: Settings): ...
    def generate_text(self, system: str, prompt: str, json_mode: bool = True, model: str | None = None) -> dict | str: ...
    def generate_vision(self, system: str, prompt: str, images_b64: list[str], json_mode: bool = True, model: str | None = None) -> dict | str: ...
    def health_check(self) -> bool: ...
```
Robust JSON parsing (extract the `{...}` block if the model wraps it in prose; on total failure return `{"_parse_error": True, "raw": text}`). Retry w/ backoff on connection errors; raise `OllamaUnavailableError` if unreachable at all.
**Acceptance test:** `python -m app.ollama_client --ping`.
**Watch out for:** picking `/api/generate` over `/api/chat` is a deliberate, documented choice — don't let a later segment quietly assume the other shape.

### Segment 3 — Drive/Folder Discovery & File Walker
**Goal:** list scannable roots; stream `FileRecord`s for chosen roots.
**Depends on:** 1
**Files:** `app/scanner.py`
**Must implement:**
```python
def list_roots() -> list[dict]     # {"path":..., "label":..., "kind": "drive"|"removable"|"folder"}
def walk(root: str, settings: Settings) -> Iterator[FileRecord]
```
Cross-platform via `psutil.disk_partitions()` where possible, plus common mount points on macOS/Linux and a home-folder shortcut everywhere. `os.walk(followlinks=False)`, catch `PermissionError` per-directory (mark skipped, don't crash), mimetype via stdlib `mimetypes`.
**Acceptance test:** `python -m app.scanner --list-roots`, `--walk <path>`.
**Watch out for:** cloud-placeholder files (OneDrive "online-only" files) — best-effort only, note explicitly if skipped rather than silently mishandling.

### Segment 4 — Archive Handling
**Goal:** archives transparently expand into more `FileRecord`s, with guaranteed temp cleanup.
**Depends on:** 1, 3
**Files:** `app/archives.py`
**Must implement:**
```python
def is_archive(path: str) -> bool
@contextmanager
def extract_archive(record: FileRecord, settings: Settings) -> Iterator[list[FileRecord]]
```
Support `.zip/.tar/.tar.gz/.tgz/.tar.bz2` always; `.7z` via `py7zr` and `.rar` via `rarfile` only if installed — degrade gracefully (mark `metadata_only`, note "archive type unsupported here") rather than crashing. Extract to `tempfile.mkdtemp()` under `APP_DATA_DIR/tmp`, recurse into nested archives up to `MAX_ARCHIVE_DEPTH`, enforce `MAX_ARCHIVE_TOTAL_MB`. Always clean up (context manager, not "remember to call delete").
**Acceptance test:** build a test zip w/ 3 files + nested zip, extract, confirm temp dir gone after.
**Watch out for:** zip-slip — resolve extracted paths and confirm they stay under the temp dir before writing.

### Segment 5 — Content Extraction: Text & Documents
**Goal:** turn text-shaped files into `ContentPayload`.
**Depends on:** 1
**Files:** `app/extractors/text_docs.py`
**Must implement:** `def extract(record: FileRecord, settings: Settings) -> ContentPayload | None` (`None` = not mine, pass to Segment 6). Handles `.txt/.md/.log/.csv/.json/` and common code files (read+truncate at `MAX_FILE_READ_MB`), `.pdf` (text via `pypdf`/`pdfplumber`; if suspiciously little text vs page count, render first ~3 pages as images via `pymupdf` and return `kind="mixed"`), `.docx` (`python-docx`), `.xlsx` (`openpyxl`, sample first N rows/sheet), `.pptx` (`python-pptx`, slide text).
**Acceptance test:** run against a `test_assets/` sample of each type, print `kind` + excerpt length.
**Watch out for:** encoding fallbacks (utf-8 → latin-1/`errors="replace"`), cap rows/chars on huge CSV/JSON rather than loading it whole.

### Segment 6 — Content Extraction: Media, Images & Binary/Unknown
**Goal:** cover everything else — images (the vision-model core case), audio/video, and a catch-all so nothing ever crashes the pipeline.
**Depends on:** 1
**Files:** `app/extractors/media_binary.py`
**Must implement:** `def extract(record, settings) -> ContentPayload | None`, plus `def extract_any(record, settings) -> ContentPayload` (never `None` — tries text_docs, then this module, then a bare metadata fallback). Images via Pillow, downscale to a max long-edge (e.g. 1024px), base64 encode, `kind="image"`, EXIF pulled into metadata (flag GPS presence — relevant to privacy/interesting intents). Audio/video: metadata only via `mutagen`/ffprobe-if-available (no transcription in v1). Unknown/binary: magic-byte sniff (`python-magic` if available, else extension only), `kind="metadata_only"`, never read raw bytes as text.
**Acceptance test:** run against a photo, an mp3, a `.exe`, an extensionless file, an empty file — all return sensible payloads, none throw.
**Watch out for:** corrupt/truncated images throwing inside Pillow — catch, fall back to metadata_only.

### Segment 7 — Search Intents & Prompt Library
**Goal:** the actual investigative prompts.
**Depends on:** 1
**Files:** `app/intents.py`, `app/prompts.py`
**Must implement:** the `SearchIntent` registry from §5 (all fields filled in properly, not stubs). Prompt builders:
```python
INTENTS: dict[str, SearchIntent]
def build_fast_prompt(intent_id: str, payload: ContentPayload) -> tuple[str, str]      # (system, user)
def build_detailed_prompt(intent_id: str, payload: ContentPayload, fast_result: dict) -> tuple[str, str]
def build_custom_prompt(user_query: str, payload: ContentPayload, detailed: bool = False) -> tuple[str, str]
def build_interesting_prompt(payload: ContentPayload, detailed: bool = False) -> tuple[str, str]
```
Every prompt instructs strict JSON output matching the `AnalysisResult` shape, with the schema and one short example inlined (keeps small local models on-rails).
**Acceptance test:** print generated prompts for 2–3 intents against sample payloads, sanity-check length/clarity.
**Watch out for:** every extra sentence here costs tokens on *every single file scanned* — this is the highest-leverage place to be economical.

### Segment 8 — Fast-Pass Engine
**Goal:** cheaply triage every file.
**Depends on:** 1,2,5,6,7
**Files:** `app/pipeline/fast_pass.py`
```python
def run_fast_pass(job: ScanJob, files: list[FileRecord], settings: Settings,
                   on_progress: Callable[[FileRecord, AnalysisResult | None], None]) -> None
```
Per file: `extract_any()` → payload, per selected intent build fast prompt, call `generate_text`/`generate_vision` per `payload.kind`, parse into `AnalysisResult(pass_type="fast")`, `db.upsert_result()`, call `on_progress` after each file. `ThreadPoolExecutor` bounded by `OLLAMA_MAX_CONCURRENCY` (Ollama usually serializes on one accelerator anyway — keep this modest). One file's exception never kills the pass (catch, mark `status="error"`, continue). Accept an injectable `OllamaClient` so this is testable with a fake fixed-response client.
**Acceptance test:** run against a small mixed test folder with a mock client; try with a real client if Ollama's running.
**Watch out for:** dependency injection for the client is required for this segment's own test, not optional polish.

### Segment 9 — Detailed-Pass Engine
**Goal:** spend the expensive analysis only where it's earned.
**Depends on:** 1,2,5,6,7,8
**Files:** `app/pipeline/detailed_pass.py`
```python
def select_candidates(job_id: str, threshold: float = 0.5, top_k: int | None = None) -> list[FileRecord]
def run_detailed_pass(job: ScanJob, candidates: list[FileRecord], settings: Settings, on_progress) -> None
```
Re-extracts with higher fidelity where useful (fuller text excerpt, larger/more images) using `build_detailed_prompt()` with the matching fast result as context.
**Acceptance test:** seed db with fake fast-pass results at varying scores, confirm filtering; run detailed pass with a mock client.
**Watch out for:** this segment will likely want an optional `detail_level` param added to the Segment 5/6 extractor interface — call that out explicitly in your `PROJECT_STATE.md` update so it isn't silently reinvented later.

### Segment 10 — Pipeline Orchestrator
**Goal:** one thing the API calls to run a whole scan job.
**Depends on:** 1,3,4,8,9,11 (stub the output call if 11 doesn't exist yet)
**Files:** `app/pipeline/orchestrator.py`
```python
class Orchestrator:
    def start_job(self, roots: list[str], intent_ids: list[str], custom_query: str | None, settings: Settings) -> ScanJob
    def get_progress(self, job_id: str) -> dict
    def cancel(self, job_id: str) -> None
```
Runs in a background thread/task: create job → walk roots (expanding archives inline via Segment 4) → fast pass → detailed pass → output stage → update job status throughout. Cancellation checked between files, not mid-file.
**Acceptance test:** full small end-to-end run on a test folder with a mock client, job ends `status="done"` with a populated output folder + report.
**Watch out for:** this is the integration seam — if earlier segments drifted from their documented interfaces it surfaces here; fix by adapting the orchestrator's calls, and record any mismatch found.

### Segment 11 — Output Stage
**Goal:** matched files → a real folder + a report a human wants to read.
**Depends on:** 1
**Files:** `app/output.py`
```python
def finalize_output(job: ScanJob, settings: Settings) -> str   # returns report path
```
Copies (never moves) matched files into `APP_DATA_DIR/output/{job_id}/{intent_label}/`, de-duplicating name collisions. Writes `REPORT.md`: summary counts, per-intent breakdown, one entry per matched file (display_path, tags, short reasoning), a distinct **Interesting Things** section, plus a machine-readable `results.json` dump.
**Acceptance test:** seed a few fake results + dummy files, run, confirm folder + report content.
**Watch out for:** copying large video files could be slow — a size cap / "note instead of copy" fallback is fine as a documented option, don't assume unlimited disk.

### Segment 12 — Backend API (FastAPI)
**Goal:** the HTTP surface the frontend talks to.
**Depends on:** 1,3,10
**Files:** `app/api.py`
Routes: `GET /api/roots`, `GET /api/intents`, `POST /api/scan {roots, intent_ids, custom_query} -> {job_id}`, `GET /api/scan/{id}/progress`, `GET /api/scan/{id}/results`, `POST /api/scan/{id}/cancel`, `GET /api/scan/{id}/report`, `POST /api/open-folder {path}` (best-effort `os.startfile`/`open`/`xdg-open`). SSE stream endpoint is a nice-to-have, not required.
**Acceptance test:** `uvicorn app.api:app`, smoke-test every route (mock orchestrator underneath is fine if 10 isn't fully wired to real Ollama yet).
**Watch out for:** document every JSON request/response shape precisely in your output — three separate frontend segments get built against this contract in isolation from each other.

### Segment 13 — Frontend: Setup/Selection Screen + shared theme
**Goal:** first screen, and the shared visual language everything else reuses.
**Depends on:** 12 (can build against the documented JSON shapes even without a live server)
**Files:** `frontend/index.html`, `frontend/static/css/detective.css`, `frontend/static/js/setup.js`
Read the **frontend-design** skill before writing CSS. Aesthetic: Victorian-noir detective study — sepia/parchment tones, deep burgundy + brass accents, a serif/typewriter font pairing, subtle paper-grain texture, restrained magnifying-glass/wax-seal motifs. Understated, not costume-party. Shows: checkbox list/tree of roots (`GET /api/roots`), a card grid of intents (`GET /api/intents`, icon+label+description, toggle), a visually distinct **Interesting Things** card, a free-text "or describe what you're looking for" field, and a prominent "Begin the Investigation" button (`POST /api/scan` → navigate to progress.html with `job_id`).
**Deliverable also includes:** an explicit list of the shared CSS variables/component classes (colors, card, button, font stack) that Segments 14/15 must reuse.
**Acceptance test:** open with hardcoded/mocked roots+intents if no server is running, confirm layout.
**Watch out for:** no build step — plain HTML/CSS/JS only.

### Segment 14 — Frontend: Investigation Progress Screen
**Goal:** live progress, on-theme.
**Depends on:** 12, 13 (reuses `detective.css`)
**Files:** `frontend/progress.html`, `frontend/static/js/progress.js`
Polls `GET /api/scan/{id}/progress` (~1s). Shows overall progress bar, counts (scanned/matched/errored), current file being examined, a pure CSS/SVG themed animation (no external animation libs), a Cancel button, auto-navigates to results.html on `status="done"`.
**Acceptance test:** mock progress endpoint cycling through every status (`scanning/fast_pass/detailed_pass/done/error/cancelled`) — confirm all render.
**Watch out for:** handle `error`/`cancelled` states, not just the happy path.

### Segment 15 — Frontend: Case Results Screen
**Goal:** present findings, let the user act.
**Depends on:** 12, 13
**Files:** `frontend/results.html`, `frontend/static/js/results.js`
Fetches `/results` + `/report`. Summary header (counts, top tags), filterable/sortable card list (display_path, thumbnail if image, tag pills, short reasoning), a highlighted section for `flagged_interesting`, "Open Case Folder" button, a report viewer panel.
**Acceptance test:** mock results across 2–3 intents plus one interesting flag, confirm rendering + filtering.
**Watch out for:** most results won't have a thumbnail — design cards to look good text-only too.

### Segment 16 — App Shell (pywebview) & Packaging
**Goal:** one double-clickable entry point.
**Depends on:** 12,13,14,15
**Files:** `main.py`, finalize `.env.example`, `requirements.txt`, draft `README.md`
Starts FastAPI (uvicorn) in a background thread, waits for readiness (retry loop against `/api/roots`), opens a `pywebview` window at `http://localhost:{APP_PORT}`, themed window title, graceful shutdown on close. PyInstaller: mention as an optional stretch goal with a one-paragraph note on what it'd need (bundling `frontend/`, hidden imports) — don't fully build it now.
**Acceptance test:** `python main.py` opens the window with real (not mocked) data if the backend is wired up by this point.
**Watch out for:** avoid a hardcoded port; pick something uncommon and configurable.

### Segment 17 — Integration Test Pass & Documentation
**Goal:** confidence it actually works end to end, and a README a future-you can follow cold.
**Depends on:** everything
**Files:** `tests/test_end_to_end.py` (or a manual script if a full suite is overkill), finalized `README.md`
A scripted run against `test_assets/` (text file, image, pdf, docx, zip w/ nested files, an unknown extension, an empty file), asserting a report is produced and nothing throws unhandled. README covers: prerequisites (Python version, `ollama serve` running, `ollama pull llama3.2-vision` or similar), install, configure, run, and a short "how search intents work" explainer.
**Watch out for:** grep `PROJECT_STATE.md` for every "watch out for" flag earlier segments left open, and confirm each got addressed before calling it done.

---

## 8. Future extensions (not part of v1 — don't build yet)

- Perceptual-hash duplicate detection for images (exact-hash dedup is enough for v1).
- Audio/video transcription (Whisper) for the media extractor.
- Incremental re-scans (skip unchanged files via stored mtime+size+hash).
- Scheduled/watched-folder scanning.
- Semantic search across past scan results.
