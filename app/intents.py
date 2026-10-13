"""The predefined search-intent registry — PLAN.md §5, Segment 7.

`INTENTS` is the single source of truth for every preset a user can pick
in the frontend's intent-selection screen: id, display label/icon/
description for the UI, and the two prompt-focus strings `prompts.py`
weaves into the actual model prompts.

Two ids from PLAN.md §5's table are deliberately *not* here:
  - `interesting` IS here (it's a normal, selectable registry entry with
    real templates), but note it's flagged "(special)" in PLAN.md's own
    table — `prompts.build_interesting_prompt()` exists as a convenience
    wrapper around this entry for callers that want to run it without
    going through the generic `intent_id` lookup (e.g. an orchestrator
    that always runs "interesting" on every file regardless of what the
    user selected). See that function's docstring in `prompts.py`.
  - Free-text "Custom" has no id at all in PLAN.md's table (its row says
    "(free text)") — it's driven by `ScanJob.custom_query`, a plain user-
    typed string, not a registry lookup. There is intentionally no
    `INTENTS["custom"]` entry; `prompts.build_custom_prompt()` takes the
    user's query text directly instead.

Every `fast_prompt_template` / `detailed_prompt_template` here is the
*focus* text only (what to look for / what to confirm) — the shared
JSON-output contract, role framing, and file-content rendering that wrap
around it live in `prompts.py`, applied uniformly so this file doesn't
duplicate that boilerplate ten times over.
"""

from __future__ import annotations

from app.models import SearchIntent

INTENTS: dict[str, SearchIntent] = {
    "sensitive_docs": SearchIntent(
        id="sensitive_docs",
        label="Sensitive & Personal Documents",
        description="IDs, passports, SSNs, medical records, and other documents that reveal who you are.",
        icon="🪪",
        fast_prompt_template=(
            "Look for sensitive personal documents: government-issued IDs, passports, driver's "
            "licenses, Social Security numbers or other tax IDs, medical or health records, and "
            "insurance paperwork. Flag anything that exposes someone's identity, health, or "
            "personal circumstances, whether it's the whole document or just a fragment of one."
        ),
        detailed_prompt_template=(
            "This file was already flagged as a possible sensitive personal document. Confirm the "
            "specific type (ID/passport/medical/insurance/etc.), state which specific pieces of PII "
            "are actually visible (name, ID number, date of birth, address, diagnosis, policy "
            "number, and so on) without inventing anything not present, and judge how exposed that "
            "information is."
        ),
    ),
    "financial": SearchIntent(
        id="financial",
        label="Financial Records",
        description="Bank and credit statements, invoices, receipts, budgets, and crypto wallet info.",
        icon="💰",
        fast_prompt_template=(
            "Look for financial records: bank or credit card statements, invoices, receipts, tax "
            "filings, budget spreadsheets, or cryptocurrency wallet addresses, seed phrases, and "
            "exchange records."
        ),
        detailed_prompt_template=(
            "This file was already flagged as a possible financial record. Identify the specific "
            "type (statement/invoice/receipt/budget/crypto/etc.), the approximate time period or "
            "amounts involved if visible, and whether any account numbers or other sensitive "
            "financial identifiers are exposed."
        ),
    ),
    "credentials": SearchIntent(
        id="credentials",
        label="Credentials & Security Risks",
        description="Passwords, API keys, tokens, and other secrets sitting in plaintext.",
        icon="🔑",
        fast_prompt_template=(
            "Look for exposed credentials and security risks: plaintext passwords, API keys or "
            "access tokens, secrets embedded in config or code files, private keys, exported "
            "browser password lists, or connection strings with embedded credentials."
        ),
        detailed_prompt_template=(
            "This file was already flagged as a possible credential or secret exposure. Identify "
            "exactly what kind of secret it is and where it appears, and judge how severe the "
            "exposure looks — a live-looking cloud API key or a real password is more urgent than "
            "an obvious placeholder or example value."
        ),
    ),
    "people_photos": SearchIntent(
        id="people_photos",
        label="Photos of People",
        description="Photos and images containing faces or people.",
        icon="👤",
        fast_prompt_template=(
            "Look at the image for people: portraits, group photos, selfies, candid shots, or "
            "ID-style photos — anything where one or more human faces are visible."
        ),
        detailed_prompt_template=(
            "This image was already flagged as likely containing people. Describe how many people "
            "appear and the setting (portrait, group, candid, ID photo, etc.), without attempting "
            "to identify who any individual actually is."
        ),
    ),
    "legal": SearchIntent(
        id="legal",
        label="Legal & Contracts",
        description="Contracts, NDAs, agreements, court documents, and legal correspondence.",
        icon="⚖️",
        fast_prompt_template=(
            "Look for legal documents: contracts, NDAs, agreements, court filings, legal "
            "correspondence to or from lawyers or courts, leases, or other material with binding "
            "legal language."
        ),
        detailed_prompt_template=(
            "This file was already flagged as a possible legal document. Identify the document "
            "type (contract/NDA/lease/court filing/correspondence/etc.), the parties involved if "
            "they're named, and whether it looks signed/executed or still a draft."
        ),
    ),
    "work_docs": SearchIntent(
        id="work_docs",
        label="Work & Professional",
        description="Resumes, business plans, reports, and other professional material.",
        icon="💼",
        fast_prompt_template=(
            "Look for work and professional documents: resumes or CVs, cover letters, business "
            "plans, professional reports, presentations, or other career- or business-related "
            "material."
        ),
        detailed_prompt_template=(
            "This file was already flagged as a possible work document. Identify the specific type "
            "(resume/report/plan/presentation/etc.) and summarize, briefly, what it's actually "
            "about."
        ),
    ),
    "memories": SearchIntent(
        id="memories",
        label="Media & Memories",
        description="Photos, videos, screenshots, and personal creative work tied to specific moments.",
        icon="📸",
        fast_prompt_template=(
            "Look for personal media: photos or videos of specific events (trips, celebrations, "
            "gatherings), screenshots, or personal creative work such as art, writing, or music."
        ),
        detailed_prompt_template=(
            "This file was already flagged as a possible personal memory or creative work. Briefly "
            "describe the event, subject, or style, and note anything visible — a date, location, "
            "or occasion — that helps place it in time."
        ),
    ),
    "cleanup": SearchIntent(
        id="cleanup",
        label="Duplicates & Reclaimable Space",
        description="Duplicates, cache/temp junk, and large files worth reclaiming space from.",
        icon="🧹",
        fast_prompt_template=(
            "Look for reclaimable clutter: obvious cache or temp files, installer leftovers, log "
            "files, thumbnail caches, or unusually large files that look like forgotten downloads "
            "or backups rather than active work."
        ),
        detailed_prompt_template=(
            "This file was already flagged as possible clutter. Judge whether it genuinely looks "
            "safe to delete or archive (temp/cache/log/installer/old backup) versus something that "
            "might still matter, and state the basis for that judgment — name pattern, size, file "
            "type, or metadata."
        ),
    ),
    "forgotten": SearchIntent(
        id="forgotten",
        label="Old & Forgotten",
        description="Files untouched for years or orphaned from software you no longer have.",
        icon="🕸️",
        fast_prompt_template=(
            "Look for old, forgotten files: content that appears untouched for a very long time "
            "based on whatever timestamp metadata is available, or files that look orphaned from "
            "software that's probably no longer installed."
        ),
        detailed_prompt_template=(
            "This file was already flagged as possibly old or forgotten. Use whatever timestamp or "
            "version metadata is available to judge how stale it really is, and note if it looks "
            "tied to software or data that's probably gone now."
        ),
    ),
    "interesting": SearchIntent(
        id="interesting",
        label="Interesting Things",
        description="Anything that strikes the model as anomalous, out of place, or worth a second look — no fixed target.",
        icon="🔎",
        fast_prompt_template=(
            "Use your own judgment rather than a fixed checklist. Flag this file only if something "
            "about it is genuinely unusual, out of place, contradictory, or surprising — content "
            "that doesn't match its filename, location, or apparent type; something that looks "
            "deliberately hidden or disguised; or anything else worth a second look. Most ordinary "
            "files should score low and stay unflagged."
        ),
        detailed_prompt_template=(
            "This file was already flagged as potentially interesting or anomalous. Explain "
            "precisely what's unusual about it and why it stands out — be specific about the "
            "mismatch or anomaly rather than giving a generic 'seems odd' judgment."
        ),
    ),
}


