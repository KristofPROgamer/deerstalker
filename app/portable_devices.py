"""Windows MTP/portable-device discovery and temporary staging.

Modern phones normally expose Media Transfer Protocol (MTP), which Explorer
can browse but which does not have a drive letter or ordinary filesystem path.
The existing pipeline needs readable local paths, so this module exposes MTP
storage roots as ``mtp://`` identifiers and copies selected files into a
private temporary staging directory for the lifetime of a scan job.

This is deliberately Windows-only.  Other platforms return no portable roots
and retain their normal drive/folder behaviour.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import mimetypes
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.log import logger
from app.models import FileRecord


_ROOT_PREFIX = "mtp://"
_THIS_PC_NAMESPACE = 17
_COPY_FLAGS = 0x4 | 0x10 | 0x200 | 0x400  # silent, no confirmations, no UI
_COPY_TIMEOUT_SECONDS = 300.0
_COPY_POLL_SECONDS = 0.05
_COPY_STABLE_SECONDS = 1.5  # fallback only — see _copy_file(); the primary
                            # completion check compares against source_item.Size


@dataclass(frozen=True)
class StagedBatch:
    """One temporary, bounded-size group of phone files ready to analyze."""

    index: int
    records: list[FileRecord]
    bytes_staged: int


class PortableDeviceError(RuntimeError):
    """A selected Windows portable-device root is unavailable or unreadable."""


def is_portable_root(root: str) -> bool:
    """Return whether ``root`` is one of this module's encoded MTP roots."""
    return isinstance(root, str) and root.startswith(_ROOT_PREFIX)


def list_roots() -> list[dict[str, str]]:
    """Return accessible MTP storage roots shown in Windows Explorer.

    Each item is intentionally a storage root (for example, ``Internal shared
    storage``), not merely a device shell icon. Its encoded path can round-trip
    through the existing API and ``ScanJob.roots`` without pretending it is an
    ``os.path`` path.
    """
    if sys.platform != "win32":
        return []

    try:
        with _shell_session() as shell:
            this_pc = shell.NameSpace(_THIS_PC_NAMESPACE)
            if this_pc is None:
                return []
            roots: list[dict[str, str]] = []
            for device in _items(this_pc):
                if not _looks_like_portable_device(device):
                    continue
                device_folder = shell.NameSpace(device)
                if device_folder is None:
                    continue
                for storage in _items(device_folder):
                    if not bool(getattr(storage, "IsFolder", False)):
                        continue
                    parsing_path = str(getattr(storage, "Path", ""))
                    if not parsing_path:
                        continue
                    device_name = str(getattr(device, "Name", "Phone")).strip() or "Phone"
                    storage_name = str(getattr(storage, "Name", "Storage")).strip() or "Storage"
                    roots.append(
                        {
                            "path": encode_root(parsing_path),
                            "label": f"{device_name} - {storage_name}",
                            "kind": "portable",
                        }
                    )
            return roots
    except Exception as exc:  # Discovery must never break normal drive listing.
        logger.info("portable-device discovery unavailable: %s", exc)
        return []


