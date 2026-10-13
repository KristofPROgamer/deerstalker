# Deerstalker v1.5 — Multi-Provider Worker Pool (Ollama Cloud + Gemini)

An addendum to `DEERSTALKER_PLAN.md` (v1, Segments 1–17, already built and
integrated). v1.5 adds a second and third model backend — Ollama Cloud and
Google Gemini — and runs several of them **at once** against a shared queue
of files, instead of one blocking call to one local Ollama model at a time.

This supersedes an earlier same-session attempt at this feature that ran out
of message budget mid-implementation before anything was saved to disk. The
files you have on disk right now are the clean, unmodified v1 originals —
this plan starts from that same clean baseline, deliberately split into
small segments this time so no single session has to hold the whole thing.

Use this doc exactly like `DEERSTALKER_PLAN.md`: attach it (as a file) plus
`PROJECT_STATE.md` to a fresh chat, one segment at a time.

---

## 0. What v1.5 actually is

- Today: one `OllamaClient`, one local model, `OLLAMA_MAX_CONCURRENCY`
  workers *in theory* — see §1 below for why that number currently does
  nothing.
- After v1.5: **1 worker slot per Ollama Cloud API key** (you currently have
  10) **+ 1 slot per Google Gemini API key** (you have 7) — both lists live
  in one file, `api_keys.py`, at the project root (`ollama_apikeys` and
  `google_apikeys`) — 17 slots today, pulling from one shared queue of
  files, each producing the exact same `AnalysisResult` shape the rest of
  the app already knows how to store, dedupe, and report on. **That 17 is
  never written down anywhere** — no segment in this plan hardcodes a
  worker count for either provider, not as a default, not as a fallback,
  not in a comment claiming "up to N." Every slot count, on both sides, is
  `len()` of whichever list `api_keys.py` actually provided, read fresh at
  `Settings.load()` time — add an 11th Ollama key or drop to 3 Gemini keys
  and the pool just resizes next run, no code touched.
- **Fully optional and additive.** Nobody who never creates an `api_keys.py`
  sees any behavior change at all — every new Settings field defaults to
  "off."
- **`models.py` is unchanged, on purpose** — see that file's own docstring
  ("field names and types must not drift"). Provider provenance rides along
  inside the existing `model_used: str` field instead of adding a new one.

## 1. Read this before touching anything — what's actually true today

