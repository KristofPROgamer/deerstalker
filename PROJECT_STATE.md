# Deerstalker - Project State

Last updated: after a post-v1.5 fix (retryable transient server errors, `app/ollama_client.py`/`app/gemini_client.py`) found from a real production run against real Ollama Cloud/Gemini credentials — v1.5 is complete

## Status

Segments 1-17 (v1, `DEERSTALKER_PLAN.md`) are implemented and integrated. The application is ready for local use after configuring a local, vision-capable Ollama model in `.env` and running `python main.py`. No plan-level defects or unresolved integration failures remain in v1.

v1.5 (`DEERSTALKER_PLAN_v1_5.md`, multi-provider worker pool) is **complete** — all 24 segments done, plus one additional post-v1.5 fix (see "Post-v1.5 fix" below) found from the first real production run against actual Ollama Cloud/Gemini credentials (16 slots: 9 Ollama Cloud + 7 Gemini). That run also confirmed Segment 24's own fix holds under real load — multiple fast-pass jobs completed with no job-wide abort and no `OllamaUnavailableError` anywhere in the logs. Segment 24 found and fixed one real defect in Segment 21's already-"DONE" `worker_pool.py` (see its own section below for the full writeup); every other v1.5 segment's own acceptance criteria still holds unchanged. Both items carried forward from earlier segments are now resolved:

  1. **Segment 21's model-forwarding conflict** (every Ollama Cloud slot call always uses `settings.ollama_model`, never `ollama_fast_model`/`ollama_detailed_model`, since a pooled slot has no way to know which pass is calling it): decision made this segment — **accepted as actual v1.5 behavior**, not a bug. A real fix would need `MultiProviderClient` to expose separate fast/detailed borrow paths or build two pools, which is a genuine design change out of scope for a docs/verification segment and not something this session should guess at. Documented plainly in `README.md`'s new "Multi-provider worker pool" section ("Known limitation") so it's visible to users, not just future sessions.
  2. **Segment 22's frontend flag** (`current_file` sometimes reads `"{n} files (parallel)"` instead of a single path): still genuinely open — no frontend files exist anywhere in this project yet for any segment to have touched, v1.5 included. Stays flagged for whichever future session first builds `frontend/`/`static/js/`.

Segment 23 itself closed clean — no new flags; see its section below.

## Post-v1.5 fix — Retryable Transient Server Errors (500/502/503/504) — DONE

**Not part of the original 24-segment plan** — genuine new work found
after the fact, from real production use, not a spec segment. Named
"post-v1.5" rather than "Segment 25" to keep that distinction honest in
the historical record.

**Context:** a real production run (outside any sandboxed session) — the
first time any v1.5 code actually saw real network traffic against
`ollama.com`/`generativelanguage.googleapis.com`. The pool built 16 real
slots (9 Ollama Cloud + 7 Gemini) and multiple fast-pass jobs completed
cleanly with no job-wide abort, confirming Segment 24's own fix holds
under real load. But the run's own logs also showed a steady trickle of
per-file warnings, e.g.:

```
target interesting failed for <file>: 500 Server Error: Internal Server
Error for url: https://ollama.com/api/generate
target interesting failed for <file>: 503 Server Error: Service
Unavailable for url: https://generativelanguage.googleapis.com/...
```

