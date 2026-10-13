"""Content extraction — Segment 5: text-shaped files (plain text/code,
PDF, and the three common Office XML formats) into a `ContentPayload`.

One job, matching PLAN.md Segment 5:
  - `extract(record, settings)`: given one `FileRecord` (as produced by
    `app.scanner.walk()`, or by `app.archives.extract_archive()` for a
    file that came out of an archive), return a `ContentPayload` if this
    module knows how to read that file's type, or `None` if it doesn't.
    `None` is a real, expected return value meaning "not mine, hand it
    to Segment 6" (per PLAN.md Segment 6: `extract_any()` tries this
    module first, then `media_binary.py`, then a bare metadata
    fallback). It is *not* this module's way of reporting failure on a
    file whose extension it does recognize — see "Recognized-but-broken
    files" below.

What this module owns (by extension, case-insensitive):
  - Plain text / data: `.txt/.text/.md/.markdown/.log/.csv/.tsv/.json/
    .jsonl/.ndjson/.xml/.yaml/.yml/.ini/.cfg/.conf/.toml/.rst/.tex/
    .properties`.
  - "Common code files": a curated (not exhaustive) list of popular
    source/script/markup/stylesheet extensions — see `_CODE_EXTENSIONS`
    below. Something textual but missing from this list isn't lost:
    Segment 6's magic-byte/content sniff is the documented fallback for
    "looks like text but has a weird/unlisted extension."
  - A short list of well-known *extensionless* filenames and dotfiles
    that `scanner.py` hands over with `record.ext == ""` (`Dockerfile`,
    `Makefile`, `README`, `LICENSE`, `.gitignore`, `.env`, ...) — matched
    by basename, see `_KNOWN_TEXT_BASENAMES` below. This is a deliberate
    addition beyond PLAN.md's literal extension list for this segment;
    flagged in PROJECT_STATE.md rather than silently expanding scope.
  - `.pdf` — text via `pypdf`; if the extracted text looks suspiciously
    thin relative to the page count (a strong signal of a scanned/
    image-only PDF with no text layer), the first few pages are
    rendered to images via `pymupdf` instead, and the payload comes
    back `kind="mixed"` (text + whatever thin text was found, since a
    scanned PDF's title page sometimes does carry a little real text).
  - `.docx` — `python-docx`: paragraph text + table cell text.
  - `.xlsx` — `openpyxl`, opened in streaming `read_only` mode: the
    first N rows of every sheet (`_MAX_XLSX_ROWS_PER_SHEET`), matching
    PLAN.md's "sample first N rows/sheet."
  - `.pptx` — `python-pptx`: every shape's text, table cell text, and
    speaker notes, per slide.
  - Legacy binary Office formats (`.doc`/`.xls`/`.ppt`, pre-2007) and
    OpenDocument formats (`.odt`/`.ods`/`.odp`) are **not** handled —
    PLAN.md's Segment 5 spec only calls out the XML-based formats above,
    and none of the libraries used here read the old binary ones anyway.
    `extract()` returns `None` for these, same as any other unrecognized
    extension, and they fall through to Segment 6.

Recognized-but-broken files: if the extension says this module owns the
file (e.g. `.pdf`) but the file itself is corrupt/truncated/not actually
that format, `extract()` does **not** return `None` — that would hand a
file this module *should* be able to explain off to Segment 6's generic
binary handling, which has no more insight into "this claims to be a PDF
but isn't" than this module does. Instead it returns a real
`ContentPayload` with `kind="metadata_only"`, `text_excerpt=None`, and a
short note in `metadata["error"]`, so the file still gets an
`AnalysisResult` instead of silently vanishing from a scan. This mirrors
the "never crash the pipeline, always yield *something*" posture the
rest of the codebase already has (`scanner.py`'s per-file
`status="error"` records, `archives.py`'s corrupt-archive handling). The
same shape is used if a required third-party library isn't installed at
all (see below) — this module never raises out of `extract()`.

Optional-import guarding: `pypdf`/`pymupdf`/`python-docx`/`openpyxl`/
`python-pptx` are all *intended* to be regular (non-optional)
dependencies for this segment — see `requirements.txt` — but each import
is still guarded at module load time, so a single missing library
degrades only the one format it backs (to the "recognized-but-broken"
shape above) rather than making this whole module fail to import and
silently taking plain-text/code extraction down with it. Same defensive
posture `archives.py` already uses for its own optional `py7zr`/
`rarfile` imports, applied here for load-time robustness even though
these five aren't meant to be optional in a normal install.

Truncation, matching PLAN.md's "cap it, don't load huge files whole":
  - Plain text/code: read at most `settings.max_file_read_mb` MB of raw
    bytes — a real read cap (`file.read(n)` only pulls `n` bytes off
    disk), not "read the whole file then slice a string." See
    `_read_capped_bytes()`.
  - `.csv`/`.tsv`: on top of the byte cap above, also capped to
    `_MAX_TABULAR_LINES` lines, so a huge CSV comes back as a clean
    N-row *sample* (header + first rows) instead of a byte-boundary cut
    that likely lands mid-row.
  - `.xlsx`: capped to `_MAX_XLSX_ROWS_PER_SHEET` rows per sheet, read
    via `openpyxl`'s `read_only` streaming iterator so a huge sheet is
    never loaded into memory just to sample its first few rows.
  - `.docx`/`.pptx`: PLAN.md doesn't specify a separate cap for these,
    so `settings.max_file_read_mb` is reused as a *character* budget
    here (there's no raw byte stream to cap a read against once a
    library has parsed the XML for us) — a backstop against a
    pathological thousand-page document, not a precise MB-of-file cap.
  - `.pdf`: same character-budget backstop across the page-by-page text
    extraction loop; page rendering (the scanned-PDF path) is separately
    capped to `_PDF_RENDER_PAGE_COUNT` pages regardless of budget, since
    images are the expensive part there, not characters.

Encoding fallback (plain text/code only — the PDF/Office formats above
handle their own text encoding internally via their libraries): try
UTF-8 first; on failure, retry after trimming up to 3 trailing bytes
(the longest an incomplete UTF-8 sequence can be — this specifically
rescues the common case where the byte cap above landed mid-character,
which would otherwise make an oversized-but-otherwise-clean UTF-8 file
decode via the much lossier Latin-1 path just because of its very last
character); if it still fails, fall back to Latin-1 with
`errors="replace"`, which cannot itself raise (every byte 0-255 is a
valid Latin-1 codepoint).

Nothing in this module reads a file's content more than once, and
nothing here calls `db.upsert_file()` or writes anywhere — same division
of responsibility as `archives.py`, that's the caller's job.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from app.config import Settings
from app.log import logger
from app.models import ContentPayload, FileRecord

# --------------------------------------------------------------------------
# Optional-but-expected third-party imports — see module docstring's
# "Optional-import guarding" section for why these are try/excepted even
# though none of them are meant to be actually optional in a real install.
# --------------------------------------------------------------------------

try:
    import pypdf

    _HAS_PYPDF = True
except ImportError:  # pragma: no cover - exercised only in a stripped-down env
    pypdf = None  # type: ignore[assignment]
    _HAS_PYPDF = False

try:
    import fitz  # PyMuPDF

    _HAS_FITZ = True
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore[assignment]
    _HAS_FITZ = False

try:
    import docx  # python-docx

    _HAS_DOCX = True
except ImportError:  # pragma: no cover
    docx = None  # type: ignore[assignment]
    _HAS_DOCX = False

try:
    import openpyxl

    _HAS_OPENPYXL = True
except ImportError:  # pragma: no cover
    openpyxl = None  # type: ignore[assignment]
    _HAS_OPENPYXL = False

try:
    from pptx import Presentation as _PptxPresentation

    _HAS_PPTX = True
except ImportError:  # pragma: no cover
    _PptxPresentation = None  # type: ignore[assignment]
    _HAS_PPTX = False


# --------------------------------------------------------------------------
# What this module recognizes
# --------------------------------------------------------------------------

_PLAIN_TEXT_EXTENSIONS = {
    ".txt", ".text", ".md", ".markdown", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".ndjson", ".xml", ".yaml", ".yml", ".ini",
    ".cfg", ".conf", ".toml", ".rst", ".tex", ".properties", ".svg",
}

# Curated, not exhaustive — see module docstring. Grouped loosely by
# ecosystem for readability; not alphabetized within a group.
_CODE_EXTENSIONS = {
    # Python / notebooks
    ".py", ".pyw", ".pyi", ".ipynb",
    # JS/TS ecosystem
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    # JVM / .NET
    ".java", ".kt", ".kts", ".scala", ".cs",
    # C-family / systems
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".rs", ".go",
    ".swift", ".m", ".mm", ".zig", ".asm", ".s",
    # Scripting / shell
    ".rb", ".php", ".pl", ".pm", ".lua", ".r", ".sh", ".bash", ".zsh",
    ".fish", ".ps1", ".psm1", ".bat", ".cmd",
    # Web/markup/style
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    # Data/query/schema languages
    ".sql", ".graphql", ".gql", ".proto",
    # Misc functional/other languages
    ".dart", ".clj", ".cljs", ".ex", ".exs", ".erl", ".hs", ".jl",
    ".nim", ".elm", ".vim",
    # Diffs/patches — plain text, commonly relevant to "investigate this"
    ".diff", ".patch",
}

# Extensionless filenames scanner.py hands over with ext == "" (matched
# by exact lowercased basename), plus a prefix rule for env-file
# variants like ".env.local" / ".env.production" (which *do* get a
# non-empty `ext` from `os.path.splitext` — ".local" / ".production" —
# so they need their own check rather than falling out of the basename
# match). Not part of PLAN.md's literal list for this segment; see
# module docstring and PROJECT_STATE.md.
_KNOWN_TEXT_BASENAMES = {
    "dockerfile", "makefile", "gnumakefile", "rakefile", "gemfile",
    "procfile", "vagrantfile",
    "readme", "license", "licence", "changelog", "changes", "authors",
    "contributors", "notice", "copying", "install", "news",
    ".gitignore", ".gitattributes", ".gitmodules", ".dockerignore",
    ".editorconfig", ".env", ".npmrc", ".yarnrc", ".babelrc",
    ".eslintrc", ".prettierrc", ".flake8", ".bashrc", ".bash_profile",
    ".zshrc", ".profile", ".vimrc", ".npmignore",
}
_ENV_FILE_BASENAME_PREFIX = ".env."  # .env.local, .env.production, ...

# --------------------------------------------------------------------------
# Truncation knobs not covered by an env var. PLAN.md only specifies
# MAX_FILE_READ_MB; these are this module's own documented judgment
# calls, layered on top, for row/page-oriented formats where a raw byte
# cap alone would produce an awkward cut.
# --------------------------------------------------------------------------

_MAX_TABULAR_LINES = 500            # .csv / .tsv
_MAX_XLSX_ROWS_PER_SHEET = 50       # .xlsx — PLAN.md: "sample first N rows/sheet"
_PDF_RENDER_PAGE_COUNT = 3          # pages rendered when a PDF looks scanned
_PDF_MIN_AVG_CHARS_PER_PAGE = 30.0  # below this average -> "suspiciously little text"
_PDF_RENDER_MAX_LONG_EDGE_PX = 1024  # matches PLAN.md Segment 6's stated image convention
_PDF_RENDER_JPEG_QUALITY = 85
_PDF_RENDER_MAX_ZOOM = 4.0          # sanity cap for unusually small/odd page sizes

# Detailed passes get fuller row and image samples. Text-byte/character
# budgets still come from Settings, which the detailed pass multiplies.
_DETAIL_MAX_TABULAR_LINES = 2_000
_DETAIL_MAX_XLSX_ROWS_PER_SHEET = 250
_DETAIL_PDF_RENDER_PAGE_COUNT = 8
_DETAIL_PDF_RENDER_MAX_LONG_EDGE_PX = 1600
_DETAIL_PDF_RENDER_JPEG_QUALITY = 90


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def extract(
    record: FileRecord,
    settings: Settings,
    detail_level: str = "fast",
) -> ContentPayload | None:
    """Entry point. Returns `None` if this module doesn't recognize
    `record`'s type — see the module docstring for exactly what's
    owned. Never raises: a recognized-but-unreadable file still comes
    back as a `ContentPayload` (`kind="metadata_only"`), not an
    exception or `None`.

    ``detail_level="detailed"`` preserves the public default while using
    larger CSV/XLSX samples and a higher-fidelity scanned-PDF render.
    """
    detail_level = _normalized_detail_level(detail_level)
    ext = record.ext.lower()
    basename = os.path.basename(record.path).lower()

    if ext in _PLAIN_TEXT_EXTENSIONS or ext in _CODE_EXTENSIONS or _is_known_text_basename(basename):
        return _extract_plain_text(record, settings, detail_level)
    if ext == ".pdf":
        return _extract_pdf(record, settings, detail_level)
    if ext == ".docx":
        return _extract_docx(record, settings, detail_level)
    if ext == ".xlsx":
        return _extract_xlsx(record, settings, detail_level)
    if ext == ".pptx":
        return _extract_pptx(record, settings, detail_level)
    return None


def _is_known_text_basename(basename: str) -> bool:
    return basename in _KNOWN_TEXT_BASENAMES or basename.startswith(_ENV_FILE_BASENAME_PREFIX)


def _normalized_detail_level(detail_level: str) -> str:
    """Treat an unknown level as the cheap, compatible fast mode."""
    return "detailed" if detail_level == "detailed" else "fast"


def _tabular_line_limit(detail_level: str) -> int:
    return _DETAIL_MAX_TABULAR_LINES if detail_level == "detailed" else _MAX_TABULAR_LINES


def _xlsx_row_limit(detail_level: str) -> int:
    return _DETAIL_MAX_XLSX_ROWS_PER_SHEET if detail_level == "detailed" else _MAX_XLSX_ROWS_PER_SHEET


# --------------------------------------------------------------------------
# Shared helpers: capped reads, encoding fallback, the shared
# "recognized but couldn't read it" payload shape
# --------------------------------------------------------------------------


def _read_capped_bytes(path: str, max_bytes: int) -> tuple[bytes, bool]:
    """Read at most `max_bytes` from `path` — a real read cap
    (`file.read(n)` only pulls `n` bytes off disk), not "read the whole
    file then slice a string." Returns `(data, truncated)`."""
    with open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        return data[:max_bytes], True
    return data, False


