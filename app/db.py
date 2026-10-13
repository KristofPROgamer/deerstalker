"""SQLite storage layer. Plain sqlite3, no ORM, on purpose (PLAN.md
Segment 1 spec) — every later segment should go through the functions
below rather than opening its own connection or writing raw SQL.

Three tables, each mirroring one of the models in app.models:
  files   <-> FileRecord
  results <-> AnalysisResult
  jobs    <-> ScanJob

(ContentPayload and SearchIntent are never persisted — they're transient
per-file extraction output and a static in-code registry, respectively.)

List/dict fields (tags, roots, intent_ids, counts) are stored as JSON text
in their own column; every scalar field gets a real typed column so later
segments can filter/query on it directly (e.g. `get_results(job_id,
intent_id=...)`).
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from app.config import get_settings
from app.log import logger
from app.models import AnalysisResult, FileRecord, ScanJob

_DB_PATH: str | None = None


# --------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------


def _default_db_path() -> str:
    return os.path.join(get_settings().app_data_dir, "deerstalker.db")


def init_db(db_path: str | None = None) -> None:
    """Create the DB file (if needed) and all tables (if needed).

    Args:
        db_path: override the DB file location, e.g. for tests. Defaults
            to `{APP_DATA_DIR}/deerstalker.db`.
    """
    global _DB_PATH
    _DB_PATH = db_path or _default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(_DB_PATH)), exist_ok=True)

    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                id                TEXT PRIMARY KEY,
                path              TEXT NOT NULL,
                display_path      TEXT NOT NULL,
                source_archive_id TEXT,
                ext               TEXT NOT NULL,
                size_bytes        INTEGER NOT NULL,
                mime_type         TEXT,
                mtime             REAL NOT NULL,
                is_from_archive   INTEGER NOT NULL,
                status            TEXT NOT NULL,
                error_message     TEXT
            );

            CREATE TABLE IF NOT EXISTS results (
                id                   TEXT PRIMARY KEY,
                file_id              TEXT NOT NULL,
                job_id               TEXT NOT NULL,
                pass_type            TEXT NOT NULL,
                intent_id            TEXT,
                tags                 TEXT NOT NULL,
                relevance_score      REAL NOT NULL,
                summary              TEXT NOT NULL,
                reasoning            TEXT NOT NULL,
                flagged_interesting  INTEGER NOT NULL,
                model_used           TEXT NOT NULL,
                created_at           REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_results_job_id ON results(job_id);
            CREATE INDEX IF NOT EXISTS idx_results_intent_id ON results(intent_id);

            CREATE TABLE IF NOT EXISTS job_files (
                job_id  TEXT NOT NULL,
                file_id TEXT NOT NULL,
                PRIMARY KEY (job_id, file_id)
            );
            CREATE INDEX IF NOT EXISTS idx_job_files_job_id ON job_files(job_id);

            CREATE TABLE IF NOT EXISTS jobs (
                id             TEXT PRIMARY KEY,
                roots          TEXT NOT NULL,
                intent_ids     TEXT NOT NULL,
                custom_query   TEXT,
                status         TEXT NOT NULL,
                counts         TEXT NOT NULL,
                output_folder  TEXT NOT NULL,
                report_path    TEXT,
                created_at     REAL NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("db initialized at %s", _DB_PATH)


def _connect() -> sqlite3.Connection:
    global _DB_PATH
    if _DB_PATH is None:
        init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --------------------------------------------------------------------------
# files / FileRecord
# --------------------------------------------------------------------------


def upsert_file(record: FileRecord) -> None:
    """Insert a FileRecord, or fully overwrite it if `record.id` already
    exists."""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO files (id, path, display_path, source_archive_id, ext,
                                size_bytes, mime_type, mtime, is_from_archive,
                                status, error_message)
            VALUES (:id, :path, :display_path, :source_archive_id, :ext,
                     :size_bytes, :mime_type, :mtime, :is_from_archive,
                     :status, :error_message)
            ON CONFLICT(id) DO UPDATE SET
                path=excluded.path,
                display_path=excluded.display_path,
                source_archive_id=excluded.source_archive_id,
                ext=excluded.ext,
                size_bytes=excluded.size_bytes,
                mime_type=excluded.mime_type,
                mtime=excluded.mtime,
                is_from_archive=excluded.is_from_archive,
                status=excluded.status,
                error_message=excluded.error_message
            """,
            {
                "id": record.id,
                "path": record.path,
                "display_path": record.display_path,
                "source_archive_id": record.source_archive_id,
                "ext": record.ext,
                "size_bytes": record.size_bytes,
                "mime_type": record.mime_type,
                "mtime": record.mtime,
                "is_from_archive": int(record.is_from_archive),
                "status": record.status,
                "error_message": record.error_message,
            },
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_file_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"],
        path=row["path"],
        display_path=row["display_path"],
        source_archive_id=row["source_archive_id"],
        ext=row["ext"],
        size_bytes=row["size_bytes"],
        mime_type=row["mime_type"],
        mtime=row["mtime"],
        is_from_archive=bool(row["is_from_archive"]),
        status=row["status"],
        error_message=row["error_message"],
    )


