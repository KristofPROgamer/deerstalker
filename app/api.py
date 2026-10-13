"""Local FastAPI surface for Deerstalker (PLAN.md Segment 12)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app import db, scanner
from app.config import get_settings
from app.intents import INTENTS
from app.pipeline.orchestrator import Orchestrator


app = FastAPI(title="Deerstalker", version="0.1.0")
settings = get_settings()
orchestrator = Orchestrator()
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class ScanRequest(BaseModel):
    """``POST /api/scan`` request body."""

    roots: list[str] = Field(min_length=1)
    intent_ids: list[str] = Field(default_factory=list)
    custom_query: str | None = None

    @field_validator("roots")
    @classmethod
    def roots_must_be_nonblank(cls, roots: list[str]) -> list[str]:
        cleaned = [root.strip() for root in roots if isinstance(root, str) and root.strip()]
        if not cleaned:
            raise ValueError("at least one non-empty root is required")
        return cleaned

    @field_validator("intent_ids")
    @classmethod
    def intents_must_exist(cls, intent_ids: list[str]) -> list[str]:
        unknown = sorted({intent_id for intent_id in intent_ids if intent_id not in INTENTS})
        if unknown:
            raise ValueError(f"unknown intent id(s): {', '.join(unknown)}")
        return list(dict.fromkeys(intent_ids))


class OpenFolderRequest(BaseModel):
    """``POST /api/open-folder`` request body."""

    path: str = Field(min_length=1)


class ChosenRoot(BaseModel):
    """A root returned by the host-native folder chooser."""

    path: str
    label: str
    kind: str


@app.get("/api/roots")
def get_roots() -> dict[str, list[dict[str, str]]]:
    """Return ``{"roots": [{"path", "label", "kind"}, ...]}``."""
    return {"roots": scanner.list_roots()}


@app.get("/api/intents")
def get_intents() -> dict[str, list[dict[str, Any]]]:
    """Return ``{"intents": [SearchIntent.to_dict(), ...]}`` in registry order."""
    return {"intents": [intent.to_dict() for intent in INTENTS.values()]}


@app.get("/api/portable-stage-settings")
def get_portable_stage_settings() -> dict[str, Any]:
    """Return the current local staging location for connected-phone scans."""
    configured = settings.portable_stage_dir
    directory = os.path.abspath(configured or os.path.join(settings.app_data_dir, "tmp"))
    return {
        "directory": directory,
        "uses_default": not bool(configured),
        "batch_gib": settings.portable_stage_batch_gib,
    }


@app.post("/api/choose-folder")
def choose_folder() -> dict[str, Any]:
    """Open the host folder chooser and return the selected scan root.

    A browser cannot reveal the absolute path of a directory selected through
    its file-picker APIs.  Because Deerstalker is local-first, this endpoint
    deliberately opens the native chooser in the local app process instead.
    Cancelling is a successful no-op rather than an error.
    """
    try:
        path = _choose_local_folder()
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"folder picker is unavailable: {exc}") from exc
    if not path:
        return {"selected": False}

    path = os.path.abspath(path)
    drive, tail = os.path.splitdrive(path)
    kind = "drive" if drive and tail in {"\\", "/"} else "folder"
    label = drive if kind == "drive" else os.path.basename(os.path.normpath(path))
    root = ChosenRoot(path=path, label=label or path, kind=kind)
    return {"selected": True, "root": root.model_dump()}


@app.post("/api/choose-portable-stage-folder")
def choose_portable_stage_folder() -> dict[str, Any]:
    """Choose and persist the local folder used for temporary phone batches."""
    initial_directory = os.path.abspath(settings.portable_stage_dir or os.path.join(settings.app_data_dir, "tmp"))
    try:
        path = _choose_local_folder(
            title="Choose a temporary folder for connected-phone scans",
            initial_directory=initial_directory,
        )
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"folder picker is unavailable: {exc}") from exc
    if not path:
        return {"selected": False}

    selected = os.path.abspath(path)
    os.makedirs(selected, exist_ok=True)
    settings.portable_stage_dir = selected
    persisted = _persist_env_value("PORTABLE_STAGE_DIR", selected)
    return {
        "selected": True,
        "directory": selected,
        "uses_default": False,
        "batch_gib": settings.portable_stage_batch_gib,
        "persisted": persisted,
    }


@app.post("/api/scan")
def start_scan(request: ScanRequest) -> dict[str, str]:
    """Start a scan and return exactly ``{"job_id": "..."}``."""
    custom_query = request.custom_query.strip() if request.custom_query and request.custom_query.strip() else None
    if not request.intent_ids and custom_query is None:
        raise HTTPException(status_code=422, detail="select at least one intent or provide a custom_query")
    job = orchestrator.start_job(request.roots, request.intent_ids, custom_query, settings)
    return {"job_id": job.id}


@app.get("/api/scan/{job_id}/progress")
def get_scan_progress(job_id: str) -> dict[str, Any]:
    """Return job lifecycle data for the polling progress view.

    Shape: ``{job_id, status, counts, current_file, output_folder,
    report_path, cancel_requested}``, where ``counts`` always has ``total``,
    ``scanned``, ``matched``, and ``errored`` integer keys.
    """
    try:
        return orchestrator.get_progress(job_id)
    except KeyError:
        _unknown_job(job_id)


@app.get("/api/scan/{job_id}/results")
def get_scan_results(job_id: str) -> dict[str, Any]:
    """Return all durable results and the records they reference.

    Shape: ``{"job": ScanJob, "results": [AnalysisResult, ...], "files":
    {file_id: FileRecord | null, ...}}``.  ``results`` contains both fast and
    detailed verdicts; the frontend can prefer a detailed verdict for the same
    ``(file_id, intent_id)`` pair just as the output stage does.
    """
    job = db.get_job(job_id)
    if job is None:
        _unknown_job(job_id)
    results = db.get_results(job_id)
    files = {record.id: record.to_dict() for record in db.get_files_for_job(job_id)}
    return {
        "job": job.to_dict(),
        "results": [result.to_dict() for result in results],
        "files": files,
    }


@app.post("/api/scan/{job_id}/cancel")
def cancel_scan(job_id: str) -> dict[str, str]:
    """Request a between-files cancellation and return ``{job_id, status}``."""
    try:
        orchestrator.cancel(job_id)
        status = orchestrator.get_progress(job_id)["status"]
    except KeyError:
        _unknown_job(job_id)
    return {"job_id": job_id, "status": status}


@app.get("/api/scan/{job_id}/report")
def get_scan_report(job_id: str) -> FileResponse:
    """Download the completed Markdown report (404 until it exists)."""
    job = db.get_job(job_id)
    if job is None:
        _unknown_job(job_id)
    if not job.report_path or not os.path.isfile(job.report_path):
        raise HTTPException(status_code=404, detail="report is not available yet")
    return FileResponse(job.report_path, media_type="text/markdown; charset=utf-8", filename="REPORT.md")


@app.post("/api/open-folder")
def open_folder(request: OpenFolderRequest) -> dict[str, Any]:
    """Ask the host desktop to open an existing local folder, best-effort."""
    path = os.path.abspath(request.path)
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="folder does not exist")
    try:
        _open_in_file_manager(path)
    except OSError as exc:
        return {"opened": False, "path": path, "error": str(exc)}
    return {"opened": True, "path": path}


# Keep this mount after every /api route: Starlette resolves routes in
# declaration order, so API requests remain JSON endpoints while / serves the
# plain static frontend shipped with the local application.
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")


def _file_as_dict(file_id: str) -> dict[str, Any] | None:
    record = db.get_file(file_id)
    return record.to_dict() if record is not None else None


def _unknown_job(job_id: str) -> None:
    raise HTTPException(status_code=404, detail=f"unknown scan job: {job_id}")


def _open_in_file_manager(path: str) -> None:
    """Launch the platform file manager without invoking a shell."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # Windows-only API
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path], close_fds=True)
    else:
        subprocess.Popen(["xdg-open", path], close_fds=True)


def _choose_local_folder(*, title: str = "Choose a folder or drive to investigate", initial_directory: str | None = None) -> str | None:
    """Show a native directory picker without adding a GUI dependency.

    Tk ships with standard CPython on the supported desktop platforms.  The
    temporary root is hidden so the only visible window is the OS directory
    picker.  A headless or stripped Python installation raises ``OSError`` so
    the frontend can explain that the picker is unavailable.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = filedialog.askdirectory(
                parent=root,
                title=title,
                initialdir=initial_directory or os.getcwd(),
                mustexist=True,
            )
        finally:
            root.destroy()
    except Exception as exc:  # Tk raises platform-specific TclError variants.
        raise OSError(str(exc)) from exc
    return selected or None


def _persist_env_value(key: str, value: str) -> bool:
    """Update one local .env setting without disturbing the other entries."""
    env_path = Path.cwd() / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        replacement = f"{key}={value}"
        for index, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[index] = replacement
                break
        else:
            if lines and lines[-1]:
                lines.append("")
            lines.append(replacement)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False
