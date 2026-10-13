"""Content extraction — Segment 6: images, audio/video, and the
binary/unknown catch-all, into a `ContentPayload`. Also home to
`extract_any()`, the never-`None` entry point later segments (fast/
detailed pass) are expected to call instead of reaching into either
extractor module directly.

Two jobs, matching PLAN.md Segment 6:
  - `extract(record, settings)`: this module's own piece of the
    extraction puzzle — images, audio/video, and (unlike Segment 5,
    which returns `None` for anything it doesn't own) a true catch-all
    for everything else, so that *this* module never leaves a file
    unaccounted for. See "Why `extract()` never actually returns
    `None`" below.
  - `extract_any(record, settings)`: the pipeline-facing entry point.
    Tries `text_docs.extract()` first, then this module's `extract()`,
    then one last bare-metadata fallback — a defensive backstop, not a
    normally-reached code path, given `extract()`'s own catch-all
    design. Never raises, never returns `None`.

What this module owns (by extension, case-insensitive):
  - Images (`_IMAGE_EXTENSIONS`): opened via Pillow, downscaled to a
    max long edge of `_IMAGE_MAX_LONG_EDGE_PX` (1024px — see "Image
    convention reconciled with Segment 5" below), re-encoded as JPEG,
    base64'd (no data-uri prefix, matching
    `ContentPayload.images_b64`'s documented shape), `kind="image"`.
    EXIF is pulled into `metadata`: a curated set of human-relevant
    tags (camera make/model, capture datetime, orientation, etc.) plus
    an explicit `gps_present: bool` flag and, when decodable, the
    actual decimal lat/long — PLAN.md calls out GPS presence
    specifically as "relevant to privacy/interesting intents," and a
    flag alone would make that intent's own job harder than it needs
    to be.
  - Audio/video (`_AUDIO_EXTENSIONS` / `_VIDEO_EXTENSIONS`): metadata
    only, never transcribed (out of scope for v1 per PLAN.md) and
    never treated as `kind="image"`/`"text"` — always
    `kind="metadata_only"`, since neither of those literal `kind`
    values fits "a sound/video file's tag and technical metadata."
    `mutagen` supplies tag data (title/artist/album/...) plus basic
    technical info (duration/bitrate/sample rate/channels) — solid for
    audio, thin-to-absent for most video containers. `ffprobe` (an
    external binary, used via `subprocess` only if found on `PATH`)
    supplements/refines that — especially for video, where it's the
    only one of the two that reliably reports codec/resolution/frame
    rate. Both are best-effort and independently optional; either,
    both, or neither being available all degrade gracefully to
    whatever subset of metadata could actually be gathered.
  - Everything else (`_extract_unknown`): magic-byte sniff via
    `python-magic` if it's installed (it wraps `libmagic`, a system
    library — see `requirements.txt`'s note), otherwise falls back to
    the extension-derived `mime_type` the `FileRecord` already carries
    from `scanner.py`/`archives.py`. Always `kind="metadata_only"`.
    Raw bytes are never read as text here, in either branch — a
    magic-byte sniff inspects a header, it doesn't decode/expose file
    content, which is the distinction PLAN.md's "never read raw bytes
    as text" is drawing (Segment 5's plain-text path is the *only*
    place in the pipeline that's supposed to do that).

Why `extract()` never actually returns `None` (judgment call, not a
contract deviation — the `ContentPayload | None` signature from
PLAN.md is honored verbatim, `None` is just an unreached branch of
it): PLAN.md's own framing for this segment is "cover everything
else... and a catch-all so nothing ever crashes the pipeline," which
only makes sense if *something* in the Segment 5 → Segment 6 →
bare-fallback chain is guaranteed to produce a real payload for a
totally unrecognized file. Segment 5 (`text_docs.py`) is deliberately
picky — `None` there is meaningful ("not text-shaped, try the next
stage"). This module is the next stage after which there *is* no more
specific stage, so it was written to be the one that actually closes
the loop: every extension not claimed by the image/audio/video
branches above falls through to `_extract_unknown()`, which always
succeeds. `extract_any()`'s "bare metadata fallback" tier below this
one is therefore a pure defensive backstop (an unexpected exception,
or some future change here that reintroduces a real `None` case) —
not something a normal run should ever reach.

Image convention reconciled with Segment 5: `text_docs.py`'s scanned-
PDF page renderer had to guess at an image convention before this
module existed (see its `_render_pdf_pages()` docstring and
PROJECT_STATE.md's "Open flags" from Segment 5) and landed on JPEG,
quality 85, long edge ~1024px. This module reuses those exact
numbers (`_IMAGE_MAX_LONG_EDGE_PX` / `_IMAGE_JPEG_QUALITY` below) so a
vision-model prompt built from a rendered PDF page and one built from
a photograph are sized/compressed consistently. Nothing in
`text_docs.py` was touched to effect this — it already matched.

Corrupt/truncated files never raise out of `extract()` or
`extract_any()` — same "never crash the pipeline" posture as
`scanner.py`, `archives.py`, and `text_docs.py`: a recognized-but-
broken image/audio/video file degrades to `kind="metadata_only"` with
an error note, exactly like Segment 5's "recognized-but-broken files"
handling, not a raised exception or a lost record.

Optional-import guarding: `mutagen` is a regular (non-optional)
dependency for this segment (see `requirements.txt`) but is still a
guarded import, same defensive posture Segment 4/5 already established
for their own libraries — one missing library degrades only its own
slice of metadata rather than taking the whole module's import down.
`python-magic` is guarded for a different reason: PLAN.md itself
phrases it as "if available," and it additionally needs the system
`libmagic` library present, which isn't something `pip install` alone
guarantees on every platform — see `requirements.txt`. `ffprobe` isn't
a Python import at all; it's located via `shutil.which()` at call
time (not cached at import time, so a mid-session PATH change is
picked up — cheap enough to check per-file).
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import shutil
import subprocess
from typing import Any

from app.config import Settings
from app.extractors import text_docs
from app.log import logger
from app.models import ContentPayload, FileRecord

# --------------------------------------------------------------------------
# Optional-but-expected / best-effort third-party imports — see module
# docstring's "Optional-import guarding" section.
# --------------------------------------------------------------------------

try:
    from PIL import Image
    from PIL.ExifTags import IFD, TAGS

    _HAS_PILLOW = True
except ImportError:  # pragma: no cover - exercised only in a stripped-down env
    Image = None  # type: ignore[assignment]
    IFD = None  # type: ignore[assignment]
    TAGS = {}
    _HAS_PILLOW = False

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HAS_PILLOW_HEIF = True
except (ImportError, RuntimeError, OSError, AttributeError):  # pragma: no cover - optional codec/runtime support
    _HAS_PILLOW_HEIF = False

try:
    import mutagen

    _HAS_MUTAGEN = True
except ImportError:  # pragma: no cover
    mutagen = None  # type: ignore[assignment]
    _HAS_MUTAGEN = False

try:
    import magic  # python-magic, wraps libmagic

    _HAS_MAGIC = True
except ImportError:  # pragma: no cover
    magic = None  # type: ignore[assignment]
    _HAS_MAGIC = False


# --------------------------------------------------------------------------
# What this module recognizes
# --------------------------------------------------------------------------

_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jfif", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".ico", ".heic", ".heif",
}

_AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".wma",
    ".opus", ".aiff", ".aif", ".ape", ".mid", ".midi",
}

_VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".3gp", ".3g2",
}

# --------------------------------------------------------------------------
# Image handling knobs — see module docstring's "Image convention
# reconciled with Segment 5."
# --------------------------------------------------------------------------

_IMAGE_MAX_LONG_EDGE_PX = 1024
_IMAGE_JPEG_QUALITY = 85
_DETAIL_IMAGE_MAX_LONG_EDGE_PX = 1600
_DETAIL_IMAGE_JPEG_QUALITY = 90

# A curated, human-relevant subset of the ~100+ possible EXIF tags —
# PLAN.md asks for EXIF "pulled into metadata," not a full raw dump;
# keeping this compact matters because it rides along on every single
# image scanned. GPS is handled separately below (not via this list),
# since it needs its own IFD lookup and DMS-to-decimal conversion.
_EXIF_TAGS_OF_INTEREST = {
    "Make", "Model", "Software", "DateTime", "DateTimeOriginal",
    "Orientation", "LensModel", "ExposureTime", "FNumber", "ISOSpeedRatings",
}
_EXIF_MAX_VALUE_LEN = 200  # defensive cap — some tags (e.g. UserComment) can be huge

# --------------------------------------------------------------------------
# Audio/video handling knobs
# --------------------------------------------------------------------------

_MUTAGEN_TAGS_OF_INTEREST = ("title", "artist", "album", "date", "genre", "albumartist", "tracknumber")
_FFPROBE_TIMEOUT_SECONDS = 20
_FFPROBE_PATH = shutil.which("ffprobe")  # resolved once at import; None if not on PATH

if _FFPROBE_PATH is None:
    logger.debug("ffprobe not found on PATH — video/audio metadata will rely on mutagen only")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def extract(
    record: FileRecord,
    settings: Settings,
    detail_level: str = "fast",
) -> ContentPayload | None:
    """This module's own extraction entry point. See the module
    docstring's "Why `extract()` never actually returns `None`" for why
    the `| None` in this signature (kept for interface parity with
    `text_docs.extract()`) doesn't correspond to a live branch below:
    every extension this module sees is handled by one of the three
    branches, including a true catch-all for anything unrecognized.
    Never raises — any recognized-but-broken file (corrupt image,
    unreadable audio/video, whatever) degrades to `kind="metadata_only"`
    with an error note instead.
    """
    detail_level = _normalized_detail_level(detail_level)
    ext = record.ext.lower()

    if ext in _IMAGE_EXTENSIONS:
        return _extract_image(record, settings, detail_level)
    if ext in _AUDIO_EXTENSIONS or ext in _VIDEO_EXTENSIONS:
        return _extract_audio_video(record, settings)
    return _extract_unknown(record, settings)


def extract_any(
    record: FileRecord,
    settings: Settings,
    detail_level: str = "fast",
) -> ContentPayload:
    """The pipeline-facing entry point (fast/detailed pass should call
    this, not `text_docs.extract()` or this module's `extract()`
    directly). Tries `text_docs.extract()` first (its `None` is the
    normal "not text-shaped" signal, not an error), then this module's
    own `extract()` (which — by design, see above — always produces a
    payload for anything Segment 5 didn't claim), then one last bare-
    metadata fallback purely as a defensive backstop. Never raises,
    never returns `None`. ``detail_level="detailed"`` asks image handling
    for a larger model-ready image while leaving the default fast mode
    unchanged.
    """
    try:
        payload = text_docs.extract(record, settings, detail_level=detail_level)
    except Exception as exc:
        logger.warning("text_docs.extract() raised unexpectedly for %s: %s — falling back", record.display_path, exc)
        payload = None
    if payload is not None:
        return payload

    try:
        payload = extract(record, settings, detail_level=detail_level)
    except Exception as exc:
        logger.warning("media_binary.extract() raised unexpectedly for %s: %s — falling back", record.display_path, exc)
        payload = None
    if payload is not None:
        return payload

    return _bare_metadata_fallback(record, "no extractor produced a payload")


def _normalized_detail_level(detail_level: str) -> str:
    """Treat unknown values as the cheap, backwards-compatible fast mode."""
    return "detailed" if detail_level == "detailed" else "fast"


def _image_limits(detail_level: str) -> tuple[int, int]:
    if detail_level == "detailed":
        return _DETAIL_IMAGE_MAX_LONG_EDGE_PX, _DETAIL_IMAGE_JPEG_QUALITY
    return _IMAGE_MAX_LONG_EDGE_PX, _IMAGE_JPEG_QUALITY


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _metadata_only_payload(record: FileRecord, error: str, extra_metadata: dict | None = None) -> ContentPayload:
    """Shared shape for "this module owns the extension but couldn't
    actually read this particular file" — mirrors `text_docs.py`'s
    `_metadata_only_payload()` (same reasoning, duplicated rather than
    imported: PLAN.md doesn't specify a shared-helpers module for the
    two extractors, and this keeps each extractor file self-contained,
    matching how neither imports private helpers from the other)."""
    logger.warning("could not extract %s (%s): %s", record.display_path, record.ext, error)
    metadata: dict[str, Any] = {"error": error}
    if extra_metadata:
        metadata.update(extra_metadata)
    return ContentPayload(
        file_id=record.id,
        kind="metadata_only",
        text_excerpt=None,
        images_b64=[],
        metadata=metadata,
        truncated=False,
    )


def _bare_metadata_fallback(record: FileRecord, note: str) -> ContentPayload:
    """`extract_any()`'s last-resort payload — see its docstring. Not
    normally reached; exists so the "never returns `None`" promise
    holds even if something above misbehaves."""
    logger.warning("extract_any() falling back to bare metadata for %s: %s", record.display_path, note)
    return ContentPayload(
        file_id=record.id,
        kind="metadata_only",
        text_excerpt=None,
        images_b64=[],
        metadata={"ext": record.ext, "mime_type": record.mime_type, "note": note},
        truncated=False,
    )


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def _extract_image(record: FileRecord, settings: Settings, detail_level: str) -> ContentPayload:
    if not _HAS_PILLOW:
        return _metadata_only_payload(record, "Pillow not installed")

    try:
        with Image.open(record.path) as img:
            img.load()  # forces the actual pixel decode now, inside this try — this
            # is what surfaces "truncated image" errors; without it, Image.open()'s
            # lazy loading would let a corrupt file slip past unnoticed until some
            # later, harder-to-attribute point.
            width, height = img.size
            metadata: dict[str, Any] = {
                "width": width,
                "height": height,
                "format": img.format,
                "mode": img.mode,
            }
            metadata.update(_extract_exif_metadata(img))

            max_long_edge, jpeg_quality = _image_limits(detail_level)
            resized = _downscale_for_model(img, max_long_edge)
            jpeg_bytes = _encode_jpeg(resized, jpeg_quality)

    except Exception as exc:
        return _metadata_only_payload(record, f"could not open/process image: {exc}")

    image_b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    long_edge = max(width, height)

    return ContentPayload(
        file_id=record.id,
        kind="image",
        text_excerpt=None,
        images_b64=[image_b64],
        metadata={
            **metadata,
            "detail_level": detail_level,
            "model_max_long_edge_px": max_long_edge,
        },
        truncated=long_edge > max_long_edge,
    )


def _downscale_for_model(img: "Image.Image", max_long_edge: int) -> "Image.Image":
    """Flatten to RGB (handles palette/RGBA/CMYK/1-bit/animated-first-
    frame images so the JPEG encode below never fails on mode) and
    downscale so the long edge is at most `_IMAGE_MAX_LONG_EDGE_PX`.
    Images already smaller than that are returned unscaled (only ever
    shrinks, never enlarges). Multi-frame images (animated GIF/WEBP)
    use whatever frame Pillow has active on open — the first — since
    animation playback is out of scope here."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        flattened = Image.new("RGB", img.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.split()[-1])
    else:
        flattened = img.convert("RGB")

    long_edge = max(flattened.size)
    if long_edge > max_long_edge:
        scale = max_long_edge / long_edge
        new_size = (max(1, round(flattened.width * scale)), max(1, round(flattened.height * scale)))
        flattened = flattened.resize(new_size, Image.LANCZOS)
    return flattened


def _encode_jpeg(img: "Image.Image", quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _extract_exif_metadata(img: "Image.Image") -> dict:
    """Pulls the curated `_EXIF_TAGS_OF_INTEREST` subset, plus a
    `gps_present` flag and (when decodable) decimal lat/long — see
    module docstring. Never raises: images with no EXIF at all (very
    common — screenshots, most PNGs, web-downloaded images) just get
    `{"gps_present": False}` back with no `exif` key, not an error."""
    metadata: dict[str, Any] = {"gps_present": False}
    try:
        exif = img.getexif()
    except Exception as exc:
        logger.debug("could not read EXIF: %s", exc)
        return metadata
    if not exif:
        return metadata

    readable: dict[str, str] = {}
    for tag_id, value in exif.items():
        tag_name = TAGS.get(tag_id, str(tag_id))
        if tag_name in _EXIF_TAGS_OF_INTEREST:
            text = str(value).strip()
            if text:
                readable[tag_name] = text[:_EXIF_MAX_VALUE_LEN]
    if readable:
        metadata["exif"] = readable

    gps_coords = _extract_gps_coords(exif)
    if gps_coords is not None:
        metadata["gps_present"] = True
        metadata["gps_latitude"], metadata["gps_longitude"] = gps_coords
    else:
        # GPSInfo IFD present but not decodable as coordinates still
        # counts as "GPS data present" for the privacy-relevance flag
        # PLAN.md asks for, even without usable lat/long.
        try:
            gps_ifd = exif.get_ifd(IFD.GPSInfo)
            metadata["gps_present"] = bool(gps_ifd)
        except Exception:
            pass

    return metadata


def _extract_gps_coords(exif) -> tuple[float, float] | None:
    """Decodes the GPS IFD's lat/long (stored as degrees/minutes/seconds
    plus N/S/E/W refs) into plain decimal degrees. Returns `None` if
    there's no GPS IFD, or it's missing the fields needed to compute a
    coordinate — never raises."""
    try:
        gps_ifd = exif.get_ifd(IFD.GPSInfo)
    except Exception:
        return None
    if not gps_ifd:
        return None

    # Numeric GPS tag IDs per the EXIF spec (GPSTAGS maps these, but
    # indexing by the well-known small integers directly avoids a second
    # lookup table for just four fields).
    lat = gps_ifd.get(2)  # GPSLatitude
    lat_ref = gps_ifd.get(1)  # GPSLatitudeRef
    lon = gps_ifd.get(4)  # GPSLongitude
    lon_ref = gps_ifd.get(3)  # GPSLongitudeRef
    if lat is None or lon is None:
        return None

    lat_decimal = _dms_to_decimal(lat, lat_ref)
    lon_decimal = _dms_to_decimal(lon, lon_ref)
    if lat_decimal is None or lon_decimal is None:
        return None
    return lat_decimal, lon_decimal


def _dms_to_decimal(dms: Any, ref: Any) -> float | None:
    try:
        degrees, minutes, seconds = (float(v) for v in dms)
        value = degrees + minutes / 60.0 + seconds / 3600.0
        if isinstance(ref, str) and ref.upper() in ("S", "W"):
            value = -value
        return round(value, 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# --------------------------------------------------------------------------
# Audio / video
# --------------------------------------------------------------------------


def _extract_audio_video(record: FileRecord, settings: Settings) -> ContentPayload:
    ext = record.ext.lower()
    media_kind = "audio" if ext in _AUDIO_EXTENSIONS else "video"
    metadata: dict[str, Any] = {"media_kind": media_kind}
    notes: list[str] = []

    mutagen_meta = _read_mutagen_metadata(record.path)
    if mutagen_meta:
        metadata.update(mutagen_meta)
    elif not _HAS_MUTAGEN:
        notes.append("mutagen not installed")

    # ffprobe's fields (when available) are generally more complete and
    # reliable, especially for video (codec/resolution/frame rate,
    # which mutagen mostly can't give for non-MP4 containers) — later
    # update wins, so ffprobe's values take precedence over mutagen's
    # for any key both happen to report (e.g. duration_seconds).
    ffprobe_meta = _read_ffprobe_metadata(record.path)
    if ffprobe_meta:
        metadata.update(ffprobe_meta)
    elif _FFPROBE_PATH is None:
        notes.append("ffprobe not found on PATH")

    if not mutagen_meta and not ffprobe_meta:
        notes.append("no technical metadata could be extracted (unsupported/corrupt file, or both mutagen and ffprobe unavailable)")
    if notes:
        metadata["extraction_notes"] = notes

    return ContentPayload(
        file_id=record.id,
        kind="metadata_only",
        text_excerpt=None,
        images_b64=[],
        metadata=metadata,
        truncated=False,
    )


def _read_mutagen_metadata(path: str) -> dict:
    if not _HAS_MUTAGEN:
        return {}
    try:
        audio = mutagen.File(path, easy=True)
    except Exception as exc:
        logger.debug("mutagen failed on %s: %s", path, exc)
        return {}
    if audio is None:
        return {}

    out: dict[str, Any] = {}
    info = getattr(audio, "info", None)
    if info is not None:
        length = getattr(info, "length", None)
        if length:
            out["duration_seconds"] = round(length, 2)
        bitrate = getattr(info, "bitrate", None)
        if bitrate:
            out["bitrate"] = bitrate
        sample_rate = getattr(info, "sample_rate", None)
        if sample_rate:
            out["sample_rate"] = sample_rate
        channels = getattr(info, "channels", None)
        if channels:
            out["channels"] = channels

    tags: dict[str, str] = {}
    if audio.tags:
        for key in _MUTAGEN_TAGS_OF_INTEREST:
            try:
                values = audio.tags.get(key)
            except Exception:
                values = None
            if values:
                tags[key] = ", ".join(str(v) for v in values)[:_EXIF_MAX_VALUE_LEN]
    if tags:
        out["tags"] = tags

    return out


def _read_ffprobe_metadata(path: str) -> dict:
    if not _FFPROBE_PATH:
        return {}
    try:
        result = subprocess.run(
            [_FFPROBE_PATH, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("ffprobe failed on %s: %s", path, exc)
        return {}

    if result.returncode != 0:
        logger.debug("ffprobe non-zero exit for %s: %s", path, (result.stderr or "")[:200])
        return {}

    try:
        probe = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("ffprobe produced unparseable JSON for %s: %s", path, exc)
        return {}

    out: dict[str, Any] = {}
    fmt = probe.get("format") or {}
    duration = _safe_float(fmt.get("duration"))
    if duration is not None:
        out["duration_seconds"] = round(duration, 2)
    bit_rate = _safe_int(fmt.get("bit_rate"))
    if bit_rate is not None:
        out["bitrate"] = bit_rate

    streams = probe.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video_stream:
        if video_stream.get("codec_name"):
            out["video_codec"] = video_stream["codec_name"]
        width = _safe_int(video_stream.get("width"))
        height = _safe_int(video_stream.get("height"))
        if width and height:
            out["width"] = width
            out["height"] = height
        frame_rate = _parse_frame_rate(video_stream.get("r_frame_rate"))
        if frame_rate is not None:
            out["frame_rate"] = frame_rate

    if audio_stream:
        if audio_stream.get("codec_name"):
            out["audio_codec"] = audio_stream["codec_name"]
        sample_rate = _safe_int(audio_stream.get("sample_rate"))
        if sample_rate is not None:
            out["sample_rate"] = sample_rate
        channels = _safe_int(audio_stream.get("channels"))
        if channels is not None:
            out["channels"] = channels

    return out


def _parse_frame_rate(raw: str | None) -> float | None:
    """ffprobe reports frame rate as a `"num/den"` fraction string
    (e.g. `"30000/1001"`), not a plain float."""
    if not raw:
        return None
    try:
        if "/" in raw:
            num, den = raw.split("/", 1)
            den_f = float(den)
            return round(float(num) / den_f, 3) if den_f else None
        return round(float(raw), 3)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Unknown / binary catch-all
# --------------------------------------------------------------------------


def _extract_unknown(record: FileRecord, settings: Settings) -> ContentPayload:
    """The true catch-all — see module docstring. Never reads file
    content as text; a magic-byte sniff inspects a header only."""
    sniffed_mime = _sniff_mime(record.path)
    metadata: dict[str, Any] = {
        "declared_ext": record.ext or None,
        "declared_mime_type": record.mime_type,
        "sniffed_mime_type": sniffed_mime,
        "magic_available": _HAS_MAGIC,
    }
    try:
        metadata["size_bytes"] = os.path.getsize(record.path)
    except OSError:
        pass

    return ContentPayload(
        file_id=record.id,
        kind="metadata_only",
        text_excerpt=None,
        images_b64=[],
        metadata=metadata,
        truncated=False,
    )


def _sniff_mime(path: str) -> str | None:
    """`python-magic` if installed (and `libmagic` is actually present
    on the system — see `requirements.txt`); falls back to the stdlib
    `mimetypes` extension-based guess otherwise. Never raises."""
    if _HAS_MAGIC:
        try:
            return magic.from_file(path, mime=True)
        except Exception as exc:
            logger.debug("python-magic failed to sniff %s: %s", path, exc)
    guessed, _ = mimetypes.guess_type(path)
    return guessed


# --------------------------------------------------------------------------
# Self-test: `python -m app.extractors.media_binary --selftest`
# --------------------------------------------------------------------------


def _run_selftest() -> None:
    """Builds tiny throwaway fixtures on the fly (no `test_assets/`
    dependency) and runs `extract()`/`extract_any()` against each,
    mirroring `text_docs.py`'s and `archives.py`'s self-test style."""
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
            max_file_read_mb=25,
            app_port=8731,
        )

        def make_record(path: str) -> FileRecord:
            ext = os.path.splitext(path)[1].lower()
            mime_type, _ = mimetypes.guess_type(path)
            return FileRecord(
                id=f"id-{os.path.basename(path)}",
                path=path,
                display_path=path,
                source_archive_id=None,
                ext=ext,
                size_bytes=os.path.getsize(path) if os.path.exists(path) else 0,
                mime_type=mime_type,
                mtime=time.time(),
                is_from_archive=False,
                status="pending",
                error_message=None,
            )

        def report(label: str, payload: ContentPayload | None) -> None:
            if payload is None:
                print(f"  [{label}] -> None")
                return
            print(
                f"  [{label}] kind={payload.kind} images={len(payload.images_b64)} "
                f"truncated={payload.truncated} metadata_keys={sorted(payload.metadata.keys())}"
            )

        print("Segment 6 self-test — building throwaway fixtures...")

        # --- a real photo with EXIF incl. GPS ---------------------------
        if _HAS_PILLOW:
            photo_path = os.path.join(work_dir, "photo.jpg")
            img = Image.new("RGB", (2000, 1000), color=(120, 160, 200))
            exif = img.getexif()
            exif[271] = "TestCam"  # Make
            exif[272] = "Model X"  # Model
            exif[306] = "2024:01:01 12:00:00"  # DateTime
            gps_ifd = exif.get_ifd(IFD.GPSInfo)
            gps_ifd[1] = "N"
            gps_ifd[2] = (37.0, 46.0, 30.0)
            gps_ifd[3] = "W"
            gps_ifd[4] = (122.0, 25.0, 10.0)
            img.save(photo_path, exif=exif)

            payload = extract(make_record(photo_path), settings)
            report("photo.jpg (2000x1000, EXIF+GPS)", payload)
            assert payload is not None and payload.kind == "image"
            assert len(payload.images_b64) == 1
            assert payload.truncated is True  # long edge 2000 > 1024 cap
            assert payload.metadata["gps_present"] is True
            assert abs(payload.metadata["gps_latitude"] - 37.775) < 0.01
            assert payload.metadata["exif"]["Make"] == "TestCam"

            # decode the returned image and confirm it was actually downscaled
            decoded = Image.open(io.BytesIO(base64.b64decode(payload.images_b64[0])))
            assert max(decoded.size) <= _IMAGE_MAX_LONG_EDGE_PX

            # --- a small photo with no EXIF at all -----------------------
            small_path = os.path.join(work_dir, "small.png")
            Image.new("RGBA", (100, 80), color=(10, 20, 30, 128)).save(small_path)
            payload = extract(make_record(small_path), settings)
            report("small.png (RGBA, no EXIF)", payload)
            assert payload is not None and payload.kind == "image"
            assert payload.truncated is False
            assert payload.metadata["gps_present"] is False

            # --- corrupt/truncated image -> metadata_only, no raise ------
            corrupt_path = os.path.join(work_dir, "corrupt.jpg")
            with open(corrupt_path, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0" + b"not really a jpeg" * 5)
            payload = extract(make_record(corrupt_path), settings)
            report("corrupt.jpg", payload)
            assert payload is not None and payload.kind == "metadata_only"
            assert "error" in payload.metadata
        else:
            print("  [images] skipped -- Pillow not installed, can't build fixtures")

        # --- audio (real, valid WAV via stdlib `wave`) -------------------
        import wave

        wav_path = os.path.join(work_dir, "sample.wav")
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 8000)  # 1 second of silence
        payload = extract(make_record(wav_path), settings)
        report("sample.wav (1s silence)", payload)
        assert payload is not None and payload.kind == "metadata_only"
        assert payload.metadata["media_kind"] == "audio"
        assert payload.text_excerpt is None

        # --- video, only if ffmpeg's available to build a real fixture ---
        if shutil.which("ffmpeg"):
            mp4_path = os.path.join(work_dir, "sample.mp4")
            build = subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=1",
                    "-pix_fmt", "yuv420p", mp4_path,
                ],
                capture_output=True,
                timeout=30,
            )
            if build.returncode == 0 and os.path.exists(mp4_path):
                payload = extract(make_record(mp4_path), settings)
                report("sample.mp4 (1s, 64x48)", payload)
                assert payload is not None and payload.kind == "metadata_only"
                assert payload.metadata["media_kind"] == "video"
                if _FFPROBE_PATH:
                    assert payload.metadata.get("width") == 64
            else:
                print("  [sample.mp4] skipped -- ffmpeg fixture build failed")
        else:
            print("  [sample.mp4] skipped -- ffmpeg not available to build a fixture")

        # --- unknown binary (.exe-shaped) --------------------------------
        exe_path = os.path.join(work_dir, "unknown.exe")
        with open(exe_path, "wb") as f:
            f.write(b"MZ\x00\x00" + b"fake-pe-header" * 10)
        payload = extract(make_record(exe_path), settings)
        report("unknown.exe", payload)
        assert payload is not None and payload.kind == "metadata_only"
        assert payload.text_excerpt is None

        # --- extensionless file -------------------------------------------
        noext_path = os.path.join(work_dir, "mystery_file")
        with open(noext_path, "wb") as f:
            f.write(b"\x01\x02\x03some-binary-looking-content\x00\x00")
        payload = extract(make_record(noext_path), settings)
        report("mystery_file (extensionless)", payload)
        assert payload is not None and payload.kind == "metadata_only"

        # --- empty file -----------------------------------------------------
        empty_path = os.path.join(work_dir, "empty.dat")
        open(empty_path, "wb").close()
        payload = extract(make_record(empty_path), settings)
        report("empty.dat (0 bytes)", payload)
        assert payload is not None and payload.kind == "metadata_only"

        # --- extract_any(): text-shaped file defers to text_docs.py -------
        txt_path = os.path.join(work_dir, "sample.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("plain text, should be handled by Segment 5, not this module\n")
        payload = extract_any(make_record(txt_path), settings)
        report("extract_any(sample.txt)", payload)
        assert payload.kind == "text"  # came from text_docs.py, not media_binary.py

        # --- extract_any(): binary file handled by this module ------------
        payload = extract_any(make_record(exe_path), settings)
        report("extract_any(unknown.exe)", payload)
        assert payload.kind == "metadata_only"

        # --- extract_any(): never None, never raises, even for a
        # nonexistent path (defensive backstop path) -----------------------
        ghost_record = make_record(exe_path)
        ghost_record = FileRecord(**{**ghost_record.to_dict(), "path": os.path.join(work_dir, "does_not_exist.bin")})
        payload = extract_any(ghost_record, settings)
        report("extract_any(nonexistent path)", payload)
        assert payload is not None and payload.kind == "metadata_only"

        print("OK")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker media/binary extraction utility")
    parser.add_argument("--selftest", action="store_true", help="build sample fixtures and run extract()/extract_any() on each")
    args = parser.parse_args()

    if args.selftest:
        _run_selftest()
    else:
        parser.print_help()