def get_file(file_id: str) -> FileRecord | None:
    """Fetch one FileRecord by id, or None if it doesn't exist."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return _row_to_file_record(row) if row is not None else None
    finally:
        conn.close()


def link_file_to_job(job_id: str, file_id: str) -> None:
    """Record that a discovered file belongs to one scan job.

    ``FileRecord`` IDs intentionally remain stable across scans, so the
    relationship is kept in this small join table rather than changing the
    Segment 1 model contract. It lets the results screen show source errors
    even when no model result was produced for that file.
    """
    conn = _connect()
    try:
        conn.execute("INSERT OR IGNORE INTO job_files (job_id, file_id) VALUES (?, ?)", (job_id, file_id))
        conn.commit()
    finally:
        conn.close()


def get_files_for_job(job_id: str) -> list[FileRecord]:
    """Return all files discovered for ``job_id``, including source errors."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT files.*
            FROM files
            INNER JOIN job_files ON job_files.file_id = files.id
            WHERE job_files.job_id = ?
            ORDER BY files.display_path COLLATE NOCASE
            """,
            (job_id,),
        ).fetchall()
        return [_row_to_file_record(row) for row in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# results / AnalysisResult
# --------------------------------------------------------------------------


def upsert_result(result: AnalysisResult) -> None:
    """Insert an AnalysisResult, or fully overwrite it if `result.id`
    already exists (e.g. a detailed pass replacing its own earlier
    attempt)."""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO results (id, file_id, job_id, pass_type, intent_id,
                                  tags, relevance_score, summary, reasoning,
                                  flagged_interesting, model_used, created_at)
            VALUES (:id, :file_id, :job_id, :pass_type, :intent_id,
                     :tags, :relevance_score, :summary, :reasoning,
                     :flagged_interesting, :model_used, :created_at)
            ON CONFLICT(id) DO UPDATE SET
                file_id=excluded.file_id,
                job_id=excluded.job_id,
                pass_type=excluded.pass_type,
                intent_id=excluded.intent_id,
                tags=excluded.tags,
                relevance_score=excluded.relevance_score,
                summary=excluded.summary,
                reasoning=excluded.reasoning,
                flagged_interesting=excluded.flagged_interesting,
                model_used=excluded.model_used,
                created_at=excluded.created_at
            """,
            {
                "id": result.id,
                "file_id": result.file_id,
                "job_id": result.job_id,
                "pass_type": result.pass_type,
                "intent_id": result.intent_id,
                "tags": json.dumps(result.tags),
                "relevance_score": result.relevance_score,
                "summary": result.summary,
                "reasoning": result.reasoning,
                "flagged_interesting": int(result.flagged_interesting),
                "model_used": result.model_used,
                "created_at": result.created_at,
            },
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_analysis_result(row: sqlite3.Row) -> AnalysisResult:
    return AnalysisResult(
        id=row["id"],
        file_id=row["file_id"],
        job_id=row["job_id"],
        pass_type=row["pass_type"],
        intent_id=row["intent_id"],
        tags=json.loads(row["tags"]),
        relevance_score=row["relevance_score"],
        summary=row["summary"],
        reasoning=row["reasoning"],
        flagged_interesting=bool(row["flagged_interesting"]),
        model_used=row["model_used"],
        created_at=row["created_at"],
    )


