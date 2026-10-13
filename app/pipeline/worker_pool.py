"""Multi-provider worker pool -- Segment 21 of DEERSTALKER_PLAN_v1_5.md.

Turns "one client slot per Ollama Cloud key + one slot per Gemini key" --
`len(settings.ollama_cloud_api_keys) + len(settings.google_api_keys)`
slots, whatever that number actually is today -- into a single object,
`MultiProviderClient`, that is a drop-in for `run_fast_pass`/
`run_detailed_pass`'s existing `client:` parameter (PLAN_v1_5.md §1/§4).
Neither pass engine's own file-loop, per-file error handling, or "one bad
file never kills the job" logic needs to change: this class matches the
same three-method "Provider" shape `OllamaClient`/`GeminiClient` already
expose (`generate_text`, `generate_vision`, `health_check`), so it just
looks like a (better) `OllamaClient` to them.

Slot construction (see `_build_ollama_cloud_slots`/`_build_gemini_slots`):
one Ollama-Cloud-flavored `OllamaClient` (Segment 19's `host_override`/
`api_key`/`label` params) per entry in `settings.ollama_cloud_api_keys`,
plus one `GeminiClient` (Segment 20) per entry in
`settings.google_api_keys`, optionally truncated to
`settings.worker_pool_max_slots`. Both source counts are read via `len()`
at construction time -- never a literal written down anywhere in this
file, per the plan's explicit "no fixed number of keys/slots is ever
assumed" requirement (§0, §1, §7 Segment 18/21).

Cooldown-and-retry design (PLAN_v1_5.md §1, §7 Segment 21): a
`ProviderRateLimitedError`/`ProviderUnavailableError` from a borrowed slot
means only *that slot* had a problem for *this* call -- never that the
whole backend is down. `app.ollama_client.OllamaUnavailableError` keeps
meaning "stop the whole pass" for both pass engines' shared
`threading.Event` short-circuit, and this module must never let a
per-slot rate limit or a transient network blip escalate into that
exception. So: on either of those two exception types, the failing slot
is put on a short cooldown (parsed from the exception's own
`Retry-After=...` message text when present -- see `_cooldown_seconds_for`
-- else a fixed default) and the same call is retried on a different,
not-yet-tried, not-currently-cooling-down slot, up to `slot_count`
distinct attempts, before finally raising a plain `RuntimeError`. Any
*other* exception (e.g. a non-429 `requests.HTTPError`, which both
clients deliberately let propagate unretried -- see their own docstrings)
is not caught here at all; it propagates straight out to the pass engine,
exactly as it would from a single `OllamaClient`, since the pass engines
already catch broadly per-file (PLAN.md Segment 8).

**Segment 24 fix:** the paragraph above describes the *intent* Segment 21
shipped with, but its `_call_with_retry` implementation only actually
caught `ProviderRateLimitedError`/`ProviderUnavailableError` --
`OllamaUnavailableError` was missing from that tuple. An Ollama-Cloud-
flavored `OllamaClient` slot (Segment 19) raises exactly
`OllamaUnavailableError` -- the same exception a plain local Ollama
client raises for "the whole backend is down" -- once its own internal
`_post_with_retry` backoff is exhausted (connection refused, DNS
failure, repeated timeout). Uncaught here, that exception used to
propagate straight past this class into `fast_pass.py`'s/
`detailed_pass.py`'s own `except OllamaUnavailableError` handler, which
aborts the *entire* pass -- so one revoked or unreachable Ollama Cloud
key looked exactly like "Ollama itself is down," even with other healthy
Cloud slots and every Gemini slot sitting unused. `OllamaUnavailableError`
is now caught in the same `except` clause as the other two per-slot
errors and handled identically (cooldown, retry on a different slot).
This is safe specifically *because* this class only ever wraps Ollama-
Cloud-flavored `OllamaClient` instances (Segment 19's `host_override=`/
`api_key=` constructor path) -- it never wraps a plain local client, so
from inside this class that exception can only ever mean "this one Cloud
slot," never "the whole backend." `app/ollama_client.py` itself was not
touched: a caller that isn't going through this pool still sees
`OllamaUnavailableError` mean exactly what Segment 19 intended, "stop the
whole pass." See PROJECT_STATE.md's Segment 24 section for the full
bug writeup and verification.

A `queue.Queue` of slot indices implements borrow/return and is what makes
"N configured slots" actually mean "N simultaneous in-flight calls" once
Segment 22 gives the pass engines' own `ThreadPoolExecutor` one worker per
slot: each worker thread borrows a distinct index, uses that slot's
client, and returns the index when done (success, per-slot failure, or
any other exception -- the `finally` block below always returns it, so an
unexpected exception can never permanently strand a slot out of
rotation). A separate lock guards each slot's cooldown-expiry timestamp,
since multiple threads may check/set cooldowns concurrently.

`model` argument handling (PLAN_v1_5.md §7 Segment 21): accepted on
`generate_text`/`generate_vision` for signature compatibility with the
Provider contract, but never forwarded to whichever slot is borrowed --
each slot is always called with `model=None`, so it falls back to
whatever *that client instance's own* default already is. **Flagging a
real ambiguity here, per this session's instructions to flag rather than
silently reconcile:** the plan's prose describes this as "the Cloud
slot's own fast/detailed Ollama model," but the actual `OllamaClient`
(Segment 19, unchanged here) has no such per-instance fast/detailed
notion -- its own docstring says picking fast vs. detailed model is
strictly the *caller's* job via the explicit `model=` argument on each
call, and its no-override fallback is just `settings.ollama_model` (the
plain base model). Since this pool deliberately never forwards `model`,
an Ollama Cloud slot built here will use `settings.ollama_model` for
*every* call regardless of whether it's serving the fast or detailed
pass -- there is no way for a single pool object, shared across both
passes for the whole job (per Segment 22), to also vary an Ollama Cloud
slot's default model by pass. See PROJECT_STATE.md's Segment 21 section
for the full writeup; this file implements the literal, explicit
"don't forward `model`" instruction rather than guessing at a
fast/detailed-aware scheme the plan doesn't actually specify a mechanism
for.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import Settings
from app.gemini_client import GeminiClient
from app.log import logger
from app.ollama_client import OllamaClient, OllamaUnavailableError
from app.provider_errors import ProviderRateLimitedError, ProviderUnavailableError

# Fallback cooldown when a failing slot's exception message doesn't carry a
# usable `Retry-After` value (e.g. any `ProviderUnavailableError`, or a
# `ProviderRateLimitedError` whose 429 response had no `Retry-After`
# header at all). Within the plan's suggested 30-60s range.
_DEFAULT_COOLDOWN_SECONDS = 45.0
_MIN_COOLDOWN_SECONDS = 5.0
_MAX_COOLDOWN_SECONDS = 300.0

# Both `OllamaClient` and `GeminiClient` embed a parsed `Retry-After` value
# into their `ProviderRateLimitedError` message text as `Retry-After=<val>`
# when the response header was present (see each module's `_post_with_retry`)
# -- neither exception class carries it as a structured attribute (a
# deliberate Segment 19/20 choice -- see PROJECT_STATE.md -- since
# `provider_errors.py` is outside both of those segments' file lists, and
# is outside this one too). Opportunistically parsed back out here rather
# than guessed at.
_RETRY_AFTER_RE = re.compile(r"Retry-After=(\S+)")

_HEALTH_CHECK_MAX_WORKERS = 8


@dataclass
class _Slot:
    """One worker-pool slot: one backend client instance (an Ollama-Cloud-
    flavored `OllamaClient` or a `GeminiClient`) plus its own cooldown
    state. `label` is purely cosmetic (logs, and the `_provider_used` tag
    Segment 23 reads) -- never derived from or containing the slot's API
    key."""

    client: Any
    label: str
    cooldown_until: float = field(default=0.0)


def _build_ollama_cloud_slots(settings: Settings) -> list[_Slot]:
    """One `OllamaClient` slot per entry in `settings.ollama_cloud_api_keys`
    -- count is whatever `len()` of that list is *today*, never a literal
    here. Each slot points at `settings.ollama_cloud_host` with that one
    key's Bearer auth (Segment 19)."""
    slots: list[_Slot] = []
    for index, api_key in enumerate(settings.ollama_cloud_api_keys):
        label = f"ollama-cloud-{index}"
        client = OllamaClient(
            settings,
            host_override=settings.ollama_cloud_host,
            api_key=api_key,
            label=label,
        )
        slots.append(_Slot(client=client, label=label))
    return slots


