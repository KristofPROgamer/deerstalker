"""Google Gemini REST client — Segment 20 of DEERSTALKER_PLAN_v1_5.md.

A second backend client with the exact same call shape as
`app.ollama_client.OllamaClient` (`generate_text`, `generate_vision`,
`health_check` — see PLAN_v1_5.md §4's "Provider" contract), so it's a
drop-in for the same worker-pool slot `MultiProviderClient` (Segment 21)
hands to `run_fast_pass`/`run_detailed_pass`'s existing `client:`
parameter. This module has no opinion on *which* provider a given call
should use, how many Gemini slots exist, or retrying on a different slot
-- that's the pool's job. This client only knows how to make one Gemini
API key talk to one Gemini model correctly.

Talks to Google's `generateContent` REST endpoint,
`POST https://generativelanguage.googleapis.com/v1beta/models/{model}
:generateContent` -- verified against ai.google.dev's current API
reference and multiple current (2026) official examples at the time this
was written, not assumed from training data alone, per the plan's
explicit "verify the current exact request/response JSON field names"
instruction. Request shape: a `contents` list of `{role, parts}`, where
each part is either `{"text": ...}` or `{"inlineData": {"mimeType":
..., "data": <base64>}}`; a top-level `systemInstruction` with its own
`parts` list; and `generationConfig.responseMimeType = "application/json"`
to ask for JSON-mode output. The API key goes in the `x-goog-api-key`
request header (current official convention) rather than a `?key=` query
param, exactly per the plan -- headers don't end up in URL-based request
logs the way a query string does.

Image parts always use `mimeType: "image/jpeg"` unconditionally, not
derived per-call: confirmed against the real `app/extractors
/media_binary.py` (attached this session per the plan's own file list)
that every entry `ContentPayload.images_b64` ever contains is already a
plain-base64 (no data-uri prefix), JPEG-re-encoded string --
`_extract_image()`'s `_encode_jpeg()` always saves `format="JPEG"`
regardless of the source file's original format. There is no other image
encoding this client will ever be asked to send.

Error mapping, mirroring `OllamaClient`'s reachability-vs-errors split
(see that module's own docstring) but landing on the provider-agnostic
exception types instead of `OllamaUnavailableError`, since a Gemini key
problem must never be able to trigger that class's "stop the whole pass"
meaning (PLAN_v1_5.md §1):
  - HTTP 429 (Google's `RESOURCE_EXHAUSTED` quota/rate-limit response) ->
    `app.provider_errors.ProviderRateLimitedError` immediately, not
    retried in this client -- same "let the pool retry on a different
    slot" contract Segment 19 gave `OllamaClient`.
  - Connection-level failures (refused connection, DNS, timeout) are
    retried with backoff (same `_MAX_ATTEMPTS`/backoff shape as
    `OllamaClient._post_with_retry`), then raised as
    `app.provider_errors.ProviderUnavailableError` once exhausted --
    `OllamaClient`'s equivalent case raises `OllamaUnavailableError`
    instead, precisely because that one *is* allowed to stop the whole
    pass (it's the local-Ollama-server-is-down signal); one Gemini key
    being unreachable is not that.
  - A transient server-side HTTP error response
    (`_RETRYABLE_SERVER_ERROR_STATUS_CODES` -- 500/502/503/504, Google's
    backend having a bad moment rather than a problem with the request)
    is retried with the same backoff, then also raised as
    `ProviderUnavailableError` once exhausted -- the *same* exception
    type as the connection-failure case above, since from the worker
    pool's perspective both just mean "this slot didn't work this time,
    try another." (Real-world addition, post-Segment 24: 503 in
    particular shows up fairly often from Gemini's shared capacity under
    load -- not retrying it wasted a worker-pool slot's whole purpose on
    exactly the blip the pool exists to absorb.)
  - Any other HTTP error response (400/403/404/...) is left to propagate
    as a plain `requests.HTTPError` via `raise_for_status()`, unretried --
    identical posture to `OllamaClient` for its non-retryable HTTP
    errors. The pass engines already catch broadly per-file (PLAN.md
    Segment 8), so this doesn't need its own special handling here.

JSON-parsing helpers (`_parse_json_response`/`_extract_first_json_object`)
are **duplicated** from `app.ollama_client`, not imported -- deliberately,
per the plan: this keeps each backend client module self-contained, the
same convention `detailed_pass.py` already established relative to
`fast_pass.py` (see that file's own docstring). Keep any future fix to
this parsing logic in sync across both copies by hand.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from app.log import logger
from app.provider_errors import ProviderRateLimitedError, ProviderUnavailableError

_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_HEALTH_CHECK_TIMEOUT_SECONDS = 5

# Transient server-side error responses -- same rationale and status-code
# set as app.ollama_client's copy of this constant; duplicated rather than
# imported per this module's own "keep each backend client self-contained"
# convention (see module docstring's "JSON-parsing helpers" note).
_RETRYABLE_SERVER_ERROR_STATUS_CODES = frozenset({500, 502, 503, 504})

# Every image this client is ever asked to send has already been
# re-encoded as JPEG by `app/extractors/media_binary.py` -- see module
# docstring. Not derived per-call; there is nothing to derive it from,
# since `images_b64` entries carry no format metadata of their own.
_IMAGE_MIME_TYPE = "image/jpeg"


class GeminiClient:
    """Thin wrapper around one Gemini API key's `generateContent` calls.

    One instance == one API key == one worker-pool slot (Segment 21 builds
    one `GeminiClient` per entry in `settings.google_api_keys`). Holds no
    shared state with any other instance/key.
    """

    def __init__(self, api_key: str, model: str, timeout_seconds: int, label: str) -> None:
        """`model` is this slot's default (`settings.gemini_model` for a
        pool-built slot) -- callers can still override it per-call via
        `generate_text`/`generate_vision`'s own `model` param, matching
        `OllamaClient`'s shape, even though the worker pool itself never
        exercises that override (see `pipeline/worker_pool.py`'s own
        docstring: a borrowed slot always uses its own preconfigured
        model). `label` is purely cosmetic, for logs -- identifies which
        slot a given log line/error came from without ever printing the
        key itself."""
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._label = label

    def _headers(self) -> dict[str, str]:
        """`x-goog-api-key` on every request (generate calls *and*
        `health_check`) -- the current officially documented way to
        authenticate, and, per the plan, deliberately a header rather than
        a `?key=` query param so the key doesn't end up in URL-based
        request logs."""
        return {"x-goog-api-key": self._api_key}

    # ----------------------------------------------------------------
    # Public API -- matches OllamaClient's three-method "Provider" shape
    # exactly (PLAN_v1_5.md §4), so either can be passed as `client=` to
    # run_fast_pass/run_detailed_pass with zero changes to their logic.
    # ----------------------------------------------------------------

    def generate_text(
        self,
        system: str,
        prompt: str,
        json_mode: bool = True,
        model: str | None = None,
    ) -> dict | str:
        """Text-only generation. Returns a parsed `dict` when
        `json_mode=True` (or `{"_parse_error": True, "raw": <text>}` if
        the model's output couldn't be parsed as JSON), otherwise the raw
        response `str`. `model` overrides this slot's configured model for
        this one call only; omit it to use the model this instance was
        constructed with."""
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
        entries must be raw base64 JPEG bytes, no `data:image/...;base64,`
        prefix -- exactly the shape `ContentPayload.images_b64` already
        stores them in (see module docstring)."""
        return self._generate(system=system, prompt=prompt, images_b64=images_b64, json_mode=json_mode, model=model)

    def health_check(self) -> bool:
        """Lightweight reachability + auth probe -- `GET
        /v1beta/models/{model}` (Google's `models.get`), which returns
        model metadata without spending a generation call. Returns
        `True`/`False`, never raises, and uses a short fixed timeout
        (independent of `timeout_seconds`, which governs generation calls)
        so a bad key/dead network doesn't stall a startup check."""
        try:
            response = requests.get(
                f"{_API_BASE}/models/{self._model}",
                timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
                headers=self._headers(),
            )
            return response.ok
        except requests.exceptions.RequestException as exc:
            logger.debug("gemini health_check failed (slot '%s'): %s", self._label, exc)
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
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if images_b64:
            for image_b64 in images_b64:
                parts.append({"inlineData": {"mimeType": _IMAGE_MIME_TYPE, "data": image_b64}})

        payload: dict[str, Any] = {"contents": [{"role": "user", "parts": parts}]}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            payload["generationConfig"] = {"responseMimeType": "application/json"}

        response_body = self._post_with_retry(model or self._model, payload)
        text = _extract_text(response_body, label=self._label)

        if json_mode:
            return _parse_json_response(text)
        return text

    def _post_with_retry(self, model: str, payload: dict) -> dict:
        """POST to `{_API_BASE}/models/{model}:generateContent` with
        retry+backoff on connection errors/timeouts and on transient
        server errors (`_RETRYABLE_SERVER_ERROR_STATUS_CODES` --
        500/502/503/504) -- same shape as `OllamaClient._post_with_retry`.
        Raises `ProviderUnavailableError` (not `OllamaUnavailableError` --
        see module docstring) once every attempt is exhausted, regardless
        of which of those two failure modes caused the final attempt to
        fail; raises `ProviderRateLimitedError` immediately (unretried
        here) on HTTP 429; lets any other `requests.HTTPError` propagate
        as-is, unretried."""
        url = f"{_API_BASE}/models/{model}:generateContent"
        last_error: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=self._timeout_seconds,
                )
                if response.status_code == 429:
                    # One key's quota/rate limit, not "Gemini is down" --
                    # raised immediately (not retried in this loop, same
                    # as any other HTTP error response) so the worker pool
                    # (Segment 21, not yet built) can retry this same call
                    # on a different slot instead.
                    retry_after = response.headers.get("Retry-After")
                    detail = f"; Retry-After={retry_after}" if retry_after else ""
                    logger.warning(
                        "gemini slot '%s' at %s was rate limited (HTTP 429)%s",
                        self._label,
                        url,
                        detail,
                    )
                    raise ProviderRateLimitedError(
                        f"gemini slot '{self._label}' at {url} was rate limited (HTTP 429){detail}"
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
                                "gemini slot '%s' at %s returned HTTP %d (attempt %d/%d): %s — retrying in %.1fs",
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
                            "gemini slot '%s' at %s kept returning HTTP %d after %d attempts: %s",
                            self._label,
                            url,
                            response.status_code,
                            _MAX_ATTEMPTS,
                            exc,
                        )
                        raise ProviderUnavailableError(
                            f"gemini slot '{self._label}' at {url} kept returning "
                            f"HTTP {response.status_code} after {_MAX_ATTEMPTS} attempts"
                        ) from last_error
                response.raise_for_status()
                return response.json()
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = exc
                if attempt < _MAX_ATTEMPTS:
                    backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "gemini request to %s failed (attempt %d/%d): %s — retrying in %.1fs",
                        url,
                        attempt,
                        _MAX_ATTEMPTS,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)
                else:
                    logger.error(
                        "gemini slot '%s' unreachable at %s after %d attempts: %s",
                        self._label,
                        url,
                        _MAX_ATTEMPTS,
                        exc,
                    )

        raise ProviderUnavailableError(
            f"could not reach gemini slot '{self._label}' at {url} after {_MAX_ATTEMPTS} attempts"
        ) from last_error


