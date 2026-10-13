"""Drive/folder discovery and the file walker.

Two jobs, matching PLAN.md Segment 3:
  - `list_roots()`: what top-level locations should the frontend's root
    picker offer? (drives, removable media, the user's home folder)
  - `walk()`: given one chosen root, stream a `FileRecord` for every file
    under it.

No file *content* is read here — that's Segment 5/6's job. Archive files
(`.zip`, etc.) are yielded as ordinary FileRecords, same as anything else;
expanding them into their contents is Segment 4's job (`app/archives.py`),
which is expected to consume this module's output and check
`archives.is_archive(record.path)` itself rather than this module trying
to special-case archives.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import sys
from collections.abc import Iterator

import psutil

from app.config import Settings
from app.log import logger
from app.models import FileRecord
from app import portable_devices

# Mount-point prefixes that usually mean "removable/external media" on
# desktop Linux/macOS but aren't reliably flagged as such by
# `psutil.disk_partitions()` alone. Supplementary to psutil, not a
# replacement for it.
_LINUX_REMOVABLE_PREFIXES = ("/media/", "/run/media/")
_MACOS_REMOVABLE_PREFIX = "/Volumes/"

# Windows `GetDriveTypeW` return codes we care about (via ctypes, so no
# pywin32 dependency needed just for this).
_WIN_DRIVE_REMOVABLE = 2
_WIN_DRIVE_FIXED = 3
_WIN_DRIVE_REMOTE = 4
_WIN_DRIVE_CDROM = 5

# Windows file-attribute bits that indicate a cloud-storage placeholder
# (OneDrive/iCloud/Dropbox "online-only" file — present on disk as a stub,
# content not downloaded yet). Best-effort only; see `walk()` docstring.
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000


# --------------------------------------------------------------------------
# list_roots()
# --------------------------------------------------------------------------


def list_roots() -> list[dict]:
    """Enumerate scannable top-level locations for the frontend's root
    picker.

    Best-effort and cross-platform, layering three sources:
      1. `psutil.disk_partitions(all=False)` — real disks/mounts, however
         the current OS exposes them. This is the primary source and
         (per psutil's own docs) already excludes pseudo filesystems
         (proc, sysfs, tmpfs, ...).
      2. A couple of well-known removable-media mount directories psutil
         sometimes misses depending on how a drive was auto-mounted:
         `/Volumes/*` on macOS, `/media/$USER/*` and `/run/media/$USER/*`
         on Linux.
      3. The user's home directory, always included as a `"folder"` entry
         — the one guaranteed-useful default even if every disk-
         enumeration trick above comes up empty (e.g. sandboxed/
         containerized environments).

    Returns:
        `[{"path": str, "label": str, "kind": "drive"|"removable"|"folder"|"portable"}, ...]`,
        deduplicated by resolved absolute path. Home folder first, then
        everything else sorted by path.
    """
    roots: dict[str, dict] = {}  # keyed by realpath, for de-dup

    home = os.path.expanduser("~")
    roots[os.path.realpath(home)] = {"path": home, "label": "Home", "kind": "folder"}

    for entry in _partitions_from_psutil():
        key = os.path.realpath(entry["path"])
        roots.setdefault(key, entry)

    for entry in _supplementary_mount_points():
        key = os.path.realpath(entry["path"])
        roots.setdefault(key, entry)

    # Phones using Media Transfer Protocol (MTP) are Explorer shell items,
    # not normal filesystem mount points.  Keep their encoded roots intact:
    # resolving them as paths would corrupt the identifier needed to reopen
    # the storage later in the scan pipeline.
    for entry in portable_devices.list_roots():
        roots.setdefault(entry["path"], entry)

    home_key = os.path.realpath(home)
    others = sorted((v for k, v in roots.items() if k != home_key), key=lambda e: e["path"])
    return [roots[home_key], *others]


def _partitions_from_psutil() -> list[dict]:
    entries: list[dict] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except OSError as exc:
        logger.warning("psutil.disk_partitions() failed, continuing without it: %s", exc)
        return entries

    for part in partitions:
        try:
            kind = _classify_partition(part.mountpoint)
            label = _label_for_mountpoint(part.mountpoint)
            entries.append({"path": part.mountpoint, "label": label, "kind": kind})
        except OSError as exc:
            # Seen in practice for CD-ROM drives with no disc inserted, or
            # network shares that have gone stale — skip that one entry
            # rather than losing the whole list.
            logger.debug("skipping partition %s: %s", part.mountpoint, exc)
    return entries


def _classify_partition(mountpoint: str) -> str:
    if sys.platform == "win32":
        return _classify_windows_drive(mountpoint)
    if sys.platform == "darwin":
        return "removable" if mountpoint.startswith(_MACOS_REMOVABLE_PREFIX) else "drive"
    if mountpoint.startswith(_LINUX_REMOVABLE_PREFIXES):
        return "removable"
    return "drive"


def _classify_windows_drive(mountpoint: str) -> str:
    """Windows-only: ask the OS what kind of drive this is via
    `GetDriveTypeW`, through ctypes so we don't need pywin32 just for
    this one call. Never raises — falls back to `"drive"` if anything
    about this lookup goes wrong."""
    try:
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(mountpoint))
        if drive_type == _WIN_DRIVE_REMOVABLE:
            return "removable"
        if drive_type == _WIN_DRIVE_CDROM:
            return "removable"
        if drive_type in (_WIN_DRIVE_FIXED, _WIN_DRIVE_REMOTE):
            return "drive"
    except Exception as exc:  # pragma: no cover - best-effort, non-Windows can't hit this
        logger.debug("GetDriveTypeW lookup failed for %s: %s", mountpoint, exc)
    return "drive"


def _label_for_mountpoint(mountpoint: str) -> str:
    if mountpoint in ("/", ""):
        return "Root"
    name = os.path.basename(mountpoint.rstrip("/\\"))
    return name or mountpoint


def _supplementary_mount_points() -> list[dict]:
    """Removable-media directories `psutil` sometimes doesn't surface as
    distinct partitions, checked directly. Silently skipped (not an
    error) if they don't exist — that's the normal case on any OS other
    than the one each check targets."""
    entries: list[dict] = []

    candidate_dirs: list[str] = []
    if sys.platform == "darwin":
        candidate_dirs.append("/Volumes")
    elif sys.platform.startswith("linux"):
        user = os.environ.get("USER") or os.environ.get("LOGNAME")
        if user:
            candidate_dirs.append(f"/media/{user}")
            candidate_dirs.append(f"/run/media/{user}")
        candidate_dirs.append("/media")

    for base_dir in candidate_dirs:
        try:
            if not os.path.isdir(base_dir):
                continue
            for name in os.listdir(base_dir):
                full = os.path.join(base_dir, name)
                if os.path.isdir(full):
                    entries.append({"path": full, "label": name, "kind": "removable"})
        except OSError as exc:
            logger.debug("could not list %s for removable media: %s", base_dir, exc)

    return entries


# --------------------------------------------------------------------------
# walk()
# --------------------------------------------------------------------------


def walk(root: str, settings: Settings) -> Iterator[FileRecord]:
    """Recursively walk `root`, yielding one `FileRecord` per file found.

    `settings` is accepted for interface symmetry with the rest of the
    pipeline and future extensibility (e.g. skip patterns); this segment
    doesn't currently branch on any of its fields — size/depth limits are
    Segments 4-6's concern (archives, content extraction), not the walk
    itself.

    Behavior:
      - `os.walk(followlinks=False)`: symlinked directories are listed
        but never descended into (avoids infinite loops from a
        self-referential symlink). Symlinked *files* are walked and
        `stat()`'d like any other file.
      - A directory Python can't even list (`PermissionError`, or similar
        `OSError`) is logged and skipped — there's no filename to build a
        `FileRecord` around, so nothing is yielded for it; the walk
        continues with the rest of the tree.
      - A file that's listed but can't be `stat()`'d (permission denied on
        the file itself, or a broken symlink) still gets a `FileRecord`,
        with `status="error"` and `error_message` explaining why, so it
        doesn't just silently vanish from the caller's view of "what's
        under this root."
      - No content is read, which is what makes this safe for cloud-
        placeholder files (OneDrive/iCloud/Dropbox "online-only" entries)
        — local metadata (name/size/mtime) is available for a placeholder
        without triggering a download; reading its actual bytes (later
        segments' job) is what would hang. On Windows, a best-effort check
        of the placeholder file-attribute bits logs a note when one is
        spotted, purely so it's visible rather than silently
        indistinguishable from a fully-synced file — see PROJECT_STATE.md,
        this isn't modeled as a `FileRecord` field.
    """
    root = os.path.abspath(root)

    def _on_walk_error(exc: OSError) -> None:
        logger.warning("skipping directory (cannot list): %s (%s)", exc.filename or root, exc)

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_on_walk_error, followlinks=False):
        for filename in filenames:
            yield _build_file_record(dirpath, filename)


def _build_file_record(dirpath: str, filename: str) -> FileRecord:
    full_path = os.path.join(dirpath, filename)
    abs_path = os.path.abspath(full_path)
    file_id = _stable_file_id(abs_path)
    ext = os.path.splitext(filename)[1].lower()

    try:
        st = os.stat(full_path)  # follows symlinks by default
    except OSError as exc:
        logger.debug("could not stat file, recording as error: %s (%s)", abs_path, exc)
        return FileRecord(
            id=file_id,
            path=abs_path,
            display_path=abs_path,
            source_archive_id=None,
            ext=ext,
            size_bytes=0,
            mime_type=None,
            mtime=0.0,
            is_from_archive=False,
            status="error",
            error_message=f"could not stat file: {exc}",
        )

    if sys.platform == "win32" and _is_windows_cloud_placeholder(st):
        logger.info("possible cloud-placeholder (not fully synced locally): %s", abs_path)

    return FileRecord(
        id=file_id,
        path=abs_path,
        display_path=abs_path,
        source_archive_id=None,
        ext=ext,
        size_bytes=st.st_size,
        mime_type=mimetypes.guess_type(full_path)[0],
        mtime=st.st_mtime,
        is_from_archive=False,
        status="pending",
        error_message=None,
    )


def _stable_file_id(abs_path: str) -> str:
    """Deterministic id for a file, derived from its absolute path (sha256
    hex digest) so the same file maps to the same `FileRecord.id` across
    repeated scans — that's what lets `db.upsert_file()` overwrite a
    previous scan's row for it instead of duplicating it."""
    return hashlib.sha256(abs_path.encode("utf-8", errors="surrogateescape")).hexdigest()


def _is_windows_cloud_placeholder(stat_result: os.stat_result) -> bool:
    """Best-effort detection of a Windows cloud-storage placeholder file.
    Windows-only; always `False` elsewhere (and never raises — if the
    attribute isn't there for any reason, this just isn't important
    enough to fail the walk over)."""
    try:
        attrs = stat_result.st_file_attributes
    except AttributeError:
        return False
    return bool(attrs & (_FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))


# --------------------------------------------------------------------------
# Self-test: `python -m app.scanner --list-roots` / `--walk <path>`
# --------------------------------------------------------------------------


def _run_list_roots() -> None:
    import json

    print(json.dumps(list_roots(), indent=2))


def _run_walk(path: str) -> None:
    from app.config import get_settings

    settings = get_settings()
    counts = {"pending": 0, "error": 0}
    total_bytes = 0
    sample: list[str] = []

    for record in walk(path, settings):
        counts[record.status] = counts.get(record.status, 0) + 1
        total_bytes += record.size_bytes
        if len(sample) < 20:
            sample.append(f"[{record.status}] {record.display_path} ({record.size_bytes} bytes)")

    print(f"Walked: {path}")
    for line in sample:
        print(f"  {line}")
    if sum(counts.values()) > len(sample):
        print(f"  ... and {sum(counts.values()) - len(sample)} more")
    print(f"Total files: {sum(counts.values())} | by status: {counts} | total size: {total_bytes} bytes")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker scanner utility")
    parser.add_argument("--list-roots", action="store_true", help="print discovered scannable roots")
    parser.add_argument("--walk", metavar="PATH", help="walk PATH and print a summary")
    args = parser.parse_args()

    if args.list_roots:
        _run_list_roots()
    elif args.walk:
        _run_walk(args.walk)
    else:
        parser.print_help()