def encode_root(parsing_path: str) -> str:
    """Encode a Windows Shell parsing name for transport as a scan root."""
    encoded = base64.urlsafe_b64encode(parsing_path.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{_ROOT_PREFIX}{encoded}"


def decode_root(root: str) -> str:
    """Decode an ``mtp://`` root or raise :class:`PortableDeviceError`."""
    if not is_portable_root(root):
        raise PortableDeviceError("not a portable-device root")
    encoded = root[len(_ROOT_PREFIX) :]
    if not encoded:
        raise PortableDeviceError("portable-device root is empty")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise PortableDeviceError("portable-device root is invalid") from exc


@contextlib.contextmanager
def stage_root(root: str, settings: Settings) -> Iterator[list[FileRecord]]:
    """Stage every accessible file under an MTP storage root locally.

    The yielded records use staging-file paths for the existing extraction and
    output code, while their display paths retain phone/storage provenance.
    The staging tree is removed only after the orchestrator's output stage has
    copied its matches, so the phone must remain connected until the scan ends.
    """
    if sys.platform != "win32":
        raise PortableDeviceError("portable-device scanning is supported on Windows only")

    parsing_path = decode_root(root)
    staging_parent = Path(settings.app_data_dir) / "tmp"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging_directory = Path(tempfile.mkdtemp(prefix="portable-device-", dir=staging_parent))
    try:
        with _shell_session() as shell:
            # An MTP parsing name looks path-like but Explorer cannot reopen it
            # through ``NameSpace(str)``. Reacquire the matching Shell item
            # from This PC, then pass that item to NameSpace instead.
            source_folder = _find_storage_folder(shell, parsing_path)
            if source_folder is None:
                raise PortableDeviceError("phone storage is no longer available; reconnect and unlock the device")
            title = str(getattr(source_folder, "Title", "Phone storage")).strip() or "Phone storage"
            records = list(_stage_folder(shell, source_folder, staging_directory, root, title, ""))
        yield records
    finally:
        shutil.rmtree(staging_directory, ignore_errors=True)


@contextlib.contextmanager
def staged_batches(
    root: str,
    settings: Settings,
    *,
    on_progress: Callable[[str, str, int, int], None] | None = None,
) -> Iterator[Iterator[StagedBatch]]:
    """Yield phone files in temporary batches, deleting each after use.

    A batch contains at most ``settings.portable_stage_batch_gib`` GiB plus a
    possible single oversized file. The caller must finish analyzing/copying a
    yielded batch before requesting the next one; that advance removes the
    prior batch's staging directory. ``on_progress`` receives
    ``(event, display_path, batch_bytes, batch_limit_bytes)`` for copy start,
    completion, and per-file errors.
    """
    if sys.platform != "win32":
        raise PortableDeviceError("portable-device scanning is supported on Windows only")

    parsing_path = decode_root(root)
    staging_parent = _staging_parent(settings)
    batch_limit = max(1, int(settings.portable_stage_batch_gib)) * 1024**3
    free_bytes = shutil.disk_usage(staging_parent).free
    logger.info(
        "portable staging: root=%s directory=%s batch_limit=%0.2f GiB free=%0.2f GiB",
        root,
        staging_parent,
        batch_limit / 1024**3,
        free_bytes / 1024**3,
    )
    if free_bytes < min(batch_limit, 1024**3):
        raise PortableDeviceError(f"not enough free space in staging directory: {staging_parent}")

    with _shell_session() as shell:
        source_folder = _find_storage_folder(shell, parsing_path)
        if source_folder is None:
            raise PortableDeviceError("phone storage is no longer available; reconnect and unlock the device")
        title = str(getattr(source_folder, "Title", "Phone storage")).strip() or "Phone storage"
        logger.info("portable staging: reopened '%s', beginning batch enumeration", title)
        yield _stage_batches(shell, source_folder, root, title, staging_parent, batch_limit, on_progress)


def error_record(root: str, message: str) -> FileRecord:
    """Represent an inaccessible portable root as a normal pipeline error."""
    return FileRecord(
        id=_stable_id(root),
        path=root,
        display_path="Connected phone storage",
        source_archive_id=None,
        ext="",
        size_bytes=0,
        mime_type=None,
        mtime=0.0,
        is_from_archive=False,
        status="error",
        error_message=message,
    )


@contextlib.contextmanager
def _shell_session() -> Iterator[Any]:
    """Open a COM apartment and the Windows Explorer Shell automation API."""
    if sys.platform != "win32":
        raise PortableDeviceError("Windows Shell automation is unavailable on this platform")
    try:
        import pythoncom
        from win32com.client import Dispatch
    except ImportError as exc:
        raise PortableDeviceError("pywin32 is required for portable-device scanning") from exc

    pythoncom.CoInitialize()
    try:
        yield Dispatch("Shell.Application")
    finally:
        pythoncom.CoUninitialize()


def _looks_like_portable_device(item: Any) -> bool:
    """Distinguish MTP shell items from ordinary drive-letter entries."""
    try:
        path = str(item.Path).casefold()
        return bool(item.IsFolder) and path.startswith("::{") and ("usb#" in path or "mtp" in path)
    except Exception:
        return False


def _find_storage_folder(shell: Any, parsing_path: str) -> Any | None:
    """Reopen MTP storage through its live Explorer parent item.

    ``Shell.NameSpace(parsing_path)`` returns ``None`` for many valid MTP
    parsing names. The same storage becomes usable when passed as the
    ``FolderItem`` COM object found below the connected device in This PC.
    """
    this_pc = shell.NameSpace(_THIS_PC_NAMESPACE)
    if this_pc is None:
        return None
    for device in _items(this_pc):
        if not _looks_like_portable_device(device):
            continue
        device_folder = shell.NameSpace(device)
        if device_folder is None:
            continue
        for storage in _items(device_folder):
            if str(getattr(storage, "Path", "")) == parsing_path:
                return shell.NameSpace(storage)
    return None


def _items(folder: Any) -> list[Any]:
    try:
        return list(folder.Items())
    except Exception as exc:
        raise PortableDeviceError(f"could not enumerate connected phone storage: {exc}") from exc


def _staging_parent(settings: Settings) -> Path:
    configured = (settings.portable_stage_dir or "").strip()
    parent = Path(configured) if configured else Path(settings.app_data_dir) / "tmp"
    parent = parent.expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def _stage_batches(
    shell: Any,
    source_folder: Any,
    root: str,
    title: str,
    staging_parent: Path,
    batch_limit: int,
    on_progress: Callable[[str, str, int, int], None] | None,
) -> Iterator[StagedBatch]:
    """Copy source files in bounded batches and remove each after yielding."""
    batch_index = 1
    batch_directory: Path | None = None
    batch_started = time.monotonic()
    records: list[FileRecord] = []
    batch_bytes = 0
    root_started = time.monotonic()
    total_files = 0
    total_bytes = 0
    total_errors = 0

    def open_batch() -> Path:
        nonlocal batch_directory, batch_started
        batch_directory = Path(tempfile.mkdtemp(prefix=f"portable-batch-{batch_index:04d}-", dir=staging_parent))
        batch_started = time.monotonic()
        logger.info("portable batch %d: staging into %s", batch_index, batch_directory)
        return batch_directory

    def close_batch() -> None:
        elapsed = max(time.monotonic() - batch_started, 0.001)
        logger.info(
            "portable batch %d: completed %d record(s), %0.2f GiB in %0.1fs (%0.2f MB/s); removing temporary files",
            batch_index,
            len(records),
            batch_bytes / 1024**3,
            elapsed,
            (batch_bytes / 1024**2) / elapsed,
        )

    try:
        for item, relative_path, display_path in _iter_source_files(shell, source_folder, title, ""):
            if batch_directory is None:
                open_batch()
            assert batch_directory is not None
            logger.debug("portable file indexed: batch=%d file=%s", batch_index, display_path)
            if on_progress is not None:
                on_progress("indexed", display_path, batch_bytes, batch_limit)
                on_progress("copying", display_path, batch_bytes, batch_limit)
            try:
                record = _copy_item_to_record(shell, item, batch_directory, root, relative_path, display_path)
                batch_bytes += record.size_bytes
                total_bytes += record.size_bytes
                logger.info(
                    "portable copy complete: batch=%d file=%s bytes=%d batch_total=%0.2f GiB",
                    batch_index,
                    display_path,
                    record.size_bytes,
                    batch_bytes / 1024**3,
                )
                if on_progress is not None:
                    on_progress("staged", display_path, batch_bytes, batch_limit)
            except Exception as exc:
                logger.warning("portable copy failed: batch=%d file=%s error=%s", batch_index, display_path, exc)
                record = _item_error_record(root, relative_path, display_path, str(exc))
                total_errors += 1
                if on_progress is not None:
                    on_progress("error", display_path, batch_bytes, batch_limit)
            records.append(record)
            total_files += 1

            # Emit once the configured capacity has been reached. A single
            # individual file may exceed the limit, but never shares a batch
            # with later files.
            if batch_bytes >= batch_limit or len(records) >= 2_000:
                yield StagedBatch(batch_index, records, batch_bytes)
                close_batch()
                shutil.rmtree(batch_directory, ignore_errors=True)
                batch_index += 1
                batch_directory = None
                records = []
                batch_bytes = 0

        if records and batch_directory is not None:
            yield StagedBatch(batch_index, records, batch_bytes)
            close_batch()

        root_elapsed = max(time.monotonic() - root_started, 0.001)
        logger.info(
            "portable staging finished for %s: %d file(s), %0.2f GiB, %d error(s), %0.1fs total (%0.2f MB/s average)",
            root,
            total_files,
            total_bytes / 1024**3,
            total_errors,
            root_elapsed,
            (total_bytes / 1024**2) / root_elapsed,
        )
    finally:
        if batch_directory is not None:
            shutil.rmtree(batch_directory, ignore_errors=True)


def _iter_source_files(shell: Any, folder: Any, title: str, relative_path: str) -> Iterator[tuple[Any, str, str]]:
    """Yield live Shell file items without copying their contents yet."""
    for item in _items(folder):
        name = _safe_name(str(getattr(item, "Name", "")))
        if not name:
            continue
        child_relative = f"{relative_path}/{name}" if relative_path else name
        display_path = f"{title}/{child_relative}"
        if bool(getattr(item, "IsFolder", False)):
            child_folder = shell.NameSpace(item)
            if child_folder is None:
                logger.warning("portable folder unavailable; skipped: %s", display_path)
                continue
            yield from _iter_source_files(shell, child_folder, title, child_relative)
        else:
            yield item, child_relative, display_path


def _copy_item_to_record(
    shell: Any,
    item: Any,
    destination_root: Path,
    root: str,
    relative_path: str,
    display_path: str,
) -> FileRecord:
    """Stage one MTP item and construct the normal filesystem record."""
    parts = [_safe_name(part) for part in relative_path.split("/")]
    staged_path = destination_root.joinpath(*parts)
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    copied_path = _copy_file(shell, item, staged_path)
    stat = copied_path.stat()
    return FileRecord(
        id=_stable_id(f"{root}/{relative_path}"),
        path=str(copied_path.resolve()),
        display_path=display_path,
        source_archive_id=None,
        ext=copied_path.suffix.lower(),
        size_bytes=stat.st_size,
        mime_type=mimetypes.guess_type(copied_path.name)[0],
        mtime=stat.st_mtime,
        is_from_archive=False,
        status="pending",
        error_message=None,
    )


def _stage_folder(
    shell: Any,
    source_folder: Any,
    destination: Path,
    root: str,
    title: str,
    relative_path: str,
) -> Iterator[FileRecord]:
    """Recursively mirror a shell folder into ``destination`` one file at a time."""
    for item in _items(source_folder):
        name = _safe_name(str(getattr(item, "Name", "")))
        if not name:
            continue
        child_relative = f"{relative_path}/{name}" if relative_path else name
        display_path = f"{title}/{child_relative}"
        try:
            if bool(getattr(item, "IsFolder", False)):
                child_folder = shell.NameSpace(item)
                if child_folder is None:
                    yield _item_error_record(root, child_relative, display_path, "folder is unavailable on the phone")
                    continue
                child_destination = destination / name
                child_destination.mkdir(exist_ok=True)
                yield from _stage_folder(shell, child_folder, child_destination, root, title, child_relative)
                continue

            staged_path = destination / name
            copied_path = _copy_file(shell, item, staged_path)
            stat = copied_path.stat()
            yield FileRecord(
                id=_stable_id(f"{root}/{child_relative}"),
                path=str(copied_path.resolve()),
                display_path=display_path,
                source_archive_id=None,
                ext=copied_path.suffix.lower(),
                size_bytes=stat.st_size,
                mime_type=mimetypes.guess_type(copied_path.name)[0],
                mtime=stat.st_mtime,
                is_from_archive=False,
                status="pending",
                error_message=None,
            )
        except Exception as exc:
            logger.warning("could not stage %s from connected phone: %s", display_path, exc)
            yield _item_error_record(root, child_relative, display_path, str(exc))


def _copy_file(shell: Any, source_item: Any, destination: Path) -> Path:
    """Copy a shell/MTP file to disk and wait until Explorer finishes it.

    `CopyHere()` is fire-and-forget, so completion has to be detected by
    polling the destination. The primary check compares the staged file's
    size against `source_item.Size` — a byte count Explorer already knows
    from enumerating the device, no extra device round-trip — so a small
    file (the overwhelming majority in a typical phone dump) returns the
    moment its bytes land instead of always sitting through the same wait.
    The previous approach (declare done once size stops changing for
    `_COPY_STABLE_SECONDS`) is kept as a fallback for the rare case a
    source item reports no usable size.
    """
    destination = destination.resolve()
    target_folder = shell.NameSpace(str(destination.parent))
    if target_folder is None:
        raise PortableDeviceError(f"could not open local staging folder {destination.parent}")
    before = {path.resolve() for path in destination.parent.iterdir() if path.is_file()}
    expected_size = _item_size(source_item)

    copy_started = time.monotonic()
    logger.info(
        "portable copy start: %s (expected %s)",
        destination,
        f"{expected_size} bytes" if expected_size is not None else "size unknown",
    )
    target_folder.CopyHere(source_item, _COPY_FLAGS)

    deadline = copy_started + _COPY_TIMEOUT_SECONDS
    last_size = -1
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        candidate = _locate_copy_target(destination, before)
        if candidate is not None:
            size = candidate.stat().st_size
            now = time.monotonic()
            if expected_size is not None and size >= expected_size:
                # Exact signal: a local copy never grows past the source's
                # real size, so matching it means the copy is actually done —
                # no settle wait needed on top of this.
                _log_copy_done(candidate, size, copy_started)
                return candidate
            if size != last_size:
                last_size = size
                last_change = now
            elif now - last_change >= _COPY_STABLE_SECONDS:
                # Fallback path — only reached when expected_size was
                # unavailable, so this keeps the old, more cautious behavior
                # rather than guessing at a shorter window blind.
                _log_copy_done(candidate, size, copy_started)
                return candidate
        time.sleep(_COPY_POLL_SECONDS)
    elapsed = time.monotonic() - copy_started
    raise PortableDeviceError(f"timed out copying {destination.name} from the phone after {elapsed:.1f}s")


def _item_size(item: Any) -> int | None:
    """Best-effort read of a shell item's known size in bytes, or `None` if
    it's missing/unusable — callers then fall back to the stability
    heuristic instead of trusting a bad value."""
    try:
        size = int(getattr(item, "Size"))
    except Exception:
        return None
    return size if size >= 0 else None


def _locate_copy_target(destination: Path, before: set[Path]) -> Path | None:
    """Find the file Explorer is actually writing to.

    Usually just `destination`. Some MTP providers normalize a filename
    during shell copy, so as a fallback, a newly created file that wasn't
    present before the copy started is used instead of waiting forever for
    an exact spelling that can never appear.
    """
    if destination.is_file():
        return destination
    created = [path for path in destination.parent.iterdir() if path.is_file() and path.resolve() not in before]
    return created[0] if len(created) == 1 else None


def _log_copy_done(path: Path, size_bytes: int, copy_started: float) -> None:
    """One INFO line per finished file: size, duration, throughput."""
    elapsed = max(time.monotonic() - copy_started, 0.001)
    logger.info(
        "portable copy done: %s (%0.2f MB in %0.2fs, %0.2f MB/s)",
        path.name,
        size_bytes / 1024**2,
        elapsed,
        (size_bytes / 1024**2) / elapsed,
    )


def _item_error_record(root: str, relative_path: str, display_path: str, message: str) -> FileRecord:
    return FileRecord(
        id=_stable_id(f"{root}/{relative_path}"),
        path=f"{root}/{relative_path}",
        display_path=display_path,
        source_archive_id=None,
        ext=Path(relative_path).suffix.lower(),
        size_bytes=0,
        mime_type=None,
        mtime=0.0,
        is_from_archive=False,
        status="error",
        error_message=f"portable-device staging failed: {message}",
    )


def _safe_name(name: str) -> str:
    name = name.strip().replace("/", "_").replace("\\", "_")
    if name in {"", ".", ".."}:
        return ""
    return name


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()
