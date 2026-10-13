"""Archive handling — Segment 4.

Two jobs, matching PLAN.md Segment 4:
  - `is_archive(path)`: does this path *look* like an archive, by
    extension? This says nothing about whether we can actually open it
    (a `.rar` is still `is_archive() == True` even if `rarfile` isn't
    installed) — that distinction is `extract_archive()`'s problem, not
    the caller's.
  - `extract_archive(record, settings)`: a context manager that expands
    one archive `FileRecord` (as produced by `app.scanner.walk()`) into
    the `FileRecord`s for everything *inside* it, extracted to a private
    temp directory that's guaranteed to be gone when the `with` block
    exits — success, exception, or otherwise.

Formats:
  - `.zip`, `.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, and (bonus, same
    stdlib `tarfile` support, no extra cost) `.tar.xz`/`.txz` always work
    (stdlib `zipfile`/`tarfile`).
  - `.7z` works if `py7zr` is installed; `.rar` works if `rarfile` is
    installed (and, per that library's own requirement, an `unrar`/`unar`/
    `bsdtar` binary is reachable on PATH). Neither is a hard dependency —
    see requirements.txt. Missing library or a corrupt/unreadable archive
    both degrade the same way: log a warning, expand nothing for that
    archive, and let the caller fall back to treating the archive file as
    an ordinary opaque file (which is what it already is, as far as the
    scanner/caller is concerned — see "What `extract_archive()` returns"
    below).

Nesting: an archive found *inside* an archive is itself expanded, up to
`settings.max_archive_depth` levels deep (the top-level archive passed in
counts as depth 1 and is always attempted — the depth setting only caps
how much further *nested* archives get auto-expanded). Past that depth,
a nested archive is left as a single opaque `FileRecord` instead of being
opened.

Safety:
  - zip-slip: every member's extraction target is resolved and checked to
    stay under that archive's own extraction subdirectory before being
    written. A member that fails this check is skipped (logged), not
    fatal to the rest of the archive.
  - Symlink members (tar can store these; zip does via Unix external-attr
    bits) are never extracted — skipped and logged. A symlink inside an
    archive is exactly the zip-slip trick in a different shape (point a
    link outside the extraction dir, then "extract" a same-named file
    through it), so the simplest safe answer is to not follow them.
  - `settings.max_archive_total_mb` is a running budget shared across the
    whole `extract_archive()` call (top-level archive + everything nested
    under it). Once it's exhausted, extraction of the archive currently
    being read stops (whatever was already extracted is kept and
    returned) — a zip-bomb guard, not a per-archive limit.

What `extract_archive()` returns: only the records for what's *inside*
the archive — not a record for the archive file itself (the caller
already has that; it's the `record` argument). If the top-level archive
can't be expanded at all (unsupported/missing-library format, or
corrupt), the yielded list is simply empty `[]` — this is not an error
the caller needs to handle specially, it just means "nothing to add;
treat `record` as an ordinary file like any other," which is exactly
what happens anyway once it reaches Segment 5/6's extractors (an
unopenable `.rar` falls through their format-specific handlers same as
any other binary they don't recognize, and comes out `kind="metadata_only"`
— archives.py doesn't need to force that, it's just what already happens).
"""

from __future__ import annotations

import os
import re
import shutil
import stat as stat_module
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Callable

from app.config import Settings
from app.log import logger
from app.models import FileRecord

import hashlib
import mimetypes

try:
    import py7zr

    _HAVE_PY7ZR = True
except ImportError:
    _HAVE_PY7ZR = False

try:
    import rarfile

    _HAVE_RARFILE = True
except ImportError:
    _HAVE_RARFILE = False


# --------------------------------------------------------------------------
# is_archive()
# --------------------------------------------------------------------------

# Order matters only in that longer/compound suffixes should be checked
# before their shorter siblings if we ever do prefix-sensitive logic; for
# a plain `endswith()` check on a tuple it doesn't, but grouped by format
# for readability.
_ZIP_SUFFIXES = (".zip",)
_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
_SEVEN_ZIP_SUFFIXES = (".7z",)
_RAR_SUFFIXES = (".rar",)

