"""Shared exception types for multi-provider (Ollama Cloud + Gemini) worker
slots. See DEERSTALKER_PLAN_v1_5.md section 4.

These exist so a single provider client (`OllamaClient` in Ollama-Cloud
mode, `GeminiClient`) can report "this one slot/key had a problem" in a way
the worker pool (Segment 21) can tell apart from "the whole backend is
down" -- which is what `app.ollama_client.OllamaUnavailableError` means
today and must go on meaning: both pass engines treat it as a signal to
stop the entire fast/detailed pass (see plan section 1). A rate limit on
one of ten Ollama Cloud keys, or a timeout hitting one of seven Gemini
keys, is not that. It's the pool's job to catch these two types, retry the
same call on a different slot, and only fall back to a plain `RuntimeError`
if every slot has been tried and failed -- never re-raising or converting
either of these into `OllamaUnavailableError`.
"""

from __future__ import annotations


class ProviderRateLimitedError(Exception):
    """One specific credential/slot returned HTTP 429, or the provider's
    response otherwise indicated a quota/rate-limit rejection, for this
    call.

    This means only that *slot* is temporarily unusable -- not that the
    provider or the pass as a whole is down. The worker pool should put
    that slot on a short cooldown and retry the same call on a different,
    not-yet-tried slot rather than failing the call outright.
    """


class ProviderUnavailableError(Exception):
    """One specific credential/slot could not be used for this call, after
    that client's own internal retry logic (e.g.
    `OllamaClient._post_with_retry`) was already exhausted -- either
    because it couldn't be reached at all (connection error, timeout, DNS
    failure), or because it kept returning a transient server-side error
    (HTTP 500/502/503/504) instead of actually processing the request.

    Handled identically to `ProviderRateLimitedError` by the worker pool --
    kept as a separate class purely so logs (and any future metrics) can
    distinguish "rate limited" from "network/connectivity/server problem"
    on a per-slot basis.
    """
