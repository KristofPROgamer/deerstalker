"""Human- and machine-readable scan output (PLAN.md Segment 11)."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections import defaultdict
from typing import Any

from app import db
from app.config import Settings
from app.intents import INTENTS
from app.log import logger
from app.models import AnalysisResult, FileRecord, ScanJob


_MATCH_THRESHOLD = 0.5
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_STAGED_MATCH_MANIFEST = ".deerstalker-staged-matches.json"


def preserve_staged_matches(job: ScanJob, file_ids: set[str]) -> int:
    """Copy final matches from one temporary staging batch before cleanup.

    Phone batches are deliberately deleted before the next batch is copied.
    This durable manifest lets :func:`finalize_output` compose the one final
    report without trying to reopen those already-deleted temporary sources.
    """
    if not file_ids:
        return 0
    output_folder = os.path.abspath(job.output_folder)
    os.makedirs(output_folder, exist_ok=True)
    existing = _load_staged_manifest(output_folder)
    by_key = {_result_key(entry["analysis"]): entry for entry in existing if isinstance(entry, dict) and "analysis" in entry}

    copied = 0
    verdicts = _preferred_results(db.get_results(job.id))
    for result in verdicts:
        if result.file_id not in file_ids:
            continue
        if result.relevance_score < _MATCH_THRESHOLD and not result.flagged_interesting:
            continue
        key = _result_key(result.to_dict())
        if key in by_key:
            continue
        record = db.get_file(result.file_id)
        destination_folder = os.path.join(output_folder, _safe_directory_name(_intent_label(result.intent_id)))
        os.makedirs(destination_folder, exist_ok=True)
        copied_path, copy_error = _copy_match(record, destination_folder)
        entry = {
            "analysis": result.to_dict(),
            "file": record.to_dict() if record is not None else None,
            "intent_label": _intent_label(result.intent_id),
            "copied_path": copied_path,
            "copy_error": copy_error,
        }
        existing.append(entry)
        by_key[key] = entry
        copied += 1

    if copied:
        _save_staged_manifest(output_folder, existing)
        logger.info("output: preserved %d match(es) from temporary phone staging for job %s", copied, job.id)
    return copied


def finalize_output(job: ScanJob, settings: Settings) -> str:
    """Copy matched files and write ``REPORT.md`` plus ``results.json``.

    The destination is ``job.output_folder`` (created by Segment 10 as
    ``{APP_DATA_DIR}/output/{job_id}``).  Every qualifying result is copied
    into its intent-label directory.  A file matching more than one intent is
    therefore represented in each relevant folder; source files are never
    moved or deleted.  Returns the absolute ``REPORT.md`` path.

    Fast results are used until a detailed result exists for the same
    ``(file_id, intent_id)`` pair, at which point the detailed result is the
    final verdict.  A verdict matches at relevance >= 0.5 or when
    ``flagged_interesting`` is true, mirroring Segment 9's candidate threshold.
    """
    del settings  # The contract accepts Settings; output location is stored on the job itself.

    output_folder = os.path.abspath(job.output_folder)
    os.makedirs(output_folder, exist_ok=True)

    verdicts = _preferred_results(db.get_results(job.id))
    matches = [
        result
        for result in verdicts
        if result.relevance_score >= _MATCH_THRESHOLD or result.flagged_interesting
    ]
    matches.sort(key=lambda result: (_intent_label(result.intent_id).casefold(), result.file_id, result.created_at))

    records: dict[str, FileRecord | None] = {}
    copied_entries: list[dict[str, Any]] = []
    staged_entries = _load_staged_manifest(output_folder)
    staged_by_key = {
        _result_key(entry["analysis"]): entry
        for entry in staged_entries
        if isinstance(entry, dict) and isinstance(entry.get("analysis"), dict)
    }
    for result in matches:
        staged = staged_by_key.get(_result_key(result.to_dict()))
        if staged is not None:
            copied_entries.append(staged)
            continue
        record = records.setdefault(result.file_id, db.get_file(result.file_id))
        destination_folder = os.path.join(output_folder, _safe_directory_name(_intent_label(result.intent_id)))
        os.makedirs(destination_folder, exist_ok=True)
        copied_path, copy_error = _copy_match(record, destination_folder)
        copied_entries.append(
            {
                "analysis": result.to_dict(),
                "file": record.to_dict() if record is not None else None,
                "intent_label": _intent_label(result.intent_id),
                "copied_path": copied_path,
                "copy_error": copy_error,
            }
        )

    machine_report = {
        "job": job.to_dict(),
        "generated_at": time.time(),
        "match_threshold": _MATCH_THRESHOLD,
        "matches": copied_entries,
    }
    results_path = os.path.join(output_folder, "results.json")
    with open(results_path, "w", encoding="utf-8") as handle:
        json.dump(machine_report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    report_path = os.path.join(output_folder, "REPORT.md")
    with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_render_report(job, copied_entries))

    logger.info("output finalized for job %s: %s", job.id, report_path)
    return report_path


def _preferred_results(results: list[AnalysisResult]) -> list[AnalysisResult]:
    """Keep the final result for every (file, intent) pair."""
    selected: dict[tuple[str, str | None], AnalysisResult] = {}
    for result in results:
        key = (result.file_id, result.intent_id)
        current = selected.get(key)
        if current is None or _result_rank(result) > _result_rank(current):
            selected[key] = result
    return list(selected.values())


def _result_key(analysis: AnalysisResult | dict[str, Any]) -> str:
    if isinstance(analysis, AnalysisResult):
        return f"{analysis.file_id}::{analysis.intent_id!r}"
    return f"{analysis.get('file_id')}::{analysis.get('intent_id')!r}"


def _load_staged_manifest(output_folder: str) -> list[dict[str, Any]]:
    path = os.path.join(output_folder, _STAGED_MATCH_MANIFEST)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("output: could not read temporary-match manifest %s: %s", path, exc)
        return []


def _save_staged_manifest(output_folder: str, entries: list[dict[str, Any]]) -> None:
    path = os.path.join(output_folder, _STAGED_MATCH_MANIFEST)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary_path, path)


def _result_rank(result: AnalysisResult) -> tuple[int, float]:
    return (1 if result.pass_type == "detailed" else 0, result.created_at)


def _intent_label(intent_id: str | None) -> str:
    if intent_id is None:
        return "Custom Search"
    intent = INTENTS.get(intent_id)
    return intent.label if intent is not None else f"Unknown Intent ({intent_id})"


def _safe_directory_name(label: str) -> str:
    """Preserve readable labels while ensuring one safe path component."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label).strip(" .")
    if not name or name.upper() in _WINDOWS_RESERVED:
        return "Uncategorized"
    return name