def get_results(job_id: str, intent_id: str | None = None) -> list[AnalysisResult]:
    """Fetch all AnalysisResults for a job, optionally filtered to one
    intent_id. Ordered by created_at ascending."""
    conn = _connect()
    try:
        if intent_id is None:
            rows = conn.execute(
                "SELECT * FROM results WHERE job_id = ? ORDER BY created_at",
                (job_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM results WHERE job_id = ? AND intent_id = ? ORDER BY created_at",
                (job_id, intent_id),
            ).fetchall()
        return [_row_to_analysis_result(row) for row in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# jobs / ScanJob
# --------------------------------------------------------------------------


def create_job(job: ScanJob) -> None:
    """Insert a new ScanJob. Overwrites if `job.id` already exists (kept
    idempotent so a retried create_job() call doesn't blow up)."""
    _write_job(job)


def update_job(job: ScanJob) -> None:
    """Persist changes to an existing ScanJob (status transitions, counts,
    output_folder, report_path, ...). Callers should `get_job()`, mutate
    the returned object, then call this with it."""
    _write_job(job)


def _write_job(job: ScanJob) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO jobs (id, roots, intent_ids, custom_query, status,
                               counts, output_folder, report_path, created_at)
            VALUES (:id, :roots, :intent_ids, :custom_query, :status,
                     :counts, :output_folder, :report_path, :created_at)
            ON CONFLICT(id) DO UPDATE SET
                roots=excluded.roots,
                intent_ids=excluded.intent_ids,
                custom_query=excluded.custom_query,
                status=excluded.status,
                counts=excluded.counts,
                output_folder=excluded.output_folder,
                report_path=excluded.report_path,
                created_at=excluded.created_at
            """,
            {
                "id": job.id,
                "roots": json.dumps(job.roots),
                "intent_ids": json.dumps(job.intent_ids),
                "custom_query": job.custom_query,
                "status": job.status,
                "counts": json.dumps(job.counts),
                "output_folder": job.output_folder,
                "report_path": job.report_path,
                "created_at": job.created_at,
            },
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_scan_job(row: sqlite3.Row) -> ScanJob:
    return ScanJob(
        id=row["id"],
        roots=json.loads(row["roots"]),
        intent_ids=json.loads(row["intent_ids"]),
        custom_query=row["custom_query"],
        status=row["status"],
        counts=json.loads(row["counts"]),
        output_folder=row["output_folder"],
        report_path=row["report_path"],
        created_at=row["created_at"],
    )


def get_job(job_id: str) -> ScanJob | None:
    """Fetch one ScanJob by id, or None if it doesn't exist."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_scan_job(row) if row is not None else None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Self-test: `python -m app.db --selftest`
# --------------------------------------------------------------------------


def _run_selftest() -> None:
    import tempfile
    import time

    from app.models import ContentPayload, SearchIntent

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "selftest.db")
        init_db(db_path)

        # --- FileRecord round trip -------------------------------------
        original_file = FileRecord(
            id="file-1",
            path="/tmp/example/report.pdf",
            display_path="report.pdf",
            source_archive_id=None,
            ext=".pdf",
            size_bytes=48_213,
            mime_type="application/pdf",
            mtime=time.time(),
            is_from_archive=False,
            status="pending",
            error_message=None,
        )
        upsert_file(original_file)
        fetched_file = get_file("file-1")
        assert fetched_file == original_file, "FileRecord round-trip mismatch"
        assert get_file("does-not-exist") is None, "expected None for missing file"

        # --- AnalysisResult round trip + get_results filtering ----------
        original_result = AnalysisResult(
            id="result-1",
            file_id="file-1",
            job_id="job-1",
            pass_type="fast",
            intent_id="sensitive_docs",
            tags=["id_card", "passport"],
            relevance_score=0.82,
            summary="Looks like a scanned passport page.",
            reasoning="Contains passport-style layout and a photo.",
            flagged_interesting=True,
            model_used="llama3.2-vision",
            created_at=time.time(),
        )
        upsert_result(original_result)
        all_results = get_results("job-1")
        assert all_results == [original_result], "AnalysisResult round-trip mismatch"
        matching = get_results("job-1", intent_id="sensitive_docs")
        assert matching == [original_result], "intent_id filter (match) failed"
        empty = get_results("job-1", intent_id="financial")
        assert empty == [], "intent_id filter (no match) failed"

        # --- ScanJob round trip + update_job ----------------------------
        original_job = ScanJob(
            id="job-1",
            roots=["/tmp/example"],
            intent_ids=["sensitive_docs", "financial"],
            custom_query=None,
            status="pending",
            counts={"total": 0, "scanned": 0, "matched": 0, "errored": 0},
            output_folder="/tmp/example/output/job-1",
            report_path=None,
            created_at=time.time(),
        )
        create_job(original_job)
        fetched_job = get_job("job-1")
        assert fetched_job == original_job, "ScanJob round-trip mismatch"

        fetched_job.status = "scanning"
        fetched_job.counts = {"total": 10, "scanned": 3, "matched": 1, "errored": 0}
        update_job(fetched_job)
        updated_job = get_job("job-1")
        assert updated_job is not None
        assert updated_job.status == "scanning", "update_job status change didn't persist"
        assert updated_job.counts["scanned"] == 3, "update_job counts change didn't persist"

        # --- ContentPayload / SearchIntent: not persisted, just confirm
        # the models themselves construct correctly. ---------------------
        ContentPayload(
            file_id="file-1",
            kind="mixed",
            text_excerpt="Passport\nRepublic of ...",
            images_b64=[],
            metadata={"page_count": 3},
            truncated=False,
        )
        SearchIntent(
            id="sensitive_docs",
            label="Sensitive & Personal Documents",
            description="IDs, passports, SSNs/tax IDs, medical records, insurance paperwork",
            icon="🪪",
            fast_prompt_template="...",
            detailed_prompt_template="...",
        )

    print("OK")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker DB utility")
    parser.add_argument("--selftest", action="store_true", help="round-trip test against a temp DB")
    args = parser.parse_args()

    if args.selftest:
        _run_selftest()
    else:
        parser.print_help()