Two things about the *real* code (not PROJECT_STATE.md's summary of it)
that every later segment in this plan depends on:

- **The orchestrator currently processes exactly one file at a time, always.**
  `Orchestrator._run_job()`'s main loop and `_analyze_batch()`'s
  portable-device loop both call `run_fast_pass(job, [record], ...)` /
  `run_detailed_pass(job, [record], ...)` inside a `for record in records:`
  loop — a single-element list, every call. `run_fast_pass`/
  `run_detailed_pass` *do* already build a working
  `ThreadPoolExecutor(max_workers=settings.ollama_max_concurrency)`
  internally — but since each invocation only ever hands it one task,
  that pool never has more than one thing to do. **`OLLAMA_MAX_CONCURRENCY`
  has no real effect on file-level parallelism today, whatever it's set
  to.** This is why Segment 22 (orchestrator batching) is load-bearing,
  not polish — without it, an 8-slot provider pool would still only ever
  have one file in flight.
- **`run_fast_pass`/`run_detailed_pass` already accept an injectable
  `client:` parameter**, added for their own tests. This is the exact seam
  v1.5 reuses: anything duck-typed like `OllamaClient` — i.e. it has
  `generate_text()`, `generate_vision()`, `health_check()` with matching
  signatures — can be passed in as `client=` with **zero changes** to
  either pass engine's own logic, except one intentionally tiny patch
  (Segment 23).
- **`OllamaUnavailableError` currently means "stop the whole pass."** Both
  engines set a shared `threading.Event` when it's raised, and every other
  file — running or not-yet-started — gets skipped for the rest of that
  call. That's correct for "the local Ollama server just died." It would
  be badly wrong for "one of several Gemini keys hit today's quota." **The
  worker pool must never let one slot's problem escalate into this
  exception** — see Segment 21's cooldown-and-retry design.

## 2. Architecture, in one paragraph

A new `MultiProviderClient` (Segment 21) wraps one Ollama-Cloud-flavored
`OllamaClient` slot per entry in `settings.ollama_cloud_api_keys` (currently
10) + one `GeminiClient` slot per entry in `settings.google_api_keys`
(currently 7, Segment 20) behind the exact same three-method shape
`OllamaClient` already exposes. Both counts are plain `len(...)` reads at
construction time, never a literal in code — the pool doesn't know or care
that today's numbers happen to be 10 and 7. The orchestrator (Segment 22)
builds one `MultiProviderClient` per job, sizes
`ollama_max_concurrency` to its slot count, and — critically — starts
passing real batches of files into `run_fast_pass`/`run_detailed_pass`
instead of one file per call, so their existing internal thread pool
finally has something to parallelize. Inside the pool, a rate-limited or
unreachable slot is quietly retried on a different slot for that same call
(logged, not fatal); only a from-scratch construction with zero usable
slots is treated as a real outage. Neither pass engine's own file-loop,
error-handling, or "one bad file never kills the job" logic needs to
change — the pool just looks like a (better) `OllamaClient` to them.
**All configured slots are meant to run simultaneously, not round-robin:**
a chunk of files is sized to exactly `slot_count`, the pass engine's
`ThreadPoolExecutor` gets exactly `slot_count` workers, and each thread
borrows a distinct slot from the pool's `queue.Queue` — so a full chunk
means all 10 Ollama Cloud keys and all 7 Gemini keys are making calls at
the same instant, not queued behind one another. The only exceptions are
mechanical, not design gaps: a trailing chunk with fewer files than slots
simply uses fewer slots, and a single file's own multiple intents still
run sequentially within that one file's call (see §8).

## 3. New/changed env vars

```ini
# Multi-provider worker pool (v1.5) -- entirely optional, additive.

# Both providers' worker-slot API keys come from api_keys.py at the
# project root -- NOT .env, since these are lists of secrets and a
# comma-separated string in .env is an easy way for a key to end up in a
# process listing or shell history. api_keys.py is expected to be
# .gitignore'd, as a plain python file with two lists:
#   google_apikeys = ["key-1", "key-2", ...]   # one Gemini slot per entry
#   ollama_apikeys = ["key-1", "key-2", ...]   # one Ollama Cloud slot per entry
# Missing/absent file, or either list missing/empty, = zero slots for that
# provider -- no crash, no warning beyond one INFO log line.

# Ollama Cloud: hosted Ollama, same request/response shape as local Ollama,
# just a different host + a Bearer API key per entry in ollama_apikeys.
OLLAMA_CLOUD_HOST=https://ollama.com

# Google Gemini: model/timeout are the only Gemini settings that stay in
# .env -- the keys themselves live in api_keys.py above.
GEMINI_MODEL=gemini-flash-latest
GEMINI_TIMEOUT_SECONDS=120

# Optional hard cap on total slots (every Ollama Cloud key + every Gemini
# key combined). Blank = uncapped -- use every configured slot. This is the
# ONLY numeric worker-count setting anywhere in v1.5 -- everything else is
# derived from how many keys are actually configured, never a fixed number.
WORKER_POOL_MAX_SLOTS=
```

New `Settings` fields (all with safe defaults — see Segment 18):

```python
ollama_cloud_host: str                  # default "https://ollama.com"
ollama_cloud_api_keys: list[str]        # loaded from api_keys.py's ollama_apikeys — default []
gemini_model: str                       # default "gemini-flash-latest"
gemini_timeout_seconds: int             # default 120
google_api_keys: list[str]              # loaded from api_keys.py's google_apikeys — default []
worker_pool_max_slots: int | None       # default None (uncapped) — the only place a slot COUNT is ever capped by a literal; slot counts themselves are always len(ollama_cloud_api_keys) + len(google_api_keys)
```

## 4. New/changed core interfaces

```python
# app/provider_errors.py — new
class ProviderRateLimitedError(Exception):
    """One specific credential/slot returned HTTP 429 / hit a quota. The
    pool should retry this call on a different slot, not fail it outright
    -- and must not treat this as "everything is down."""

class ProviderUnavailableError(Exception):
    """One specific credential/slot couldn't be reached at all (connection
    error/timeout) after its own client's internal retries. Pool handling
    is identical to ProviderRateLimitedError -- kept as a separate class
    only so logs can distinguish "rate limited" from "network problem."""
```

```python
# The duck-typed "Provider" contract every backend client must satisfy --
# OllamaClient already matches this; GeminiClient and MultiProviderClient
# are built to match it too. This is what lets any of them drop into
# fast_pass.py / detailed_pass.py's existing `client:` parameter.
def generate_text(self, system: str, prompt: str, json_mode: bool = True,
                   model: str | None = None) -> dict | str: ...
def generate_vision(self, system: str, prompt: str, images_b64: list[str],
                     json_mode: bool = True, model: str | None = None) -> dict | str: ...
def health_check(self) -> bool: ...
```

```python
# app/pipeline/worker_pool.py — new
class MultiProviderClient:
    def __init__(self, settings: Settings) -> None: ...
    def generate_text(self, ...) -> dict | str: ...      # contract above
    def generate_vision(self, ...) -> dict | str: ...    # contract above
    def health_check(self) -> bool: ...
    @property
    def slot_count(self) -> int: ...

def build_client(settings: Settings) -> "MultiProviderClient | None":
    """None when both ollama_cloud_api_keys and google_api_keys are empty
    -- caller falls back to today's single-local-Ollama default."""
```

## 5. New file tree additions

```
deerstalker/
  app/
    config.py                 # edited — Segment 18
    provider_errors.py        # new  — Segment 18
    ollama_client.py          # edited — Segment 19
    gemini_client.py          # new  — Segment 20
    pipeline/
      worker_pool.py          # new  — Segment 21
      orchestrator.py         # edited — Segment 22
      fast_pass.py            # edited (1 line) — Segment 23
      detailed_pass.py        # edited (1 line) — Segment 23
  .env.example                 # edited — Segments 18 & 24
  requirements.txt              # confirmed unchanged — Segment 24
  README.md                      # edited — Segment 24
  PROJECT_STATE.md                # updated throughout
```

## 6. Segment map

| # | Name | Depends on | Files touched |
|---|---|---|---|
| 18 | Provider Foundations: Config & Shared Errors | 1 | `config.py`, `.env.example`, `provider_errors.py` |
| 19 | Ollama Cloud Support in OllamaClient | 2, 18 | `ollama_client.py` |
| 20 | Gemini REST Client | 18 (+ attach `ollama_client.py`, `media_binary.py`) | `gemini_client.py` |
| 21 | Multi-Provider Worker Pool | 18, 19, 20 | `pipeline/worker_pool.py` |
| 22 | Orchestrator: Real Batching + Pool Wiring | 10, 21 | `pipeline/orchestrator.py` |
| 23 | `model_used` Provenance Patch | 8, 9, 21 | `pipeline/fast_pass.py`, `pipeline/detailed_pass.py` |
| 24 | Docs, Env Finalization & End-to-End Verification | everything above | `.env.example`, `requirements.txt`, `README.md`, `PROJECT_STATE.md` |

Build in order — each segment genuinely needs the one(s) before it this time
(unlike v1's phase-parallel segments).

---

## 7. Segment specs

### Segment 18 — Provider Foundations: Config & Shared Errors
**Goal:** the shared groundwork every later v1.5 segment needs — new
Settings fields, the `api_keys.py` loader, and the two exception types
that let the pool tell "try another slot" apart from "everything is down."
**Depends on:** 1
**Files:** `app/config.py`, `.env.example`, `app/provider_errors.py`

- Add the Settings fields and env vars from §3. `_load_api_keys()`:
  `from api_keys import google_apikeys, ollama_apikeys`, guarded by
  `try/except (ImportError, ModuleNotFoundError)` → log one INFO line
  ("api_keys.py not found — Ollama Cloud and Gemini worker slots disabled")
  and treat both lists as `[]`. If the file exists but one of the two names
  is missing, that name alone falls back to `[]` (a per-name
  `getattr(module, name, [])`, not one `try` around both — a typo in one
  list must not take down the other). Validate each imported value is
  actually a `list`; coerce entries to `str`; drop blanks/dupes; preserve
  order. **No fixed number of keys is assumed for either provider
  anywhere** — 0, 1, 10, or 40 entries in either list must all just work.
  Confirm in this segment that nothing downstream (Segment 21's slot
  builder, any log line, any test fixture) writes a literal worker count
  into code — the only number that should ever appear is `len(...)`
  evaluated fresh each `Settings.load()`.
- `app/provider_errors.py`: the two exception classes from §4, each with
  the docstring explaining when to raise it and why it's distinct from
  `app.ollama_client.OllamaUnavailableError`.
- `Settings.load()` must not raise if `.env` has none of the new vars and
  `api_keys.py` doesn't exist — every new field needs a safe default.

**Acceptance test:** `Settings.load()` against today's unmodified
`.env.example` (no new vars set, no `api_keys.py` present) produces
`google_api_keys == []`, `ollama_cloud_api_keys == []` — identical
effective config to pre-v1.5. Also confirm an `api_keys.py` with
`ollama_apikeys` holding 10 entries and `google_apikeys` holding 7
produces `len(settings.ollama_cloud_api_keys) == 10` and
`len(settings.google_api_keys) == 7` — proving the loader's count follows
whatever's configured rather than any number baked into `config.py`.
**Watch out for:** never log a key value anywhere, not even truncated —
in this segment or any later one. Logs are the easiest place for a key to
leak.

### Segment 19 — Ollama Cloud Support in OllamaClient
**Goal:** let the existing, already-tested `OllamaClient` also speak to
`https://ollama.com`, with zero behavior change for every current caller.
**Depends on:** 2, 18
**Files:** `app/ollama_client.py`

- Add optional `host_override: str | None`, `api_key: str | None`,
  `label: str = "ollama-local"` constructor params. When `api_key` is set,
  send `Authorization: Bearer {api_key}` on every request (generate calls
  *and* `health_check`).
- Map an HTTP 429 response to `ProviderRateLimitedError` instead of letting
  `raise_for_status()` surface a generic `requests.HTTPError` — the one
  behavioral change, and it only fires on 429.
- Every existing call site (`OllamaClient(settings)` with no extra args —
  fast_pass.py, detailed_pass.py, main.py, tests) must keep working
  byte-for-byte the same; every new param defaults to today's behavior.

**Acceptance test:** existing `python -m app.ollama_client --ping` and the
fast_pass/detailed_pass self-tests still pass unmodified. If you have a
real Ollama Cloud key, confirm one live call round-trips with
`host_override="https://ollama.com"`.
**Watch out for:** don't touch `_post_with_retry`'s connection-error/
timeout retry-then-`OllamaUnavailableError` path — that's still exactly
right for "this one slot can't be reached." Only the 429 case is new.

### Segment 20 — Gemini REST Client
**Goal:** a second backend client with the exact same call shape as
`OllamaClient`, so it's a drop-in for the same worker-pool slot.
**Depends on:** 18; attach `app/ollama_client.py` (interface to mirror) and
`app/extractors/media_binary.py` (to confirm what image encoding
`ContentPayload.images_b64` actually contains).
**Files:** `app/gemini_client.py`

- Constructor: `(api_key: str, model: str, timeout_seconds: int,
  label: str)`. Public methods match §4's contract exactly: `generate_text`,
  `generate_vision`, `health_check`.
- Talks to Google's `generateContent` REST endpoint for `{model}` (default
  `settings.gemini_model`, i.e. `gemini-flash-latest`). **Verify the
  current exact request/response JSON field names against ai.google.dev's
  docs when you build this** — this plan describes the shape (system
  instruction, a `contents` list with text + inline-base64-image parts, a
  JSON-response-mode flag), not a guarantee it hasn't shifted since this
  was written. Send the API key as a request **header**, not a `?key=`
  query param, so it doesn't end up in request logs.
- Reuse `ollama_client.py`'s robust JSON-parsing approach
  (`_parse_json_response`/`_extract_first_json_object`) — **duplicate**
  those ~40 lines here rather than importing the other module's private
  functions, the same convention `detailed_pass.py` already established
  relative to `fast_pass.py` (see that file's own docstring for the
  rationale).
- Map Google's 429/quota-exceeded responses to `ProviderRateLimitedError`;
  connection-level failures (after a small retry, same pattern as
  `OllamaClient`) to `ProviderUnavailableError`.

**Acceptance test:** a fake-transport unit test proving the request shape
and response parsing round-trip; a live smoke test with one real key
confirms one `generate_text` and one `generate_vision` call each parse
into the expected 5-key dict (or a clean `_parse_error` fallback).
**Watch out for:** Google's error JSON envelope isn't shaped like Ollama's
— don't assume `response.json()["error"]["message"]` is always present;
degrade to the raw response text if it isn't.

### Segment 21 — Multi-Provider Worker Pool
**Goal:** turn "one client slot per Ollama Cloud key + one slot per Gemini
key" (10 + 7 = 17 today, but always whatever's actually configured) into
one object that's a drop-in for `run_fast_pass`/`run_detailed_pass`'s
existing `client:` parameter — no changes to either pass engine's
call-handling logic required.
**Depends on:** 18, 19, 20
**Files:** `app/pipeline/worker_pool.py`

- `MultiProviderClient(settings)` builds one slot per configured backend —
  one Ollama-Cloud-flavored `OllamaClient` slot per entry in
  `settings.ollama_cloud_api_keys` (currently 10, each its own client
  instance with that key's `api_key=`/`label=`), plus one `GeminiClient`
  slot per entry in `settings.google_api_keys` (currently 7) — capped at
  `settings.worker_pool_max_slots` if set. **Neither side's slot count is
  ever a literal in this file** — both are `len()` of whatever list
  `Settings` handed it; today's 10 + 7 = 17 total is an outcome of that
  math, not a number this code (or this plan) should ever repeat as a
  constant. `build_client(settings)` returns `None` (not an empty pool)
  when zero slots would result, so the orchestrator's existing default is
  untouched for anyone who hasn't configured any of this.
- A `queue.Queue` of available slot indices implements borrow/return.
  `generate_text`/`generate_vision` borrow a slot, call it, and on
  `ProviderRateLimitedError`/`ProviderUnavailableError` mark that slot's
  cooldown (e.g. 30–60s, or the response's `Retry-After` header if
  present) and retry the **same call** on a different, not-yet-tried slot
  — up to `slot_count` attempts — before finally raising a plain
  `RuntimeError`. **Never raise `OllamaUnavailableError`** from this
  per-call exhaustion path — that stays reserved for the pass engines'
  existing "the whole backend is down" short-circuit, which a transient
  multi-slot rate-limit spike is not.
- **`model` argument handling:** `settings.ollama_fast_model`/
  `ollama_detailed_model` are Ollama model names and mean nothing to a
  Gemini slot. `MultiProviderClient` accepts `model` for signature
  compatibility but does **not** forward it to whichever slot it borrows —
  each slot always uses its own preconfigured model (the Cloud slot's own
  fast/detailed Ollama model; each Gemini slot's `settings.gemini_model`).
- Tag the returned dict with a `_provider_used` key (the slot's `label`)
  before handing it back, for Segment 23 to pick up.
- Thread-safe: multiple worker threads (fast_pass.py's own
  `ThreadPoolExecutor`) call in concurrently — `queue.Queue` handles
  borrow/return safely; guard per-slot cooldown reads/writes with a lock.

**Acceptance test:** fake slots (one that always 429s, one that always
succeeds) prove a call transparently succeeds via the working slot, and
that a cooling-down slot isn't retried again within its cooldown window.
A fake all-slots-failing case proves the call raises plain `RuntimeError`,
never `OllamaUnavailableError`.
**Watch out for:** `slot_count` must reflect the number of slots actually
built (post-cap) — Segment 22 sizes `ollama_max_concurrency` off it
directly.

### Segment 22 — Orchestrator: Real Batching + Pool Wiring
**Goal:** make the concurrency Segments 19–21 built actually happen — today
the orchestrator calls both passes one file at a time (see §1), which caps
real parallelism at 1 regardless of pool size.
**Depends on:** 10, 21
**Files:** `app/pipeline/orchestrator.py`

- In both `_run_job`'s ordinary-root loop and `_analyze_batch`'s
  portable-batch loop: replace
  `for record in records: run_fast_pass(job, [record], ...)` with chunking
  `records` into groups of size `effective_concurrency` and calling
  `run_fast_pass(job, chunk, ...)` once per chunk (same for
  `run_detailed_pass`/candidates). Check cancellation between chunks, not
  between every single file — a deliberate, coarser-grained change from
  today; note it in your `PROJECT_STATE.md` update.
- Resolve `effective_client = self._client if self._client is not None
  else worker_pool.build_client(settings)`, and
  `effective_concurrency = effective_client.slot_count if
  isinstance(effective_client, MultiProviderClient) else
  settings.ollama_max_concurrency` (falls back to today's default when
  the pool isn't active). Build a `settings` copy
  (`dataclasses.replace(settings, ollama_max_concurrency=
  effective_concurrency)`) to pass into both pass engines, so their own
  internal `ThreadPoolExecutor` actually spins up one thread per slot.
- `current_file`/progress display: with several files in flight at once, a
  single filename string isn't meaningful — update `_set_current_file`
  call sites around the batch loop to show something like
  `"{n} files (parallel)"` instead of one path. If a frontend file reads
  this field directly, flag it in `PROJECT_STATE.md` rather than editing
  frontend files in this segment.

**Acceptance test:** existing orchestrator tests keep passing with a fake
single-slot client (batch size 1 == today's exact behavior). A new test
with a fake multi-slot client (e.g. 3 fake slots with an artificial delay
each) proves several files' calls overlap in wall-clock time rather than
serializing — specifically, with 3 fake slots and a chunk of 3 files each
sleeping 1s, total wall-clock time for that chunk should be ~1s, not ~3s.
That's the concrete proof that "N slots" means N simultaneous calls, not
N calls taking turns.
**Watch out for:** this is the integration seam for all of v1.5, same role
Segment 10 played for v1 — if 18–21 drifted from their documented
interfaces, it surfaces here first.

### Segment 23 — `model_used` Provenance Patch
**Goal:** make `AnalysisResult.model_used` say which backend/key actually
produced each result, instead of always repeating one fixed configured
model name.
**Depends on:** 8, 9, 21
**Files:** `app/pipeline/fast_pass.py`, `app/pipeline/detailed_pass.py`

- The **only** sanctioned change to these two files anywhere in v1.5: in
  each file's `_build_result()`, change `model_used=model_used` to
  `model_used=response.get("_provider_used") or model_used`. Nothing else
  in either file changes.

**Acceptance test:** both files' existing `--selftest` runs still pass
unmodified — their fake clients never set `_provider_used`, so
`model_used` falls back to exactly today's value.
**Watch out for:** resist doing anything else in this segment, even
something that looks like an obvious nearby cleanup — the whole point of
keeping this separate from Segment 22 is a one-line diff per file that's
trivial to review.

### Segment 24 — Docs, Env Finalization & End-to-End Verification
**Goal:** confirm it all actually works together, and leave the next
reader (human or AI) able to turn it on.
**Depends on:** everything above
**Files:** `.env.example` (finalize), `requirements.txt` (confirm — Gemini
is plain REST over the already-present `requests`, so this should need
zero new lines; say so explicitly rather than silently), `README.md` (a
short "Multi-provider worker pool" section: what `api_keys.py` must look
like (two lists, `google_apikeys` and `ollama_apikeys`, at the project
root, gitignored), and — explicitly — that worker-slot counts are never
fixed numbers anywhere in the app: they're always `len(ollama_cloud_api_keys) +
len(google_api_keys)`, computed at startup, whether that's 0, 17, or 60),
`PROJECT_STATE.md`.

- Run a real scan against a small test folder with the pool active (a
  couple of real or intentionally-broken Gemini keys + one or two
  intentionally-broken Ollama Cloud keys is enough): confirm results land
  in the db/report exactly like a plain run, confirm logs show multiple
  provider labels, confirm one bad key
  logs and gets skipped without ever surfacing `OllamaUnavailableError` or
  killing the job.

**Watch out for:** grep every earlier v1.5 segment's "Watch out for" (same
discipline Segment 17 used for v1) and confirm each one actually got
addressed before calling this done.

---

## 8. Explicitly out of scope for v1.5 (future extensions)

- Per-(file, target) work-item granularity — today's design still
  parallelizes per *file*; a file with several selected intents still runs
  them sequentially within itself. Full slot utilization on small scans
  with many intents is a future refinement.
- Retrying a fully-exhausted file later in the *same* pass run — today, a
  call that exhausts every slot still just marks that one target failed
  for that file, per the existing per-target catch; it doesn't get
  requeued once cooldowns clear later in the same run.
- Any provider beyond Ollama (local + Cloud) and Gemini.
- A UI for which key handled which file, live slot health, or automatic
  key rotation/testing.
- Hot-reloading `api_keys.py` mid-run — keys are read once, at job
  start.