def _build_gemini_slots(settings: Settings) -> list[_Slot]:
    """One `GeminiClient` slot per entry in `settings.google_api_keys` --
    same "count follows the list" rule as the Ollama Cloud side. Each slot
    gets its own key plus the shared `settings.gemini_model`/
    `settings.gemini_timeout_seconds` (Segment 20's constructor takes
    plain values, not a `Settings` object -- see PROJECT_STATE.md's
    Segment 20 handoff notes for why)."""
    slots: list[_Slot] = []
    for index, api_key in enumerate(settings.google_api_keys):
        label = f"gemini-{index}"
        client = GeminiClient(
            api_key=api_key,
            model=settings.gemini_model,
            timeout_seconds=settings.gemini_timeout_seconds,
            label=label,
        )
        slots.append(_Slot(client=client, label=label))
    return slots


def _cooldown_seconds_for(exc: Exception) -> float:
    """Pick a cooldown duration for a slot that just raised
    `ProviderRateLimitedError`/`ProviderUnavailableError`: try to recover a
    `Retry-After` value the raising client embedded in its message text,
    clamped to a sane range; otherwise fall back to a fixed default.
    Never raises -- a malformed/missing value just means "use the
    default," not a crash."""
    match = _RETRY_AFTER_RE.search(str(exc))
    if match:
        try:
            seconds = float(match.group(1))
        except ValueError:
            return _DEFAULT_COOLDOWN_SECONDS
        return max(_MIN_COOLDOWN_SECONDS, min(seconds, _MAX_COOLDOWN_SECONDS))
    return _DEFAULT_COOLDOWN_SECONDS


