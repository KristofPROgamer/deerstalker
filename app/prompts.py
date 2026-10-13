"""Prompt library — turns a `ContentPayload` plus an investigative target
(a registered `SearchIntent`, a free-text custom query, or the special
"interesting" mode) into the `(system, user)` string pair `OllamaClient.
generate_text()` / `generate_vision()` expect. PLAN.md §5/§7, Segment 7.

Design, so later segments don't have to reverse-engineer it:

- **`system` vs `user` split.** `system` carries the stable stuff for
  this call — the investigator persona plus what this particular target
  is looking for (from `SearchIntent.fast_prompt_template` /
  `.detailed_prompt_template`, a user's own custom query, or the
  "interesting" focus). `user` carries the per-file stuff — the file's
  actual content/metadata, and (last, right before generation, where
  small local models follow it most reliably) the JSON output contract.
  This lines up with `OllamaClient`'s `system=`/`prompt=` split (Segment
  2's docstring: "`system` is sent via Ollama's top-level `system`
  field, not concatenated into the prompt").

- **Images are never embedded in the prompt text.** `ContentPayload.
  images_b64` goes straight into `OllamaClient.generate_vision(images_b64=
  payload.images_b64)` unchanged — these builders only ever *mention*
  that N image(s) are attached, in the `user` text, so the model has
  context for them. Picking `generate_text` vs `generate_vision` based on
  `payload.kind` is the caller's (Segment 8/9's) job, not this module's.

- **What the model is actually asked to return.** The four fields below
  are the *only* JSON keys any of these prompts asks the model for:
  `relevance_score` (0.0-1.0 float), `tags` (list[str]), `summary` (str),
  `reasoning` (str), `flagged_interesting` (bool). That's five keys, not
  a typo — see `_OUTPUT_SCHEMA_LINE`. This is deliberately a *subset* of
  `AnalysisResult`'s fields: `id`/`file_id`/`job_id`/`pass_type`/
  `intent_id`/`model_used`/`created_at` are pipeline metadata the model
  has no way to know and isn't asked for — Segment 8/9 fills those in
  itself when constructing the actual `AnalysisResult`, using the parsed
  dict from `OllamaClient.generate_text/generate_vision` for the other
  five. **This mapping is the load-bearing contract between this module
  and Segment 8/9** — see PROJECT_STATE.md.

- **`build_fast_prompt` / `build_detailed_prompt` vs `build_interesting_prompt`
  / `build_custom_prompt`.** The first two look an id up in `app.intents.
  INTENTS` (this covers `"interesting"` too — it's a normal registry
  entry). The last two are for the cases PLAN.md's own intent table calls
  out as "(special)"/free-text and don't go through `intent_id` at all:
  `build_interesting_prompt(payload, detailed=False)` is a convenience
  wrapper equivalent to `build_fast_prompt("interesting", payload)` /
  `build_detailed_prompt("interesting", payload, {})` for a caller that
  wants to run it unconditionally without threading a fast_result through
  (its signature, per PLAN.md, has no `fast_result` param — see
  PROJECT_STATE.md for the resulting minor asymmetry with
  `build_detailed_prompt`). `build_custom_prompt(user_query, payload,
  detailed=False)` builds straight from the user's own typed text; there
  is no registry entry for it (see `app/intents.py`'s docstring).
"""

from __future__ import annotations

import json

from app.intents import INTENTS, get_intent
from app.models import ContentPayload

# --------------------------------------------------------------------------
# Shared role framing (stable across every file/intent within a pass type)
# --------------------------------------------------------------------------

_FAST_ROLE = (
    "You are a fast triage assistant scanning a user's own local files during a file "
    "investigation they requested. You'll be shown one file's content (or metadata only, if "
    "no content could be read) at a time. Decide whether it matches the target below — this "
    "is a first pass, so lean toward flagging borderline matches rather than missing them, "
    "but don't flag things that plainly don't match."
)

_DETAILED_ROLE = (
    "You are doing a careful second-look review of a file that already passed an initial fast "
    "triage for the target below. You may have more/higher-fidelity content this time. Give a "
    "confident, specific judgment — don't just restate the first pass."
)

# --------------------------------------------------------------------------
# Shared output contract — see module docstring for exactly which keys and why
# --------------------------------------------------------------------------

_OUTPUT_SCHEMA_LINE = (
    '{{"relevance_score": <0.0-1.0 float>, "tags": [<short lowercase keywords>], '
    '"summary": "<one sentence: what this file is>", "reasoning": "<{reasoning_hint}>", '
    '"flagged_interesting": <true|false>}}'
)

_OUTPUT_EXAMPLE = (
    '{"relevance_score": 0.82, "tags": ["passport", "scan"], '
    '"summary": "Scanned passport photo page.", '
    '"reasoning": "Visible passport number, photo, and MRZ lines.", '
    '"flagged_interesting": false}'
)