def _decode_bytes(raw: bytes) -> tuple[str, str]:
    """UTF-8 first; on failure, retry after trimming up to 3 trailing
    bytes (rescues the common case of a byte-cap truncation landing
    mid multi-byte character); final fallback is Latin-1 with
    `errors="replace"`, which cannot itself raise. Returns
    `(text, encoding_used)`."""
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        for trim in (1, 2, 3):
            if trim >= len(raw):
                break
            try:
                return raw[:-trim].decode("utf-8"), "utf-8"
            except UnicodeDecodeError:
                continue
        return raw.decode("latin-1", errors="replace"), "latin-1"


def _cap_lines(text: str, max_lines: int) -> tuple[str, bool]:
    """Cap `text` to its first `max_lines` lines. Returns `(text,
    truncated)`. A note is appended when truncated, so it's obvious to
    a reader (human or model) that rows were cut, not just silently
    absent."""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text, False
    kept = lines[:max_lines]
    kept.append(f"... ({len(lines) - max_lines} more line(s) not shown)")
    return "\n".join(kept), True


def _char_budget(settings: Settings) -> int:
    """`settings.max_file_read_mb` reused as a character budget for the
    structured (library-parsed) formats below, where there's no raw
    byte stream to cap a read against the way there is for plain
    text."""
    return settings.max_file_read_mb * 1024 * 1024