_ALL_ARCHIVE_SUFFIXES = _ZIP_SUFFIXES + _TAR_SUFFIXES + _SEVEN_ZIP_SUFFIXES + _RAR_SUFFIXES


def is_archive(path: str) -> bool:
    """Does `path` look like an archive, by extension?

    Extension-only, case-insensitive, and deliberately blind to whether
    the format is actually openable right now (e.g. `.rar` is `True` here
    even without `rarfile` installed) — that's `extract_archive()`'s
    concern. This keeps `is_archive()` cheap enough to call on every file
    the scanner yields without needing to touch the filesystem.
    """
    lower = path.lower()
    return lower.endswith(_ALL_ARCHIVE_SUFFIXES)


def _format_for(path: str) -> str | None:
    """Which handler owns this extension? Returns `"zip"`, `"tar"`,
    `"7z"`, `"rar"`, or `None` if it isn't a recognized archive suffix at
    all (shouldn't happen for anything `is_archive()` already said yes
    to, but this is called recursively on names discovered inside other
    archives too, so it's kept defensive)."""
    lower = path.lower()
    if lower.endswith(_ZIP_SUFFIXES):
        return "zip"
    if lower.endswith(_TAR_SUFFIXES):
        return "tar"
    if lower.endswith(_SEVEN_ZIP_SUFFIXES):
        return "7z"
    if lower.endswith(_RAR_SUFFIXES):
        return "rar"
    return None


# --------------------------------------------------------------------------
# extract_archive()
# --------------------------------------------------------------------------


@contextmanager
def extract_archive(record: FileRecord, settings: Settings) -> Iterator[list[FileRecord]]:
    """Expand the archive at `record.path` into `FileRecord`s for its
    contents (recursing into nested archives per the module docstring),
    for the duration of the `with` block. The temp directory everything
    was extracted into is removed when the block exits, no matter how it
    exits — extract once, do all analysis of the contents *inside* the
    `with`, don't hold onto `.path` values from the yielded records past
    it.

    Usage:
        with extract_archive(record, settings) as contents:
            for inner in contents:
                ...  # inner.path is real and readable, but only in here
    """
    tmp_base = os.path.join(settings.app_data_dir, "tmp")
    os.makedirs(tmp_base, exist_ok=True)
    extraction_root = tempfile.mkdtemp(dir=tmp_base, prefix="archive_")

    out: list[FileRecord] = []
    try:
        max_depth = max(1, settings.max_archive_depth)
        budget = _ExtractionBudget(max(0, settings.max_archive_total_mb) * 1024 * 1024)
        try:
            _expand_one_archive(
                archive_path=record.path,
                archive_display_path=record.display_path,
                extraction_root=extraction_root,
                depth=1,
                max_depth=max_depth,
                budget=budget,
                source_archive_id=record.id,
                out=out,
            )
        except Exception:
            # Belt-and-suspenders: every format-specific path below already
            # catches its own errors, but a genuinely unexpected failure
            # here should degrade to "nothing expanded," never crash the
            # caller's pipeline over one bad archive.
            logger.exception("unexpected failure expanding archive %s — yielding no contents", record.path)
        yield out
    finally:
        shutil.rmtree(extraction_root, ignore_errors=True)


class _ExtractionBudget:
    """Shared, mutable byte budget for one `extract_archive()` call —
    the zip-bomb guard (`MAX_ARCHIVE_TOTAL_MB`). One instance is threaded
    through the whole recursive expansion (top-level archive + everything
    nested under it), not reset per-archive."""

    def __init__(self, max_bytes: int) -> None:
        self._remaining = max_bytes

    def try_consume(self, n: int) -> bool:
        if n > self._remaining:
            return False
        self._remaining -= n
        return True


@dataclass
class _Member:
    """One entry inside an archive, normalized across zip/tar/rar so the
    generic extraction loop doesn't need to know which library it came
    from."""

    name: str  # internal path within the archive, as stored (may use '\' on Windows-authored zips)
    size: int
    is_dir: bool
    is_symlink: bool
    mtime: float
    opener: Callable[[], BinaryIO | None]  # call to get a readable file-like object; None if not extractable


