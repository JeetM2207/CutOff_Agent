"""Optional, best-effort interview-prep intel gathering (new extension):
searches high-signal domains (LeetCode, GeeksforGeeks, Glassdoor) for a
company/role's interview experiences via DuckDuckGo's public search
interface — no API key, and this never touches LeetCode/GeeksforGeeks/
Glassdoor directly, only their already-public search-result snippets.

Worth being explicit about the risk profile here, unlike GitHub's REST API
(cutoff.adapters.developer_footprint): `ddgs` is not an officially
documented or sanctioned DuckDuckGo API — it works by querying DuckDuckGo's
own search interface the way a browser would, which can change shape or
start rate-limiting without notice. Every failure mode here (timeout, rate
limit, network error, the library itself misbehaving) degrades to an empty
list, never raises — this is pure enrichment for the approval card, never
a requirement for registration to proceed."""
from __future__ import annotations

import logging

from ddgs import DDGS

logger = logging.getLogger("cutoff.web_research")

SEARCH_TIMEOUT_SECONDS = 5.0
MAX_RESULTS = 5
TARGET_DOMAINS = ("leetcode.com", "geeksforgeeks.org", "glassdoor.co.in")


def fetch_interview_experiences(company_name: str, role_name: str) -> list[dict]:
    """Returns up to MAX_RESULTS {"title", "snippet", "url"} dicts. Never
    raises; returns [] on any failure (timeout, rate limit, network error,
    no results)."""
    if not company_name or not company_name.strip():
        return []

    domain_filter = " OR ".join(f"site:{d}" for d in TARGET_DOMAINS)
    query = f"{company_name} {role_name} interview questions {domain_filter}"

    try:
        raw_results = DDGS(timeout=SEARCH_TIMEOUT_SECONDS).text(query, max_results=MAX_RESULTS)
    except Exception:
        logger.exception("interview-prep search failed for %r / %r", company_name, role_name)
        return []

    return [
        {"title": r.get("title", ""), "snippet": r.get("body", ""), "url": r["href"]}
        for r in (raw_results or [])
        if r.get("href")
    ]