def _metadata_only_payload(record: FileRecord, error: str) -> ContentPayload:
    """Shared shape for "this module owns the extension but couldn't
    actually read this particular file" — see module docstring's
    "Recognized-but-broken files" section. Never `None`: the caller has
    already committed to this module owning the extension by the time
    this is used."""
    logger.warning("could not extract %s (%s): %s", record.display_path, record.ext, error)
    return ContentPayload(
        file_id=record.id,
        kind="metadata_only",
        text_excerpt=None,
        images_b64=[],
        metadata={"error": error},
        truncated=False,
    )


# --------------------------------------------------------------------------
# Plain text / code
# --------------------------------------------------------------------------


def _extract_plain_text(record: FileRecord, settings: Settings, detail_level: str) -> ContentPayload:
    max_bytes = settings.max_file_read_mb * 1024 * 1024
    try:
        raw, byte_truncated = _read_capped_bytes(record.path, max_bytes)
    except OSError as exc:
        return _metadata_only_payload(record, f"could not read file: {exc}")

    text, encoding_used = _decode_bytes(raw)
    truncated = byte_truncated

    if record.ext.lower() in (".csv", ".tsv"):
        text, line_truncated = _cap_lines(text, _tabular_line_limit(detail_level))
        truncated = truncated or line_truncated

    return ContentPayload(
        file_id=record.id,
        kind="text",
        text_excerpt=text,
        images_b64=[],
        metadata={"encoding": encoding_used, "bytes_read": len(raw), "detail_level": detail_level},
        truncated=truncated,
    )


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def _extract_pdf(record: FileRecord, settings: Settings, detail_level: str) -> ContentPayload:
    if not _HAS_PYPDF:
        return _metadata_only_payload(record, "pypdf not installed")

    try:
        reader = pypdf.PdfReader(record.path)
        page_count = len(reader.pages)
    except Exception as exc:  # pypdf raises several distinct types for corrupt/non-PDF input
        return _metadata_only_payload(record, f"could not open PDF: {exc}")

    if page_count == 0:
        return ContentPayload(
            file_id=record.id,
            kind="metadata_only",
            text_excerpt=None,
            images_b64=[],
            metadata={"page_count": 0},
            truncated=False,
        )

    char_budget = _char_budget(settings)
    text_parts: list[str] = []
    total_chars = 0
    pages_read = 0
    budget_hit = False
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            logger.debug("pypdf failed to extract text from one page of %s: %s", record.display_path, exc)
            page_text = ""
        text_parts.append(page_text)
        total_chars += len(page_text)
        pages_read += 1
        if total_chars >= char_budget:
            budget_hit = True
            break

    text = "\n\n".join(text_parts)
    avg_chars_per_page = total_chars / pages_read if pages_read else 0.0
    truncated = budget_hit or pages_read < page_count

    if avg_chars_per_page >= _PDF_MIN_AVG_CHARS_PER_PAGE:
        return ContentPayload(
            file_id=record.id,
            kind="text",
            text_excerpt=text,
            images_b64=[],
            metadata={"page_count": page_count, "avg_chars_per_page": round(avg_chars_per_page, 1), "detail_level": detail_level},
            truncated=truncated,
        )

    # Suspiciously little text relative to page count -- likely a
    # scanned/image-only PDF. Render the first few pages as images so
    # the vision model has something to actually look at.
    images_b64, render_error = _render_pdf_pages(record.path, detail_level)
    metadata: dict[str, Any] = {
        "page_count": page_count,
        "avg_chars_per_page": round(avg_chars_per_page, 1),
        "rendered_pages": len(images_b64),
        "detail_level": detail_level,
    }
    if render_error:
        metadata["render_error"] = render_error

    if images_b64:
        return ContentPayload(
            file_id=record.id,
            kind="mixed",
            text_excerpt=text or None,
            images_b64=images_b64,
            metadata=metadata,
            truncated=truncated or page_count > _pdf_render_page_count(detail_level),
        )

    # Couldn't render either (pymupdf missing, or every page failed to
    # render) -- fall back to whatever thin text was found, rather than
    # giving up on the file entirely.
    return ContentPayload(
        file_id=record.id,
        kind="text" if text.strip() else "metadata_only",
        text_excerpt=text or None,
        images_b64=[],
        metadata=metadata,
        truncated=truncated,
    )