**Root cause:** not an implementation bug against an existing spec this
time — a real gap in the v1.5 plan itself. Segments 19/20's own specs are
explicit that only HTTP 429 gets mapped to `ProviderRateLimitedError`;
everything else, including 500/502/503/504, was deliberately left to
`response.raise_for_status()` to propagate as a raw, **unretried**
`requests.HTTPError` (`DEERSTALKER_PLAN_v1_5.md` §7, Segment 19: "the one
behavioral change, and it only fires on 429"). The pass engines' existing
broad per-file catch (PLAN.md Segment 8) meant no job ever crashed from
this — every file that hit a 500/503 was correctly logged and skipped,
not fatal — but it meant a transient server-side hiccup (which cloud LLM
APIs throw fairly often under real load) permanently cost that one
file/target result, even with other slots sitting idle at that exact
moment. Given the pool's entire reason to exist is absorbing exactly this
kind of failure, this was worth fixing rather than leaving as documented
behavior.

**The fix (`app/ollama_client.py`, `app/gemini_client.py`):** both
clients' `_post_with_retry` now retry
`_RETRYABLE_SERVER_ERROR_STATUS_CODES = frozenset({500, 502, 503, 504})`
with the same backoff shape already used for connection errors/timeouts,
then raise `app.provider_errors.ProviderUnavailableError` (never
`OllamaUnavailableError`, even for a plain local Ollama client — Segment
19's local-abort contract is untouched) once every attempt is exhausted.
`provider_errors.py`'s `ProviderUnavailableError` docstring updated to
say so. Every other HTTP error (400/401/403/404, and any 5xx outside that
four-code set) is completely untouched — still an immediate, unretried
`requests.HTTPError`, exactly as before; retrying those wouldn't help,
since they mean something about the request itself is wrong, not that the
server had a bad moment. `app/pipeline/worker_pool.py` needed **zero**
changes here — Segment 21 already caught `ProviderUnavailableError` and
retries on a different slot; once the clients started raising it for
exhausted 5xx too, the pool's existing machinery just worked.

**Verification completed** (mocked HTTP transport, same sandbox
limitation as every prior segment — see "Not yet verified" below):

- Before/after, both clients: all 3 attempts return the target status
  (500 for Ollama, 503 for Gemini) — **before:** fails immediately on the
  first response (1 call made, raw `HTTPError`); **after:** retries 3x
  with backoff, then raises `ProviderUnavailableError`.
- One bad response then a good one: **after** transparently recovers
  (2 calls, real result returned); **before** never gets the chance.
- HTTP 404 and HTTP 429: byte-for-byte identical before and after —
  confirms the new retryable-status check doesn't touch either path.
- Connection-error exhaustion (Ollama): still raises
  `OllamaUnavailableError` after 3 attempts, unchanged — confirms Segment
  19's own "don't touch the connection-error/timeout retry path"
  instruction still holds; this fix adds a *parallel* retry path for a
  different failure mode, not a change to that one.
- **Full-stack integration test**, combining this fix with Segment 24's
  already-patched `worker_pool.py`: a real `MultiProviderClient` with 2
  Ollama Cloud slots, one programmed to return HTTP 500 on every attempt
  — the broken slot is genuinely retried 3x internally (checked via
  call-count assertions on the mocked transport, not just a status
  check), then the pool falls back to the healthy slot and returns a
  real `{"summary": "ok", "_provider_used": "ollama-cloud-1"}` result.
  This is the exact scenario from the real run's logs, reproduced and
  fixed end-to-end.

**Not yet verified:** this fix itself has not yet been run against the
real `ollama.com`/`generativelanguage.googleapis.com` endpoints — same
sandbox limitation as every prior segment, HTTP transport is mocked, not
real. Confirming it actually eliminates the specific warnings seen in the
real run's log requires one more real run with real credentials. It also
doesn't cover every conceivable transient failure: a 500 that only
resolves on a 4th+ attempt (past `_MAX_ATTEMPTS=3`), or a transient
failure surfaced under a status code outside `{500, 502, 503, 504}`,
would still fail unretried. If a future real run shows a different
status code recurring, `_RETRYABLE_SERVER_ERROR_STATUS_CODES` is the one
place to extend it.

## v1.5 — Segment 24: Docs, Env Finalization & End-to-End Verification — DONE

**What changed:**

- `.env.example` — checked, not edited. Every env var `config.py` actually
  reads (`os.getenv("...")`) and every var `.env.example` declares were
  diffed against each other: exact match, both directions, zero drift.
  Already "finalized" from Segment 18 — nothing to do here.
- `requirements.txt` — checked, not edited, **as the plan explicitly asks
  to state rather than silently confirm**: `gemini_client.py` imports
  only `json`, `time`, `typing` (stdlib) and `requests` — already present
  as `requests>=2.31` since Segment 2. `worker_pool.py` imports only
  `queue`, `re`, `threading`, `time`, `dataclasses`, `typing` — all
  stdlib. Zero new lines needed anywhere in v1.5.
- `README.md` — added a new "Multi-provider worker pool" section
  (between "How investigation leads work" and "Verify the local
  integration"): what `api_keys.py` must contain (`google_apikeys`/
  `ollama_apikeys`, project root, gitignored), explicit confirmation that
  slot counts are never a fixed number anywhere in the app
  (`len(ollama_cloud_api_keys) + len(google_api_keys)`, always), a plain-
  language description of the retry-on-a-different-slot behavior, and the
  Segment 21 model-forwarding limitation stated as a known limitation
  rather than left undocumented.
- **`app/pipeline/worker_pool.py`** — one real bug fix, found by this
  segment's own required end-to-end run and described in detail below.
  Not in Segment 24's nominal file list, but its fix is a direct
  consequence of Segment 24's own literal acceptance test ("confirm one
  bad key logs and gets skipped without ever surfacing
  `OllamaUnavailableError` or killing the job") failing against the
  as-shipped Segment 21 code — this isn't next-segment work or an
  unrelated cleanup, it's finishing what Segment 24 was explicitly asked
  to verify. See "Bug found and fixed" below for the full reasoning on
  why this was treated as in-scope.
- `PROJECT_STATE.md` — this section, plus the "Status" header resolving
  both items carried forward from Segments 21/22 (see above).

**Bug found and fixed (`app/pipeline/worker_pool.py`, `_call_with_retry`):**
`OllamaUnavailableError` was missing from the tuple of exceptions
`_call_with_retry` catches — it only caught `ProviderRateLimitedError`/
`ProviderUnavailableError`. An Ollama-Cloud-flavored `OllamaClient` slot
(Segment 19) raises exactly `OllamaUnavailableError` — the same exception
type a plain local Ollama client raises for "the whole backend is
down" — once its own internal `_post_with_retry` backoff loop is
exhausted (connection refused, DNS failure, repeated timeout). With the
bug: that exception propagated straight out of `MultiProviderClient
.generate_text()`/`.generate_vision()`, past the pool entirely, into
`fast_pass.py`'s/`detailed_pass.py`'s own `except OllamaUnavailableError`
handler — the one that sets the shared `threading.Event` and aborts the
*entire* pass, logging "aborting remaining targets/files this run". One
revoked or temporarily-unreachable Ollama Cloud key looked exactly like
"Ollama itself is down" to both pass engines, even with other healthy
Ollama Cloud slots and every Gemini slot sitting unused. This directly
contradicts Segment 21's own module docstring ("this module must never
let a per-slot rate limit or a transient network blip escalate into that
exception") and its own literal acceptance test ("a fake all-slots-failing
case proves the call raises plain `RuntimeError`, never
`OllamaUnavailableError`") — so this was a real implementation defect
against Segment 21's own already-agreed spec, not a case of "the real
files win, flag the conflict" (spec and code agreed on the intended
behavior; the code just didn't deliver it).

**Why Segment 21's own acceptance test didn't catch this:** it used a
hand-rolled fake slot object that directly `raise`d `ProviderUnavailableError`
for its "connection-unavailable" scenario — accurately proving the pool's
*retry/cooldown machinery* works, but never actually exercising a real
`OllamaClient` instance's own exhaustion path, which is what produces
`OllamaUnavailableError` in the first place. A fake that raises the
*intended* exception type can't catch a bug in *which* exception types
are caught. This is exactly the gap Segment 24's plan anticipates by
requiring an end-to-end run "with the pool active" using real
credentials/clients rather than more unit-level fakes — and it's why this
segment's own regression tests (below) deliberately route through real
`OllamaClient`/`GeminiClient` instances with only the HTTP transport layer
mocked, not through hand-rolled fake slot objects, so this class of bug
can't recur silently the same way.

**The fix:** `app.ollama_client.OllamaUnavailableError` added to the
caught-exception tuple in `_call_with_retry`, treated identically to
`ProviderRateLimitedError`/`ProviderUnavailableError` — cooldown, retry on
a different slot. Safe because `MultiProviderClient` only ever wraps
Ollama-Cloud-flavored `OllamaClient` instances (Segment 19's
`host_override=`/`api_key=` constructor path) — it never wraps a plain
local client, so this exception can only mean "this one Cloud slot",
never "the whole backend," from inside this class. Local/non-pooled
`OllamaClient` usage elsewhere in the app is completely untouched —
`app/ollama_client.py` itself was not modified at all this segment, so
Segment 19's own "don't touch `_post_with_retry`'s retry-then-
`OllamaUnavailableError` path" instruction holds; that path still means
exactly "stop the whole pass" for any caller that isn't going through the
pool. Net diff: one import line, one exception type added to one `except`
tuple, plus docstring updates explaining why. See the file's own module
docstring ("Segment 24 fix" paragraph) and `_call_with_retry`'s docstring
for the in-code version of this same explanation.

**Verification completed (real client classes with HTTP-layer mocking —
no real Ollama Cloud/Gemini network reachable from this sandbox, same
limitation as every prior v1.5 segment):**

- Built a stub `app` package (`log`, `models`, `db`, `prompts`,
  `extractors.media_binary`) sized to run the real, unmodified
  `fast_pass.py`/`detailed_pass.py` end to end via their own `--selftest`
  suites — baseline-checked against the real files first (both passed
  clean, exit 0, same scenarios as Segments 8/9/23 previously reported)
  before trusting the stub for anything new.
- **Direct before/after proof of the fix**, same test run against both
  the original (unmodified, as delivered after Segment 23) and the
  patched `worker_pool.py`: a real `MultiProviderClient` built with 2
  Ollama Cloud keys (one deliberately broken — mocked
  `requests.post` raises `ConnectionError`) + 2 Gemini keys (one
  deliberately rate-limited). **Before:** `pool.generate_text()` raised
  `OllamaUnavailableError` directly — reproducing the bug exactly.
  **After:** the same call transparently succeeded via `ollama-cloud-1`
  (confirmed via `_provider_used` tag and call log — the broken slot was
  genuinely attempted three times internally, not skipped), and a second
  call correctly exercised the pre-existing Gemini rate-limit path too
  (landed on `gemini-1`), confirming the fix doesn't disturb the
  already-working retry path.
- **Segment 21's own original acceptance test, re-run against the patched
  code:** all-slots-failing (2 Ollama Cloud keys, both simulating
  connection failure) still raises exactly `RuntimeError` (checked via
  `type(exc) is RuntimeError`, not just `isinstance`) — never
  `OllamaUnavailableError`. Confirms the fix only *adds* a caught
  exception type to the retry loop; it doesn't change what happens once
  every slot is genuinely exhausted.
- **The plan's literal Segment 24 acceptance test**, run against both the
  original and patched code with the real `run_fast_pass`/
  `run_detailed_pass` (not a hand-rolled harness) as the driver, 2 real
  files on disk, a real 3-slot pool (2 Ollama Cloud, 1 deliberately
  broken + 1 Gemini):
  - **Before:** fast pass logged "Ollama unreachable ... aborting
    remaining targets/files this run" on the very first file and the job
    produced zero fast-pass results for either file — one bad key killed
    the whole job.
  - **After:** both files got real fast-pass and detailed-pass results
    (2 + 2), `"OllamaUnavailableError"` never appears anywhere in the
    logs, the total-outage short-circuit message never fires, the broken
    key is confirmed to have actually been attempted (not silently
    skipped), and `model_used` on the stored results shows real,
    distinct provider-slot labels (`ollama-cloud-1`, `gemini-0`) —
    confirming Segment 23's provenance tagging keeps working correctly
    through the pool. Both files land in the db with real statuses
    (`fast_done`/`detailed_done`), exactly like a plain single-Ollama
    run would.
- `python3 -m py_compile` on every v1.5 file together (`config.py`,
  `provider_errors.py`, `ollama_client.py`, `gemini_client.py`,
  `worker_pool.py`, `orchestrator.py`, `fast_pass.py`,
  `detailed_pass.py`): clean.
- Grepped every earlier v1.5 segment's "Watch out for" per this segment's
  own instruction and confirmed each addressed:
  - **Segment 18** ("never log a key value, not even truncated"): still
    holds — the fix only ever logs `slot.label` (a cosmetic index like
    `"ollama-cloud-0"`), never the key itself, same as every other log
    line in this file.
  - **Segment 19** ("don't touch `_post_with_retry`'s connection-error/
    timeout retry path"): `app/ollama_client.py` was not modified at all
    this segment — confirmed by diffing it against the exact file
    attached this session, zero changes.
  - **Segment 20** (Google's error envelope isn't Ollama-shaped): no
    changes needed or made to `gemini_client.py` this segment; its
    existing `_extract_text` degrade-to-`""`-with-a-warning behavior for
    a missing `candidates`/`promptFeedback` shape was already correct and
    untouched.
  - **Segment 21** ("`slot_count` must reflect actual built slots"):
    unaffected by this fix — `slot_count` still reads `len(self._slots)`
    post-cap, unchanged.
  - **Segment 22** (frontend flag): still open, no frontend files exist
    in this project for any segment to have addressed it in — carried
    forward, see "Status" above.
  - **Segment 23**: already closed clean, nothing to check.

**Not yet verified:** no real Ollama Cloud or Gemini credentials/network
access exist in this sandbox (same limitation as every prior v1.5
segment) — every check above uses real client/pool code with only the
`requests.post`/`requests.get` transport layer mocked, never a hand-rolled
fake slot object, specifically to close the test-fidelity gap that let
this segment's bug ship in the first place. But this still doesn't
confirm real HTTP behavior against the actual `ollama.com`/
`generativelanguage.googleapis.com` endpoints (response shape drift,
real auth failures, real rate-limit headers, real latency). Integration
with the real `app/log.py` remains unverified for the same reason noted
in every prior v1.5 segment (a plain stdlib-logger stand-in was used
again here, not the real module — still never reunited with this
session). The full `Orchestrator`/`scanner.py`/file-walking path was not
re-exercised this segment (Segment 22 already covered that integration
seam on its own); this segment's end-to-end test drives
`run_fast_pass`/`run_detailed_pass` directly against real `FileRecord`s
for 2 real files, which is where the actual bug and its fix live, rather
than re-proving file discovery that Segment 22 already verified works.

**Update, post-Segment-24:** the real-network run flagged above as the
one remaining gap has since happened, outside any sandboxed session — see
"Post-v1.5 fix" above. It confirmed this segment's fix holds under real
load (multiple fast-pass jobs completed, no job-wide abort, no
`OllamaUnavailableError` anywhere in the logs) and separately surfaced a
different, previously-undiscovered gap (transient 5xx responses weren't
retried at all), which has now been fixed on its own.

**v1.5 is now complete.** No known defects remain unfixed from this
segment; the two carried-forward open items are resolved (one accepted as
documented behavior, one genuinely deferred to a not-yet-started frontend
effort, both explained above under "Status"). At the time this segment
was written, the first real-network run against actual Ollama Cloud/
Gemini credentials was the one meaningful gap between "verified in this
sandbox" and "verified for real" — out of reach of any sandboxed AI
session without those credentials and without network access to
`ollama.com`/`googleapis.com`. That run has since happened (see "Update,
post-Segment-24" above and "Post-v1.5 fix" further up this file) and
confirmed this segment's own fix holds; it also found one thing this
segment's mocked testing couldn't have — see that section for the
follow-up fix.

## v1.5 — Segment 23: `model_used` Provenance Patch — DONE

**What changed (`app/pipeline/fast_pass.py`, `app/pipeline/detailed_pass.py` —
the plan's only files for this segment):**

- Exactly the plan's own one-line diff, applied identically in each file's
  `_build_result()`: `model_used=model_used,` →
  `model_used=response.get("_provider_used") or model_used,`. Nothing else
  in either file touched — confirmed by diffing the delivered files against
  the exact originals attached this session (`diff` shows one changed line
  per file, nothing more).
- Confirmed against the real files (attached this session, along with
  `worker_pool.py`) rather than assumed: `response` reaching `_build_result`
  is always a plain `dict` in both files' own `_call_model()` — a `str`
  return (`json_mode=False`, which nothing in either pass engine actually
  requests) is wrapped into `{"_parse_error": True, "raw": response}` before
  it ever reaches `_build_result`, so `.get("_provider_used")` never risks
  an `AttributeError` on a bare string.
- Confirmed against the real `worker_pool.py` (attached this session) that
  `_tag_provider()` only ever sets `_provider_used` on dict-shaped results,
  exactly matching the assumption both patched lines depend on — no drift
  from Segment 21's own description of this.

**Verification completed:**

- Built a minimal stub `app` package (config/log/models/db/ollama_client/
  extractors.media_binary/prompts — none of which travel with the real
  `fast_pass.py`/`detailed_pass.py` in isolation, same posture Segment 22's
  build session used for `orchestrator.py`) sized to exercise the two real,
  edited files' own `--selftest` end to end, not just their imports.
- Baseline run against the **unmodified** real files first, to prove the
  stub itself is faithful before trusting any post-patch result: both
  `python -m app.pipeline.fast_pass --selftest` and
  `python -m app.pipeline.detailed_pass --selftest` passed clean against
  the stub, all scenarios, exit code 0.
- Applied the one-line patch, reran both `--selftest`s: still clean, exit
  code 0, identical scenario output to the baseline run — this is the
  plan's literal acceptance test ("both files' existing `--selftest` runs
  still pass unmodified") and it holds.
- That alone only proves *non-regression*, since neither file's own fake
  `_FakeOllamaClient` fixture ever sets `_provider_used` (it has no reason
  to — that tag is `MultiProviderClient`'s job, not a bare fake client's).
  To confirm the new branch actually *works*, not just that it doesn't
  break anything, directly called both patched `_build_result()` functions
  with three hand-built `response` dicts: no `_provider_used` key (→
  `model_used` unchanged, matching pre-patch behavior), `_provider_used`
  set to a real-looking label (→ that label wins), and `_provider_used`
  present but falsy/empty-string (→ falls back to `model_used`, confirming
  the `or` short-circuit does what it looks like it does rather than e.g.
  treating an empty string as a truthy tag). All three passed in both
  files.
- `python3 -m py_compile` on both patched files: clean.

**Watch out for:** nothing new from this segment — it's exactly the
one-line-per-file diff the plan asked for, verified working and
non-regressing. See Segment 21's still-open model-forwarding conflict
(unaffected by this patch — it's about which model a slot picks, not
about how `model_used` gets recorded afterward) for the one real open
item this segment's own change doesn't touch.

## v1.5 — Segment 22: Orchestrator: Real Batching + Pool Wiring — DONE

**What changed (`app/pipeline/orchestrator.py`, the plan's only file for this segment):**

- `_resolve_effective_client(settings)` (new) — called exactly once per job, before branching into the portable vs. ordinary path, so the resulting pool is one shared object per job per Segment 21's design. Resolution order matches §7 literally: injected `self._client` wins if set, else `worker_pool.build_client(settings)`; `effective_concurrency` mirrors the pool's real `slot_count` only when the resolved client is actually a `MultiProviderClient`, else it stays `settings.ollama_max_concurrency` unchanged — so nobody without `api_keys.py` configured sees any behavior change. A `dataclasses.replace()`'d `Settings` copy carries that concurrency into both pass engines.
- `_chunk_records`/`_chunk_display` (new static helpers) replace every `for record in records: run_fast_pass(job, [record], ...)` one-file-at-a-time call site (both `_run_job`'s ordinary-root loop and `_analyze_batch`'s portable-batch loop, fast and detailed passes in each) with chunking by `effective_concurrency`, one `run_fast_pass`/`run_detailed_pass` call per chunk. Chunk size is floored to 1 as a defensive guard against a zero/negative concurrency value breaking the slice — not a hardcoded worker count. Cancellation is now checked between chunks, not between every single file — a deliberate coarser-grained change the plan calls out explicitly.
- `current_file` display: a chunk of one file still shows that file's path (unchanged from pre-Segment-22 behavior); a chunk of several shows `"{n} files (parallel)"` instead, via `_chunk_display`.
- `_run_portable_batched_job` and `_analyze_batch` now take an explicit `client` parameter (threaded from the one `_resolve_effective_client` call in `_run_job`) instead of each reading `self._client` independently — so a portable job's several batches genuinely share one pool object.

**Bug found and fixed, in scope (flagging prominently, per the plan's instruction to surface anything Segments 18-21 drifted on — this one's on Segment 22 itself, not an earlier one):** `_on_fast_progress`/`_on_detailed_progress` mutated `job.counts` without holding `self._lock`. Before this segment that was harmless — every chunk was size 1, so calls into these callbacks were effectively serial. Segment 22's real chunking makes them genuinely concurrent for the first time, and the counts read-modify-write is not atomic on its own — a concurrent `scanned`/`errored` increment can be silently lost (last writer wins on the whole `job.counts` dict). Fixed by wrapping both callbacks' bodies in `self._lock` (safe: it's an `RLock`, and `_persist` — called from inside both callbacks — already re-enters it). Proven empirically during the build session with an artificially widened race window simulating real SQLite I/O latency: unlocked lost 35/40 concurrent updates, locked lost 0/40.

**Flag for a future frontend session (per the plan's own instruction — not editing frontend files in this segment):** any frontend code that reads `current_file` from the progress endpoint expecting a single file path will now sometimes see `"{n} files (parallel)"` instead, whenever `ollama_max_concurrency`/the pool's `slot_count` is greater than 1. No frontend files exist in this segment's scope to fix this in.

**Resolving Segment 21's open flag** (noted above in "For Segment 22" as of the last update — whether the real `orchestrator.py`'s pre-edit loop actually matched plan §1's "one file at a time" description before assuming it): confirmed true. The pre-edit file called both pass engines with single-element `[record]` lists at every site in both loops, exactly as §1 and the plan's Segment 22 goal describe — no drift found here. (This module was the integration seam the plan itself warned about for all of v1.5; it held.)

**Verification:**

- Build session: a full stub `app` package (config/db/models/scanner/portable_devices/output/fast_pass/detailed_pass/ollama_client/gemini_client/provider_errors, none of which travel with this file in isolation) was built to exercise the real, edited `orchestrator.py` against the real, unmodified `worker_pool.py`. Reported passing: the plan's literal acceptance test (3 fake slots, 3 files each sleeping 1s, chunk wall-clock ~1s not ~3s) plus single-slot fidelity (chunk size 1, matching pre-Segment-22 behavior exactly), real-pool-sizes-concurrency via the actual `MultiProviderClient`/`build_client`, no-keys fallback, portable-job-path chunking, cancellation-between-chunks, detailed-pass chunking, and the counts-lock race proof above.
- This session (independent re-check against the delivered file, with a fresh minimal stub package — `worker_pool.py` and the rest of the real `app` tree weren't reattached this time, so this is narrower than the build session's run): the file imports and parses cleanly end-to-end; `_chunk_records` produces `[3,3,1]` for 7 records at concurrency 3, all-size-1 chunks at concurrency ≤0 (floored) and at concurrency 1; `_chunk_display` returns the bare path for a 1-file chunk and `"N files (parallel)"` for a multi-file chunk; `_resolve_effective_client` was checked against all four cases the resolution order implies (no pool / pool active via `build_client` / injected `MultiProviderClient` honored over `build_client` / injected non-pool object falls back to `settings.ollama_max_concurrency`) — all matched spec exactly. The threaded wall-clock-overlap and lock-race proofs above were not independently rerun this session (they need the fuller stub harness); the lock's placement itself was confirmed directly by reading the delivered source.

## v1.5 — Segment 21: Multi-Provider Worker Pool — DONE

**What changed (`app/pipeline/worker_pool.py`, new file — per the plan's
file list):**

- `MultiProviderClient(settings)` builds one slot per configured
  backend — one Ollama-Cloud-flavored `OllamaClient` (Segment 19's
  `host_override=settings.ollama_cloud_host` / `api_key=<one entry>` /
  `label="ollama-cloud-{i}"`) per entry in `settings.ollama_cloud_api_keys`,
  plus one `GeminiClient` (`label="gemini-{i}"`, `settings.gemini_model`/
  `settings.gemini_timeout_seconds`) per entry in `settings.google_api_keys`.
  Both counts are plain `len()` reads at construction time — confirmed by
  test that adding an 11th Ollama key + dropping to 1 Gemini key resizes
  the pool to 12 slots with zero code changes, and that no literal
  worker-count integer appears anywhere in the file (grepped for it
  explicitly).
- Capped by `settings.worker_pool_max_slots` if set — a negative cap
  (e.g. a stray `WORKER_POOL_MAX_SLOTS=-1`) is floored to `0` rather than
  taking a confusing negative Python slice.
- `build_client(settings)` returns `None` — never an empty/unusable pool
  object — whenever the resulting slot count is zero, whether that's
  because both key lists are empty or because a cap trims a nonzero total
  down to zero. Returns a real `MultiProviderClient` otherwise.
- `slot_count` property reflects the actual post-cap slot count, exactly
  what Segment 22 needs to size `ollama_max_concurrency`.
- Borrow/return via a `queue.Queue[int]` of slot indices, per the plan.
  `generate_text`/`generate_vision` borrow a slot, call it, and on
  `ProviderRateLimitedError`/`ProviderUnavailableError` cool that slot down
  and retry the same call on a different, not-yet-tried, not-currently-
  cooling-down slot — up to `slot_count` distinct attempts — before
  raising a plain `RuntimeError`. **Confirmed `OllamaUnavailableError` is
  never imported, referenced, or raised anywhere in this file** (only
  mentioned in docstrings explaining why it's deliberately avoided) — a
  fake all-slots-failing case raises exactly `RuntimeError`, nothing else.
  The borrowed slot index is always returned to the queue in a `finally`,
  so an unexpected (non-provider) exception can never permanently strand
  a slot out of rotation — it propagates to the caller unretried, same as
  it would from a single direct `OllamaClient`/`GeminiClient` call.
- Cooldown duration: opportunistically parses `Retry-After=<value>` back
  out of the failing exception's message text (both `OllamaClient` and
  `GeminiClient` embed it there when the response header was present —
  see Segment 19/20's own notes on why it isn't a structured exception
  attribute), clamped to 5-300s; falls back to a fixed 45s default when
  absent or unparsable. Guarded by one `threading.Lock` across all slots'
  cooldown timestamps (infrequent access; a per-slot lock would be
  over-engineering here).
- `_provider_used` is set on the returned value only when it's a `dict`
  (i.e. `json_mode=True`) — a plain `str` result (`json_mode=False`) has
  nowhere to carry the tag and is returned unchanged, for Segment 23 to
  pick up later.
- **`model` argument is accepted (signature compatibility with the
  Provider contract) but never forwarded to the borrowed slot** — see
  "Flagging a conflict" below, this is the one place this segment's
  behavior doesn't fully match the plan's own prose.
- `health_check()`: `True` if at least one slot's own `health_check()`
  returns `True` (short-circuits on the first healthy slot), `False` if
  every slot fails or there are zero slots. The plan doesn't specify
  pool-level `health_check` semantics beyond "matches the three-method
  Provider shape" — this is a reasonable design choice made this segment,
  not a literal spec requirement, flagged here rather than presented as
  if the plan mandated this exact behavior.
- One INFO log line on construction (slot counts by provider — no key
  values, obviously) — not explicitly requested by the plan's own bullet
  list, but low-risk and consistent with Segment 18's "one INFO log line"
  precedent for the `api_keys.py`-not-found case; still only touches the
  one file this segment is scoped to.

**Flagging a conflict (plan prose vs. what Segment 19 actually built —
not silently reconciled, per this session's instructions):** §7 Segment
21 says a borrowed slot "always uses its own preconfigured model (the
Cloud slot's own fast/detailed Ollama model; each Gemini slot's
`settings.gemini_model`)." But the real `OllamaClient` (Segment 19,
unchanged) has no per-instance "fast" or "detailed" model concept at
all — its own docstring is explicit that picking between
`ollama_fast_model`/`ollama_detailed_model`/the base model is the
*caller's* job via the `model=` argument on each individual call, and its
no-override fallback is simply `settings.ollama_model`. Since this
segment's own spec (same paragraph) says `MultiProviderClient` must
*not* forward `model` to the slot it borrows, and the pool is a single
object built once per job and shared across both the fast and detailed
pass (per Segment 22's design in §7), there is no mechanism available
for an Ollama Cloud slot to ever know which pass is calling it. **Net
effect: every Ollama Cloud slot call in the real running app will use
`settings.ollama_model` (the plain base model) for both fast and detailed
passes, never `ollama_fast_model`/`ollama_detailed_model`**, contradicting
the plan's parenthetical (though not its explicit, bolded "do not
forward `model`" instruction, which this implementation follows
literally). Local (non-Cloud) Ollama calls and direct-`OllamaClient`
paths are entirely unaffected — this only concerns calls routed through
the pool. Worth a decision next session: either (a) accept this as the
actual v1.5 behavior and fix the plan's own wording, or (b) if
fast/detailed model selection for Ollama Cloud slots is actually wanted,
that needs a real design change (e.g. `MultiProviderClient` exposing two
separate borrow paths, or building two pools) — out of scope for a
one-file segment, and not something this session guessed at silently.

**Verification completed (fake slots / mocked clients — no real Ollama
Cloud or Gemini network reachable from this sandbox, consistent with
Segments 19/20):**

- 35 assertions across a throwaway script (Segment 21's own "Files:" list
  is `app/pipeline/worker_pool.py` only, no test file — same posture as
  Segments 18-20): `len()`-driven slot construction and resizing;
  `worker_pool_max_slots` capping (including a negative-cap edge case);
  `build_client` returning `None` for zero-keys and capped-to-zero cases,
  and a real object otherwise.
- **The plan's literal acceptance test:** two fake slots, one that always
  429s and one that always succeeds — confirmed the call transparently
  succeeds via the working slot (with `_provider_used` tagged to its
  label), and that a second call within the failing slot's cooldown
  window does **not** invoke it again (confirmed by call-count, not just
  by outcome). Separately confirmed that once a cooldown is simulated as
  expired, the previously-failing slot becomes eligible again.
- **The plan's all-slots-failing case:** two fake slots (one 429, one
  connection-unavailable) both fail — confirmed the call raises exactly
  `RuntimeError` (checked via `type(exc) is RuntimeError`, not just
  `isinstance`), confirmed both slots were actually invoked once each,
  and confirmed `OllamaUnavailableError` is not what's raised (caught and
  explicitly failed the test if it had been).
- `model=` passed into the pool's `generate_text`/`generate_vision` is
  confirmed to never reach the underlying fake client's own call
  arguments (asserted the recorded call's `model` was `None`).
- `generate_vision` confirmed to also retry transparently across slots,
  same as `generate_text`.
- `json_mode=False` (`str` return) confirmed to pass through untagged,
  no crash attempting to tag a non-dict.
- `health_check()`: confirmed `True` with a mix of healthy/unhealthy
  slots, `False` when every slot is unhealthy, and confirmed a slot whose
  own `health_check()` raises unexpectedly doesn't crash the pool's
  aggregate check (defensive `try/except` around each slot's call, even
  though both real clients' own contracts say they never raise here).
- **Thread-safety:** 3 fake slots (each holding a 0.15s simulated call)
  driven by 6 concurrent threads — confirmed via a shared active-set with
  its own lock that no two threads ever used the *same* slot index at
  the same instant (mutual exclusion via the `queue.Queue` borrow/return
  works as intended), and confirmed the 6 calls completed in ~0.3s wall
  clock (2 sequential rounds across 3 slots), not ~0.9s (fully serial) —
  early, informal evidence for the same "N slots ⇒ N simultaneous calls"
  property Segment 22's own acceptance test will check properly at the
  orchestrator level.
- `python3 -m py_compile` on all v1.5 files together (`config.py`,
  `provider_errors.py`, `ollama_client.py`, `gemini_client.py`,
  `worker_pool.py`): clean.

**Not yet verified:** no real Ollama Cloud or Gemini credentials/network
access exist in this sandbox (same limitation as Segments 19-20), so
nothing here confirms real HTTP behavior end-to-end through the pool —
only that the pool's own borrow/retry/cooldown/tagging logic is correct
given each client's already-verified contract. Integration with the real
`app/log.py` remains unverified for the same reason noted in Segments
18/19 (this session again used a plain stdlib-logger stand-in, not the
real module). Segment 22's own acceptance test (3 fake slots, 3 files,
~1s overlap) is a stronger, purpose-built version of this segment's
informal thread-safety check above and should be treated as the
authoritative proof once built.

**Watch out for:** the `model`-forwarding conflict flagged above is the
one thing in this segment most likely to need a real decision before
Segment 24's end-to-end verification — it won't break anything (Ollama
Cloud slots will just quietly always use `settings.ollama_model`), but
it's a silent behavior gap from what the plan's prose implies, not a
crash, so it's easy to miss without this note.

## v1.5 — Segment 20: Gemini REST Client — DONE

**What changed (`app/gemini_client.py`, new file — per the plan's file list):**

- New `GeminiClient` class, constructor `(api_key: str, model: str,
  timeout_seconds: int, label: str)` exactly per the plan's §4 signature —
  all four required, no `Settings` object taken directly (see "Handoff
  notes for Segment 21" below for why this is a deliberate difference
  from `OllamaClient`'s constructor shape).
- `generate_text` / `generate_vision` / `health_check` match §4's
  "Provider" contract byte-for-byte (same param names, same defaults,
  same `dict | str` return shape) — confirmed a `GeminiClient` instance
  can stand in anywhere an `OllamaClient` instance currently can.
- Talks to `POST https://generativelanguage.googleapis.com/v1beta/models/
  {model}:generateContent`. Request shape verified against ai.google.dev's
  current API reference and several current (2026-dated) official
  examples this session, per the plan's explicit "verify the current
  exact... shape" instruction, not assumed from training data:
  - `contents: [{"role": "user", "parts": [...]}]`, where each part is
    `{"text": ...}` or `{"inlineData": {"mimeType": ..., "data": <b64>}}`.
  - `systemInstruction: {"parts": [{"text": ...}]}` — included only when
    `system` is truthy; see "Watch out for" below for why this
    deliberately does *not* mirror `OllamaClient` always sending the
    field regardless of value.
  - `generationConfig: {"responseMimeType": "application/json"}` for
    JSON mode; the key is omitted entirely (not sent as `false`/absent
    value) when `json_mode=False`.
  - Image parts always use `mimeType: "image/jpeg"` unconditionally, not
    derived per-call — confirmed against the real
    `app/extractors/media_binary.py` (attached this session per the
    plan's file list) that every `ContentPayload.images_b64` entry is
    already a plain-base64, JPEG-re-encoded string with no data-uri
    prefix (`_extract_image()` → `_encode_jpeg()` always saves
    `format="JPEG"` regardless of the source file's original format).
    **No conflict found** between the plan's assumption and the real
    file here — nothing to flag.
- Auth: `x-goog-api-key` request header (current official convention,
  confirmed live in ai.google.dev's own curl examples this session) —
  **not** a `?key=` query param, exactly per the plan, on both the
  generate call and `health_check`.
- Error mapping, mirroring `OllamaClient`'s reachability-vs-errors split
  but landing on the provider-agnostic types instead of
  `OllamaUnavailableError` (per PLAN_v1_5.md §1 — a Gemini key problem
  must never be able to trigger that class's "stop the whole pass"
  meaning):
  - HTTP 429 → `app.provider_errors.ProviderRateLimitedError` immediately
    (message includes label + `Retry-After` if present), not retried in
    this client, exactly one POST attempt — confirmed Google's real 429
    envelope is `{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
    ...}}` (checked several current, real-world examples this session),
    but this client never actually parses that body — see "Watch out
    for" below.
  - Connection error / timeout → same retry+backoff shape as
    `OllamaClient._post_with_retry` (3 attempts, 1s/2s backoff), then
    `app.provider_errors.ProviderUnavailableError` once exhausted —
    confirmed **not** a subclass of, and never raised as,
    `OllamaUnavailableError`.
  - Any other HTTP error (400/403/500/...) propagates as a plain
    `requests.HTTPError` via `raise_for_status()`, unretried — identical
    posture to `OllamaClient`'s non-429 case.
- `health_check()` uses `GET /v1beta/models/{model}` (Google's
  `models.get`) rather than a generation call — cheap reachability+auth
  probe, no equivalent-cost spend, same "short fixed timeout independent
  of the configured one, never raises" shape as `OllamaClient`'s
  `/api/tags` check.
- `_extract_text()` (new — no `OllamaClient` equivalent, since Ollama's
  response is a single flat `response` string field, not Gemini's
  `candidates[0].content.parts[*].text`): concatenates all text parts of
  the first candidate. A response with zero candidates (safety-blocked,
  reported via `promptFeedback`, not an HTTP error) degrades to `""` with
  a warning log — never raises.
- `_parse_json_response` / `_extract_first_json_object`: **duplicated**
  from `app.ollama_client`, not imported, exactly as the plan specifies
  (same convention `detailed_pass.py` already established relative to
  `fast_pass.py`). Logic is byte-for-byte identical to the originals;
  only the log message text differs ("gemini response" vs "model
  response") to keep log lines attributable to the right client.
- `--ping --api-key KEY [--model M]` CLI entry point, mirroring
  `ollama_client.py`'s own `--ping` convention but taking the key/model as
  explicit CLI args rather than reading `get_settings()`: unlike
  `OLLAMA_HOST` (one fixed thing to check), there's no single "the"
  Gemini key to default to — `settings.google_api_keys` may hold any
  number of independent slots, and this is a manual single-key smoke-test
  tool, not a whole-list iterator.

**Handoff notes for Segment 21 (two constructor asymmetries worth
knowing before building the pool):**

1. `OllamaClient(settings, *, host_override=None, api_key=None,
   label="ollama-local")` takes the whole `Settings` object plus
   *optional* overrides; `GeminiClient(api_key, model, timeout_seconds,
   label)` takes four *required* plain values and no `Settings` object at
   all. This was a deliberate choice, not an oversight: `OllamaClient`
   has a real local-only default mode to fall back to (no `api_key` at
   all), which is exactly why it needs `settings.ollama_host` as its
   base case. `GeminiClient` has no equivalent "default/local" mode — it
   always needs a real key — so there was nothing for an all-optional,
   `Settings`-shaped constructor to default *to*. Segment 21 will need to
   pull `settings.gemini_model` / `settings.gemini_timeout_seconds` out
   itself and pass them positionally per key, rather than handing
   `GeminiClient` a `Settings` instance the way `OllamaClient` gets one.
2. `GeminiClient` has no `host_override` — Gemini's endpoint is a fixed
   public host (`_API_BASE` in the module), unlike Ollama Cloud, which is
   the *same* server software as local Ollama at a different address.
   There was nothing in the plan asking for one, and no sane use case for
   it in this app, so it wasn't added.

**Verification completed (mocked HTTP only — see "Not verified" below for
why):**

- 44 assertions across a throwaway mocked-`requests` script (same ad hoc,
  not-part-of-the-deliverable posture as Segment 18's verification —
  Segment 20's own "Files:" list in the plan is `app/gemini_client.py`
  only, no test file): request shape (contents/parts/systemInstruction/
  generationConfig) for both `generate_text` and `generate_vision`;
  header-based auth with no `?key=` anywhere in the URL; multi-image
  ordering; per-call `model` override vs. constructor default; the full
  429/500/connection-error/timeout matrix above, including exact retry
  counts and exception types; graceful no-candidates handling in both
  `json_mode=True` and `False`; multi-part response concatenation; the
  balanced-brace JSON extractor against a `}` embedded inside a string
  value; `health_check`'s True/False/never-raises/short-timeout behavior;
  and — checked explicitly, across the 429 path, the connection-error
  path, and `health_check`'s failure path — that the fake API key used in
  every test never appears in any log line at any level.
- `python3 -m py_compile` on `app/gemini_client.py` (plus the three
  Segment 18/19 files, re-checked together): clean.
- CLI sanity-checked directly (not just imported): no-args prints help
  and exits 0; `--ping` without `--api-key` fails via `argparse` with a
  clear message (exit 2); `--ping --api-key <fake>` against a blocked
  network fails gracefully with a plain `FAIL —` message and exit 1, no
  traceback.

**Not verified:** a live round-trip against a real Gemini key. This
sandbox's network egress proxy blocks `generativelanguage.googleapis.com`
outright — confirmed directly (not assumed): a real HTTPS request to both
the `health_check` and `generateContent` endpoints, made with an
intentionally-fake key from this sandbox, came back `403 Host not in
allowlist: generativelanguage.googleapis.com` from the *egress proxy
itself*, not from Google — so this doesn't even confirm the endpoint path
is reachable from the open internet, only that this client sends a
well-formed request to the right place. Recommend running
`python -m app.gemini_client --ping --api-key <a real key>` from a
machine with normal network access, and separately one real
`generate_text`/`generate_vision` call each, before trusting this against
production traffic — exactly what the plan's own acceptance test asks
for ("a live smoke test with one real key").
**Watch out for:**

- Confirmed **not** relying on Google's error JSON envelope shape
  anywhere — non-429 HTTP errors are left to propagate as a plain,
  unparsed `requests.HTTPError` rather than this code reaching into
  `response.json()["error"]["message"]`, exactly the plan's own warning
  ("don't assume that's always present"). The 429 path also never parses
  the body — it keys off `response.status_code` alone, same as
  `OllamaClient`'s 429 handling.
- `systemInstruction` is only sent when `system` is truthy — a small,
  intentional divergence from `OllamaClient`, which always sends its
  `system` field regardless of value. Every real call site in this app
  passes a non-empty system prompt, so this never bites in practice; it
  exists because Gemini's API is documented to reject empty/whitespace-
  only text parts with a 400, while Ollama's local `/api/generate` is
  tolerant of an empty `system` string — mirroring "always send the
  field" here would risk a spurious `requests.HTTPError` on an edge case
  `OllamaClient` simply doesn't have. Worth a second look if a future
  caller ever legitimately wants a no-system-prompt call.

## v1.5 — Segment 19: Ollama Cloud Support in OllamaClient — DONE

**What changed (`app/ollama_client.py` only, per the plan's file list):**

- `OllamaClient.__init__` gained three keyword-only params, all defaulting
  to exact pre-v1.5 behavior: `host_override: str | None = None`,
  `api_key: str | None = None`, `label: str = "ollama-local"`.
  `self._base_url` now resolves to `host_override or settings.ollama_host`.
- New `_headers()` method: returns `{"Authorization": f"Bearer
  {self._api_key}"}` when `api_key` is set, else `{}`. Wired into both the
  generate POST (`_post_with_retry`) and `health_check`'s GET, per the
  spec's "every request" requirement.
- `_post_with_retry` now checks `response.status_code == 429` right after
  the request (before `raise_for_status()`) and raises
  `app.provider_errors.ProviderRateLimitedError` (message includes the
  slot's `label` and the response's `Retry-After` header if present) —
  the one behavioral change, exactly as scoped. This check sits inside the
  same `try` block as before but is not a subtype of
  `(ConnectionError, Timeout)`, so it isn't caught by that `except` and
  isn't retried locally — it propagates straight out for the pool
  (Segment 21, not yet built) to catch and retry on a different slot.
- The connection-error/timeout retry-then-`OllamaUnavailableError` block
  (backoff, log messages, exception message text) was **not touched at
  all** — verified byte-for-byte identical output (see verification
  below), per the segment's explicit "watch out for."
- Deliberately did **not** add a `retry_after_seconds` attribute to
  `ProviderRateLimitedError` itself (only put it in the message string) —
  that class lives in `provider_errors.py`, which is outside this
  segment's file list. If Segment 21 wants structured access to
  `Retry-After` rather than parsing it back out of the message text,
  that's a small addition to make there against the real
  `provider_errors.py`.

**Verification completed (mocked HTTP — no real Ollama/Ollama Cloud
reachable from this sandbox):**

- Default `OllamaClient(settings)` (no new args): identical request URL,
  `headers={}` (functionally identical to omitting headers), identical
  success-path return value — for both `generate_text` and
  `health_check`.
- `host_override` + `api_key` + `label` together: correct overridden URL,
  and `Authorization: Bearer <key>` sent on **both** the generate call and
  `health_check`.
- HTTP 429 (with a `Retry-After` header): raises
  `ProviderRateLimitedError` (message includes label + retry-after),
  never `OllamaUnavailableError`, never a bare `requests.HTTPError`;
  confirmed exactly **one** POST attempt (not retried locally) and zero
  `time.sleep` calls.
- Non-429 HTTP error (500): still raises plain `requests.HTTPError`,
  unretried — confirms the 429 change didn't leak into other status
  codes.
- Connection-error path (`ConnectionError`/`Timeout`, both a local and a
  Cloud-flavored client): still exactly 3 attempts, 2 backoff sleeps, and
  the **exact same** `OllamaUnavailableError` message text as before
  (`"could not reach Ollama at {base_url} after 3 attempts"`) — confirms
  this path is untouched, label included.
- Confirmed no key value (a realistic-looking fake secret was used) ever
  appears in any log output, at any log level, in any scenario including
  the 429 case whose log line quotes the label and `Retry-After` value.
- `python -m app.ollama_client --ping` and `python -m app.ollama_client`
  (no args) both still run without error/crash and produce the same
  output shape as before (fails to reach `localhost:11434` in this
  sandbox, same as pre-v1.5 would — no real Ollama server is available in
  this environment either way, so this proves "doesn't crash," not "can
  reach a server").
- `python3 -m py_compile` on all three touched/created v1.5 files: clean.

**Not verified:** a live round-trip against a real Ollama Cloud key
(`host_override="https://ollama.com"`) — this sandbox's network egress
allowlist doesn't include `ollama.com`, and no real Cloud key was
available anyway. The plan marks this as conditional ("if you have a real
Ollama Cloud key"); recommend doing this once a key is available.

**`app/log.py` interface note (updates the flag raised in Segment 18):**
this segment's real `app/ollama_client.py` confirms `from app.log import
logger` is genuinely the codebase's convention, and that `logger` behaves
like a standard `logging.Logger` (`.debug/.warning/.error` with %-style
args) — consistent with the compatibility assumption made for
`app/config.py` in Segment 18. I deliberately did **not** go back and
change `app/config.py` to `from app.log import logger`, though: `app/log.py`
itself is still unseen, and if it derives a log file path from
`Settings` (e.g. `app_data_dir`), having `config.py` import `app.log`
would create `config → log → config`, a circular import that could break
`Settings.load()` — the single most foundational call in the app. This is
a deliberate, conservative choice, not an oversight. Please check the real
`app/log.py` for this and switch `config.py` over if it's actually safe.

## v1.5 — Segment 18: Provider Foundations (Config & Shared Errors) — DONE

**What changed:**

- `app/config.py`: `Settings` gained six new fields, all defaulting to
  "off"/pre-v1.5 behavior: `ollama_cloud_host` (default
  `https://ollama.com`), `ollama_cloud_api_keys: list[str]` (default `[]`),
  `gemini_model` (default `gemini-flash-latest`), `gemini_timeout_seconds`
  (default `120`), `google_api_keys: list[str]` (default `[]`),
  `worker_pool_max_slots: int | None` (default `None`, uncapped). Added
  `_load_api_keys()` (imports `api_keys.py` from the project root,
  independently `getattr`s `google_apikeys`/`ollama_apikeys` so a typo or
  missing name in one list can't disable the other), `_coerce_key_list()`
  (validates list-ness, coerces entries to `str`, drops blanks/dupes,
  preserves order), and `_parse_optional_int()` (blank/unset env var ->
  `None`). No worker/slot count is hardcoded anywhere in this segment —
  both list lengths are read fresh on every `Settings.load()` call.
- `app/provider_errors.py` (new): `ProviderRateLimitedError` and
  `ProviderUnavailableError`, each documented per the plan — "retry on a
  different slot" errors, distinct from and never escalated into
  `app.ollama_client.OllamaUnavailableError`.
- `.env.example`: appended the v1.5 block (`OLLAMA_CLOUD_HOST`,
  `GEMINI_MODEL`, `GEMINI_TIMEOUT_SECONDS`, `WORKER_POOL_MAX_SLOTS`) with
  the same explanatory comments as the plan, including that the two API
  key lists live in `api_keys.py`, not `.env`.
- No `api_keys.py` was created — correctly: it's a gitignored secret file
  the user creates themselves, and its absence is the tested "everything
  stays off" default path.

**Session environment note (flagging, not silently reconciling):** this
session only had `app/config.py`, `.env.example`, `PROJECT_STATE.md`, and
the plan itself uploaded — not the rest of the real v1 codebase
(`app/log.py`, `app/ollama_client.py`, `app/pipeline/*`, `tests/`, etc.).
That happened to be exactly Segment 18's file list, so it was sufficient
to build and verify this segment in isolation against real files rather
than assumptions. Two consequences worth confirming against the actual
repo next session:

1. **Logging:** the plan's Segment 18 spec says to log one INFO line via
   presumably the app's normal logging path, and this file's own earlier
   sections describe portable-device logging going through a shared
   `app.log.logger`. `app/log.py` wasn't available this session to confirm
   its exact export shape (a `logger` object? a `get_logger()` factory?),
   so `app/config.py` uses a plain stdlib
   `logger = logging.getLogger(__name__)` instead of guessing at that
   interface. A child logger like this still feeds into whatever
   handlers/formatters a central `app.log` setup installs on the root
   logger, so it's very likely already compatible — but please check
   `app/log.py`'s real interface and swap to it if it turns out to differ
   (e.g. expects callers to use `app.log.get_logger(__name__)` or similar)
   at the start of the next v1.5 segment.
2. **No test file was added.** Segment 18's own "Files:" list is
   `app/config.py`, `.env.example`, `app/provider_errors.py` only — it
   doesn't include a test file, and `tests/test_end_to_end.py`'s actual
   contents weren't available this session to safely extend. Acceptance
   verification (below) was done with an ad hoc, throwaway subprocess-based
   script, not saved anywhere in the deliverable. Worth adding a permanent
   unit test for `_load_api_keys()`/`_coerce_key_list()` once the full test
   suite is back in context.

**Verification completed (Segment 18 acceptance test, run this session):**

- Against the real, unmodified `.env.example` (pre-this-segment, zero v1.5
  vars present at all) with no `api_keys.py`: `Settings.load()` doesn't
  raise; `google_api_keys == []`, `ollama_cloud_api_keys == []`; every
  pre-existing field (`ollama_host`, `app_port`, `portable_stage_batch_gib`,
  etc.) is byte-for-byte unchanged from pre-v1.5 — confirmed identical
  effective config.
- Against the same, but with `api_keys.py` defining 10 `ollama_apikeys` +
  7 `google_apikeys`: `len(settings.ollama_cloud_api_keys) == 10`,
  `len(settings.google_api_keys) == 7` — the loader's counts follow
  whatever's configured, not a number in `config.py`.
- `api_keys.py` defining only `ollama_apikeys` (no `google_apikeys` at
  all): `ollama_cloud_api_keys` populates correctly, `google_api_keys`
  falls back to `[]` independently — one missing/typo'd name doesn't take
  down the other provider.
- `api_keys.py` with a malformed `google_apikeys` (dupes, blanks, a
  non-string `int` entry) and a non-list `ollama_apikeys` (a bare string):
  both coerce safely — duplicates/blanks dropped, order preserved,
  non-string entries stringified, non-list value becomes `[]` — no
  exception raised.
- `WORKER_POOL_MAX_SLOTS` unset/blank -> `None`; set to `"5"` -> `5`.
- Confirmed via `logging.basicConfig` at both INFO and DEBUG level, with
  real-looking key strings in `api_keys.py`, that no key value ever
  appears in log output (stdout or stderr) in either the found-file or
  not-found-file case — only the fixed "api_keys.py not found — Ollama
  Cloud and Gemini worker slots disabled" INFO line, with no key content.
- `python3 -m py_compile app/config.py app/provider_errors.py` and a
  direct import/raise/catch check of both new exception classes: clean.

**Not yet verified:** a real live round-trip isn't applicable to this
segment (no network calls exist yet — that starts at Segment 19/20).
Integration with the actual `app/log.py` interface (see the note in the
Segment 19 section above) and with `tests/test_end_to_end.py` is
unverified since neither file has been available yet this project.

`starter.bat` is the Windows one-click launcher. It requests UAC elevation before starting any process, creates/updates `.venv`, starts `ollama serve` in a hidden `cmd.exe` window if necessary, waits up to 60 seconds for its local HTTP API, verifies the locally installed `gemma3:12b-it-qat` model, and launches `main.py` with that model selected for both passes. `main.py` requests the same elevation when launched directly.

## Windows MTP support — batched staging (supersedes the original single-shot version)

The original approach (below) copied an entire phone to `APP_DATA_DIR/tmp/` before any file counter or analysis started, which read as a frozen UI on large phones. `app/portable_devices.py` now stages and analyzes a connected phone in bounded-size batches instead, so temporary disk usage stays capped and progress updates continuously through the copy phase, not just after it.

New/changed public surface of `app/portable_devices.py`:

```python
def is_portable_root(root: str) -> bool
def list_roots() -> list[dict[str, str]]
def encode_root(parsing_path: str) -> str
def decode_root(root: str) -> str
def stage_root(root: str, settings: Settings) -> Iterator[list[FileRecord]]   # kept; unused by the orchestrator now
def staged_batches(root: str, settings: Settings, *, on_progress=None) -> Iterator[Iterator[StagedBatch]]
def error_record(root: str, message: str) -> FileRecord

@dataclass(frozen=True)
class StagedBatch:
    index: int
    records: list[FileRecord]
    bytes_staged: int
```

`staged_batches()` is a context manager yielding a generator of `StagedBatch`. Each batch holds at most `Settings.portable_stage_batch_gib` GiB (default 5; a single oversized file may exceed the cap but never shares a batch), copied one file at a time through the same live Explorer `FolderItem` reopening as before (`_find_storage_folder()`). A batch's temporary directory is deleted only once the caller has advanced past it (normal Python generator semantics — the cleanup line runs when `next()` is called again, i.e. after the caller's loop body for that batch has finished). `on_progress(event, display_path, batch_bytes, batch_limit)` fires for `"indexed"` (file discovered, about to copy), `"copying"`, `"staged"` (copy done), and `"error"`.

`Settings` gained two fields (`app/config.py`, `.env.example`): `portable_stage_dir` (blank = `{APP_DATA_DIR}/tmp`) and `portable_stage_batch_gib` (default `5`, env `PORTABLE_STAGE_BATCH_GIB`). `app/api.py` exposes `GET /api/portable-stage-settings` and `POST /api/choose-portable-stage-folder` (opens the same Tk-based native picker as `/api/choose-folder`, persists the choice into `.env` via `_persist_env_value`). The setup screen (`frontend/index.html`, `static/js/setup.js`, `static/css/detective.css`) shows a "Connected-phone staging" panel with the current directory/batch size and a picker button wired to those two endpoints.

`Orchestrator._run_portable_batched_job()` (`app/pipeline/orchestrator.py`) is the phone-scan entry point, used whenever any `job.roots` entry is a portable root: ordinary roots are walked and analyzed first, then each portable root's `staged_batches()` is consumed one batch at a time — analyze (fast pass, then detailed pass on candidates) — `output.preserve_staged_matches()` copies that batch's matches into a durable per-job manifest — then the batch's staging directory is deleted before the next batch is requested. `output.finalize_output()` reads that manifest for phone-derived matches (their staging paths no longer exist by the time the job finishes) alongside any ordinary-root matches, so `REPORT.md`/`results.json` come out complete either way.

**Live progress / "0 files" fix:** the `"indexed"` progress event increments `job.counts["total"]` and persists it to SQLite immediately, per file, *inside* the synchronous copy loop — before that batch's analysis pass even begins. `Orchestrator.get_progress()` reads `job` fresh from `db.get_job()` on every call, so `GET /api/scan/{id}/progress` reflects a growing `total` throughout the copy phase rather than jumping from 0 once an entire batch finishes. `current_file` updates the same way for the in-progress copy/analysis file. (Frontend `progress.js`/`progress.html` were not part of this pass — not attached to this session — so if the count still reads 0 in the actual running app, the remaining bug is most likely on the polling/rendering side there, not in this backend counting logic.)

**Logging** (all via `app.log.logger`, not `print`): batch open/close with elapsed time and MB/s; per-file copy start and done (size, duration, MB/s); per-file copy failure with the batch index; DEBUG-level per-file "indexed" line; and one INFO summary per portable root once staging finishes (total files, total GiB, error count, elapsed, average MB/s).

Phone scans still require the device to stay connected and unlocked for the duration of the job; temporary disk usage is now bounded by `portable_stage_batch_gib` rather than the whole phone's size.

## Segment 16 - Desktop app shell

New or finalized files:

- `main.py`
- `.env.example`
- `requirements.txt`
- `README.md`

`main.py` exposes:

```python
def start_backend(settings: Settings) -> tuple[uvicorn.Server, threading.Thread]
def wait_for_backend(port: int, timeout_seconds: float = 15.0) -> bool
def stop_backend(server: uvicorn.Server, thread: threading.Thread) -> None
def run() -> None
def ensure_elevated() -> bool
```

`run()` starts Uvicorn locally on the configurable `APP_PORT`, waits for `GET /api/roots`, opens a pywebview window at `http://localhost:{APP_PORT}`, and shuts the server down when the window closes. `app/api.py` now mounts `frontend/` after its API routes, so the desktop window serves the real setup, progress, and results screens.

## Segment 17 - Integration and docs

New files:

- `tests/test_end_to_end.py`
- `tests/__init__.py`
- `test_assets/README.md` plus generated mixed fixtures on test run

`python -m unittest tests.test_end_to_end` uses a deterministic local model stub and verifies: a mixed text/image/PDF/DOCX/nested-ZIP/unknown/empty-file scan produces `REPORT.md`; FastAPI serves the frontend; the app-shell readiness and shutdown paths work; the native folder-picker route returns a usable root without opening a dialog during tests; and portable-device root identifiers both round-trip and reopen through the correct Explorer item without needing a physical phone in automated tests.

## Additive local integration

The setup screen's **Add a folder or drive** action uses the additive local-only route:

```text
POST /api/choose-folder
-> {"selected": false}
 | {"selected": true, "root": {"path": str, "label": str, "kind": "folder"|"drive"}}
```

It opens the operating-system directory picker in the local app process because a browser picker cannot safely reveal the absolute path required by the scanner. Existing API contracts remain unchanged.

## Verification completed

- Installed `requirements.txt`, including document extractors, HEIC support, pywebview, and the Windows `pywin32` dependency used for MTP discovery/staging.
- Confirmed the installed `gemma3:12b-it-qat` model reports both completion and vision capabilities.
- `python -m unittest tests.test_end_to_end -v`: 6 tests passed.
- Confirmed the currently connected Xiaomi MTP storage reopens through the live Explorer device item and exposes 29 top-level items. No phone file content was copied during this verification.
- Passed self-tests for database, scanner, archives, text/document extraction, media extraction, intents, prompts, fast pass, and detailed pass.
- Confirmed generated image and PDF fixtures are readable by Pillow and pypdf.
- **Not yet verified this pass:** the batched-staging code above (`staged_batches`, `_run_portable_batched_job`, the new logging) was reviewed statically — read end-to-end against the actual files, checked call signatures, dedup/counting math, and generator/cancellation cleanup paths by hand — but not run against a real phone or `tests.test_end_to_end`, since that requires Windows/pywin32/a connected device this session doesn't have. Run `python -m unittest tests.test_end_to_end -v` and a real phone scan before trusting this in production.

## Intentional v1 boundaries

- Legacy binary Office (`.doc`, `.xls`, `.ppt`), OpenDocument files, and optional `.7z`/`.rar` archive tooling are outside the PLAN.md v1 format contract. They degrade safely to metadata/opaque-file handling and never abort a scan. Enabling `.7z`/`.rar` additionally requires their documented optional dependencies (and an external extractor for many RAR files).
- A real Ollama scan still requires `ollama serve` and a locally pulled vision-capable model; integration tests intentionally use a deterministic stub instead of contacting a model service.