def _expand_one_archive(
    *,
    archive_path: str,
    archive_display_path: str,
    extraction_root: str,
    depth: int,
    max_depth: int,
    budget: _ExtractionBudget,
    source_archive_id: str,
    out: list[FileRecord],
) -> bool:
    """Try to open and fully expand the archive at `archive_path`
    (contents appended to `out`, recursing into nested archives as
    depth allows). Returns `True` if this archive was opened and at
    least attempted (even if some/all members were skipped, e.g. due to
    the zip-slip guard or the size budget running out) — meaning the
    caller should NOT also record `archive_path` itself as an opaque
    leaf file, since expansion was attempted. Returns `False` if the
    archive couldn't be opened at all (unrecognized/unsupported format,
    missing optional library, or corrupt/unreadable file) — meaning the
    caller SHOULD fall back to treating it as an ordinary opaque file.
    """
    fmt = _format_for(archive_path)
    dest_dir = tempfile.mkdtemp(dir=extraction_root, prefix=f"d{depth}_")

    if fmt == "7z":
        return _expand_7z(archive_path, archive_display_path, dest_dir, depth, max_depth, budget, source_archive_id, out)

    opener_fn: Callable[[str], tuple[Any, list[_Member]]] | None = {
        "zip": _open_zip,
        "tar": _open_tar,
        "rar": _open_rar,
    }.get(fmt)

    if opener_fn is None:
        logger.warning("archive type unsupported here (unrecognized extension): %s", archive_path)
        return False

    if fmt == "rar" and not _HAVE_RARFILE:
        logger.warning("archive type unsupported here (rarfile not installed): %s", archive_path)
        return False

    try:
        handle, members = opener_fn(archive_path)
    except Exception as exc:
        logger.warning("could not open/read archive %s (%s) — leaving unexpanded: %s", archive_path, fmt, exc)
        return False

    # `handle` (the ZipFile/TarFile/RarFile) has to stay open for the
    # whole loop below — each `member.opener()` reads lazily from it —
    # and only gets closed once every member has been extracted (or
    # skipped).
    try:
        for member in members:
            if member.is_dir:
                continue
            if member.is_symlink:
                logger.debug("skipping symlink member (not followed): %s in %s", member.name, archive_path)
                continue

            internal_name = member.name.replace("\\", "/").strip("/")
            if not internal_name:
                continue
            member_display_path = f"{archive_display_path}/{internal_name}"

            target_path = _safe_extract_target(dest_dir, internal_name)
            if target_path is None:
                logger.warning("skipping suspicious path (zip-slip guard) in %s: %s", archive_path, member.name)
                continue
            target_path = _sanitize_target_path(dest_dir, target_path)

            if not budget.try_consume(max(0, member.size)):
                logger.warning(
                    "MAX_ARCHIVE_TOTAL_MB budget exhausted while expanding %s — stopping further expansion here",
                    archive_path,
                )
                break

            try:
                source = member.opener()
                if source is None:
                    logger.debug("member has no readable stream, skipping: %s in %s", member.name, archive_path)
                    continue
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with source, open(target_path, "wb") as dest:
                    shutil.copyfileobj(source, dest)
            except Exception as exc:
                logger.warning("failed to extract member %s from %s: %s", member.name, archive_path, exc)
                continue

            try:
                os.utime(target_path, (member.mtime, member.mtime))
            except (OSError, OverflowError, ValueError):
                pass  # cosmetic only — mtime from the archive can be nonsense; never worth failing extraction over

            _process_extracted_member(
                on_disk_path=target_path,
                display_path=member_display_path,
                extraction_root=extraction_root,
                depth=depth,
                max_depth=max_depth,
                budget=budget,
                source_archive_id=source_archive_id,
                out=out,
            )
    finally:
        try:
            handle.close()
        except Exception:
            pass

    return True