def _pdf_render_page_count(detail_level: str) -> int:
    return _DETAIL_PDF_RENDER_PAGE_COUNT if detail_level == "detailed" else _PDF_RENDER_PAGE_COUNT


def _pdf_render_long_edge(detail_level: str) -> int:
    return _DETAIL_PDF_RENDER_MAX_LONG_EDGE_PX if detail_level == "detailed" else _PDF_RENDER_MAX_LONG_EDGE_PX


def _pdf_render_jpeg_quality(detail_level: str) -> int:
    return _DETAIL_PDF_RENDER_JPEG_QUALITY if detail_level == "detailed" else _PDF_RENDER_JPEG_QUALITY


def _render_pdf_pages(path: str, detail_level: str) -> tuple[list[str], str | None]:
    """Render the first `_PDF_RENDER_PAGE_COUNT` pages of the PDF at
    `path` to base64-encoded JPEGs (no data-uri prefix, matching
    `ContentPayload.images_b64`'s documented shape), scaled so each
    page's long edge is about `_PDF_RENDER_MAX_LONG_EDGE_PX` pixels —
    the convention PLAN.md's Segment 6 spec states for images generally
    ("downscale to a max long edge (e.g. 1024px)"), applied here since
    Segment 6 doesn't exist yet for this module to delegate to (worth
    reconciling once it does — see PROJECT_STATE.md). Returns
    `(images_b64, error_message_or_None)`. Never raises: a mid-loop
    failure on one page just returns whatever pages rendered
    successfully before it; total failure returns `([], error)`."""
    if not _HAS_FITZ:
        return [], "pymupdf not installed"

    try:
        doc = fitz.open(path)
    except Exception as exc:
        return [], f"could not open PDF for rendering: {exc}"

    images: list[str] = []
    try:
        page_count = doc.page_count
        for i in range(min(_pdf_render_page_count(detail_level), page_count)):
            try:
                page = doc[i]
                rect = page.rect
                long_edge_pts = max(rect.width, rect.height) or 1.0
                zoom = min(_pdf_render_long_edge(detail_level) / long_edge_pts, _PDF_RENDER_MAX_ZOOM)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                jpg_bytes = pix.tobytes("jpg", jpg_quality=_pdf_render_jpeg_quality(detail_level))
                images.append(base64.b64encode(jpg_bytes).decode("ascii"))
            except Exception as exc:
                logger.debug("could not render PDF page %d of %s: %s", i, path, exc)
                continue
    finally:
        doc.close()

    return images, None


