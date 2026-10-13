"""Ollama HTTP client — the one module every analysis segment (fast pass,
detailed pass, and anything else that needs a model judgement) calls into.

Deliberately talks to `POST {OLLAMA_HOST}/api/generate`, not `/api/chat` —
see PLAN.md §0. This is a documented, load-bearing choice: don't let a
later segment quietly assume the `/api/chat` message-list shape instead.

Every method here is synchronous/blocking. Callers that want concurrency
(Segment 8/9) are expected to wrap calls to this class in their own
`ThreadPoolExecutor`, bounded by `settings.ollama_max_concurrency` — this
client does not manage concurrency itself.

Reachability vs. errors, on purpose:
  - Connection-level failures (Ollama not running / host unreachable /
    request timeout) are retried with backoff, then raised as
    `OllamaUnavailableError` once retries are exhausted. This is the
    "is Ollama even up" failure mode.
  - A transient server-side HTTP error response
    (`_RETRYABLE_SERVER_ERROR_STATUS_CODES` -- 500/502/503/504: Ollama's
    own backend having a bad moment, not a problem with the request) is
    retried with the same backoff, then raised as `app.provider_errors
    .ProviderUnavailableError` once exhausted -- see "Real-world addition"
    below for why this is `ProviderUnavailableError`, not
    `OllamaUnavailableError`, even for a plain local client.
  - Any other HTTP error response (e.g. 404 for a model that isn't
    pulled, 400 for a malformed request) means the server responded with
    something retrying won't fix, so it's not retried here --
    `response.raise_for_status()` lets `requests.HTTPError` propagate
    directly. Callers (fast/detailed pass) should catch broadly per-file
    regardless, per PLAN.md Segment 8's "one file's exception never kills
    the pass" requirement, so this distinction mainly affects log
    messages / retry behavior, not whether the pipeline survives.

v1.5 addition (see DEERSTALKER_PLAN_v1_5.md, Segment 19): this same class
also speaks to Ollama Cloud (`https://ollama.com`), not just a local
`OLLAMA_HOST`. Pass `host_override=`/`api_key=`/`label=` to the
constructor to get an Ollama-Cloud-flavored instance -- one such instance
per configured Cloud API key is how `app.pipeline.worker_pool` builds its
Ollama Cloud slots. All three params default to today's plain-local
behavior, so every existing call site (`OllamaClient(settings)`) is
unaffected. The one real behavioral change: an HTTP 429 response (rate
limit / quota) is now raised as `app.provider_errors
.ProviderRateLimitedError` instead of a generic `requests.HTTPError`, so
the worker pool can retry that call on a different slot instead of
failing it outright -- see that module's docstring for why this is kept
distinct from `OllamaUnavailableError` below. This does not touch the
connection-error/timeout retry path at all; that's still exactly the "is
this slot even reachable" signal it always was.

Real-world addition (post-Segment 24): the worker pool only caught
`ProviderRateLimitedError`/`ProviderUnavailableError` from a borrowed
slot -- a plain non-429 HTTP error (esp. 500/502/503/504, which cloud
LLM backends throw fairly often under load, transiently) fell straight
through the pool uncaught, permanently failing that one file's result
even with other healthy slots sitting idle. `_post_with_retry` now
retries those four status codes with the same backoff as a connection
failure, then raises `app.provider_errors.ProviderUnavailableError` (not
`OllamaUnavailableError`) once exhausted -- so a pool-wrapped Cloud slot
gets retried on a different slot exactly like a 429 or a connection
failure would be. This applies identically to a plain local client (no
`host_override`/`api_key`): the pass engines only specially handle
`OllamaUnavailableError`, so a `ProviderUnavailableError` from a local
call is simply caught by their existing broad per-file handler -- the
same "fail this one file, move on" outcome as before, just after a few
backoff retries first instead of failing on the very first 500. Every
other non-retryable HTTP error (400, 401, 403, 404, and any 5xx outside
`_RETRYABLE_SERVER_ERROR_STATUS_CODES`) is completely untouched -- still
an immediate, unretried `requests.HTTPError`, exactly as before. Segment
19's own connection-error/timeout -> `OllamaUnavailableError` path (see
"watch out for" in PROJECT_STATE.md's Segment 19 section) is also
completely untouched -- this is a new, parallel retry path for a
different failure mode, not a change to that one.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from app.config import Settings, get_settings
from app.log import logger
from app.provider_errors import ProviderRateLimitedError, ProviderUnavailableError

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_HEALTH_CHECK_TIMEOUT_SECONDS = 5

# Transient server-side error responses -- the backend responded, but with
# a status that usually means "try again," not "something about this
# request is wrong" (that's the other 4xx codes, which stay unretried).
# Ollama Cloud and local Ollama both surface these under real load; not
# retrying them wastes a worker-pool slot's fallback capacity on exactly
# the kind of blip the pool exists to absorb. See module docstring's
# "Real-world addition" paragraph.
_RETRYABLE_SERVER_ERROR_STATUS_CODES = frozenset({500, 502, 503, 504})


class OllamaUnavailableError(Exception):
    """Raised when Ollama can't be reached at all (connection refused,
    DNS failure, repeated timeouts, ...) after retries are exhausted.
    Distinct from an HTTP error response, which means Ollama *was*
    reachable but returned a failure (see module docstring)."""


class OllamaClient:
    """Thin wrapper around Ollama's `/api/generate` HTTP endpoint."""

    def __init__(
        self,
        settings: Settings,
        *,
        host_override: str | None = None,
        api_key: str | None = None,
        label: str = "ollama-local",
    ) -> None:
        """`host_override`/`api_key`/`label` are all optional and default
        to plain local-Ollama behavior (no override host, no auth header,
        label purely cosmetic for logs). Set all three to point this same
        client at one Ollama Cloud slot: `host_override=settings
        .ollama_cloud_host`, `api_key=<one entry from settings
        .ollama_cloud_api_keys>`, `label=<something identifying that
        slot>`."""
        self._settings = settings
        self._base_url = (host_override or settings.ollama_host).rstrip("/")
        self._api_key = api_key
        self._label = label

    def _headers(self) -> dict[str, str]:
        """`Authorization: Bearer {api_key}` on every request (generate
        calls and `health_check`) when this instance has an `api_key` --
        i.e. an Ollama Cloud slot. Local Ollama (no `api_key`, the
        default) sends no auth header, identical to pre-v1.5 behavior."""
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def generate_text(
        self,
        system: str,
        prompt: str,
        json_mode: bool = True,
        model: str | None = None,
    ) -> dict | str:
        """Text-only generation. Returns a parsed `dict` when
        `json_mode=True` (or `{"_parse_error": True, "raw": <text>}` if the
        model's output couldn't be parsed as JSON), otherwise the raw
        response `str`.

        `model` overrides the model used for this one call; if omitted,
        falls back to `settings.ollama_model`. Picking between
        `settings.ollama_fast_model` / `settings.ollama_detailed_model` /
        the base model is the caller's job (Segment 8/9), not this
        client's — this method has no opinion on fast vs. detailed.
        """
        return self._generate(system=system, prompt=prompt, images_b64=None, json_mode=json_mode, model=model)

    def generate_vision(
        self,
        system: str,
        prompt: str,
        images_b64: list[str],
        json_mode: bool = True,
        model: str | None = None,
    ) -> dict | str:
        """Same as `generate_text`, plus one or more images. `images_b64`
        entries must be raw base64 (no `data:image/...;base64,` prefix) —
        the same shape `ContentPayload.images_b64` already stores them in.
        """
        return self._generate(system=system, prompt=prompt, images_b64=images_b64, json_mode=json_mode, model=model)

    def health_check(self) -> bool:
        """Lightweight reachability probe — `GET /api/tags`. Returns
        `True`/`False`, never raises, and uses a short fixed timeout
        (independent of `OLLAMA_TIMEOUT_SECONDS`, which governs generation
        calls) so a dead host doesn't stall a startup check."""
        try:
            response = requests.get(
                f"{self._base_url}/api/tags",
                timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
                headers=self._headers(),
            )
            return response.ok
        except requests.exceptions.RequestException as exc:
            logger.debug("ollama health_check failed: %s", exc)
            return False

    # ----------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------

    def _generate(
        self,
        *,
        system: str,
        prompt: str,
        images_b64: list[str] | None,
        json_mode: bool,
        model: str | None,
    ) -> dict | str:
        payload: dict[str, Any] = {
            "model": model or self._settings.ollama_model,
            "prompt": prompt,
            "system": system,
            "stream": False,
        }
        if images_b64:
            payload["images"] = images_b64
        if json_mode:
            payload["format"] = "json"

        response_body = self._post_with_retry("/api/generate", payload)
        text = response_body.get("response", "")

        if json_mode:
            return _parse_json_response(text)
        return text

    def _post_with_retry(self, path: str, payload: dict) -> dict:
        """POST to `{base_url}{path}` with retry+backoff on connection
        errors/timeouts and on transient server errors
        (`_RETRYABLE_SERVER_ERROR_STATUS_CODES` -- 500/502/503/504).
        Raises `OllamaUnavailableError` if every attempt fails to
        connect; raises `app.provider_errors.ProviderUnavailableError` if
        every attempt instead gets one of those transient server-error
        statuses (see module docstring's "Real-world addition" paragraph
        for why these two exhaustion cases raise different exception
        types even though the retry shape is identical). Any other HTTP
        error response propagates immediately as `requests.HTTPError`,
        unretried (see module docstring)."""
        url = f"{self._base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=self._settings.ollama_timeout_seconds,
                )
                if response.status_code == 429:
                    # One slot's rate limit, not "Ollama is down" -- raised
                    # immediately (not retried in this loop, same as any
                    # other HTTP error response per the module docstring)
                    # so the worker pool (Segment 21) can retry this same
                    # call on a different slot instead.
                    retry_after = response.headers.get("Retry-After")
                    detail = f"; Retry-After={retry_after}" if retry_after else ""
                    logger.warning(
                        "ollama slot '%s' at %s was rate limited (HTTP 429)%s",
                        self._label,
                        url,
                        detail,
                    )
                    raise ProviderRateLimitedError(
                        f"ollama slot '{self._label}' at {url} was rate limited (HTTP 429){detail}"
                    )
                if response.status_code in _RETRYABLE_SERVER_ERROR_STATUS_CODES:
                    # The server responded but with a transient-looking
                    # error (its own backend having a bad moment, not a
                    # problem with this request) -- retried with the same
                    # backoff as a connection failure, below, rather than
                    # the immediate-unretried treatment other HTTP errors
                    # get.
                    try:
                        response.raise_for_status()
                    except requests.HTTPError as exc:
                        last_error = exc
                        if attempt < _MAX_ATTEMPTS:
                            backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                            logger.warning(
                                "ollama slot '%s' at %s returned HTTP %d (attempt %d/%d): %s — retrying in %.1fs",
                                self._label,
                                url,
                                response.status_code,
                                attempt,
                                _MAX_ATTEMPTS,
                                exc,
                                backoff,
                            )
                            time.sleep(backoff)
                            continue
                        logger.error(
                            "ollama slot '%s' at %s kept returning HTTP %d after %d attempts: %s",
                            self._label,
                            url,
                            response.status_code,
                            _MAX_ATTEMPTS,
                            exc,
                        )
                        raise ProviderUnavailableError(
                            f"ollama slot '{self._label}' at {url} kept returning "
                            f"HTTP {response.status_code} after {_MAX_ATTEMPTS} attempts"
                        ) from last_error
                response.raise_for_status()
                return response.json()
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = exc
                if attempt < _MAX_ATTEMPTS:
                    backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "ollama request to %s failed (attempt %d/%d): %s — retrying in %.1fs",
                        url,
                        attempt,
                        _MAX_ATTEMPTS,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)
                else:
                    logger.error("ollama unreachable at %s after %d attempts: %s", url, _MAX_ATTEMPTS, exc)

        raise OllamaUnavailableError(
            f"could not reach Ollama at {self._base_url} after {_MAX_ATTEMPTS} attempts"
        ) from last_error