def _tag_provider(result: dict | str, label: str) -> dict | str:
    """Tag a successful call's result with `_provider_used` (Segment 23
    reads this to set `AnalysisResult.model_used`) -- only when `result`
    is actually a `dict` (i.e. `json_mode=True`). A plain `str` result
    (`json_mode=False`) has nowhere to carry the tag and is returned
    unchanged; Segment 23's own patch only ever reads `.get(...)` off a
    dict-shaped response in the first place."""
    if isinstance(result, dict):
        result["_provider_used"] = label
    return result


class MultiProviderClient:
    """Drop-in `client:` replacement for `run_fast_pass`/`run_detailed_pass`
    that fans a shared queue of files out across every configured Ollama
    Cloud + Gemini slot. See module docstring for the full design; in
    short, `generate_text`/`generate_vision` borrow a slot, call it, and
    transparently retry on a different slot if the borrowed one reports a
    per-slot problem -- never surfacing that as
    `app.ollama_client.OllamaUnavailableError`.
    """

    def __init__(self, settings: Settings) -> None:
        slots = _build_ollama_cloud_slots(settings) + _build_gemini_slots(settings)

        if settings.worker_pool_max_slots is not None:
            # The one place a slot count is ever clamped by a config
            # value rather than purely `len(...)` -- and even here, the
            # *source* counts above are still `len()` reads; this only
            # trims the combined result. Guard against a negative cap
            # (e.g. a stray `WORKER_POOL_MAX_SLOTS=-1`) meaning something
            # other than "zero slots."
            cap = max(0, settings.worker_pool_max_slots)
            slots = slots[:cap]

        self._slots: list[_Slot] = slots
        self._cooldown_lock = threading.Lock()
        self._queue: queue.Queue[int] = queue.Queue()
        for index in range(len(self._slots)):
            self._queue.put(index)

        ollama_cloud_count = sum(1 for s in self._slots if s.label.startswith("ollama-cloud-"))
        gemini_count = sum(1 for s in self._slots if s.label.startswith("gemini-"))
        logger.info(
            "worker pool built with %d slot(s) (%d ollama-cloud + %d gemini)%s",
            len(self._slots),
            ollama_cloud_count,
            gemini_count,
            " -- capped by WORKER_POOL_MAX_SLOTS" if settings.worker_pool_max_slots is not None else "",
        )

    @property
    def slot_count(self) -> int:
        """Number of slots actually built (post-cap). Segment 22 sizes
        `ollama_max_concurrency` off this directly, so this must reflect
        reality, not the pre-cap total."""
        return len(self._slots)

    # ------------------------------------------------------------------
    # Public API -- matches the Provider contract (PLAN_v1_5.md §4)
    # exactly, so this class drops into `run_fast_pass`/`run_detailed_pass`
    # `client:` parameter with zero changes to either pass engine.
    # ------------------------------------------------------------------

    def generate_text(
        self,
        system: str,
        prompt: str,
        json_mode: bool = True,
        model: str | None = None,
    ) -> dict | str:
        """`model` is accepted for signature compatibility only -- see
        module docstring's "model argument handling" section for why it's
        never forwarded to the borrowed slot."""
        return self._call_with_retry(lambda client: client.generate_text(system, prompt, json_mode=json_mode))

    def generate_vision(
        self,
        system: str,
        prompt: str,
        images_b64: list[str],
        json_mode: bool = True,
        model: str | None = None,
    ) -> dict | str:
        """Same `model`-is-ignored posture as `generate_text` -- see module
        docstring."""
        return self._call_with_retry(
            lambda client: client.generate_vision(system, prompt, images_b64, json_mode=json_mode)
        )

    def health_check(self) -> bool:
        """`True` if at least one configured slot is reachable, `False` if
        every slot fails its own `health_check()` (which itself never
        raises) or there are zero slots. This is a pool-level "is there
        *any* usable backend right now" probe, not a per-slot report --
        the plan doesn't specify exact pool-level `health_check` semantics
        beyond matching the three-method Provider shape, so this is a
        straightforward design choice, not a spec requirement. Short-
        circuits on the first healthy slot rather than checking all of
        them."""
        return any(self._safe_health_check(slot) for slot in self._slots)

    @staticmethod
    def _safe_health_check(slot: _Slot) -> bool:
        try:
            return bool(slot.client.health_check())
        except Exception as exc:  # noqa: BLE001 - defensive; client contracts say this never raises
            logger.debug("worker-pool slot '%s' health_check raised unexpectedly: %s", slot.label, exc)
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _cooldown_remaining(self, index: int) -> float:
        with self._cooldown_lock:
            until = self._slots[index].cooldown_until
        remaining = until - time.monotonic()
        return remaining if remaining > 0 else 0.0

    def _set_cooldown(self, index: int, seconds: float) -> None:
        with self._cooldown_lock:
            self._slots[index].cooldown_until = time.monotonic() + seconds

    def _call_with_retry(self, invoke: Callable[[Any], dict | str]) -> dict | str:
        """Borrow a slot, call it via `invoke`, and on
        `ProviderRateLimitedError`/`ProviderUnavailableError`/
        `OllamaUnavailableError` (the last added in Segment 24 -- see
        module docstring's "Segment 24 fix" paragraph) cool that slot down
        and retry on a different, not-yet-tried slot -- up to `slot_count`
        distinct attempts -- before raising a plain `RuntimeError`. Any
        other exception propagates immediately, unretried (see module
        docstring). The borrowed index always goes back to the queue in a
        `finally`, so no exception -- expected or not -- can permanently
        strand a slot out of rotation.

        `tried` bounds real attempts (slots actually invoked and failed
        this call); `consecutive_skips` separately bounds slots dequeued
        but currently cooling down or already tried -- since every
        dequeue is matched by exactly one re-enqueue, the queue always
        holds exactly `slot_count` distinct indices, so `slot_count`
        consecutive skips guarantees every slot has been examined at
        least once without making progress (e.g. "every slot is currently
        cooling down") -- at which point this gives up rather than
        blocking until some cooldown clears.
        """
        slot_count = self.slot_count
        if slot_count == 0:
            raise RuntimeError("worker pool has zero configured slots")

        tried: set[int] = set()
        last_error: Exception | None = None
        consecutive_skips = 0

        while len(tried) < slot_count and consecutive_skips < slot_count:
            index = self._queue.get()
            try:
                if index in tried or self._cooldown_remaining(index) > 0:
                    consecutive_skips += 1
                    continue
                consecutive_skips = 0
                slot = self._slots[index]
                try:
                    result = invoke(slot.client)
                except (ProviderRateLimitedError, ProviderUnavailableError, OllamaUnavailableError) as exc:
                    last_error = exc
                    tried.add(index)
                    cooldown = _cooldown_seconds_for(exc)
                    self._set_cooldown(index, cooldown)
                    logger.warning(
                        "worker-pool slot '%s' failed (%s) -- cooling down %.0fs and trying a different slot",
                        slot.label,
                        exc,
                        cooldown,
                    )
                    continue
                else:
                    return _tag_provider(result, slot.label)
            finally:
                self._queue.put(index)

        raise RuntimeError(
            f"all {slot_count} worker-pool slot(s) failed or are cooling down for this call"
        ) from last_error


def build_client(settings: Settings) -> "MultiProviderClient | None":
    """`None` when zero slots would result -- whether because both
    `ollama_cloud_api_keys` and `google_api_keys` are empty, or because
    `worker_pool_max_slots` caps a nonzero total down to zero -- so the
    orchestrator's existing single-local-Ollama default (Segment 22) is
    completely untouched for anyone who hasn't configured any of this.
    Never returns an empty/unusable pool object; the zero-slot case is
    always `None`, checked once here rather than in every caller.
    """
    client = MultiProviderClient(settings)
    if client.slot_count == 0:
        return None
    return client