def get_intent(intent_id: str) -> SearchIntent:
    """Look up one intent by id.

    Raises `ValueError` (not a raw `KeyError`) for an unknown id, since
    callers — chiefly `prompts.py` — want an immediately actionable error
    message (including the list of valid ids) rather than a bare
    `KeyError: 'whatever'`.
    """
    try:
        return INTENTS[intent_id]
    except KeyError:
        raise ValueError(f"unknown intent_id {intent_id!r} — known ids: {sorted(INTENTS)}") from None


# --------------------------------------------------------------------------
# Self-test: `python -m app.intents --list` / `--selftest`
# --------------------------------------------------------------------------


def _run_list() -> None:
    import json

    print(json.dumps([intent.to_dict() for intent in INTENTS.values()], indent=2, ensure_ascii=False))


def _run_selftest() -> None:
    assert "custom" not in INTENTS, "free-text 'Custom' must not be a registry entry (see module docstring)"
    for intent_id, intent in INTENTS.items():
        assert intent.id == intent_id, f"key/id mismatch for {intent_id!r}"
        for field in ("label", "description", "icon", "fast_prompt_template", "detailed_prompt_template"):
            value = getattr(intent, field)
            assert isinstance(value, str) and value.strip(), f"{intent_id}.{field} is empty/stub"
        # round-trip through the shared serialization helpers
        assert SearchIntent.from_dict(intent.to_dict()) == intent, f"{intent_id} failed to_dict/from_dict round-trip"
        assert SearchIntent.from_json(intent.to_json()) == intent, f"{intent_id} failed to_json/from_json round-trip"

    print(f"OK — {len(INTENTS)} intents registered: {sorted(INTENTS)}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deerstalker search-intent registry utility")
    parser.add_argument("--list", action="store_true", help="print the full intent registry as JSON")
    parser.add_argument("--selftest", action="store_true", help="validate every registered intent")
    args = parser.parse_args()

    if args.list:
        _run_list()
    elif args.selftest:
        _run_selftest()
    else:
        parser.print_help()