def _process_extracted_member(
    *,
    on_disk_path: str,
    display_path: str,
    extraction_root: str,
    depth: int,
    max_depth: int,
    budget: _ExtractionBudget,
    source_archive_id: str,
    out: list[FileRecord],
) -> None:
    """A file has just been extracted to `on_disk_path`. If it's itself
    an archive and we haven't hit `max_depth`, recurse into it (its
    contents get added to `out` instead of it); otherwise record it as a
    normal leaf `FileRecord`."""
    if depth < max_depth and is_archive(on_disk_path):
        expanded = _expand_one_archive(
            archive_path=on_disk_path,
            archive_display_path=display_path,
            extraction_root=extraction_root,
            depth=depth + 1,
            max_depth=max_depth,
            budget=budget,
            source_archive_id=source_archive_id,
            out=out,
        )
        if expanded:
            return
        # Nested archive couldn't be opened (unsupported/corrupt) — fall
        # through and keep it as an ordinary opaque file instead, same as
        # the top-level equivalent of this situation.

    out.append(_leaf_file_record(on_disk_path, display_path, source_archive_id))


def _leaf_file_record(on_disk_path: str, display_path: str, source_archive_id: str) -> FileRecord:
    """Build the `FileRecord` for one already-extracted, non-archive (or
    depth-limited/unopenable-archive) file.

    `id` is a hash of `display_path`, not of `on_disk_path` — `path` here
    is a temp-extraction path that's different every single scan (fresh
    `tempfile.mkdtemp()` each time), so hashing it the way
    `scanner._stable_file_id()` hashes a real filesystem path would give
    a different id for the same logical file (same archive, same
    internal path) on every re-scan. `display_path` — the archive's own
    path plus the stable internal path chain — is what's actually stable
    across repeated scans, so that's the hash input instead. See
    PROJECT_STATE.md for why this is called out as worth a second set of
    eyes from Segment 8+ (whichever segment first upserts these into the
    `files` table).
    """
    file_id = hashlib.sha256(display_path.encode("utf-8", errors="surrogateescape")).hexdigest()
    ext = os.path.splitext(display_path)[1].lower()

    try:
        st = os.stat(on_disk_path)
    except OSError as exc:
        logger.debug("could not stat extracted file, recording as error: %s (%s)", on_disk_path, exc)
        return FileRecord(
            id=file_id,
            path=on_disk_path,
            display_path=display_path,
            source_archive_id=source_archive_id,
            ext=ext,
            size_bytes=0,
            mime_type=None,
            mtime=0.0,
            is_from_archive=True,
            status="error",
            error_message=f"could not stat extracted file: {exc}",
        )

    return FileRecord(
        id=file_id,
        path=on_disk_path,
        display_path=display_path,
        source_archive_id=source_archive_id,
        ext=ext,
        size_bytes=st.st_size,
        mime_type=mimetypes.guess_type(display_path)[0],
        mtime=st.st_mtime,
        is_from_archive=True,
        status="pending",
        error_message=None,
    )


# --------------------------------------------------------------------------
# Windows filename sanitization
# --------------------------------------------------------------------------
#
# Archives built on Unix (this shows up in practice with Android
# `bugreport` ZIPs, whose lshal-debug/ HIDL dumps are named like
# "android.hardware.media.c2@1.2::IComponentStore_software.txt") can
# legally contain characters in a member name that Windows' CreateFile
# rejects outright: `< > : " | ? *`, C0 control characters, and a
# trailing '.' or space on any individual path component. Without this,
# the extraction loop's `open(target_path, "wb")` raises
# `OSError: [Errno 22] Invalid argument` and the member is silently
# dropped (logged as "failed to extract member ... from ...zip"), which
# is real, silent data loss for a whole category of files — not a crash,
# the scan just keeps going without them.
#
# This is applied to the on-disk temp extraction path only, *after* the
# zip-slip guard below has already verified the (unsanitized) path
# resolves safely inside `dest_dir` — the traversal check and the
# Windows-legality check are independent concerns and are kept that way
# so sanitizing `..`/`.` never accidentally changes zip-slip behavior.
# `display_path` (what shows up in the final report) is built separately
# from the original, unsanitized member name, so the report still shows
# the file's real name even though the temp copy on disk is mangled.

_WINDOWS_ILLEGAL_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')


def _sanitize_path_segment(segment: str) -> str:
    """Make one path *segment* (a single component between `/`s — the
    caller splits first) safe as a Windows filename. `.` and `..` are
    passed through untouched: they're navigation tokens, not filenames,
    and `_safe_extract_target`'s traversal check needs to see the real
    ones to keep working."""
    if segment in (".", ".."):
        return segment
    cleaned = _WINDOWS_ILLEGAL_CHARS_RE.sub("_", segment)
    cleaned = cleaned.rstrip(". ")  # Windows disallows a trailing dot/space per component
    return cleaned or "_"  # a segment that sanitizes to nothing still needs a name


def _sanitize_target_path(dest_dir: str, target_path: str) -> str:
    """`target_path` has already been confirmed (by `_safe_extract_target`)
    to resolve inside `dest_dir`. Rewrite each path segment *under*
    `dest_dir` through `_sanitize_path_segment`, preserving directory
    structure, so the result is legal to `open(..., "wb")` on Windows."""
    rel = os.path.relpath(target_path, dest_dir)
    sanitized_parts = [_sanitize_path_segment(part) for part in rel.split(os.sep)]
    return os.path.join(dest_dir, *sanitized_parts)


# --------------------------------------------------------------------------
# zip-slip guard
# --------------------------------------------------------------------------


def _safe_extract_target(dest_dir: str, member_name: str) -> str | None:
    """Resolve `member_name` (an archive-internal path) against
    `dest_dir` and confirm the result stays inside `dest_dir`. Returns
    the target path if safe, `None` if the member should be rejected
    (absolute path, drive letter, `..` traversal — anything that would
    write outside the extraction directory)."""
    normalized = member_name.replace("\\", "/")
    # Strip a leading drive letter (Windows-authored archive: "C:/...")
    if len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")

    target = os.path.normpath(os.path.join(dest_dir, normalized))
    dest_real = os.path.realpath(dest_dir)
    target_real = os.path.realpath(target)

    if target_real == dest_real or target_real.startswith(dest_real + os.sep):
        return target
    return None


# --------------------------------------------------------------------------
# Per-format member listing (zip / tar / rar share one extraction loop in
# `_expand_one_archive`; 7z is different enough to get its own path
# below). Each `_open_*` function opens the archive, lists its members as
# `_Member`s (whose `.opener()` closures read lazily from the still-open
# handle), and returns `(handle, members)` — the caller is responsible
# for closing `handle` once it's done extracting from it.
# --------------------------------------------------------------------------


def _open_zip(path: str) -> tuple[zipfile.ZipFile, list[_Member]]:
    zf = zipfile.ZipFile(path)
    members: list[_Member] = []
    for info in zf.infolist():
        is_symlink = False
        try:
            # Unix "external_attr" high 16 bits hold the st_mode when the
            # archive was written on a Unix system; zip has no portable
            # symlink flag otherwise.
            unix_mode = info.external_attr >> 16
            is_symlink = stat_module.S_ISLNK(unix_mode) if unix_mode else False
        except (AttributeError, ValueError):
            pass

        try:
            mtime = datetime(*info.date_time).timestamp()
        except (ValueError, OverflowError):
            mtime = time.time()

        members.append(
            _Member(
                name=info.filename,
                size=info.file_size,
                is_dir=info.is_dir(),
                is_symlink=is_symlink,
                mtime=mtime,
                opener=(lambda info=info: zf.open(info)),
            )
        )
    return zf, members


def _open_tar(path: str) -> tuple[tarfile.TarFile, list[_Member]]:
    tf = tarfile.open(path, mode="r:*")
    members = [
        _Member(
            name=member.name,
            size=member.size,
            is_dir=member.isdir(),
            is_symlink=member.issym() or member.islnk(),
            mtime=float(member.mtime or 0.0),
            opener=(lambda member=member: tf.extractfile(member)),
        )
        for member in tf.getmembers()
    ]
    return tf, members


def _open_rar(path: str) -> tuple["rarfile.RarFile", list[_Member]]:
    rf = rarfile.RarFile(path)
    members = []
    for info in rf.infolist():
        try:
            mtime = info.mtime.timestamp() if info.mtime else time.time()
        except (AttributeError, ValueError, OverflowError):
            mtime = time.time()

        members.append(
            _Member(
                name=info.filename,
                size=info.file_size,
                is_dir=info.is_dir(),
                is_symlink=False,  # rarfile doesn't expose this portably; best-effort as-is
                mtime=mtime,
                opener=(lambda info=info: rf.open(info)),
            )
        )
    return rf, members