# --------------------------------------------------------------------------
# .docx
# --------------------------------------------------------------------------


def _extract_docx(record: FileRecord, settings: Settings, detail_level: str) -> ContentPayload:
    if not _HAS_DOCX:
        return _metadata_only_payload(record, "python-docx not installed")

    try:
        document = docx.Document(record.path)
    except Exception as exc:
        return _metadata_only_payload(record, f"could not open DOCX: {exc}")

    char_budget = _char_budget(settings)
    parts: list[str] = []
    total_chars = 0
    truncated = False

    for para in document.paragraphs:
        text = para.text
        if not text.strip():
            continue
        parts.append(text)
        total_chars += len(text)
        if total_chars >= char_budget:
            truncated = True
            break

    if not truncated:
        for table in document.tables:
            row_break = False
            for row in table.rows:
                row_text = "\t".join(cell.text for cell in row.cells)
                if not row_text.strip():
                    continue
                parts.append(row_text)
                total_chars += len(row_text)
                if total_chars >= char_budget:
                    truncated = True
                    row_break = True
                    break
            if row_break:
                break

    return ContentPayload(
        file_id=record.id,
        kind="text",
        text_excerpt="\n".join(parts),
        images_b64=[],
        metadata={"paragraph_count": len(document.paragraphs), "table_count": len(document.tables), "detail_level": detail_level},
        truncated=truncated,
    )