# --------------------------------------------------------------------------
# Robust JSON parsing
# --------------------------------------------------------------------------


def _parse_json_response(text: str) -> dict:
    """Best-effort JSON parsing of a model's text response.

    Tries a straight `json.loads()` first (the common case when
    `format: "json"` did its job), then falls back to locating the first
    balanced `{...}` block in case the model wrapped its JSON in prose
    (e.g. "Sure, here's the analysis: {...}"). Never raises — on total
    failure returns `{"_parse_error": True, "raw": text}` so callers can
    detect and handle it (log + skip, mark the file `status="error"`,
    whatever's appropriate) without wrapping every call in a try/except.
    """
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass

    extracted = _extract_first_json_object(stripped)
    if extracted is not None:
        try:
            return json.loads(extracted)
        except (json.JSONDecodeError, ValueError):
            pass

    logger.warning("could not parse JSON from model response (%d chars)", len(text))
    return {"_parse_error": True, "raw": text}


def _extract_first_json_object(text: str) -> str | None:
    """Find the first balanced `{...}` substring in `text`, tracking
    quoted strings (with escape handling) so a `}` inside a string value
    doesn't end the match early. Returns `None` if no balanced object is
    found."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# --------------------------------------------------------------------------
# Self-test: `python -m app.ollama_client --ping`
# --------------------------------------------------------------------------


def _run_ping() -> None:
    settings = get_settings()
    client = OllamaClient(settings)
    if client.health_check():
        print(f"OK — Ollama reachable at {settings.ollama_host}")
    else:
        print(f"FAIL — Ollama not reachable at {settings.ollama_host}")
        raise SystemExit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker Ollama client utility")
    parser.add_argument("--ping", action="store_true", help="health-check the configured Ollama host")
    args = parser.parse_args()

    if args.ping:
        _run_ping()
    else:
        parser.print_help()
