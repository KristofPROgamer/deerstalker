"""Deerstalker configuration.

Loads environment variables (via python-dotenv) into a single `Settings`
dataclass. Every later segment should accept a `Settings` instance as a
constructor/function parameter (dependency injection) rather than reading
os.environ directly — that's what keeps each segment unit-testable in
isolation with a throwaway Settings object.

Field-name convention: each `.env` var (SCREAMING_SNAKE_CASE, see
.env.example) maps to a lower snake_case attribute of the same name, e.g.
OLLAMA_HOST -> Settings.ollama_host. Later segments should rely on this
mapping rather than re-deriving their own names for the same values.

v1.5 addition (see DEERSTALKER_PLAN_v1_5.md): the multi-provider worker
pool's two API key lists (`google_apikeys`, `ollama_apikeys`) are secrets,
not config values, so they deliberately do NOT live in `.env` -- they come
from `api_keys.py` at the project root instead (gitignored, imported by
`_load_api_keys()` below). Every other new v1.5 field is a normal `.env`
var like any other. Both key lists default to `[]` and every new field
defaults to "off" if neither `.env` nor `api_keys.py` mention it, so
existing single-local-Ollama setups are unaffected. No slot/worker COUNT
is ever hardcoded here or anywhere downstream -- it's always `len()` of
whichever list `api_keys.py` actually provided, read fresh on each
`Settings.load()` call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - normal installs use python-dotenv
    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        """Keep local CLI tools usable if the optional .env helper is absent."""
        return False

# NOTE: this session didn't have `app/log.py` on disk to confirm its exact
# interface (PROJECT_STATE.md references a shared `app.log.logger`, but the
# file itself wasn't attached), so this module uses a plain stdlib logger
# instead of guessing at that module's export shape. A child logger like
# this still feeds into whatever handlers/formatters a central `app.log`
# setup installs on the root logger, so this should already be compatible
# -- but flagged in PROJECT_STATE.md for confirmation once reunited with
# the rest of the codebase.
logger = logging.getLogger(__name__)


def _coerce_key_list(value: object) -> list[str]:
    """Turn whatever `api_keys.py` actually defined for one list name into
    a clean `list[str]`.

    Anything that isn't a `list` (missing attribute, wrong type, a typo'd
    single string, etc.) becomes `[]` rather than raising -- one malformed
    list must never crash startup or take down the *other* provider's
    list. Entries are coerced to `str`, stripped, and blanks/duplicates are
    dropped while preserving first-seen order.
    """
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for entry in value:
        candidate = str(entry).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def _load_api_keys() -> tuple[list[str], list[str]]:
    """Load (google_api_keys, ollama_cloud_api_keys) from `api_keys.py` at
    the project root, if present. Returns `([], [])` -- zero worker slots
    for both providers, no crash -- when the file is absent.

    Deliberately does NOT do `from api_keys import google_apikeys,
    ollama_apikeys`: that form fails the whole statement if either name is
    missing, which would let a typo in one list silently disable the
    *other* provider too. Importing the module once and then reading each
    name with its own `getattr(..., [])` keeps the two lists fully
    independent, per DEERSTALKER_PLAN_v1_5.md §7 (Segment 18).

    Never logs key values, truncated or otherwise -- only the fact that
    the file was or wasn't found.
    """
    try:
        import api_keys as _api_keys_module  # project-root api_keys.py, gitignored
    except (ImportError, ModuleNotFoundError):
        logger.info(
            "api_keys.py not found — Ollama Cloud and Gemini worker slots disabled"
        )
        return [], []

    google_api_keys = _coerce_key_list(getattr(_api_keys_module, "google_apikeys", []))
    ollama_cloud_api_keys = _coerce_key_list(getattr(_api_keys_module, "ollama_apikeys", []))
    return google_api_keys, ollama_cloud_api_keys


def _parse_optional_int(raw: str | None) -> int | None:
    """Parse an optional-int env var: unset or blank -> None (uncapped)."""
    if raw is None or raw.strip() == "":
        return None
    return int(raw)


@dataclass
class Settings:
    """Runtime configuration for the whole app. See PLAN.md §4 for the
    authoritative list of env vars this mirrors."""

    ollama_host: str
    ollama_model: str
    ollama_fast_model: str | None
    ollama_detailed_model: str | None
    ollama_timeout_seconds: int
    ollama_max_concurrency: int

    app_data_dir: str
    max_archive_depth: int
    max_archive_total_mb: int
    max_file_read_mb: int

    app_port: int

    # Windows portable-device staging. Empty uses {APP_DATA_DIR}/tmp.
    portable_stage_dir: str | None = None
    portable_stage_batch_gib: int = 5

    # v1.5 multi-provider worker pool. Entirely optional/additive -- every
    # field here defaults to "off." The two key lists are NOT read from
    # .env; see `_load_api_keys()` and the module docstring above.
    ollama_cloud_host: str = "https://ollama.com"
    ollama_cloud_api_keys: list[str] = field(default_factory=list)
    gemini_model: str = "gemini-flash-latest"
    gemini_timeout_seconds: int = 120
    google_api_keys: list[str] = field(default_factory=list)
    # Optional hard cap on total slots (Ollama Cloud + Gemini combined).
    # None = uncapped. This is the only place a slot COUNT is ever capped
    # by a literal anywhere in v1.5 -- slot counts themselves are always
    # len(ollama_cloud_api_keys) + len(google_api_keys).
    worker_pool_max_slots: int | None = None

    @classmethod
    def load(cls, env_file: str | None = ".env") -> Settings:
        """Build a Settings object from environment variables (+ .env file
        if present). Creates `app_data_dir` if it doesn't exist yet.

        Args:
            env_file: path to a dotenv file to load before reading
                os.environ. Pass None to skip dotenv loading entirely
                (useful in tests that set os.environ directly).

        Returns:
            A populated Settings instance.
        """
        if env_file is not None and os.path.exists(env_file):
            load_dotenv(env_file, override=False)

        google_api_keys, ollama_cloud_api_keys = _load_api_keys()

        settings = cls(
            ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2-vision"),
            ollama_fast_model=os.getenv("OLLAMA_FAST_MODEL") or None,
            ollama_detailed_model=os.getenv("OLLAMA_DETAILED_MODEL") or None,
            ollama_timeout_seconds=int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120")),
            ollama_max_concurrency=int(os.getenv("OLLAMA_MAX_CONCURRENCY", "2")),
            app_data_dir=os.getenv("APP_DATA_DIR", "./data"),
            max_archive_depth=int(os.getenv("MAX_ARCHIVE_DEPTH", "3")),
            max_archive_total_mb=int(os.getenv("MAX_ARCHIVE_TOTAL_MB", "2000")),
            max_file_read_mb=int(os.getenv("MAX_FILE_READ_MB", "25")),
            app_port=int(os.getenv("APP_PORT", "8731")),
            portable_stage_dir=os.getenv("PORTABLE_STAGE_DIR") or None,
            portable_stage_batch_gib=max(1, int(os.getenv("PORTABLE_STAGE_BATCH_GIB", "5"))),
            ollama_cloud_host=os.getenv("OLLAMA_CLOUD_HOST", "https://ollama.com"),
            ollama_cloud_api_keys=ollama_cloud_api_keys,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
            gemini_timeout_seconds=int(os.getenv("GEMINI_TIMEOUT_SECONDS", "120")),
            google_api_keys=google_api_keys,
            worker_pool_max_slots=_parse_optional_int(os.getenv("WORKER_POOL_MAX_SLOTS")),
        )
        os.makedirs(settings.app_data_dir, exist_ok=True)
        return settings


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide Settings singleton, lazily built on first call.

    Most modules should just call this. Segments/tests that need an
    isolated or overridden config should construct `Settings.load(...)` or
    `Settings(...)` directly instead of going through the singleton.
    """
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings
