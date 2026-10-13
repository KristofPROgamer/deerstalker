"""Core data contracts, shared verbatim by every later segment.

These five models are copied field-for-field from PLAN.md §3. Field names
and types must not drift — every other segment imports these classes
directly (`from app.models import FileRecord`, etc.) and relies on exact
field names. If a later segment needs an extra field, add it here with a
sensible default and record the deviation in PROJECT_STATE.md rather than
inventing a parallel shape.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DeerstalkerModel(BaseModel):
    """Shared base class giving every contract model the same
    serialization helpers, so segments have one consistent way to move a
    model in and out of SQLite TEXT columns, HTTP JSON bodies, etc."""

    def to_json(self) -> str:
        """Serialize to a JSON string (used for DB TEXT columns and API
        responses)."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> "DeerstalkerModel":
        """Deserialize from a JSON string produced by `to_json()`."""
        return cls.model_validate_json(data)

    def to_dict(self) -> dict:
        """Serialize to a plain dict (used for API responses that get
        wrapped in a larger JSON object)."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "DeerstalkerModel":
        """Deserialize from a plain dict produced by `to_dict()`."""
        return cls.model_validate(data)


class FileRecord(DeerstalkerModel):
    """One file discovered by the scanner (or extracted from an archive)."""

    id: str  # stable hash of the absolute path
    path: str  # real filesystem path (readable right now)
    display_path: str  # human path, shows archive lineage: "backup.zip/photos/img1.jpg"
    source_archive_id: str | None
    ext: str
    size_bytes: int
    mime_type: str | None
    mtime: float
    is_from_archive: bool
    status: Literal["pending", "fast_done", "detailed_done", "error", "skipped"]
    error_message: str | None


class ContentPayload(DeerstalkerModel):
    """What an extractor hands to the prompt builders for one file."""

    file_id: str
    kind: Literal["text", "image", "mixed", "metadata_only"]
    text_excerpt: str | None
    images_b64: list[str] = Field(default_factory=list)  # resized, no data-uri prefix
    metadata: dict = Field(default_factory=dict)  # exif / page count / duration / whatever's relevant
    truncated: bool


class SearchIntent(DeerstalkerModel):
    """One entry in the search-intent registry (see PLAN.md §5)."""

    id: str
    label: str
    description: str
    icon: str  # emoji or icon key, frontend uses this
    fast_prompt_template: str
    detailed_prompt_template: str


class AnalysisResult(DeerstalkerModel):
    """One model judgement about one file, for one intent, at one pass
    depth."""

    id: str
    file_id: str
    job_id: str
    pass_type: Literal["fast", "detailed"]
    intent_id: str | None  # None for a free-text custom-query result
    tags: list[str] = Field(default_factory=list)
    relevance_score: float  # 0..1
    summary: str
    reasoning: str
    flagged_interesting: bool
    model_used: str
    created_at: float


class ScanJob(DeerstalkerModel):
    """One end-to-end scan run."""

    id: str
    roots: list[str]
    intent_ids: list[str]
    custom_query: str | None
    status: Literal["pending", "scanning", "fast_pass", "detailed_pass", "done", "cancelled", "error"]
    counts: dict = Field(default_factory=dict)  # total, scanned, matched, errored
    output_folder: str
    report_path: str | None
    created_at: float