# --------------------------------------------------------------------------
# .xlsx
# --------------------------------------------------------------------------


def _extract_xlsx(record: FileRecord, settings: Settings, detail_level: str) -> ContentPayload:
    if not _HAS_OPENPYXL:
        return _metadata_only_payload(record, "openpyxl not installed")

    try:
        workbook = openpyxl.load_workbook(record.path, read_only=True, data_only=True)
    except Exception as exc:
        return _metadata_only_payload(record, f"could not open XLSX: {exc}")

    char_budget = _char_budget(settings)
    parts: list[str] = []
    total_chars = 0
    truncated = False
    sheet_names = list(workbook.sheetnames)

    try:
        for sheet_name in sheet_names:
            sheet = workbook[sheet_name]
            parts.append(f"## Sheet: {sheet_name}")
            row_count = 0
            budget_hit = False
            for row in sheet.iter_rows(values_only=True):
                row_limit = _xlsx_row_limit(detail_level)
                if row_count >= row_limit:
                    parts.append(f"... (more rows not shown, capped at {row_limit}/sheet)")
                    truncated = True
                    break
                row_text = "\t".join("" if v is None else str(v) for v in row)
                parts.append(row_text)
                total_chars += len(row_text)
                row_count += 1
                if total_chars >= char_budget:
                    truncated = True
                    budget_hit = True
                    break
            if budget_hit:
                break
    finally:
        workbook.close()

    return ContentPayload(
        file_id=record.id,
        kind="text",
        text_excerpt="\n".join(parts),
        images_b64=[],
        metadata={"sheet_count": len(sheet_names), "sheet_names": sheet_names, "detail_level": detail_level},
        truncated=truncated,
    )


# --------------------------------------------------------------------------
# .pptx
# --------------------------------------------------------------------------


def _extract_pptx(record: FileRecord, settings: Settings, detail_level: str) -> ContentPayload:
    if not _HAS_PPTX:
        return _metadata_only_payload(record, "python-pptx not installed")

    try:
        presentation = _PptxPresentation(record.path)
    except Exception as exc:
        return _metadata_only_payload(record, f"could not open PPTX: {exc}")

    slide_count = len(presentation.slides)
    char_budget = _char_budget(settings)
    parts: list[str] = []
    total_chars = 0
    truncated = False
    slides_read = 0

    for i, slide in enumerate(presentation.slides, start=1):
        slide_parts = [f"## Slide {i}"]
        for shape in slide.shapes:
            try:
                if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                    slide_parts.append(shape.text_frame.text)
                elif getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        row_text = "\t".join(cell.text for cell in row.cells)
                        if row_text.strip():
                            slide_parts.append(row_text)
            except Exception as exc:
                logger.debug("skipping one shape on slide %d of %s: %s", i, record.display_path, exc)
                continue

        # `slide.notes_slide` *creates* a notes slide as a side effect if
        # one doesn't already exist -- `has_notes_slide` is the
        # side-effect-free check, always tested first.
        try:
            if slide.has_notes_slide:
                notes_text = slide.notes_slide.notes_text_frame.text
                if notes_text.strip():
                    slide_parts.append(f"[speaker notes] {notes_text}")
        except Exception as exc:
            logger.debug("skipping notes on slide %d of %s: %s", i, record.display_path, exc)

        slide_text = "\n".join(slide_parts)
        parts.append(slide_text)
        total_chars += len(slide_text)
        slides_read += 1
        if total_chars >= char_budget:
            truncated = True
            break

    return ContentPayload(
        file_id=record.id,
        kind="text",
        text_excerpt="\n\n".join(parts),
        images_b64=[],
        metadata={"slide_count": slide_count, "detail_level": detail_level},
        truncated=truncated or slides_read < slide_count,
    )


# --------------------------------------------------------------------------
# Self-test: `python -m app.extractors.text_docs --selftest`
# --------------------------------------------------------------------------


