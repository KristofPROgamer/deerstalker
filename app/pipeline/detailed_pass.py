"""Detailed-Pass Engine — PLAN.md Segment 9.

The expensive second look, spent only where the fast pass says it's
earned: `select_candidates()` picks which files cleared the bar,
`run_detailed_pass()` re-examines exactly those files, one detailed
model call per (file, target) pair that itself cleared the bar — using
`build_detailed_prompt()` (or `build_custom_prompt(..., detailed=True)`
for the free-text target) with the matching fast-pass result threaded
in as context, per PLAN.md's spec.

Depends on Segments 1 (config/models/db/log), 2 (ollama_client), 5+6
(`extract_any()`), 7 (intents/prompts), and 8 (fast_pass — not imported
from, see "Deliberately not shared with fast_pass.py" below, but this
module mirrors its structure/conventions throughout so the two passes
behave predictably side by side). Read PROJECT_STATE.md's Segment 8
notes (in particular "Segment 8/9, read this before wiring prompts.py
in" and the two "Segment 9" open flags about DI and the total-outage
short-circuit) before changing anything here.

**Load-bearing contract inherited from Segment 7 (see PROJECT_STATE.md,
same as fast_pass.py):** the model is only ever asked for a 5-key
subset of `AnalysisResult` (`relevance_score`/`tags`/`summary`/
`reasoning`/`flagged_interesting`). This module fills in the rest
(`id`/`file_id`/`job_id`/`pass_type`/`intent_id`/`model_used`/
`created_at`) itself — see `_build_result()`.

**What "earned" means here, at two levels:**
  - *File* level (`select_candidates`): a file's best fast-pass
    `relevance_score` across all its targets clears `threshold`, OR
    any of its fast-pass targets came back `flagged_interesting=True`
    (that flag exists precisely to catch a borderline-score-but-still-
    worth-a-look result — see `select_candidates()`'s docstring).
  - *Target* level (`run_detailed_pass`, via `_qualifying_fast_results()`):
    within a selected file, only the specific (file, intent) fast
    results that themselves clear the same bar get a detailed call —
    not every target the file happened to run through the fast pass.
    A file selected purely because one intent scored high doesn't
    burn a second, expensive call on a different intent that scored
    low on that same file.

**Deliberately not shared with fast_pass.py — small helpers
(`_call_model`/`_coerce_score`/`_coerce_tags`/`_build_result`-shape)
are duplicated here rather than imported from `app.pipeline.fast_pass`,
even though the logic is near-identical.** These are private
(underscore-prefixed) names in a sibling segment's module; importing
them would couple two segments meant to be buildable/reviewable in
isolation (PLAN.md §1) to each other's internals rather than to a
documented public interface. The duplicated logic is small, stable,
and self-contained enough that the coupling cost outweighs the
DRY savings — flagged here rather than done silently.

**Historical detail-level note (superseded by the implementation below):**
the extractor interfaces now accept `detail_level`, and this module passes
`"detailed"` when re-extracting candidate files. The retained historical
text records why the feature was needed; it no longer describes current
behavior.

**The `detail_level` gap PLAN.md's own Segment 9 spec anticipated
("this segment will likely want an optional detail_level param added
to the Segment 5/6 extractor interface... call that out explicitly"):**
neither `text_docs.extract()` nor `media_binary.extract()`/
`extract_any()` gained such a param (this segment didn't add one —
that would be editing another segment's file, against this session's
brief). Instead, `_build_detailed_settings()` reuses the *existing*
`Settings.max_file_read_mb` knob those extractors already read from,
multiplied up for the duration of the detailed pass's own
`extract_any()` call — genuinely fuller text/PDF/DOCX/PPTX excerpts,
achieved with zero changes to Segment 5/6's files. It does **not**
get "larger/more images" (Pillow's downscale target, JPEG quality, and
how many PDF pages get rendered are module-level constants in
Segment 5/6, not `Settings`-driven) or a bigger CSV/XLSX row cap
(also hardcoded independently of `max_file_read_mb`). See that
function's docstring for the precise boundary, and PROJECT_STATE.md's
open-flags section — a real fix still wants that `detail_level` param
added to Segment 5/6 by whoever touches those files next.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import uuid

# Extractors now receive detail_level="detailed" below, in addition to this
# pass's larger Settings text budget. This increases image/PDF/tabular fidelity.
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from app.config import Settings
from app.extractors.media_binary import extract_any
from app.log import logger
from app.models import AnalysisResult, ContentPayload, FileRecord, ScanJob
from app.ollama_client import OllamaClient, OllamaUnavailableError
from app.prompts import build_custom_prompt, build_detailed_prompt

# Shared default so a caller that lets both select_candidates() and
# run_detailed_pass() fall back to their defaults gets a consistent bar
# at both the file-selection and target-selection levels. A caller that
# overrides one should generally override the other to match — see
# PROJECT_STATE.md's note to Segment 10 about this.
_DEFAULT_RELEVANCE_THRESHOLD = 0.5

# How much to multiply settings.max_file_read_mb by for the detailed
# pass's own extract_any() call — see module docstring's "detail_level
# gap" section for exactly what this does and doesn't improve.
_DETAIL_TEXT_BUDGET_MULTIPLIER = 4


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def select_candidates(
    job_id: str,
    threshold: float = _DEFAULT_RELEVANCE_THRESHOLD,
    top_k: int | None = None,
) -> list[FileRecord]:
    """Which files from `job_id`'s fast pass have earned a detailed
    look.

    A file qualifies if either is true, across its fast-pass results:
      - its best (highest) `relevance_score` is `>= threshold`, or
      - any one of its fast-pass targets came back
        `flagged_interesting=True` (regardless of that target's own
        score — that flag is exactly for "worth a second look even if
        the score alone wouldn't say so").

    Only `pass_type="fast"` results are considered — a previous
    detailed-pass run's own `pass_type="detailed"` results for this
    job (if `run_detailed_pass` already ran once) never feed back into
    this selection.

    Ordering: `flagged_interesting` files first, then by descending
    best `relevance_score`, then by `file_id` for a stable tiebreak.
    `top_k`, if given, keeps only that many from the front of this
    ordering — so a flagged_interesting-but-low-score file is
    protected from being cut by `top_k` before a same-score,
    not-flagged file would be.

    Args:
        job_id: the `ScanJob.id` whose fast-pass results to look at.
        threshold: minimum best relevance_score to qualify on score
            alone (default `0.5`, shared with `run_detailed_pass`'s
            own default — see module-level `_DEFAULT_RELEVANCE_THRESHOLD`).
        top_k: if given, cap the number of returned candidates to the
            top `top_k` by the ordering above. `None` (default) returns
            every qualifying file.

    Returns:
        The qualifying `FileRecord`s, in the order described above.
        Empty list if the job has no fast-pass results yet, or none
        qualify. A file that has qualifying results but no matching
        `FileRecord` row (shouldn't happen in normal operation — see
        `db.upsert_file`/`db.upsert_result` both being called from the
        same fast-pass file-processing step) is logged and skipped
        rather than raising.
    """
    fast_by_file = _fast_results_by_file(job_id)
    if not fast_by_file:
        return []

    best_score: dict[str, float] = {}
    any_interesting: set[str] = set()
    for file_id, results in fast_by_file.items():
        best_score[file_id] = max(r.relevance_score for r in results)
        if any(r.flagged_interesting for r in results):
            any_interesting.add(file_id)

    selected_ids = [
        file_id for file_id, score in best_score.items() if score >= threshold or file_id in any_interesting
    ]
    selected_ids.sort(key=lambda fid: (fid not in any_interesting, -best_score[fid], fid))

    if top_k is not None:
        selected_ids = selected_ids[: max(0, top_k)]

    candidates: list[FileRecord] = []
    for file_id in selected_ids:
        record = db.get_file(file_id)
        if record is None:
            logger.warning(
                "detailed pass: file_id %s has fast-pass results but no FileRecord in db — skipping as a candidate",
                file_id,
            )
            continue
        candidates.append(record)
    return candidates


def run_detailed_pass(
    job: ScanJob,
    candidates: list[FileRecord],
    settings: Settings,
    on_progress: Callable[[FileRecord, AnalysisResult | None], None],
    client: OllamaClient | None = None,
    threshold: float = _DEFAULT_RELEVANCE_THRESHOLD,
) -> None:
    """Run the detailed pass for `job` across `candidates` (typically
    `select_candidates(job.id, ...)`'s own output, though callers may
    pass any `FileRecord`s that already have `pass_type="fast"` results
    in the db for this job — a candidate with none, or none that
    qualify at `threshold`, is a documented no-op for that file, not an
    error; see below).

    Per candidate file: look up its own fast-pass results for this job
    (`db.get_results(job.id)`, filtered to `pass_type="fast"` and this
    file), keep only the ones that qualify at `threshold` (score
    `>= threshold` or `flagged_interesting`, deduped to the
    highest-scoring result per intent_id — see
    `_qualifying_fast_results()`), re-extract content via
    `extract_any()` using a higher-text-budget `Settings` copy (see
    `_build_detailed_settings()`), then for each qualifying target
    build the detailed prompt (`build_detailed_prompt()` for a
    registered intent id, `build_custom_prompt(..., detailed=True)`
    for the free-text target, i.e. `intent_id is None`), call the
    model, parse the response into an `AnalysisResult(pass_type=
    "detailed")`, and `db.upsert_result()` it.

    `FileRecord.status` becomes `"detailed_done"` (>=1 target produced
    a usable result) or `"error"` (extraction blew up, or every
    attempted target failed) and is persisted via `db.upsert_file()` —
    same convention `fast_pass.py` uses for `"fast_done"`/`"error"`.
    **Exception:** a candidate with zero *qualifying* targets (nothing
    at/above `threshold`, nothing flagged) is left completely
    untouched — no `db.upsert_file()` call, status stays whatever it
    already was (typically `"fast_done"`) — since there's genuinely
    nothing to do for it, not a failure. `on_progress` still fires for
    it, with `None`.

    Runs a `ThreadPoolExecutor` bounded by
    `settings.ollama_max_concurrency`, same as `fast_pass.py`. One
    file's exception never kills the pass. `on_progress` is called
    exactly once per entry in `candidates`, carrying the single
    highest-`relevance_score` `AnalysisResult` produced for that file
    this pass, or `None`.

    **Total-outage short-circuit (same judgment call as `fast_pass.py`
    — see PROJECT_STATE.md's Segment 8 notes and its "Segment 9" open
    flag about this):** an `OllamaUnavailableError` from any target's
    model call aborts the remaining targets for that file and skips
    every not-yet-started candidate outright (left completely
    untouched — never upserted to `db`), rather than burning a full
    retry sequence per file for a server that's already known to be
    down. `on_progress` still fires for every candidate, including
    skipped ones.

    Args:
        job: the `ScanJob` this pass belongs to (supplies `id`,
            `custom_query` — `job.intent_ids` is not consulted here;
            which targets get a detailed look is driven entirely by
            each file's own qualifying fast-pass results, not by the
            job's current intent selection).
        candidates: the `FileRecord`s to process — see above for what
            "qualifies" means at the target level within each one.
        settings: runtime config — `ollama_detailed_model`/
            `ollama_model`, `ollama_max_concurrency`, and
            `max_file_read_mb` (see `_build_detailed_settings()`).
        on_progress: progress callback, see above.
        client: an `OllamaClient`-shaped object to use instead of a
            real one — same dependency-injection pattern
            `fast_pass.py` established (PLAN.md's own wording calls
            this "required for this segment's own test, not optional
            polish"; PROJECT_STATE.md flagged that Segment 9 would
            need the same hook). Optional 6th kwarg beyond PLAN.md's
            literal 4-argument signature — see "Deviations" in this
            segment's PROJECT_STATE.md update.
        threshold: the same per-target qualification bar described
            above; defaults to the same value `select_candidates()`
            defaults to. If a caller passed a non-default `threshold`
            to `select_candidates()`, it should pass the *same* value
            here, or a candidate that only barely cleared a lowered
            file-level bar may end up with zero qualifying targets at
            this function's own (default) bar and become a no-op.
    """
    if not candidates:
        logger.info("detailed pass: job %s — no candidates to process", job.id)
        return

    active_client = client if client is not None else OllamaClient(settings)
    detailed_model = settings.ollama_detailed_model  # may be None — OllamaClient falls back to ollama_model itself
    model_used = settings.ollama_detailed_model or settings.ollama_model
    detailed_settings = _build_detailed_settings(settings)
    fast_by_file = _fast_results_by_file(job.id)
    ollama_down = threading.Event()

    def _run_one(record: FileRecord) -> tuple[FileRecord, AnalysisResult | None]:
        if ollama_down.is_set():
            logger.debug(
                "detailed pass: skipping %s — Ollama already detected unreachable this run", record.display_path
            )
            return record, None
        return _process_file(
            record,
            job,
            detailed_settings,
            active_client,
            fast_by_file.get(record.id, []),
            model_used,
            detailed_model,
            threshold,
            ollama_down,
        )

    max_workers = max(1, settings.ollama_max_concurrency)
    logger.info(
        "detailed pass: job %s — %d candidate(s), max_workers=%d",
        job.id,
        len(candidates),
        max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_one, record): record for record in candidates}
        for future in as_completed(futures):
            original_record = futures[future]
            try:
                updated_record, best_result = future.result()
            except Exception as exc:  # pragma: no cover - _process_file catches everything itself already
                logger.error(
                    "detailed pass: unexpected top-level failure for %s: %s", original_record.display_path, exc
                )
                original_record.status = "error"
                original_record.error_message = f"unexpected detailed-pass failure: {exc}"
                try:
                    db.upsert_file(original_record)
                except Exception:
                    logger.exception(
                        "detailed pass: could not persist error status for %s", original_record.display_path
                    )
                updated_record, best_result = original_record, None
            on_progress(updated_record, best_result)

    logger.info("detailed pass: job %s complete", job.id)


# --------------------------------------------------------------------------
# Which fast results this job has for each file / which of those qualify
# --------------------------------------------------------------------------


def _fast_results_by_file(job_id: str) -> dict[str, list[AnalysisResult]]:
    """All `pass_type="fast"` `AnalysisResult`s for `job_id`, grouped by
    `file_id`. Shared by `select_candidates()` and `run_detailed_pass()`
    so "which results count as fast-pass results" (this filter) lives
    in exactly one place."""
    by_file: dict[str, list[AnalysisResult]] = {}
    for r in db.get_results(job_id):
        if r.pass_type != "fast":
            continue
        by_file.setdefault(r.file_id, []).append(r)
    return by_file


def _qualifying_fast_results(fast_results: list[AnalysisResult], threshold: float) -> list[AnalysisResult]:
    """Which of one file's fast-pass results are worth a detailed
    target-level look: `relevance_score >= threshold`, or
    `flagged_interesting` regardless of score (see module docstring).

    Deduped to at most one result per `intent_id` (the free-text
    custom-query target's `intent_id` is `None`, same dedup key),
    keeping the highest-scoring one — protects against duplicate
    detailed calls for the same (file, target) pair if `fast_pass.py`
    was ever run more than once for this job without a fresh job id.
    Sorted by `relevance_score` descending, purely for a deterministic
    processing order (doesn't affect which targets qualify).
    """
    best_by_intent: dict[str | None, AnalysisResult] = {}
    for r in fast_results:
        if r.relevance_score < threshold and not r.flagged_interesting:
            continue
        current = best_by_intent.get(r.intent_id)
        if current is None or r.relevance_score > current.relevance_score:
            best_by_intent[r.intent_id] = r
    return sorted(best_by_intent.values(), key=lambda r: r.relevance_score, reverse=True)


# --------------------------------------------------------------------------
# "Higher fidelity" re-extraction — see module docstring's "detail_level gap"
# --------------------------------------------------------------------------


def _build_detailed_settings(settings: Settings) -> Settings:
    """A `Settings` copy with `max_file_read_mb` multiplied up
    (`_DETAIL_TEXT_BUDGET_MULTIPLIER`), used only for this module's own
    `extract_any()` calls — the real `settings` object callers passed
    in is never mutated (`Settings` is a plain, non-frozen `@dataclass`
    per `config.py`; `dataclasses.replace()` returns a fresh instance).

    **What this genuinely buys:** `text_docs.py`'s plain-text/code
    file byte cap, and its PDF/DOCX/PPTX character budget
    (`_char_budget()`, all reusing `settings.max_file_read_mb`
    directly per PROJECT_STATE.md), all come out larger on this
    re-extraction — a real "fuller text excerpt" as PLAN.md's Segment
    9 spec asks for, with zero changes to Segment 5's file.

    **What this does NOT buy** (documented rather than silently
    assumed — see module docstring's "detail_level gap"):
      - CSV/TSV's separate hardcoded 500-line cap and XLSX's hardcoded
        50-row/sheet cap in `text_docs.py` are independent of
        `max_file_read_mb` and untouched by this.
      - "larger/more images" — `media_binary.py`'s downscale target,
        JPEG quality, and how many PDF pages `text_docs.py` renders
        for a scanned PDF are all module-level constants, not
        `Settings`-driven. A detailed pass gets the exact same
        image(s) the fast pass would have for the same file.
    A real fix for the image/row-cap half of this wants an actual
    `detail_level` parameter added to Segment 5/6's `extract()`/
    `extract_any()` signatures — flagged forward, not solved here.
    """
    return dataclasses.replace(
        settings,
        max_file_read_mb=max(settings.max_file_read_mb, 1) * _DETAIL_TEXT_BUDGET_MULTIPLIER,
    )


# --------------------------------------------------------------------------
# Per-file processing
# --------------------------------------------------------------------------


def _process_file(
    record: FileRecord,
    job: ScanJob,
    detailed_settings: Settings,
    client: OllamaClient,
    fast_results: list[AnalysisResult],
    model_used: str,
    detailed_model: str | None,
    threshold: float,
    ollama_down: threading.Event,
) -> tuple[FileRecord, AnalysisResult | None]:
    """Runs a detailed target against every one of `fast_results` that
    qualifies at `threshold`, persisting whatever `AnalysisResult`s
    come out of it and the file's updated status, and returning the
    `(record, best_result)` pair `run_detailed_pass` passes to
    `on_progress`."""
    qualifying = _qualifying_fast_results(fast_results, threshold)
    if not qualifying:
        logger.info(
            "detailed pass: %s has no fast-pass result at/above threshold=%.2f and none flagged_interesting "
            "— nothing to deepen, leaving file untouched",
            record.display_path,
            threshold,
        )
        return record, None

    try:
        payload = extract_any(record, detailed_settings, detail_level="detailed")
    except Exception as exc:  # defensive — extract_any() is documented to never raise
        logger.error("detailed pass: extract_any() raised unexpectedly for %s: %s", record.display_path, exc)
        record.status = "error"
        record.error_message = f"content extraction failed: {exc}"
        db.upsert_file(record)
        return record, None

    results: list[AnalysisResult] = []
    for fast_result in qualifying:
        if ollama_down.is_set():
            break
        intent_id = fast_result.intent_id
        try:
            if intent_id is None:
                custom_query = (job.custom_query or "").strip()
                if not custom_query:
                    logger.warning(
                        "detailed pass: %s has a stored custom-query fast result but job.custom_query is "
                        "blank now — skipping that target",
                        record.display_path,
                    )
                    continue
                system, user = build_custom_prompt(job.custom_query, payload, detailed=True)
            else:
                system, user = build_detailed_prompt(intent_id, payload, fast_result.to_dict())
            response = _call_model(client, system, user, payload, detailed_model)
            result = _build_result(
                response=response, record=record, job=job, intent_id=intent_id, model_used=model_used
            )
            db.upsert_result(result)
            results.append(result)
        except OllamaUnavailableError as exc:
            logger.error(
                "detailed pass: Ollama unreachable while processing %s (intent=%s): %s — "
                "aborting remaining targets/files this run",
                record.display_path,
                intent_id,
                exc,
            )
            record.error_message = f"Ollama unreachable: {exc}"
            ollama_down.set()
            break
        except Exception as exc:
            logger.warning("detailed pass: target %s failed for %s: %s", intent_id, record.display_path, exc)

    if results:
        record.status = "detailed_done"
        record.error_message = None
    else:
        record.status = "error"
        record.error_message = record.error_message or "no target produced a usable detailed-pass result"
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
    same convention `fast_pass.py` uses (PROJECT_STATE.md: this choice
    belongs to Segment 8/9, not `prompts.py`): `"image"`/`"mixed"` use
    vision (images travel via `images_b64`, never embedded in the
    prompt text), anything else (`"text"`/`"metadata_only"`) is
    text-only."""
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
    response, a string, `None`, ...) becomes `0.0` rather than raising."""
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
    """Constructs the real `AnalysisResult` (`pass_type="detailed"`)
    from the 5-key subset the model was actually asked for (see module
    docstring) plus everything else the model has no way to know,
    filled in from `record`/`job`/context. Never raises — a
    `_parse_error`/malformed/sparse `response` just yields conservative
    defaults (0 relevance, empty tags/strings, not flagged), logged so
    it's visible rather than silently absorbed."""
    if response.get("_parse_error"):
        logger.warning(
            "detailed pass: unparseable model response for %s (target=%s)",
            record.display_path,
            intent_id or "custom",
        )
    return AnalysisResult(
        id=str(uuid.uuid4()),
        file_id=record.id,
        job_id=job.id,
        pass_type="detailed",
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
# Self-test: `python -m app.pipeline.detailed_pass --selftest`
# --------------------------------------------------------------------------


class _FakeOllamaClient:
    """Fixed-response fake standing in for a real `OllamaClient` — same
    shape/reasoning as `fast_pass.py`'s own `_FakeOllamaClient` (PLAN.
    md's "Watch out for" note applies equally to this segment: "the
    same [DI] pattern... required for this segment's own test, not
    optional polish"). Duplicated here rather than imported — see
    module docstring's "Deliberately not shared with fast_pass.py".

    Dispatch, identical scheme to fast_pass.py's fake:
      - `unavailable_on` / `fail_on`: matched against the rendered
        `prompt` (`user`) text — a per-*file* trigger.
      - `responses`: matched against `system` (where a target's
        label/template text lives, e.g. "Financial Records" or
        "Target (user-defined)") — a per-*target* canned response.
      - Anything matching neither falls back to a low-relevance
        default rather than raising.
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
            "relevance_score": 0.15,
            "tags": [],
            "summary": "nothing notable on closer inspection",
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

        print("Segment 9 self-test — building throwaway fixtures...")

        # --- _build_detailed_settings: confirm the text budget is
        # actually bumped, and the original settings object is untouched. ---
        detailed_settings = _build_detailed_settings(settings)
        assert detailed_settings.max_file_read_mb == settings.max_file_read_mb * _DETAIL_TEXT_BUDGET_MULTIPLIER
        assert settings.max_file_read_mb == 1, "the caller's own Settings object must not be mutated"
        print("  [detailed settings] max_file_read_mb multiplier applied without mutating caller's Settings: OK")

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
                status="fast_done",
                error_message=None,
            )

        def write(name: str, text: str) -> str:
            path = os.path.join(work_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return path

        def seed_fast_result(
            job_id: str, file_id: str, intent_id: str | None, score: float, flagged: bool = False
        ) -> AnalysisResult:
            result = AnalysisResult(
                id=str(uuid.uuid4()),
                file_id=file_id,
                job_id=job_id,
                pass_type="fast",
                intent_id=intent_id,
                tags=["seed"],
                relevance_score=score,
                summary="seeded fast-pass result",
                reasoning="seeded for detailed-pass self-test",
                flagged_interesting=flagged,
                model_used="llama3.2-vision",
                created_at=time.time(),
            )
            db.upsert_result(result)
            return result

        # --- Scenario 1: select_candidates() filtering + ordering ---------
        job1_id = "job-selftest-1"

        path_a = write("bank_high.txt", "Bank of Example -- March statement. Balance: $2,481.09.\n")
        path_b = write("odd_low_but_flagged.txt", "Grocery list, but oddly formatted like a ransom note.\n")
        path_c = write("legal_mid.txt", "Non-disclosure agreement between Acme Corp and Jane Doe.\n")
        path_d = write("boring_low.txt", "Grocery list: eggs, milk, bread.\n")

        record_a = make_record(path_a, "file-a")
        record_b = make_record(path_b, "file-b")
        record_c = make_record(path_c, "file-c")
        record_d = make_record(path_d, "file-d")
        for r in (record_a, record_b, record_c, record_d):
            db.upsert_file(r)

        seed_fast_result(job1_id, "file-a", "financial", 0.9)
        seed_fast_result(job1_id, "file-a", "interesting", 0.2)  # low, not flagged -> shouldn't qualify at target level
        seed_fast_result(job1_id, "file-b", "interesting", 0.3, flagged=True)  # below threshold, but flagged
        seed_fast_result(job1_id, "file-c", "legal", 0.6)
        seed_fast_result(job1_id, "file-d", "financial", 0.2)  # below threshold, not flagged -> excluded entirely

        candidates = select_candidates(job1_id, threshold=0.5)
        candidate_ids = [c.id for c in candidates]
        assert candidate_ids == ["file-b", "file-a", "file-c"], candidate_ids
        assert "file-d" not in candidate_ids, "below-threshold, non-flagged file must not be selected"
        print("  [scenario 1a] select_candidates: flagged-interesting-first, then score-desc, low-score excluded: OK")

        top1 = select_candidates(job1_id, threshold=0.5, top_k=1)
        assert [c.id for c in top1] == ["file-b"], "top_k=1 should keep the flagged_interesting file, not the highest score"
        print("  [scenario 1b] select_candidates: top_k protects the flagged_interesting file: OK")

        assert select_candidates("job-does-not-exist") == [], "unknown job_id should return an empty list, not raise"
        print("  [scenario 1c] select_candidates on a job with zero fast-pass results: OK")

        # --- Scenario 2: run_detailed_pass happy path — only qualifying
        # targets get a detailed call (file-a's low-scoring "interesting"
        # target must NOT get one), custom-query target included. --------
        seed_fast_result(job1_id, "file-a", None, 0.75)  # custom-query target, qualifies

        job1 = ScanJob(
            id=job1_id,
            roots=[work_dir],
            intent_ids=["financial", "legal", "interesting"],
            custom_query="anything about my old startup codename Falcon",
            status="detailed_pass",
            counts={},
            output_folder=os.path.join(work_dir, "output-1"),
            report_path=None,
            created_at=time.time(),
        )

        client1 = _FakeOllamaClient(
            responses={
                "Financial Records": {
                    "relevance_score": 0.95,
                    "tags": ["bank_statement", "confirmed"],
                    "summary": "Confirmed bank statement.",
                    "reasoning": "Balance and bank name both clearly visible on closer reading.",
                    "flagged_interesting": False,
                },
                "Interesting Things": {
                    "relevance_score": 0.6,
                    "tags": ["odd_formatting"],
                    "summary": "Grocery list formatted like a ransom note.",
                    "reasoning": "Word choice and layout mimic a ransom note despite mundane content.",
                    "flagged_interesting": True,
                },
                "Legal & Contracts": {
                    "relevance_score": 0.8,
                    "tags": ["nda"],
                    "summary": "Confirmed NDA between named parties.",
                    "reasoning": "Names Acme Corp and Jane Doe as parties to a non-disclosure agreement.",
                    "flagged_interesting": False,
                },
                "user-defined": {
                    "relevance_score": 0.1,
                    "tags": [],
                    "summary": "No mention of Falcon on closer reading either.",
                    "reasoning": "Confirmed no match.",
                    "flagged_interesting": False,
                },
            },
        )

        progress_calls: dict[str, tuple[FileRecord, AnalysisResult | None]] = {}

        def on_progress(record: FileRecord, result: AnalysisResult | None) -> None:
            progress_calls[record.id] = (record, result)

        run_detailed_pass(job1, candidates, settings, on_progress, client=client1, threshold=0.5)

        assert len(progress_calls) == 3, f"expected 1 on_progress call per candidate, got {len(progress_calls)}"

        detailed_results = [r for r in db.get_results(job1_id) if r.pass_type == "detailed"]
        by_file: dict[str, list[AnalysisResult]] = {}
        for r in detailed_results:
            by_file.setdefault(r.file_id, []).append(r)

        assert len(by_file.get("file-a", [])) == 2, (
            f"file-a should get exactly 2 detailed results (financial + custom, "
            f"NOT the low-scoring interesting target), got {len(by_file.get('file-a', []))}"
        )
        file_a_intent_ids = {r.intent_id for r in by_file["file-a"]}
        assert file_a_intent_ids == {"financial", None}, file_a_intent_ids

        assert len(by_file.get("file-b", [])) == 1, "file-b should get exactly 1 detailed result (interesting)"
        assert by_file["file-b"][0].intent_id == "interesting"
        assert by_file["file-b"][0].flagged_interesting is True

        assert len(by_file.get("file-c", [])) == 1, "file-c should get exactly 1 detailed result (legal)"
        assert by_file["file-c"][0].relevance_score == 0.8

        stored_a = db.get_file("file-a")
        stored_b = db.get_file("file-b")
        stored_c = db.get_file("file-c")
        assert stored_a is not None and stored_a.status == "detailed_done"
        assert stored_b is not None and stored_b.status == "detailed_done"
        assert stored_c is not None and stored_c.status == "detailed_done"

        # file-d was never a candidate -> must be completely untouched by this pass.
        stored_d = db.get_file("file-d")
        assert stored_d is not None and stored_d.status == "fast_done", "non-candidate file must be left alone"
        assert not any(r.pass_type == "detailed" for r in db.get_results(job1_id) if r.file_id == "file-d")

        # best_result passed to on_progress for file-a must be its single
        # highest-scoring detailed result (financial, 0.95).
        best_a = max(by_file["file-a"], key=lambda r: r.relevance_score)
        assert progress_calls["file-a"][1] is not None and progress_calls["file-a"][1].id == best_a.id

        print("  [scenario 2] target-level qualification (not just file-level) + custom-query target: OK")

        # --- Scenario 3: total-outage short-circuit, single-threaded so
        # ordering is deterministic. ----------------------------------------
        settings_seq = Settings(**{**settings.__dict__, "ollama_max_concurrency": 1})

        job2_id = "job-selftest-2"
        n1_path = write("outage_normal1.txt", "An entirely ordinary file, processed before the outage.\n")
        outage_path = write("outage_trigger.txt", "marker:simulate-ollama-outage triggers here.\n")
        n2_path = write("outage_normal2.txt", "Another ordinary file, submitted after the outage file.\n")

        n1_record = make_record(n1_path, "file-n1")
        outage_record = make_record(outage_path, "file-outage")
        n2_record = make_record(n2_path, "file-n2")
        for r in (n1_record, outage_record, n2_record):
            db.upsert_file(r)

        seed_fast_result(job2_id, "file-n1", "cleanup", 0.8)
        seed_fast_result(job2_id, "file-outage", "cleanup", 0.8)
        seed_fast_result(job2_id, "file-n2", "cleanup", 0.8)

        job2 = ScanJob(
            id=job2_id,
            roots=[work_dir],
            intent_ids=["cleanup"],
            custom_query=None,
            status="detailed_pass",
            counts={},
            output_folder=os.path.join(work_dir, "output-2"),
            report_path=None,
            created_at=time.time(),
        )

        client2 = _FakeOllamaClient(
            responses={
                "Duplicates & Reclaimable Space": {
                    "relevance_score": 0.55,
                    "tags": [],
                    "summary": "Confirmed reclaimable.",
                    "reasoning": "Still looks like clutter on closer inspection.",
                    "flagged_interesting": False,
                },
            },
            unavailable_on={"simulate-ollama-outage"},
        )

        progress_calls_2: dict[str, tuple[FileRecord, AnalysisResult | None]] = {}

        def on_progress_2(record: FileRecord, result: AnalysisResult | None) -> None:
            progress_calls_2[record.id] = (record, result)

        run_detailed_pass(
            job2,
            [n1_record, outage_record, n2_record],
            settings_seq,
            on_progress_2,
            client=client2,
            threshold=0.5,
        )

        assert len(progress_calls_2) == 3, "on_progress must still fire once per candidate, even skipped ones"
        assert progress_calls_2["file-n1"][1] is not None, "candidate before the outage should have succeeded"
        assert progress_calls_2["file-outage"][1] is None
        assert progress_calls_2["file-n2"][1] is None, "candidate submitted after the outage should be skipped"

        assert len(client2.calls) == 2, f"expected exactly 2 calls (n1 + outage), got {len(client2.calls)}"

        stored_n1 = db.get_file("file-n1")
        stored_outage = db.get_file("file-outage")
        assert stored_n1 is not None and stored_n1.status == "detailed_done"
        assert stored_outage is not None and stored_outage.status == "error"
        assert "unreachable" in (stored_outage.error_message or "").lower()
        # file-n2 was skipped before any processing began this pass -- its
        # FileRecord (already in the db from the seeding above, status
        # "fast_done") must be completely untouched, not flipped to error.
        stored_n2 = db.get_file("file-n2")
        assert stored_n2 is not None and stored_n2.status == "fast_done"
        assert not any(r.pass_type == "detailed" and r.file_id == "file-n2" for r in db.get_results(job2_id))

        print("  [scenario 3] total-outage short-circuit skips not-yet-started candidates: OK")

        # --- Scenario 4: a candidate with no qualifying fast result (e.g.
        # passed in directly by a caller without going through
        # select_candidates) is left completely untouched. -------------------
        job3_id = "job-selftest-3"
        untouched_path = write("no_qualifying_target.txt", "Ordinary file, weakly scored on everything.\n")
        untouched_record = make_record(untouched_path, "file-untouched")
        db.upsert_file(untouched_record)
        seed_fast_result(job3_id, "file-untouched", "cleanup", 0.2)  # below threshold, not flagged

        job3 = ScanJob(
            id=job3_id,
            roots=[work_dir],
            intent_ids=["cleanup"],
            custom_query=None,
            status="detailed_pass",
            counts={},
            output_folder=os.path.join(work_dir, "output-3"),
            report_path=None,
            created_at=time.time(),
        )

        untouched_progress: list[tuple[str, AnalysisResult | None]] = []
        run_detailed_pass(
            job3,
            [untouched_record],
            settings,
            lambda r, res: untouched_progress.append((r.id, res)),
            client=client2,
            threshold=0.5,
        )
        assert untouched_progress == [("file-untouched", None)]
        still_stored = db.get_file("file-untouched")
        assert still_stored is not None and still_stored.status == "fast_done", "no-qualifying-target file must be untouched"
        assert not any(r.pass_type == "detailed" for r in db.get_results(job3_id))
        print("  [scenario 4] candidate with zero qualifying targets is a clean no-op: OK")

        # --- Scenario 5: empty candidates list is a no-op. -------------------
        noop_progress: list[str] = []
        run_detailed_pass(job3, [], settings, lambda r, res: noop_progress.append(r.id), client=client2)
        assert noop_progress == [], "empty candidates list should call on_progress zero times"
        print("  [scenario 5] empty candidates list is a no-op: OK")

    print("\nOK")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker detailed-pass engine utility")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run select_candidates()/run_detailed_pass() against throwaway fixtures with a fake OllamaClient",
    )
    args = parser.parse_args()

    if args.selftest:
        _run_selftest()
    else:
        parser.print_help()