# --------------------------------------------------------------------------
# Response-shape helpers
# --------------------------------------------------------------------------


def _extract_text(response_json: dict, *, label: str) -> str:
    """Pull the generated text back out of a `generateContent` response
    body: `candidates[0].content.parts[*].text`, concatenated (a response
    can legally split its output across more than one part). Google's
    envelope is not shaped like Ollama's single `response` string field,
    so this has no equivalent in `ollama_client.py` to mirror.

    Never raises. A response with zero candidates (e.g. blocked by a
    safety filter -- Google reports that via `promptFeedback`, not an
    HTTP error) degrades to `""` with a warning log that includes the
    `promptFeedback` value, rather than raising or crashing -- consistent
    with every other extractor/client in this codebase treating
    "couldn't get usable content" as a normal, loggable outcome, never an
    exception."""
    candidates = response_json.get("candidates") or []
    if not candidates:
        logger.warning(
            "gemini slot '%s' returned no candidates (promptFeedback=%s)",
            label,
            response_json.get("promptFeedback"),
        )
        return ""

    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


# --------------------------------------------------------------------------
# Robust JSON parsing -- duplicated from app.ollama_client, byte-for-byte
# in logic, on purpose. See module docstring's "JSON-parsing helpers" note
# for why this isn't a shared import instead.
# --------------------------------------------------------------------------


