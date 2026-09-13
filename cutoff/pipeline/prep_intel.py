"""Orchestrates optional interview-prep intel gathering (new extension):
searches public sources (cutoff.adapters.web_research), then distills them
into a short strategy summary (cutoff.llm.intel_synthesize). Wraps both
steps so this NEVER raises and NEVER blocks planning — registering the
student is the whole point of this pipeline; prep intel is a bonus on top
of the approval card, never a requirement for it to send."""
from __future__ import annotations

import logging

logger = logging.getLogger("cutoff.prep_intel")

# A single, stable shape whether or not anything was actually found — Drive
# persists this and checks `attempted` to avoid re-searching the same
# drive on every subsequent revision/reminder replan, even when the first
# attempt found nothing.
NOT_FOUND = {"attempted": True, "strategy_summary": None, "top_reference_links": []}


def fetch_prep_intel(
    company: str, role: str, *, api_key: str, model: str, provider: str, base_url: str | None,
) -> dict:
    """Always returns a dict (never None, never raises) — see NOT_FOUND
    above for what "nothing found" looks like."""
    from cutoff.adapters.web_research import fetch_interview_experiences
    from cutoff.llm.intel_synthesize import synthesize_prep_strategy

    try:
        results = fetch_interview_experiences(company, role)
        if not results:
            return dict(NOT_FOUND)
        intel = synthesize_prep_strategy(
            company, role, results, api_key=api_key, model=model, provider=provider, base_url=base_url,
        )
        if intel is None:
            return dict(NOT_FOUND)
        return {"attempted": True, **intel}
    except Exception:
        logger.exception("prep-intel gathering failed for %r / %r", company, role)
        return dict(NOT_FOUND)