def _expand_7z(
    archive_path: str,
    archive_display_path: str,
    dest_dir: str,
    depth: int,
    max_depth: int,
    budget: _ExtractionBudget,
    source_archive_id: str,
    out: list[FileRecord],
) -> bool:
    """py7zr's API extracts by name in one shot rather than offering a
    lazy per-member stream like zip/tar/rar, so this doesn't share
    `_expand_one_archive`'s member loop: list names+sizes first (zip-slip
    + budget pre-check), then extract only the names that passed, then
    walk the result the same way the generic loop would have."""
    if not _HAVE_PY7ZR:
        logger.warning("archive type unsupported here (py7zr not installed): %s", archive_path)
        return False

    try:
        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            entries = archive.list()  # list[py7zr.FileInfo]: .filename, .uncompressed, .is_directory
            safe_names: list[str] = []
            safe_targets: dict[str, str] = {}  # entry.filename -> resolved on-disk path, from the same check
            for entry in entries:
                if entry.is_directory:
                    continue
                target = _safe_extract_target(dest_dir, entry.filename)
                if target is None:
                    logger.warning("skipping suspicious path (zip-slip guard) in %s: %s", archive_path, entry.filename)
                    continue
                if not budget.try_consume(max(0, int(entry.uncompressed or 0))):
                    logger.warning(
                        "MAX_ARCHIVE_TOTAL_MB budget exhausted while expanding %s — stopping further expansion here",
                        archive_path,
                    )
                    break
                safe_names.append(entry.filename)
                safe_targets[entry.filename] = target

            if safe_names:
                archive.extract(path=dest_dir, targets=safe_names)
    except Exception as exc:
        logger.warning("could not open/read archive %s (7z) — leaving unexpanded: %s", archive_path, exc)
        return False

    for name in safe_names:
        # Use the same resolved path the zip-slip check already computed
        # (rather than re-deriving it from `name`, which may use '\' on a
        # Windows-authored 7z) so this can never disagree with the check
        # above about where the file actually landed.
        on_disk_path = safe_targets[name]
        if not os.path.isfile(on_disk_path):
            # py7zr recreates directory structure itself; a name that
            # didn't materialize as a file (e.g. it was actually an empty
            # dir py7zr treated differently) is simply skipped.
            continue
        normalized_name = name.replace("\\", "/").strip("/")
        member_display_path = f"{archive_display_path}/{normalized_name}"
        _process_extracted_member(
            on_disk_path=on_disk_path,
            display_path=member_display_path,
            extraction_root=os.path.dirname(dest_dir),
            depth=depth,
            max_depth=max_depth,
            budget=budget,
            source_archive_id=source_archive_id,
            out=out,
        )

    return True


# --------------------------------------------------------------------------
# Self-test: `python -m app.archives --selftest`
# --------------------------------------------------------------------------


