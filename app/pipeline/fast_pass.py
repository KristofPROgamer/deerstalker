"""Fast-Pass Engine — PLAN.md Segment 8.

Cheap first-pass triage across every scanned file: extract content once
per file (via Segment 6's `extract_any()`, the one pipeline-facing
extraction entry point — never `text_docs.extract()`/`media_binary.
extract()` directly), then for each target the user selected — a
registered `SearchIntent` id from `job.intent_ids` (this includes the
special `"interesting"` id with no special-casing needed, see
PROJECT_STATE.md's Segment 7 notes) and/or a free-text `job.
custom_query` — build a fast prompt via `app.prompts`, ask the model,
parse the response, and persist an `AnalysisResult(pass_type="fast")`.

Depends on Segments 1 (config/models/db/log), 2 (ollama_client), 5+6
(`extract_any()`), and 7 (intents/prompts) — see PLAN.md's Segment 8
spec and PROJECT_STATE.md's "Segment 8/9, read this before wiring
prompts.py in" note before changing anything here.

**Load-bearing contract inherited from Segment 7 (see PROJECT_STATE.md):**
the model is only ever asked for a 5-key subset of `AnalysisResult`
(`relevance_score`/`tags`/`summary`/`reasoning`/`flagged_interesting`).
This module fills in the rest (`id`/`file_id`/`job_id`/`pass_type`/
`intent_id`/`model_used`/`created_at`) itself from `job`/`record`/
context — see `_build_result()`. A `_parse_error` response (malformed
JSON from the model) is treated the same as a response simply missing
all five keys, not as a special case — see `_coerce_score`/
`_coerce_tags`/`_build_result`.

**FileRecord.status ownership:** nothing before this segment ever moves
a `FileRecord` off `status="pending"` (scanner/archives only ever set
`"pending"` or `"error"` for files they themselves couldn't read/stat).
This module is what actually introduces `"fast_done"` into a file's
life cycle — see `_process_file()`. Per PLAN.md's own wording ("One
file's exception never kills the pass (catch, mark status='error',
continue)"), an unrecoverable per-file failure is recorded the same way
Segment 3 already does: `status="error"` + `error_message` set, then
move on to the next file.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from app.config import Settings
from app.extractors.media_binary import extract_any
from app.log import logger
from app.models import AnalysisResult, ContentPayload, FileRecord, ScanJob
from app.ollama_client import OllamaClient, OllamaUnavailableError
from app.prompts import build_custom_prompt, build_fast_prompt

# Sentinel appended to a job's per-file target list when `job.custom_query`
# is non-blank. Never collides with a real `SearchIntent.id` (those are
# all plain lower_snake_case words from `app.intents.INTENTS`) and is
# never itself written into `AnalysisResult.intent_id` — the resulting
# result's `intent_id` is `None` for this target, matching `models.py`'s
# own field comment ("None for ... free-text custom").
_CUSTOM_TARGET = "__custom__"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def run_fast_pass(
    job: ScanJob,
    files: list[FileRecord],
    settings: Settings,
    on_progress: Callable[[FileRecord, AnalysisResult | None], None],
    client: OllamaClient | None = None,
) -> None:
    """Run the fast pass for `job` across `files`.

    Per file: `extract_any()` once, then for every target this job
    selected (each `intent_id` in `job.intent_ids`, plus a free-text
    target if `job.custom_query` is non-blank) build the fast prompt via
    `app.prompts`, call `generate_text`/`generate_vision` (chosen from
    `payload.kind` — image/mixed use vision, everything else text-only),
    parse the response into an `AnalysisResult(pass_type="fast")`, and
    `db.upsert_result()` it. `FileRecord.status` is updated to
    `"fast_done"` (at least one target produced a usable result) or
    `"error"` (extraction blew up unexpectedly, or every target failed)
    and persisted via `db.upsert_file()`.

    `on_progress` is called exactly once per entry in `files` — after
    that file's targets have all been attempted — with the
    (possibly status-updated) `FileRecord` and the single
    highest-`relevance_score` `AnalysisResult` produced for it this
    pass, or `None` if nothing usable came out of it. It's invoked from
    worker threads (see concurrency note below); if it isn't itself
    thread-safe, the caller must add its own locking.

    Runs a `ThreadPoolExecutor` bounded by
    `settings.ollama_max_concurrency` (PLAN.md: "Ollama usually
    serializes on one accelerator anyway — keep this modest"). One
    file's exception never kills the pass — every per-file and
    per-target failure is caught, logged, and accounted for in that
    file's status rather than propagating.

    **Total-outage short-circuit (judgment call, see PROJECT_STATE.md):**
    if a target's model call raises `OllamaUnavailableError` (Ollama
    unreachable after `OllamaClient`'s own retries — a fundamentally
    different failure mode than one bad HTTP response, per
    `ollama_client.py`'s own module docstring), this is treated as "the
    whole server is down" rather than "this one call failed": no further
    targets are attempted for that file, and any files not yet started
    are skipped outright (left untouched at whatever status they already
    had — typically `"pending"` — rather than burning a full retry
    sequence per file for a server that's already known to be down).
    `on_progress` is still called for every file, including skipped
    ones, so a caller tracking "files attempted" always sees exactly
    `len(files)` calls.

    Args:
        job: the `ScanJob` this pass belongs to (supplies `id`,
            `intent_ids`, `custom_query`).
        files: the `FileRecord`s to process.
        settings: runtime config — `ollama_fast_model`/`ollama_model`
            (which model to call), `ollama_max_concurrency` (thread pool
            size), and whatever `extract_any()` itself needs.
        on_progress: progress callback, see above.
        client: an `OllamaClient`-shaped object to use instead of a real
            one — **required for this segment's own test, not optional
            polish** (PLAN.md's own wording). Not part of PLAN.md's
            literal 4-argument `run_fast_pass` signature, but added as
            an optional 5th parameter for dependency injection: real
            callers (Segment 10's orchestrator) can omit it and get a
            real `OllamaClient(settings)`; tests pass a fake fixed-
            response one. See PROJECT_STATE.md.
    """
    targets = _targets_for_job(job)
    if not targets:
        logger.warning(
            "fast pass: job %s has no intents and no custom query selected — nothing to do", job.id
        )
        return
    if not files:
        logger.info("fast pass: job %s — no files to process", job.id)
        return

    active_client = client if client is not None else OllamaClient(settings)
    fast_model = settings.ollama_fast_model  # may be None — OllamaClient falls back to ollama_model itself
    model_used = settings.ollama_fast_model or settings.ollama_model
    ollama_down = threading.Event()

    def _run_one(record: FileRecord) -> tuple[FileRecord, AnalysisResult | None]:
        if ollama_down.is_set():
            logger.debug(
                "fast pass: skipping %s — Ollama already detected unreachable this run", record.display_path
            )
            return record, None
        return _process_file(record, job, settings, active_client, targets, model_used, fast_model, ollama_down)

    max_workers = max(1, settings.ollama_max_concurrency)
    logger.info(
        "fast pass: job %s — %d file(s), %d target(s), max_workers=%d",
        job.id,
        len(files),
        len(targets),
        max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_one, record): record for record in files}
        for future in as_completed(futures):
            original_record = futures[future]
            try:
                updated_record, best_result = future.result()
            except Exception as exc:  # pragma: no cover - _process_file catches everything itself already
                logger.error(
                    "fast pass: unexpected top-level failure for %s: %s", original_record.display_path, exc
                )
                original_record.status = "error"
                original_record.error_message = f"unexpected fast-pass failure: {exc}"
                try:
                    db.upsert_file(original_record)
                except Exception:
                    logger.exception("fast pass: could not persist error status for %s", original_record.display_path)
                updated_record, best_result = original_record, None
            on_progress(updated_record, best_result)

    logger.info("fast pass: job %s complete", job.id)


# --------------------------------------------------------------------------
# Per-job / per-file target selection
# --------------------------------------------------------------------------


def _targets_for_job(job: ScanJob) -> list[str]:
    """Every distinct thing the fast pass should check each file
    against, for this job: every `intent_id` in `job.intent_ids`
    (deduped, order preserved — this includes `"interesting"` if the
    user selected it, no special-casing needed, see PROJECT_STATE.md's
    Segment 7 notes), plus one extra sentinel target (`_CUSTOM_TARGET`)
    if `job.custom_query` is a non-blank free-text query. A blank/
    whitespace-only `custom_query` is treated as "no custom target at
    all" (skipped entirely) rather than run with `build_custom_prompt`'s
    own generic fallback text on every single file."""
    seen: set[str] = set()
    targets: list[str] = []
    for intent_id in job.intent_ids:
        if intent_id not in seen:
            seen.add(intent_id)
            targets.append(intent_id)
    if job.custom_query and job.custom_query.strip():
        targets.append(_CUSTOM_TARGET)
    return targets


# --------------------------------------------------------------------------
# Per-file processing
# --------------------------------------------------------------------------


def _process_file(
    record: FileRecord,
    job: ScanJob,
    settings: Settings,
    client: OllamaClient,
    targets: list[str],
    model_used: str,
    fast_model: str | None,
    ollama_down: threading.Event,
) -> tuple[FileRecord, AnalysisResult | None]:
    """Runs every target in `targets` against one file, persisting
    whatever `AnalysisResult`s come out of it and the file's updated
    status, and returning the `(record, best_result)` pair
    `run_fast_pass` passes to `on_progress`."""
    try:
        payload = extract_any(record, settings, detail_level="fast")
    except Exception as exc:  # defensive — extract_any() is documented to never raise
        logger.error("fast pass: extract_any() raised unexpectedly for %s: %s", record.display_path, exc)
        record.status = "error"
        record.error_message = f"content extraction failed: {exc}"
        db.upsert_file(record)
        return record, None

    results: list[AnalysisResult] = []
    for target in targets:
        if ollama_down.is_set():
            break
        intent_id = None if target == _CUSTOM_TARGET else target
        try:
            if intent_id is None:
                system, user = build_custom_prompt(job.custom_query or "", payload, detailed=False)
            else:
                system, user = build_fast_prompt(intent_id, payload)
            response = _call_model(client, system, user, payload, fast_model)
            result = _build_result(
                response=response, record=record, job=job, intent_id=intent_id, model_used=model_used
            )
            db.upsert_result(result)
            results.append(result)
        except OllamaUnavailableError as exc:
            logger.error(
                "fast pass: Ollama unreachable while processing %s (target=%s): %s — "
                "aborting remaining targets/files this run",
                record.display_path,
                target,
                exc,
            )
            record.error_message = f"Ollama unreachable: {exc}"
            ollama_down.set()
            break
        except Exception as exc:
            logger.warning("fast pass: target %s failed for %s: %s", target, record.display_path, exc)

    if results:
        record.status = "fast_done"
        record.error_message = None
    else:
        record.status = "error"
        record.error_message = record.error_message or "no target produced a usable fast-pass result"
    db.upsert_file(record)

    best = max(results, key=lambda r: r.relevance_score) if results else None
    return record, best


def _call_model(
    client: OllamaClient,
    system: str,
    user: str,
    payload: ContentPayload,
    model: str | None,
) -> dict:
    """Picks `generate_vision` vs `generate_text` from `payload.kind` —
    PROJECT_STATE.md is explicit that this choice belongs to Segment
    8/9, not `prompts.py`: `"image"`/`"mixed"` use vision (images travel
    via `images_b64`, never embedded in the prompt text), anything else
    (`"text"`/`"metadata_only"`) is text-only."""
    if payload.kind in ("image", "mixed"):
        response = client.generate_vision(system=system, prompt=user, images_b64=payload.images_b64, model=model)
    else:
        response = client.generate_text(system=system, prompt=user, model=model)
    if isinstance(response, str):
        # Every call here passes json_mode's default (True), so this
        # shouldn't happen in practice — guarded rather than trusted blindly.
        return {"_parse_error": True, "raw": response}
    return response


# --------------------------------------------------------------------------
# Response -> AnalysisResult
# --------------------------------------------------------------------------


def _coerce_score(value: object) -> float:
    """Best-effort float coercion, clamped to AnalysisResult's documented
    0..1 range. Anything unparseable (missing key, `_parse_error`
    response, a string, `None`, ...) becomes `0.0` rather than raising —
    see module docstring's note on treating a parse error the same as a
    response simply missing its keys."""
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if score != score:  # NaN
        return 0.0
    return max(0.0, min(1.0, score))


def _coerce_tags(value: object) -> list[str]:
    """`tags` is only ever trusted if the model actually returned a
    list; anything else (missing, a string, a dict, ...) becomes `[]`."""
    if not isinstance(value, list):
        return []
    return [str(tag) for tag in value if str(tag).strip()]


def _build_result(
    *,
    response: dict,
    record: FileRecord,
    job: ScanJob,
    intent_id: str | None,
    model_used: str,
) -> AnalysisResult:
    """Constructs the real `AnalysisResult` from the 5-key subset the
    model was actually asked for (see module docstring) plus everything
    else the model has no way to know, filled in from `record`/`job`/
    context. Never raises — a `_parse_error`/malformed/sparse `response`
    just yields conservative defaults (0 relevance, empty tags/strings,
    not flagged), logged so it's visible rather than silently absorbed."""
    if response.get("_parse_error"):
        logger.warning(
            "fast pass: unparseable model response for %s (target=%s)",
            record.display_path,
            intent_id or "custom",
        )
    return AnalysisResult(
        id=str(uuid.uuid4()),
        file_id=record.id,
        job_id=job.id,
        pass_type="fast",
        intent_id=intent_id,
        tags=_coerce_tags(response.get("tags")),
        relevance_score=_coerce_score(response.get("relevance_score")),
        summary=str(response.get("summary") or "").strip(),
        reasoning=str(response.get("reasoning") or "").strip(),
        flagged_interesting=bool(response.get("flagged_interesting", False)),
        model_used=response.get("_provider_used") or model_used,
        created_at=time.time(),
    )


# --------------------------------------------------------------------------
# Self-test: `python -m app.pipeline.fast_pass --selftest`
# --------------------------------------------------------------------------


class _FakeOllamaClient:
    """Fixed-response fake standing in for a real `OllamaClient` — PLAN.
    md's own "Watch out for" note on Segment 8: "dependency injection
    for the client is required for this segment's own test, not
    optional polish." Duck-typed (matches `generate_text`/
    `generate_vision`'s call shape) rather than subclassing the real
    class, since no network/Settings should be needed just to construct
    one.

    Dispatch is marker-based and deliberately simple:
      - `unavailable_on`: if any marker string is found in the file's
        rendered content (the `prompt`/`user` text — i.e. this is a
        per-*file* trigger, the same regardless of which target is
        being asked about), raises `OllamaUnavailableError`.
      - `fail_on`: same per-file matching, raises a plain `RuntimeError`
        (simulating an ordinary per-call failure, e.g. an HTTP error).
      - `responses`: matched against `system` instead (which is where a
        target's label/template text lives — e.g. "Financial Records"
        or "Target (user-defined)") — a per-*target* canned response.
      - Anything matching neither falls back to a low-relevance default
        response rather than raising, so untouched combinations degrade
        harmlessly.
    """

    def __init__(
        self,
        responses: dict[str, dict] | None = None,
        fail_on: set[str] | None = None,
        unavailable_on: set[str] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.fail_on = fail_on or set()
        self.unavailable_on = unavailable_on or set()
        self.calls: list[tuple[str, str | None]] = []  # (transport, model)

    def generate_text(self, system: str, prompt: str, json_mode: bool = True, model: str | None = None):
        return self._respond("text", system, prompt, model)

    def generate_vision(
        self, system: str, prompt: str, images_b64: list[str], json_mode: bool = True, model: str | None = None
    ):
        return self._respond("vision", system, prompt, model)

    def _respond(self, transport: str, system: str, prompt: str, model: str | None) -> dict:
        self.calls.append((transport, model))
        for marker in self.unavailable_on:
            if marker in prompt:
                raise OllamaUnavailableError(f"simulated Ollama outage (marker={marker!r})")
        for marker in self.fail_on:
            if marker in prompt:
                raise RuntimeError(f"simulated per-call failure (marker={marker!r})")
        for marker, response in self.responses.items():
            if marker in system:
                return dict(response)
        return {
            "relevance_score": 0.1,
            "tags": [],
            "summary": "nothing notable",
            "reasoning": "no marker matched in self-test fake",
            "flagged_interesting": False,
        }


def _run_selftest() -> None:  # noqa: C901 - linear fixture-building test, not worth splitting up
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as work_dir:
        app_data_dir = os.path.join(work_dir, "data")
        os.makedirs(app_data_dir, exist_ok=True)
        settings = Settings(
            ollama_host="http://localhost:11434",
            ollama_model="llama3.2-vision",
            ollama_fast_model=None,
            ollama_detailed_model=None,
            ollama_timeout_seconds=5,
            ollama_max_concurrency=2,
            app_data_dir=app_data_dir,
            max_archive_depth=3,
            max_archive_total_mb=2000,
            max_file_read_mb=1,
            app_port=8731,
        )
        db.init_db(os.path.join(app_data_dir, "selftest.db"))

        def make_record(path: str, file_id: str) -> FileRecord:
            ext = os.path.splitext(path)[1].lower()
            return FileRecord(
                id=file_id,
                path=path,
                display_path=path,
                source_archive_id=None,
                ext=ext,
                size_bytes=os.path.getsize(path),
                mime_type=None,
                mtime=time.time(),
                is_from_archive=False,
                status="pending",
                error_message=None,
            )

        print("Segment 8 self-test — building throwaway fixtures...")

        # --- Scenario 1: happy path + one fully-failing file, multiple
        # targets (two intents, incl. "interesting", + a custom query),
        # default concurrency. -------------------------------------------
        bank_path = os.path.join(work_dir, "statement.txt")
        with open(bank_path, "w", encoding="utf-8") as f:
            f.write("Bank of Example -- March statement. Balance: $2,481.09.\n")

        boring_path = os.path.join(work_dir, "notes.txt")
        with open(boring_path, "w", encoding="utf-8") as f:
            f.write("Grocery list: eggs, milk, bread.\n")

        flaky_path = os.path.join(work_dir, "flaky.txt")
        with open(flaky_path, "w", encoding="utf-8") as f:
            f.write("marker:simulate-per-call-failure for every target on this file.\n")

        bank_record = make_record(bank_path, "file-bank")
        boring_record = make_record(boring_path, "file-boring")
        flaky_record = make_record(flaky_path, "file-flaky")

        job1 = ScanJob(
            id="job-selftest-1",
            roots=[work_dir],
            intent_ids=["financial", "interesting"],
            custom_query="anything about my old startup codename Falcon",
            status="fast_pass",
            counts={},
            output_folder=os.path.join(work_dir, "output-1"),
            report_path=None,
            created_at=time.time(),
        )

        client1 = _FakeOllamaClient(
            responses={
                "Financial Records": {
                    "relevance_score": 0.9,
                    "tags": ["bank_statement"],
                    "summary": "Bank statement.",
                    "reasoning": "Mentions balance and bank name.",
                    "flagged_interesting": False,
                },
                "Interesting Things": {
                    "relevance_score": 0.2,
                    "tags": [],
                    "summary": "Nothing unusual.",
                    "reasoning": "Ordinary file.",
                    "flagged_interesting": False,
                },
                "user-defined": {
                    "relevance_score": 0.05,
                    "tags": [],
                    "summary": "No mention of Falcon.",
                    "reasoning": "No match found.",
                    "flagged_interesting": False,
                },
            },
            fail_on={"simulate-per-call-failure"},
        )

        progress_calls: list[tuple[str, str | None]] = []

        def on_progress(record: FileRecord, result: AnalysisResult | None) -> None:
            progress_calls.append((record.id, result.id if result else None))

        run_fast_pass(job1, [bank_record, boring_record, flaky_record], settings, on_progress, client=client1)

        assert len(progress_calls) == 3, f"expected 1 on_progress call per file, got {len(progress_calls)}"
        assert {c[0] for c in progress_calls} == {"file-bank", "file-boring", "file-flaky"}

        bank_results = db.get_results("job-selftest-1")
        by_file: dict[str, list[AnalysisResult]] = {}
        for r in bank_results:
            by_file.setdefault(r.file_id, []).append(r)

        assert len(by_file.get("file-bank", [])) == 3, "bank file should have 3 results (2 intents + custom)"
        assert len(by_file.get("file-boring", [])) == 3, "boring file should have 3 results"
        assert "file-flaky" not in by_file, "flaky file's every target failed -> zero persisted results"

        bank_best = max(by_file["file-bank"], key=lambda r: r.relevance_score)
        assert bank_best.intent_id == "financial" and bank_best.relevance_score == 0.9

        # intent_id shape: "interesting" is a real intent_id, custom is None.
        bank_intent_ids = {r.intent_id for r in by_file["file-bank"]}
        assert bank_intent_ids == {"financial", "interesting", None}, bank_intent_ids

        stored_bank = db.get_file("file-bank")
        stored_boring = db.get_file("file-boring")
        stored_flaky = db.get_file("file-flaky")
        assert stored_bank is not None and stored_bank.status == "fast_done"
        assert stored_boring is not None and stored_boring.status == "fast_done"
        assert stored_flaky is not None and stored_flaky.status == "error"
        assert stored_flaky.error_message, "flaky file should have an error_message explaining the failure"

        # best_result passed to on_progress for the bank file must be its
        # single highest-scoring result (financial, 0.9).
        progress_by_id = dict(progress_calls)
        assert progress_by_id["file-bank"] == bank_best.id
        assert progress_by_id["file-flaky"] is None

        print("  [scenario 1] multi-target/multi-file happy path + one fully-failing file: OK")

        # --- Scenario 2: total-outage short-circuit, single-threaded so
        # ordering is deterministic. --------------------------------------
        settings_seq = Settings(**{**settings.__dict__, "ollama_max_concurrency": 1})

        normal1_path = os.path.join(work_dir, "normal1.txt")
        with open(normal1_path, "w", encoding="utf-8") as f:
            f.write("An entirely ordinary file, processed before the outage.\n")
        outage_path = os.path.join(work_dir, "outage.txt")
        with open(outage_path, "w", encoding="utf-8") as f:
            f.write("marker:simulate-ollama-outage triggers here.\n")
        normal2_path = os.path.join(work_dir, "normal2.txt")
        with open(normal2_path, "w", encoding="utf-8") as f:
            f.write("Another ordinary file, submitted after the outage file.\n")

        normal1_record = make_record(normal1_path, "file-normal1")
        outage_record = make_record(outage_path, "file-outage")
        normal2_record = make_record(normal2_path, "file-normal2")

        job2 = ScanJob(
            id="job-selftest-2",
            roots=[work_dir],
            intent_ids=["cleanup"],
            custom_query=None,
            status="fast_pass",
            counts={},
            output_folder=os.path.join(work_dir, "output-2"),
            report_path=None,
            created_at=time.time(),
        )

        client2 = _FakeOllamaClient(
            responses={
                "Duplicates & Reclaimable Space": {
                    "relevance_score": 0.3,
                    "tags": [],
                    "summary": "Ordinary file.",
                    "reasoning": "Nothing to reclaim.",
                    "flagged_interesting": False,
                },
            },
            unavailable_on={"simulate-ollama-outage"},
        )

        progress_calls_2: dict[str, tuple[FileRecord, AnalysisResult | None]] = {}

        def on_progress_2(record: FileRecord, result: AnalysisResult | None) -> None:
            progress_calls_2[record.id] = (record, result)

        run_fast_pass(
            job2,
            [normal1_record, outage_record, normal2_record],
            settings_seq,
            on_progress_2,
            client=client2,
        )

        assert len(progress_calls_2) == 3, "on_progress must still fire once per file, even skipped ones"
        assert progress_calls_2["file-normal1"][1] is not None, "file before the outage should have succeeded"
        assert progress_calls_2["file-outage"][1] is None
        assert progress_calls_2["file-normal2"][1] is None, "file submitted after the outage should be skipped"

        assert len(client2.calls) == 2, f"expected exactly 2 calls (normal1 + outage), got {len(client2.calls)}"

        stored_normal1 = db.get_file("file-normal1")
        stored_outage = db.get_file("file-outage")
        assert stored_normal1 is not None and stored_normal1.status == "fast_done"
        assert stored_outage is not None and stored_outage.status == "error"
        assert "unreachable" in (stored_outage.error_message or "").lower()
        # file-normal2 was skipped before any processing began, so nothing
        # ever wrote it to the db at all -- it simply doesn't exist there
        # yet (same as any file scanner.py found but nothing has processed
        # since). The record object itself was never mutated either.
        assert db.get_file("file-normal2") is None
        assert progress_calls_2["file-normal2"][0].status == "pending"
        assert db.get_results("job-selftest-2", intent_id="cleanup") and len(
            db.get_results("job-selftest-2")
        ) == 1, "only file-normal1 should have produced a result"

        print("  [scenario 2] total-outage short-circuit skips not-yet-started files: OK")

        # --- Scenario 3: a job with no intents and no custom query is a
        # no-op (nothing to check files against). --------------------------
        job3 = ScanJob(
            id="job-selftest-3",
            roots=[work_dir],
            intent_ids=[],
            custom_query="   ",
            status="fast_pass",
            counts={},
            output_folder=os.path.join(work_dir, "output-3"),
            report_path=None,
            created_at=time.time(),
        )
        noop_calls: list[str] = []
        run_fast_pass(job3, [normal1_record], settings, lambda r, res: noop_calls.append(r.id), client=client2)
        assert noop_calls == [], "job with no intents and a blank custom_query should do nothing"
        print("  [scenario 3] job with zero targets is a no-op: OK")

    print("\nOK")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker fast-pass engine utility")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run the fast pass against throwaway fixtures with a fake OllamaClient",
    )
    args = parser.parse_args()

    if args.selftest:
        _run_selftest()
    else:
        parser.print_help()