def _copy_match(record: FileRecord | None, destination_folder: str) -> tuple[str | None, str | None]:
    if record is None:
        return None, "file record is unavailable"
    if not os.path.isfile(record.path):
        message = "source file is no longer available"
        logger.warning("output: %s for %s", message, record.display_path)
        return None, message

    filename = os.path.basename(record.path) or record.id
    destination = _deduplicated_destination(destination_folder, filename)
    try:
        shutil.copy2(record.path, destination)
    except OSError as exc:
        message = f"copy failed: {exc}"
        logger.warning("output: could not copy %s: %s", record.display_path, exc)
        return None, message
    return destination, None


def _deduplicated_destination(folder: str, filename: str) -> str:
    candidate = os.path.join(folder, filename)
    if not os.path.exists(candidate):
        return candidate
    stem, suffix = os.path.splitext(filename)
    counter = 2
    while True:
        candidate = os.path.join(folder, f"{stem} ({counter}){suffix}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _render_report(job: ScanJob, entries: list[dict[str, Any]]) -> str:
    by_intent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    interesting: list[dict[str, Any]] = []
    for entry in entries:
        by_intent[entry["intent_label"]].append(entry)
        analysis = entry["analysis"]
        if analysis["intent_id"] == "interesting" or analysis["flagged_interesting"]:
            interesting.append(entry)

    unique_files = {entry["analysis"]["file_id"] for entry in entries}
    lines = [
        "# Deerstalker Investigation Report",
        "",
        f"- Job ID: `{job.id}`",
        f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"- Roots: {', '.join(f'`{root}`' for root in job.roots) or '(none)'}",
        "",
        "## Summary",
        "",
        f"- Files discovered: {job.counts.get('total', 0)}",
        f"- Files examined: {job.counts.get('scanned', 0)}",
        f"- Matched files: {len(unique_files)}",
        f"- Matching verdicts: {len(entries)}",
        f"- File errors: {job.counts.get('errored', 0)}",
        "",
    ]

    if not entries:
        lines.extend(["No files met the match threshold.", ""])
    else:
        for label in sorted(by_intent, key=str.casefold):
            lines.extend([f"## {label}", ""])
            lines.extend(_render_entries(by_intent[label]))

    lines.extend(["## Interesting Things", ""])
    if interesting:
        lines.extend(_render_entries(interesting))
    else:
        lines.extend(["No files were specifically flagged as interesting.", ""])

    lines.extend(["## Machine-readable results", "", "See `results.json` in this folder for the complete structured output.", ""])
    return "\n".join(lines)


def _render_entries(entries: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        analysis = entry["analysis"]
        record = entry["file"]
        display_path = record["display_path"] if record is not None else f"[missing file record: {analysis['file_id']}]"
        lines.append(f"- `{display_path}`")
        lines.append(f"  - Score: {analysis['relevance_score']:.2f} ({analysis['pass_type']} pass)")
        if analysis["tags"]:
            lines.append(f"  - Tags: {', '.join(str(tag) for tag in analysis['tags'])}")
        if analysis["reasoning"]:
            lines.append(f"  - Why: {analysis['reasoning']}")
        elif analysis["summary"]:
            lines.append(f"  - Summary: {analysis['summary']}")
        if entry["copied_path"]:
            lines.append(f"  - Copy: `{entry['copied_path']}`")
        elif entry["copy_error"]:
            lines.append(f"  - Copy note: {entry['copy_error']}")
    lines.append("")
    return lines