def _run_selftest() -> None:  # noqa: C901 - linear fixture-building test, not worth splitting up
    """Builds one throwaway sample of every type this module handles
    (plus a couple of edge cases: an extensionless known-basename file,
    an invalid-UTF-8 file, an oversized file to exercise the byte-cap
    truncation path, and two file types this module should decline),
    runs `extract()` against each, prints `kind` + excerpt length (or
    image count) per PLAN.md's acceptance-test wording, and asserts the
    shape that matters for each. Fixtures are generated on the fly
    rather than read from `test_assets/` — that directory belongs to
    Segment 17, and every other segment's own `--selftest` so far
    (`archives.py` in particular) already builds its own throwaway
    fixtures the same way."""
    import json
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as work_dir:
        app_data_dir = os.path.join(work_dir, "data")
        os.makedirs(app_data_dir, exist_ok=True)
        settings = Settings(
            ollama_host="http://localhost:11434",
            ollama_model="llama3.2-vision",
            ollama_fast_model=None,
            ollama_detailed_model=None,
            ollama_timeout_seconds=120,
            ollama_max_concurrency=2,
            app_data_dir=app_data_dir,
            max_archive_depth=3,
            max_archive_total_mb=2000,
            max_file_read_mb=1,  # small on purpose, so a truncation case is cheap to build
            app_port=8731,
        )

        def make_record(path: str) -> FileRecord:
            ext = os.path.splitext(path)[1].lower()
            return FileRecord(
                id=f"id-{os.path.basename(path)}",
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

        def report(label: str, payload: ContentPayload | None) -> None:
            if payload is None:
                print(f"  [{label}] -> None (not ours, would pass to Segment 6)")
                return
            excerpt_len = len(payload.text_excerpt) if payload.text_excerpt else 0
            print(
                f"  [{label}] kind={payload.kind} excerpt_chars={excerpt_len} "
                f"images={len(payload.images_b64)} truncated={payload.truncated}"
            )

        print("Segment 5 self-test — building throwaway fixtures...")

        # --- plain text (UTF-8 with real non-ASCII content) ----------
        txt_path = os.path.join(work_dir, "sample.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("Field notes: café, naïve, façade — plain UTF-8 text.\nSecond line.\n")
        payload = extract(make_record(txt_path), settings)
        report("sample.txt", payload)
        assert payload is not None and payload.kind == "text" and not payload.truncated
        assert payload.metadata["encoding"] == "utf-8"

        # --- oversized text file -> byte-cap truncation ---------------
        big_path = os.path.join(work_dir, "big.log")
        with open(big_path, "w", encoding="utf-8") as f:
            f.write("x" * (2 * 1024 * 1024))  # 2MB, cap is 1MB
        payload = extract(make_record(big_path), settings)
        report("big.log (2MB, 1MB cap)", payload)
        assert payload is not None and payload.truncated is True
        assert len(payload.text_excerpt) <= 1024 * 1024

        # --- invalid UTF-8 -> latin-1 fallback -------------------------
        bad_encoding_path = os.path.join(work_dir, "bad_encoding.txt")
        with open(bad_encoding_path, "wb") as f:
            f.write(b"leading ascii \xff\xfe not valid utf-8 \x80\x81 trailing")
        payload = extract(make_record(bad_encoding_path), settings)
        report("bad_encoding.txt", payload)
        assert payload is not None and payload.metadata["encoding"] == "latin-1"

        # --- CSV -> line-cap truncation ---------------------------------
        csv_path = os.path.join(work_dir, "sample.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("id,name,amount\n")
            for i in range(600):
                f.write(f"{i},row{i},{i * 1.5}\n")
        payload = extract(make_record(csv_path), settings)
        report("sample.csv (600 rows)", payload)
        assert payload is not None and payload.truncated is True
        assert payload.text_excerpt.count("\n") <= _MAX_TABULAR_LINES + 2

        # --- JSON --------------------------------------------------------
        json_path = os.path.join(work_dir, "sample.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"case_id": "DS-001", "tags": ["evidence", "photo"]}, f)
        payload = extract(make_record(json_path), settings)
        report("sample.json", payload)
        assert payload is not None and payload.kind == "text"

        # --- code file -----------------------------------------------
        py_path = os.path.join(work_dir, "sample.py")
        with open(py_path, "w", encoding="utf-8") as f:
            f.write("def suspicious():\n    return 'this looks like a code file'\n")
        payload = extract(make_record(py_path), settings)
        report("sample.py", payload)
        assert payload is not None and payload.kind == "text"

        # --- extensionless known basename -----------------------------
        dockerfile_path = os.path.join(work_dir, "Dockerfile")
        with open(dockerfile_path, "w", encoding="utf-8") as f:
            f.write("FROM python:3.12-slim\nCMD [\"python\", \"app.py\"]\n")
        payload = extract(make_record(dockerfile_path), settings)
        report("Dockerfile (extensionless)", payload)
        assert payload is not None and payload.kind == "text"

        gitignore_path = os.path.join(work_dir, ".gitignore")
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write("__pycache__/\n*.pyc\n")
        payload = extract(make_record(gitignore_path), settings)
        report(".gitignore (dotfile)", payload)
        assert payload is not None and payload.kind == "text"

        # --- PDF with real text ----------------------------------------
        text_pdf_path = os.path.join(work_dir, "sample.pdf")
        if _HAS_FITZ:
            doc = fitz.open()
            for page_num in range(2):
                page = doc.new_page()
                page.insert_text(
                    (50, 72),
                    f"Page {page_num + 1}: this PDF has a real, extractable text layer. " * 6,
                    fontsize=11,
                )
            doc.save(text_pdf_path)
            doc.close()
            payload = extract(make_record(text_pdf_path), settings)
            report("sample.pdf (real text)", payload)
            assert payload is not None and payload.kind == "text"
            assert payload.metadata["avg_chars_per_page"] >= _PDF_MIN_AVG_CHARS_PER_PAGE
        else:
            print("  [sample.pdf] skipped -- pymupdf not installed, can't build a fixture")

        # --- scanned-style PDF (image only, no text layer) -> mixed ---
        scanned_pdf_path = os.path.join(work_dir, "scanned.pdf")
        if _HAS_FITZ:
            from PIL import Image

            page_image_path = os.path.join(work_dir, "page_image.png")
            Image.new("RGB", (600, 800), color=(230, 230, 230)).save(page_image_path)

            doc = fitz.open()
            for _ in range(2):
                page = doc.new_page()
                page.insert_image(page.rect, filename=page_image_path)
            doc.save(scanned_pdf_path)
            doc.close()
            payload = extract(make_record(scanned_pdf_path), settings)
            report("scanned.pdf (image only)", payload)
            assert payload is not None and payload.kind == "mixed"
            assert len(payload.images_b64) > 0
        else:
            print("  [scanned.pdf] skipped -- pymupdf not installed, can't build a fixture")

        # --- DOCX --------------------------------------------------------
        docx_path = os.path.join(work_dir, "sample.docx")
        if _HAS_DOCX:
            document = docx.Document()
            document.add_paragraph("Case summary: nothing unusual found in this fixture.")
            table = document.add_table(rows=2, cols=2)
            table.rows[0].cells[0].text = "Item"
            table.rows[0].cells[1].text = "Status"
            table.rows[1].cells[0].text = "Backup drive"
            table.rows[1].cells[1].text = "Clean"
            document.save(docx_path)
            payload = extract(make_record(docx_path), settings)
            report("sample.docx", payload)
            assert payload is not None and payload.kind == "text"
            assert "Backup drive" in payload.text_excerpt
        else:
            print("  [sample.docx] skipped -- python-docx not installed, can't build a fixture")

        # --- XLSX (row-cap truncation on one sheet) ---------------------
        xlsx_path = os.path.join(work_dir, "sample.xlsx")
        if _HAS_OPENPYXL:
            wb = openpyxl.Workbook()
            ws1 = wb.active
            ws1.title = "Summary"
            ws1.append(["metric", "value"])
            ws1.append(["files_scanned", 42])
            ws2 = wb.create_sheet("BigSheet")
            for i in range(80):  # > _MAX_XLSX_ROWS_PER_SHEET
                ws2.append([i, f"row-{i}"])
            wb.save(xlsx_path)
            payload = extract(make_record(xlsx_path), settings)
            report("sample.xlsx (80-row sheet)", payload)
            assert payload is not None and payload.truncated is True
            assert payload.metadata["sheet_count"] == 2
        else:
            print("  [sample.xlsx] skipped -- openpyxl not installed, can't build a fixture")

        # --- PPTX --------------------------------------------------------
        pptx_path = os.path.join(work_dir, "sample.pptx")
        if _HAS_PPTX:
            prs = _PptxPresentation()
            blank_layout = prs.slide_layouts[6]
            slide1 = prs.slides.add_slide(prs.slide_layouts[0])
            slide1.shapes.title.text = "Investigation Summary"
            slide2 = prs.slides.add_slide(blank_layout)
            box = slide2.shapes.add_textbox(0, 0, prs.slide_width, prs.slide_height)
            box.text_frame.text = "Second slide body text for the fixture."
            prs.save(pptx_path)
            payload = extract(make_record(pptx_path), settings)
            report("sample.pptx", payload)
            assert payload is not None and payload.kind == "text"
            assert payload.metadata["slide_count"] == 2
        else:
            print("  [sample.pptx] skipped -- python-pptx not installed, can't build a fixture")

        # --- things this module correctly declines ----------------------
        exe_path = os.path.join(work_dir, "unknown.exe")
        with open(exe_path, "wb") as f:
            f.write(b"\x4d\x5a\x00\x00fake-pe-header")
        payload = extract(make_record(exe_path), settings)
        report("unknown.exe", payload)
        assert payload is None

        jpg_path = os.path.join(work_dir, "photo.jpg")
        with open(jpg_path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
        payload = extract(make_record(jpg_path), settings)
        report("photo.jpg", payload)
        assert payload is None

        print("OK")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker text/document extraction utility")
    parser.add_argument("--selftest", action="store_true", help="build sample fixtures and run extract() on each")
    args = parser.parse_args()

    if args.selftest:
        _run_selftest()
    else:
        parser.print_help()