def _run_selftest() -> None:
    """Builds a throwaway zip (3 files + one nested zip containing 2 more
    files), extracts it, checks the expanded FileRecords look right, and
    confirms the temp extraction directory is gone once the `with` block
    exits. Also exercises the zip-slip guard against a hand-crafted
    malicious entry. Doesn't require py7zr/rarfile to be installed —
    those paths are only smoke-tested if available."""
    import tempfile as _tempfile
    import time as _time

    from app.config import Settings

    with _tempfile.TemporaryDirectory() as work_dir:
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
            max_file_read_mb=25,
            app_port=8731,
        )

        # --- build the nested zip -----------------------------------
        nested_zip_path = os.path.join(work_dir, "nested.zip")
        with zipfile.ZipFile(nested_zip_path, "w") as zf:
            zf.writestr("deep1.txt", "deep file one")
            zf.writestr("sub/deep2.txt", "deep file two")

        # --- build the top-level zip, including the nested one + a
        # zip-slip attempt -------------------------------------------
        top_zip_path = os.path.join(work_dir, "top.zip")
        with zipfile.ZipFile(top_zip_path, "w") as zf:
            zf.writestr("a.txt", "file a")
            zf.writestr("photos/b.txt", "file b")
            zf.writestr("c.txt", "file c")
            zf.write(nested_zip_path, arcname="nested.zip")
            evil = zipfile.ZipInfo("../../evil.txt")
            zf.writestr(evil, "should never land outside the extraction dir")

        top_record = FileRecord(
            id="top-zip-id",
            path=top_zip_path,
            display_path=top_zip_path,
            source_archive_id=None,
            ext=".zip",
            size_bytes=os.path.getsize(top_zip_path),
            mime_type="application/zip",
            mtime=_time.time(),
            is_from_archive=False,
            status="pending",
            error_message=None,
        )

        assert is_archive(top_zip_path), "top.zip should be recognized as an archive"
        assert not is_archive("plain.txt"), "plain.txt should not be recognized as an archive"
        assert is_archive("weird.tar.gz") and is_archive("weird.tgz"), "tar-gz variants should be recognized"
        assert is_archive("mystery.rar"), "is_archive() should say True for .rar even without rarfile installed"

        with extract_archive(top_record, settings) as contents:
            display_paths = sorted(r.display_path for r in contents)
            print(f"Expanded {len(contents)} file(s) from {top_zip_path}:")
            for r in contents:
                print(f"  [{r.status}] {r.display_path}  ({r.size_bytes} bytes, source={r.source_archive_id})")
                assert r.path != "", "extracted record must have a real temp path"
                assert os.path.isfile(r.path), f"extracted file should exist on disk while inside the with-block: {r.path}"
                assert r.is_from_archive is True
                assert r.source_archive_id == "top-zip-id"

            # Expect a.txt, photos/b.txt, c.txt, and the two files inside
            # nested.zip (expanded, not left as one opaque nested.zip
            # record) — 5 total. The zip-slip entry must NOT appear.
            assert len(contents) == 5, f"expected 5 expanded files, got {len(contents)}: {display_paths}"
            assert any(dp.endswith("/a.txt") for dp in display_paths)
            assert any(dp.endswith("/photos/b.txt") for dp in display_paths)
            assert any(dp.endswith("/nested.zip/deep1.txt") for dp in display_paths)
            assert any(dp.endswith("/nested.zip/sub/deep2.txt") for dp in display_paths)
            assert not any("evil.txt" in dp for dp in display_paths), "zip-slip entry must not be extracted"

            # Same logical file -> same id on a second extraction (stability
            # across scans, since path derives from a fresh temp dir each
            # time but display_path doesn't).
            with extract_archive(top_record, settings) as contents2:
                ids_first = {r.display_path: r.id for r in contents}
                ids_second = {r.display_path: r.id for r in contents2}
                assert ids_first == ids_second, "same display_path should yield the same stable id across extractions"

        # Confirm cleanup: nothing extracted should still exist on disk
        # once the (outer) with-block has exited.
        for r in contents:
            assert not os.path.exists(r.path), f"extracted file should be gone after the with-block exits: {r.path}"
        tmp_root = os.path.join(app_data_dir, "tmp")
        remaining = os.listdir(tmp_root) if os.path.isdir(tmp_root) else []
        assert remaining == [], f"expected tmp dir empty after cleanup, found: {remaining}"

        # --- unsupported-format degradation: fake a .rar with rarfile
        # absent (typical default install) or, if present, a corrupt one ---
        fake_rar_path = os.path.join(work_dir, "mystery.rar")
        with open(fake_rar_path, "wb") as f:
            f.write(b"not actually a rar file")
        rar_record = FileRecord(
            id="rar-id",
            path=fake_rar_path,
            display_path=fake_rar_path,
            source_archive_id=None,
            ext=".rar",
            size_bytes=os.path.getsize(fake_rar_path),
            mime_type=None,
            mtime=_time.time(),
            is_from_archive=False,
            status="pending",
            error_message=None,
        )
        with extract_archive(rar_record, settings) as rar_contents:
            assert rar_contents == [], "unsupported/corrupt archive should yield an empty list, not raise"

        # --- budget enforcement: a tiny budget should stop expansion
        # partway through rather than crash ----------------------------
        tiny_settings = Settings(**{**settings.__dict__, "max_archive_total_mb": 0})
        with extract_archive(top_record, tiny_settings) as tiny_contents:
            assert len(tiny_contents) < 5, "a near-zero byte budget should have cut expansion short"

        # --- depth limiting: max_archive_depth=1 should leave nested.zip
        # as one opaque record instead of expanding into it -------------
        shallow_settings = Settings(**{**settings.__dict__, "max_archive_depth": 1})
        with extract_archive(top_record, shallow_settings) as shallow_contents:
            shallow_display_paths = sorted(r.display_path for r in shallow_contents)
            assert any(dp.endswith("/nested.zip") for dp in shallow_display_paths), (
                f"depth=1 should leave nested.zip unexpanded: {shallow_display_paths}"
            )
            assert not any("deep1.txt" in dp for dp in shallow_display_paths)

        # --- tar.gz sanity check ---------------------------------------
        tar_path = os.path.join(work_dir, "sample.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tf:
            member_src = os.path.join(work_dir, "tar_member.txt")
            with open(member_src, "w") as f:
                f.write("hello from tar")
            tf.add(member_src, arcname="tar_member.txt")
        tar_record = FileRecord(
            id="tar-id",
            path=tar_path,
            display_path=tar_path,
            source_archive_id=None,
            ext=".tar.gz",
            size_bytes=os.path.getsize(tar_path),
            mime_type=None,
            mtime=_time.time(),
            is_from_archive=False,
            status="pending",
            error_message=None,
        )
        with extract_archive(tar_record, settings) as tar_contents:
            assert len(tar_contents) == 1
            assert tar_contents[0].display_path.endswith("/tar_member.txt")

        # --- Windows-illegal characters in member names -----------------
        # Android `bugreport` ZIPs store HIDL debug dumps under
        # lshal-debug/ with names like
        # "pkg@ver::Interface_instance.txt" — legal on the Unix
        # filesystem the archive was built on, illegal on Windows (the
        # '::' collides with NTFS's alternate-data-stream separator and
        # CreateFile rejects it with OSError 22). This must extract
        # successfully everywhere, not just on Linux.
        illegal_zip_path = os.path.join(work_dir, "illegal_chars.zip")
        with zipfile.ZipFile(illegal_zip_path, "w") as zf:
            zf.writestr(
                "lshal-debug/android.hardware.media.c2@1.2::IComponentStore_software.txt",
                "hidl debug dump",
            )
            zf.writestr('weird<>:"|?*chars.txt', "other illegal chars")
        illegal_record = FileRecord(
            id="illegal-zip-id",
            path=illegal_zip_path,
            display_path=illegal_zip_path,
            source_archive_id=None,
            ext=".zip",
            size_bytes=os.path.getsize(illegal_zip_path),
            mime_type="application/zip",
            mtime=_time.time(),
            is_from_archive=False,
            status="pending",
            error_message=None,
        )
        with extract_archive(illegal_record, settings) as illegal_contents:
            assert len(illegal_contents) == 2, (
                f"expected both illegal-name members to survive extraction, got "
                f"{len(illegal_contents)}: {[r.display_path for r in illegal_contents]}"
            )
            for r in illegal_contents:
                assert os.path.isfile(r.path), f"sanitized member should exist on disk: {r.path}"
                for part in r.path.split(os.sep):
                    assert not _WINDOWS_ILLEGAL_CHARS_RE.search(part), (
                        f"on-disk path segment still has an illegal character: {part!r} in {r.path}"
                    )
            # display_path keeps the ORIGINAL member name — only the temp
            # extraction path on disk is sanitized, so the final report
            # still shows the file's real name from the archive.
            illegal_display_paths = sorted(r.display_path for r in illegal_contents)
            assert any("c2@1.2::IComponentStore_software.txt" in dp for dp in illegal_display_paths), illegal_display_paths
            assert any('weird<>:"|?*chars.txt' in dp for dp in illegal_display_paths), illegal_display_paths

        print("OK")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker archive-handling utility")
    parser.add_argument("--selftest", action="store_true", help="build test archives, extract, verify cleanup")
    args = parser.parse_args()

    if args.selftest:
        _run_selftest()
    else:
        parser.print_help()