def _parse_json_response(text: str) -> dict:
    """Best-effort JSON parsing of a model's text response.

    Tries a straight `json.loads()` first (the common case when
    `responseMimeType: "application/json"` did its job), then falls back
    to locating the first balanced `{...}` block in case the model
    wrapped its JSON in prose. Never raises -- on total failure returns
    `{"_parse_error": True, "raw": text}` so callers can detect and
    handle it without wrapping every call in a try/except.
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

    logger.warning("could not parse JSON from gemini response (%d chars)", len(text))
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
# Self-test: `python -m app.gemini_client --ping --api-key KEY [--model M]`
# --------------------------------------------------------------------------
#
# Deliberately takes the key/model as CLI args rather than reading
# `get_settings()`/`api_keys.py`: unlike OllamaClient's `--ping` (one
# fixed `OLLAMA_HOST` to check), there's no single "the" Gemini key to
# default to here -- `settings.google_api_keys` may hold any number of
# them, each its own independent slot, and this utility is for smoke-
# testing one key by hand, not for iterating the whole configured list.


def _run_ping(api_key: str, model: str) -> None:
    client = GeminiClient(api_key=api_key, model=model, timeout_seconds=120, label="gemini-ping")
    if client.health_check():
        print(f"OK — gemini model '{model}' reachable with this key")
    else:
        print(f"FAIL — gemini model '{model}' not reachable with this key")
        raise SystemExit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker Gemini client utility")
    parser.add_argument("--ping", action="store_true", help="health-check one Gemini API key")
    parser.add_argument("--api-key", help="Gemini API key to check (required with --ping)")
    parser.add_argument("--model", default="gemini-flash-latest", help="Gemini model to check against")
    args = parser.parse_args()

    if args.ping:
        if not args.api_key:
            parser.error("--ping requires --api-key")
        _run_ping(args.api_key, args.model)
    else:
        parser.print_help()
