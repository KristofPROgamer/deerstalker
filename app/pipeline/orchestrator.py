"""End-to-end scan-job coordination (PLAN.md Segment 10).

``Orchestrator`` is the single boundary between the HTTP API and the
individual pipeline stages.  It owns job lifecycle/status updates and the
small amount of in-memory state needed for live progress; durable state
continues to live in ``app.db``.

Archive extraction deserves special care.  ``extract_archive()`` yields
records whose paths live in a temporary directory.  This module keeps every
archive context open in an ``ExitStack`` until fast analysis, detailed
analysis, *and* output copying finish, so no later stage attempts to reopen a
deleted extracted file.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace

from app import archives, db, portable_devices, scanner
from app.config import Settings
from app.log import logger
from app.models import AnalysisResult, FileRecord, ScanJob
from app.output import finalize_output, preserve_staged_matches
from app.pipeline.detailed_pass import run_detailed_pass, select_candidates
from app.pipeline.fast_pass import run_fast_pass
from app.pipeline.worker_pool import MultiProviderClient, build_client


_MATCH_THRESHOLD = 0.5
_TERMINAL_STATUSES = {"done", "cancelled", "error"}


class _JobCancelled(Exception):
    """Internal control-flow signal; cancellation is an expected outcome."""


@dataclass
class _LiveJob:
    """Non-persistent details for a job currently owned by this process."""

    cancel_event: threading.Event = field(default_factory=threading.Event)
    current_file: str | None = None
    thread: threading.Thread | None = None


class Orchestrator:
    """Start and observe asynchronous local scan jobs.

    The three public methods are the Segment 10 contract.  ``client`` is an
    optional constructor-only dependency-injection seam for tests; production
    callers omit it and the existing pass engines instantiate ``OllamaClient``
    as documented.
    """

    def __init__(self, client: object | None = None) -> None:
        self._client = client
        self._jobs: dict[str, _LiveJob] = {}
        self._lock = threading.RLock()

    def start_job(
        self,
        roots: list[str],
        intent_ids: list[str],
        custom_query: str | None,
        settings: Settings,
    ) -> ScanJob:
        """Create a durable job and begin its pipeline in a background thread.

        This method intentionally returns immediately with a ``ScanJob``.  The
        returned status is normally ``"pending"``; callers should use
        :meth:`get_progress` for subsequent state.
        """
        normalized_roots = self._normalize_roots(roots)
        normalized_intents = self._dedupe_strings(intent_ids)
        normalized_query = custom_query.strip() if isinstance(custom_query, str) and custom_query.strip() else None
        job_id = str(uuid.uuid4())
        output_folder = os.path.abspath(os.path.join(settings.app_data_dir, "output", job_id))
        job = ScanJob(
            id=job_id,
            roots=normalized_roots,
            intent_ids=normalized_intents,
            custom_query=normalized_query,
            status="pending",
            counts={"total": 0, "scanned": 0, "matched": 0, "errored": 0},
            output_folder=output_folder,
            report_path=None,
            created_at=time.time(),
        )

        # db.init_db() accepts a path precisely so a caller-supplied Settings
        # object controls where this job's state is stored (rather than relying
        # on the process-wide Settings singleton inside app.db).
        db.init_db(os.path.join(settings.app_data_dir, "deerstalker.db"))
        db.create_job(job)

        live = _LiveJob()
        thread = threading.Thread(
            target=self._run_job,
            args=(job, settings, live),
            daemon=True,
            name=f"deerstalker-scan-{job_id[:8]}",
        )
        live.thread = thread
        with self._lock:
            self._jobs[job_id] = live
        thread.start()
        return job

    def get_progress(self, job_id: str) -> dict:
        """Return a JSON-ready snapshot for the progress UI.

        Raises ``KeyError`` for an unknown job.  Persisted jobs remain
        observable after an application restart; their ephemeral
        ``current_file``/``cancel_requested`` values then default to ``None``
        and ``False`` respectively.
        """
        job = db.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        with self._lock:
            live = self._jobs.get(job_id)
            current_file = live.current_file if live is not None else None
            cancel_requested = live.cancel_event.is_set() if live is not None else False
        return {
            "job_id": job.id,
            "status": job.status,
            "counts": self._complete_counts(job.counts),
            "current_file": current_file,
            "output_folder": job.output_folder,
            "report_path": job.report_path,
            "cancel_requested": cancel_requested,
        }

    def cancel(self, job_id: str) -> None:
        """Request cancellation; the running pipeline stops between files.

        Cancellation is idempotent.  A completed or failed job is left as-is;
        a running job immediately becomes observable as ``"cancelled"`` while
        its in-flight file is allowed to return before the worker exits.
        """
        with self._lock:
            job = db.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status in _TERMINAL_STATUSES:
                return
            live = self._jobs.setdefault(job_id, _LiveJob())
            live.cancel_event.set()
            job.status = "cancelled"
            db.update_job(job)

    # ------------------------------------------------------------------
    # Worker-pool resolution (PLAN_v1_5.md §7 Segment 22)
    # ------------------------------------------------------------------

    def _resolve_effective_client(self, settings: Settings) -> tuple[object | None, Settings]:
        """Resolve the client and concurrency-adjusted ``Settings`` both pass
        engines should use for this job. Called once per job (not once per
        chunk/batch) so the resulting pool -- when one is built -- is the
        single shared object Segment 21's own design assumes, per this
        plan's exact resolution order:

        An injected test client (``self._client``) always wins, matching
        every pre-Segment-22 call site's existing behavior. Otherwise
        ``worker_pool.build_client(settings)`` is used -- a real
        ``MultiProviderClient`` when at least one Ollama Cloud/Gemini key is
        configured, or ``None`` (today's single-local-Ollama default) when
        none are, so nobody who hasn't configured ``api_keys.py`` sees any
        behavior change. ``effective_concurrency`` mirrors the pool's real
        post-cap slot count only when the pool is actually active;
        otherwise it stays exactly ``settings.ollama_max_concurrency``,
        unchanged from pre-v1.5.
        """
        effective_client = self._client if self._client is not None else build_client(settings)
        effective_concurrency = (
            effective_client.slot_count
            if isinstance(effective_client, MultiProviderClient)
            else settings.ollama_max_concurrency
        )
        effective_settings = replace(settings, ollama_max_concurrency=effective_concurrency)
        return effective_client, effective_settings

    # ------------------------------------------------------------------
    # Background pipeline
    # ------------------------------------------------------------------

    def _run_job(self, job: ScanJob, settings: Settings, live: _LiveJob) -> None:
        """Run scan -> fast -> detailed -> output, persisting each transition."""
        try:
            self._set_status(job, live, "scanning")
            effective_client, effective_settings = self._resolve_effective_client(settings)

            if any(portable_devices.is_portable_root(root) for root in job.roots):
                self._run_portable_batched_job(job, effective_settings, live, effective_client)
                self._set_current_file(job.id, live, None)
                self._set_status(job, live, "done")
                logger.info("scan job %s completed", job.id)
                return

            # All analysis and copying are inside this block.  See the module
            # docstring: extracted records are invalid as soon as their archive
            # context exits.
            with ExitStack() as archive_contexts:
                records, preexisting_errors = self._discover_files(job, settings, live, archive_contexts)
                self._raise_if_cancelled(live)

                job.counts = {
                    "total": len(records) + len(preexisting_errors),
                    "scanned": 0,
                    "matched": 0,
                    "errored": len(preexisting_errors),
                }
                self._persist(job, live)

                self._set_status(job, live, "fast_pass")
                for chunk in self._chunk_records(records, effective_settings.ollama_max_concurrency):
                    self._raise_if_cancelled(live)
                    self._set_current_file(job.id, live, self._chunk_display(chunk))
                    run_fast_pass(
                        job,
                        chunk,
                        effective_settings,
                        lambda updated, result: self._on_fast_progress(job, live, updated, result),
                        client=effective_client,
                    )
                    self._raise_if_cancelled(live)

                self._set_current_file(job.id, live, None)
                self._set_status(job, live, "detailed_pass")
                candidates = select_candidates(job.id, threshold=_MATCH_THRESHOLD)
                for chunk in self._chunk_records(candidates, effective_settings.ollama_max_concurrency):
                    self._raise_if_cancelled(live)
                    self._set_current_file(job.id, live, self._chunk_display(chunk))
                    run_detailed_pass(
                        job,
                        chunk,
                        effective_settings,
                        lambda updated, result: self._on_detailed_progress(job, live, updated, result),
                        client=effective_client,
                        threshold=_MATCH_THRESHOLD,
                    )
                    self._raise_if_cancelled(live)

                self._set_current_file(job.id, live, None)
                self._refresh_match_count(job)
                self._persist(job, live)

                # finalize_output copies archive-derived files before
                # ExitStack removes their temporary extraction directories.
                self._raise_if_cancelled(live)
                report_path = finalize_output(job, settings)
                self._raise_if_cancelled(live)
                job.report_path = report_path

            self._set_current_file(job.id, live, None)
            self._set_status(job, live, "done")
            logger.info("scan job %s completed", job.id)
        except _JobCancelled:
            self._set_current_file(job.id, live, None)
            self._mark_cancelled(job, live)
            logger.info("scan job %s cancelled", job.id)
        except Exception as exc:
            self._set_current_file(job.id, live, None)
            logger.exception("scan job %s failed: %s", job.id, exc)
            # A concurrent cancellation wins over an unrelated late failure.
            if live.cancel_event.is_set():
                self._mark_cancelled(job, live)
            else:
                job.status = "error"
                job.counts = self._complete_counts(job.counts)
                job.counts["errored"] += 1
                self._persist(job, live, allow_cancelled=True)
        finally:
            # Keep the compact live state for API progress while the process is
            # alive, but discard the Thread reference once it is no longer live.
            with self._lock:
                tracked = self._jobs.get(job.id)
                if tracked is live:
                    live.thread = None

    def _discover_files(
        self,
        job: ScanJob,
        settings: Settings,
        live: _LiveJob,
        archive_contexts: ExitStack,
    ) -> tuple[list[FileRecord], list[FileRecord]]:
        """Walk ordinary roots and resolve archives while contexts remain open.

        `scanner.walk()` can take a long time on a large root (a whole drive,
        not just a subfolder) with nothing else happening yet — fast/detailed
        pass haven't started, so without this the progress UI has nothing to
        show ("0 of 0") for the entire duration of the walk. `on_discovered`
        gives it a live, if provisional, count: every record `_prepare_source_records`
        pulls off the walk generator bumps `total` and updates `current_file`,
        persisted every few files rather than every single one so a fast local
        walk isn't bottlenecked on SQLite writes. This count is provisional —
        `_run_job` overwrites `job.counts["total"]` with the authoritative
        figure the moment discovery finishes, before fast pass begins.
        """
        to_analyze: list[FileRecord] = []
        errors: list[FileRecord] = []
        seen_ids: set[str] = set()
        discovered = 0
        _PERSIST_EVERY = 25

        def on_discovered(record: FileRecord) -> None:
            nonlocal discovered
            discovered += 1
            counts = self._complete_counts(job.counts)
            counts["total"] = discovered
            job.counts = counts
            self._set_current_file(job.id, live, record.display_path)
            if discovered % _PERSIST_EVERY == 0:
                self._persist(job, live)

        for root in job.roots:
            self._raise_if_cancelled(live)
            records, root_errors = self._prepare_source_records(
                job,
                scanner.walk(root, settings),
                settings,
                live,
                archive_contexts,
                seen_ids,
                on_discovered=on_discovered,
            )
            to_analyze.extend(records)
            errors.extend(root_errors)

        if discovered % _PERSIST_EVERY != 0:
            self._persist(job, live)
        return to_analyze, errors

    def _run_portable_batched_job(
        self, job: ScanJob, settings: Settings, live: _LiveJob, client: object | None
    ) -> None:
        """Process phone storage batch-by-batch so temporary space stays bounded.

        ``settings`` here is already the concurrency-adjusted copy from
        ``_resolve_effective_client`` (see ``_run_job``) -- every other field
        is identical to the caller's original ``Settings``, so it is also
        the right object for the ordinary scanner/staging calls below, not
        just the pass-engine calls inside ``_analyze_batch``.
        """
        seen_ids: set[str] = set()
        with ExitStack() as regular_contexts:
            for root in (item for item in job.roots if not portable_devices.is_portable_root(item)):
                records, errors = self._prepare_source_records(
                    job, scanner.walk(root, settings), settings, live, regular_contexts, seen_ids
                )
                self._register_discovered(job, live, records, errors)
                self._analyze_batch(job, settings, live, records, client=client)

            for root in (item for item in job.roots if portable_devices.is_portable_root(item)):
                self._set_status(job, live, "scanning")

                def on_stage_progress(event: str, display_path: str, batch_bytes: int, batch_limit: int) -> None:
                    self._raise_if_cancelled(live)
                    if event == "indexed":
                        counts = self._complete_counts(job.counts)
                        counts["total"] += 1
                        job.counts = counts
                        self._persist(job, live)
                    if event == "copying":
                        self._set_current_file(
                            job.id,
                            live,
                            f"Copying phone file ({batch_bytes / 1024**3:.2f} / {batch_limit / 1024**3:.2f} GiB): {display_path}",
                        )
                    elif event == "staged":
                        self._set_current_file(
                            job.id,
                            live,
                            f"Copied phone file ({batch_bytes / 1024**3:.2f} / {batch_limit / 1024**3:.2f} GiB): {display_path}",
                        )
                    elif event == "error":
                        self._set_current_file(job.id, live, f"Could not copy phone file: {display_path}")

                try:
                    with portable_devices.staged_batches(root, settings, on_progress=on_stage_progress) as batches:
                        for batch in batches:
                            self._raise_if_cancelled(live)
                            logger.info(
                                "portable batch %d ready for analysis: %d record(s), %0.2f GiB",
                                batch.index,
                                len(batch.records),
                                batch.bytes_staged / 1024**3,
                            )
                            with ExitStack() as batch_contexts:
                                records, errors = self._prepare_source_records(
                                    job, batch.records, settings, live, batch_contexts, seen_ids
                                )
                                self._register_discovered(
                                    job,
                                    live,
                                    records,
                                    errors,
                                    indexed_source_count=len(batch.records),
                                )
                                self._analyze_batch(job, settings, live, records, client=client)
                                self._raise_if_cancelled(live)
                                preserved = preserve_staged_matches(job, {record.id for record in records})
                                logger.info("portable batch %d: preserved %d matching file(s)", batch.index, preserved)
                                self._refresh_match_count(job)
                                self._persist(job, live)
                            self._set_status(job, live, "scanning")
                except portable_devices.PortableDeviceError as exc:
                    error = portable_devices.error_record(root, str(exc))
                    records, errors = self._prepare_source_records(
                        job, [error], settings, live, regular_contexts, seen_ids
                    )
                    self._register_discovered(job, live, records, errors)

            self._set_current_file(job.id, live, None)
            self._refresh_match_count(job)
            self._persist(job, live)
            self._raise_if_cancelled(live)
            job.report_path = finalize_output(job, settings)

    def _prepare_source_records(
        self,
        job: ScanJob,
        source_records: Iterator[FileRecord] | list[FileRecord],
        settings: Settings,
        live: _LiveJob,
        archive_contexts: ExitStack,
        seen_ids: set[str],
        on_discovered: Callable[[FileRecord], None] | None = None,
    ) -> tuple[list[FileRecord], list[FileRecord]]:
        """Persist, associate, and archive-expand one discovered source stream.

        `on_discovered`, when given, fires once per record pulled off
        `source_records` — before the dedup check, so it reflects "how far
        the walk has gotten" rather than "how many new files it found".
        Only the ordinary (non-portable) discovery path passes this; the
        portable/MTP call sites leave it `None` and keep using
        `_register_discovered()`'s own counting, unchanged.
        """
        to_analyze: list[FileRecord] = []
        errors: list[FileRecord] = []
        for record in source_records:
            self._raise_if_cancelled(live)
            if on_discovered is not None:
                on_discovered(record)
            if record.id in seen_ids:
                continue
            seen_ids.add(record.id)
            db.upsert_file(record)
            db.link_file_to_job(job.id, record.id)
            if record.status == "error":
                errors.append(record)
                continue
            if archives.is_archive(record.path):
                contents = archive_contexts.enter_context(archives.extract_archive(record, settings))
                if contents:
                    record.status = "skipped"
                    record.error_message = f"archive expanded into {len(contents)} file(s)"
                    db.upsert_file(record)
                    for inner in contents:
                        if inner.id in seen_ids:
                            continue
                        seen_ids.add(inner.id)
                        db.upsert_file(inner)
                        db.link_file_to_job(job.id, inner.id)
                        if inner.status == "error":
                            errors.append(inner)
                        else:
                            to_analyze.append(inner)
                    continue
            to_analyze.append(record)
        return to_analyze, errors

    def _register_discovered(
        self,
        job: ScanJob,
        live: _LiveJob,
        records: list[FileRecord],
        errors: list[FileRecord],
        *,
        indexed_source_count: int = 0,
    ) -> None:
        """Update live discovery counts without resetting earlier phone batches."""
        counts = self._complete_counts(job.counts)
        counts["total"] += max(0, len(records) + len(errors) - indexed_source_count)
        counts["errored"] += len(errors)
        job.counts = counts
        self._persist(job, live)

    def _analyze_batch(
        self,
        job: ScanJob,
        settings: Settings,
        live: _LiveJob,
        records: list[FileRecord],
        *,
        client: object | None,
    ) -> None:
        """Run both model passes only for one independently staged batch.

        ``settings`` is expected to already be the concurrency-adjusted copy
        from ``_resolve_effective_client``; ``client`` is that same call's
        resolved client, passed explicitly by every caller rather than read
        off ``self._client`` here, so a portable job's several batches all
        share the one pool object built once for the whole job (PLAN_v1_5.md
        §7 Segment 21/22) instead of each batch re-deriving it.
        """
        if not records:
            return
        self._set_status(job, live, "fast_pass")
        for chunk in self._chunk_records(records, settings.ollama_max_concurrency):
            self._raise_if_cancelled(live)
            self._set_current_file(job.id, live, self._chunk_display(chunk))
            run_fast_pass(
                job,
                chunk,
                settings,
                lambda updated, result: self._on_fast_progress(job, live, updated, result),
                client=client,
            )
        self._set_current_file(job.id, live, None)
        self._set_status(job, live, "detailed_pass")
        record_ids = {record.id for record in records}
        candidates = [record for record in select_candidates(job.id, threshold=_MATCH_THRESHOLD) if record.id in record_ids]
        for chunk in self._chunk_records(candidates, settings.ollama_max_concurrency):
            self._raise_if_cancelled(live)
            self._set_current_file(job.id, live, self._chunk_display(chunk))
            run_detailed_pass(
                job,
                chunk,
                settings,
                lambda updated, result: self._on_detailed_progress(job, live, updated, result),
                client=client,
                threshold=_MATCH_THRESHOLD,
            )
        self._set_current_file(job.id, live, None)

    # ------------------------------------------------------------------
    # Progress accounting and cancellation-safe persistence
    # ------------------------------------------------------------------

    def _on_fast_progress(
        self,
        job: ScanJob,
        live: _LiveJob,
        record: FileRecord,
        _result: AnalysisResult | None,
    ) -> None:
        if live.cancel_event.is_set():
            return
        # Segment 22 note: before this segment every chunk was a single
        # record, so `run_fast_pass`'s internal `ThreadPoolExecutor` never
        # had more than one task and this callback was always effectively
        # serial. With real multi-file chunks, several worker threads now
        # call this concurrently -- the counts read-modify-write below is
        # not atomic on its own, so it must be locked or a concurrent
        # `scanned`/`errored` increment can be silently lost (last writer
        # wins on the whole `job.counts` dict). `self._lock` is an
        # `RLock`, so nesting into `_persist`'s own `with self._lock:` is
        # safe.
        with self._lock:
            counts = self._complete_counts(job.counts)
            counts["scanned"] = min(counts["total"], counts["scanned"] + 1)
            if record.status == "error":
                counts["errored"] += 1
            job.counts = counts
            self._refresh_match_count(job)
            self._persist(job, live)

    def _on_detailed_progress(
        self,
        job: ScanJob,
        live: _LiveJob,
        record: FileRecord,
        _result: AnalysisResult | None,
    ) -> None:
        if live.cancel_event.is_set():
            return
        # ``scanned`` counts files through their first-pass examination, not a
        # second examination.  A detailed error is only counted if it was not
        # already an error from fast pass. Same concurrency note as
        # ``_on_fast_progress`` above -- locked for the same reason.
        with self._lock:
            counts = self._complete_counts(job.counts)
            if record.status == "error":
                counts["errored"] += 1
            job.counts = counts
            self._refresh_match_count(job)
            self._persist(job, live)

    def _refresh_match_count(self, job: ScanJob) -> None:
        """Use the latest result for each (file, intent) pair as the verdict."""
        preferred: dict[tuple[str, str | None], AnalysisResult] = {}
        for result in db.get_results(job.id):
            key = (result.file_id, result.intent_id)
            current = preferred.get(key)
            if current is None or self._result_rank(result) > self._result_rank(current):
                preferred[key] = result
        matched_file_ids = {
            result.file_id
            for result in preferred.values()
            if result.relevance_score >= _MATCH_THRESHOLD or result.flagged_interesting
        }
        counts = self._complete_counts(job.counts)
        counts["matched"] = len(matched_file_ids)
        job.counts = counts

    @staticmethod
    def _result_rank(result: AnalysisResult) -> tuple[int, float]:
        return (1 if result.pass_type == "detailed" else 0, result.created_at)

    def _set_status(self, job: ScanJob, live: _LiveJob, status: str) -> None:
        self._raise_if_cancelled(live)
        job.status = status
        self._persist(job, live)

    def _mark_cancelled(self, job: ScanJob, live: _LiveJob) -> None:
        live.cancel_event.set()
        job.status = "cancelled"
        self._persist(job, live, allow_cancelled=True)

    def _persist(self, job: ScanJob, live: _LiveJob, *, allow_cancelled: bool = False) -> None:
        """Persist without letting a late worker overwrite ``cancelled``."""
        if live.cancel_event.is_set() and not allow_cancelled:
            raise _JobCancelled()
        with self._lock:
            persisted = db.get_job(job.id)
            if persisted is not None and persisted.status == "cancelled" and not allow_cancelled:
                live.cancel_event.set()
                raise _JobCancelled()
            db.update_job(job)

    @staticmethod
    def _chunk_records(records: list[FileRecord], size: int) -> list[list[FileRecord]]:
        """Split ``records`` into groups of ``size`` -- one
        ``run_fast_pass``/``run_detailed_pass`` call per chunk, so that
        call's own internal ``ThreadPoolExecutor(max_workers=
        settings.ollama_max_concurrency)`` finally has more than one file to
        parallelize at once (PLAN_v1_5.md §1/§7 Segment 22) instead of the
        single-element lists every call site used before this segment.

        ``size`` is floored to 1 here purely as a defensive guard against a
        zero/negative value breaking this slicing (``range(..., 0)`` raises
        ``ValueError``) -- it is not a hardcoded worker/slot count and does
        not affect ``effective_concurrency``'s own value; a misconfigured
        ``OLLAMA_MAX_CONCURRENCY`` of 0 already breaks the pass engines'
        own ``ThreadPoolExecutor`` construction independently of this
        segment's change.
        """
        step = max(1, size)
        return [records[i : i + step] for i in range(0, len(records), step)]

    @staticmethod
    def _chunk_display(chunk: list[FileRecord]) -> str:
        """Progress-UI label for one in-flight chunk (PLAN_v1_5.md §7
        Segment 22: "a single filename string isn't meaningful" once
        several files are genuinely in flight at once). A single-file
        chunk -- the common case whenever no pool is active and
        ``ollama_max_concurrency`` is left at a small default, or simply a
        trailing partial chunk -- keeps showing that file's own path,
        unchanged from pre-Segment-22 behavior; a multi-file chunk shows a
        count instead, per the plan's own "{n} files (parallel)"
        suggestion. Flagged in PROJECT_STATE.md: if a frontend file reads
        ``current_file`` expecting a path specifically, this format change
        needs a matching frontend update outside this segment's file list.
        """
        if len(chunk) == 1:
            return chunk[0].display_path
        return f"{len(chunk)} files (parallel)"

    @staticmethod
    def _complete_counts(counts: dict) -> dict:
        return {
            "total": int(counts.get("total", 0)),
            "scanned": int(counts.get("scanned", 0)),
            "matched": int(counts.get("matched", 0)),
            "errored": int(counts.get("errored", 0)),
        }

    @staticmethod
    def _normalize_roots(roots: list[str]) -> list[str]:
        return Orchestrator._dedupe_strings(
            [
                root if portable_devices.is_portable_root(root) else os.path.abspath(root)
                for root in roots
                if isinstance(root, str) and root
            ]
        )

    @staticmethod
    def _dedupe_strings(values: list[str]) -> list[str]:
        seen: set[str] = set()
        output: list[str] = []
        for value in values:
            if value not in seen:
                seen.add(value)
                output.append(value)
        return output

    @staticmethod
    def _raise_if_cancelled(live: _LiveJob) -> None:
        if live.cancel_event.is_set():
            raise _JobCancelled()

    def _set_current_file(self, job_id: str, live: _LiveJob, path: str | None) -> None:
        with self._lock:
            if self._jobs.get(job_id) is live:
                live.current_file = path