def _output_contract(detailed: bool) -> str:
    """The instruction block asked for by PLAN.md's Segment 7 spec:
    "every prompt instructs strict JSON output ... with the schema and
    one short example inlined." Kept intentionally short — see the
    module's "Watch out for" note in PLAN.md about token cost per file."""
    reasoning_hint = "2-4 sentences, specific" if detailed else "1-2 sentences"
    schema = _OUTPUT_SCHEMA_LINE.format(reasoning_hint=reasoning_hint)
    return (
        "Respond with ONLY one JSON object — no prose, no markdown fences — with exactly these "
        f"keys:\n{schema}\nExample:\n{_OUTPUT_EXAMPLE}"
    )


# --------------------------------------------------------------------------
# Rendering a ContentPayload into the "user" message's content section
# --------------------------------------------------------------------------


def _format_metadata(metadata: dict, max_chars: int = 400) -> str:
    """Compact one-line rendering of `ContentPayload.metadata`. Shapes
    vary a lot by format (see PROJECT_STATE.md's field-name list from
    Segments 5/6) so this doesn't assume any particular keys — it just
    dumps whatever's there and caps the length, since an EXIF dict or a
    long `extraction_notes` list shouldn't blow the prompt budget."""
    if not metadata:
        return ""
    try:
        rendered = json.dumps(metadata, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(metadata)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "...(truncated)"
    return rendered


def _render_payload_section(payload: ContentPayload) -> str:
    """The per-file "here's what you're looking at" block. Text is
    included inline; images are only ever *mentioned* here (the actual
    bytes travel separately via `generate_vision(images_b64=...)`) — see
    module docstring."""
    lines: list[str] = []

    if payload.kind == "text":
        lines.append("File content (text):")
        lines.append(payload.text_excerpt or "(no text extracted)")
    elif payload.kind == "image":
        n = len(payload.images_b64)
        lines.append(f"File content: {n} image{'s' if n != 1 else ''} attached for you to view.")
    elif payload.kind == "mixed":
        n = len(payload.images_b64)
        lines.append(f"File content: text excerpt below, plus {n} image{'s' if n != 1 else ''} attached.")
        lines.append(payload.text_excerpt or "(no text extracted)")
    else:  # "metadata_only"
        lines.append("File content: not readable as text or image — judge from metadata only.")

    if payload.truncated:
        lines.append("(Note: the content above is truncated — the file is larger than what's shown.)")

    meta_str = _format_metadata(payload.metadata)
    if meta_str:
        lines.append(f"Metadata: {meta_str}")

    return "\n".join(lines)


def _render_fast_result_section(fast_result: dict) -> str:
    """Renders the matching fast-pass judgment as context for a detailed
    pass. Tolerant of a sparse/partial dict — only prints the fields it
    actually finds, since `fast_result` is caller-supplied (typically the
    parsed dict `OllamaClient` returned for the fast pass, or an
    `AnalysisResult.to_dict()` — either shape has these key names)."""
    if not fast_result:
        return "Prior fast-pass result: (none provided)"

    parts: list[str] = []
    if (score := fast_result.get("relevance_score")) is not None:
        parts.append(f"relevance_score={score}")
    if summary := fast_result.get("summary"):
        parts.append(f"summary={summary!r}")
    if tags := fast_result.get("tags"):
        parts.append(f"tags={tags}")
    if (flagged := fast_result.get("flagged_interesting")) is not None:
        parts.append(f"flagged_interesting={flagged}")

    return "Prior fast-pass result: " + (", ".join(parts) if parts else "(no usable fields)")


# --------------------------------------------------------------------------
# Public prompt builders
# --------------------------------------------------------------------------


def build_fast_prompt(intent_id: str, payload: ContentPayload) -> tuple[str, str]:
    """Fast-pass prompt for a registered intent (including `"interesting"`
    — it's a normal registry entry, see `app/intents.py`). Raises
    `ValueError` for an unknown `intent_id` (via `get_intent`); free-text
    custom queries never reach this function — use `build_custom_prompt`."""
    intent = get_intent(intent_id)
    system = f"{_FAST_ROLE}\n\nTarget — {intent.label}: {intent.fast_prompt_template}"
    user = f"{_render_payload_section(payload)}\n\n{_output_contract(detailed=False)}"
    return system, user


def build_detailed_prompt(intent_id: str, payload: ContentPayload, fast_result: dict) -> tuple[str, str]:
    """Detailed-pass prompt for a registered intent. `fast_result` is the
    matching fast-pass judgment (a dict with at least some of
    `relevance_score`/`summary`/`tags`/`flagged_interesting`), given as
    context per PLAN.md's Segment 7 spec — pass `{}` if none is
    available. Raises `ValueError` for an unknown `intent_id`."""
    intent = get_intent(intent_id)
    system = f"{_DETAILED_ROLE}\n\nTarget — {intent.label}: {intent.detailed_prompt_template}"
    user = (
        f"{_render_fast_result_section(fast_result)}\n\n"
        f"{_render_payload_section(payload)}\n\n"
        f"{_output_contract(detailed=True)}"
    )
    return system, user


def build_custom_prompt(user_query: str, payload: ContentPayload, detailed: bool = False) -> tuple[str, str]:
    """Prompt for a user-typed free-text query (`ScanJob.custom_query`)
    — no `app.intents` registry entry backs this; the query text itself
    *is* the target. Same `detailed` flag shape as `build_interesting_prompt`,
    not a separate fast/detailed function pair, since there's no static
    per-mode template pair to select between here — see module docstring."""
    role = _DETAILED_ROLE if detailed else _FAST_ROLE
    focus = user_query.strip() or "(no query text given — use your own judgment on what's notable)"
    system = f"{role}\n\nTarget (user-defined) — {focus}"
    user = f"{_render_payload_section(payload)}\n\n{_output_contract(detailed)}"
    return system, user


def build_interesting_prompt(payload: ContentPayload, detailed: bool = False) -> tuple[str, str]:
    """Convenience wrapper for the special "interesting" mode — reads its
    templates from `INTENTS["interesting"]` (single source of truth, no
    text duplicated between here and `app/intents.py`) so this is
    equivalent to `build_fast_prompt("interesting", payload)` /
    `build_detailed_prompt("interesting", payload, {})`, just without
    needing to pass the literal id string or an (unused, for this mode)
    `fast_result`. See module docstring for why this mode gets its own
    function instead of only being reachable via `intent_id` lookup."""
    intent = INTENTS["interesting"]
    template = intent.detailed_prompt_template if detailed else intent.fast_prompt_template
    role = _DETAILED_ROLE if detailed else _FAST_ROLE
    system = f"{role}\n\nTarget — {intent.label}: {template}"
    user = f"{_render_payload_section(payload)}\n\n{_output_contract(detailed)}"
    return system, user


# --------------------------------------------------------------------------
# Self-test: `python -m app.prompts --selftest`
# --------------------------------------------------------------------------


def _sample_payloads() -> dict[str, ContentPayload]:
    return {
        "text": ContentPayload(
            file_id="sample-text",
            kind="text",
            text_excerpt=(
                "Bank of Example — Statement for March 2024\n"
                "Account ending 4471\nBalance: $2,481.09\nRouting: 021000021"
            ),
            images_b64=[],
            metadata={"encoding": "utf-8", "bytes_read": 128},
            truncated=False,
        ),
        "image": ContentPayload(
            file_id="sample-image",
            kind="image",
            text_excerpt=None,
            images_b64=["ZmFrZS1iYXNlNjQtcGF5bG9hZA=="],
            metadata={"width": 1024, "height": 768, "gps_present": True},
            truncated=False,
        ),
        "metadata_only": ContentPayload(
            file_id="sample-binary",
            kind="metadata_only",
            text_excerpt=None,
            images_b64=[],
            metadata={"declared_ext": ".exe", "sniffed_mime_type": None, "size_bytes": 45000},
            truncated=False,
        ),
    }


def _print_prompt(label: str, system: str, user: str) -> None:
    print(f"\n--- {label} ---")
    print(f"system ({len(system)} chars):\n{system}")
    print(f"user ({len(user)} chars):\n{user}")


def _run_selftest() -> None:
    payloads = _sample_payloads()

    # Every registered intent must build both fast and detailed prompts
    # without error, on some payload or other.
    for intent_id in INTENTS:
        payload = payloads["image"] if intent_id == "people_photos" else payloads["text"]
        s, u = build_fast_prompt(intent_id, payload)
        assert s.strip() and u.strip(), f"{intent_id} produced an empty fast prompt"
        s, u = build_detailed_prompt(intent_id, payload, {"relevance_score": 0.7, "summary": "example"})
        assert s.strip() and u.strip(), f"{intent_id} produced an empty detailed prompt"

    # Print a representative sample for manual sanity-checking of
    # length/clarity, per PLAN.md's Segment 7 acceptance test.
    for intent_id, kind in (("sensitive_docs", "text"), ("financial", "text"), ("people_photos", "image")):
        system, user = build_fast_prompt(intent_id, payloads[kind])
        _print_prompt(f"fast: {intent_id}", system, user)
        system, user = build_detailed_prompt(
            intent_id, payloads[kind], {"relevance_score": 0.74, "summary": "Looks like a match.", "tags": ["example"]}
        )
        _print_prompt(f"detailed: {intent_id}", system, user)

    system, user = build_interesting_prompt(payloads["metadata_only"])
    _print_prompt("interesting: fast", system, user)
    system, user = build_interesting_prompt(payloads["text"], detailed=True)
    _print_prompt("interesting: detailed", system, user)

    system, user = build_custom_prompt("anything related to my old startup, codename Falcon", payloads["text"])
    _print_prompt("custom: fast", system, user)
    system, user = build_custom_prompt("anything related to my old startup, codename Falcon", payloads["text"], detailed=True)
    _print_prompt("custom: detailed", system, user)

    # Unknown intent_id must raise a clear, actionable error, not a bare KeyError.
    try:
        build_fast_prompt("not_a_real_intent", payloads["text"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown intent_id")

    print("\nOK")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker prompt-library utility")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="build+print sample prompts for a few intents/payloads and validate every registered intent",
    )
    args = parser.parse_args()

    if args.selftest:
        _run_selftest()
    else:
        parser.print_help()
