"""Optional, best-effort public-profile fetchers (Section 6.4 onboarding
extension): GitHub's public REST API, and LeetCode's de-facto-public GraphQL
endpoint (the same one many open-source "stats card" projects already rely
on). Both are read-only, unauthenticated, and only ever asked about the
student's OWN public profile — never a scraping/credential-bypass concern.

This is enrichment, never a requirement: every failure mode (network,
timeout, 404, rate limit, malformed JSON) degrades to an empty/None result,
never raises. cutoff.llm.profile_synthesize treats missing data here exactly
like a student who never provided a GitHub/LeetCode username at all."""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("cutoff.developer_footprint")

TIMEOUT = 5.0
GITHUB_API = "https://api.github.com"
LEETCODE_GRAPHQL = "https://leetcode.com/graphql"

_LEETCODE_QUERY = """
query getUserProfile($username: String!) {
  matchedUser(username: $username) {
    submitStats: submitStatsGlobal { acSubmissionNum { difficulty count } }
    profile { ranking }
  }
}
"""


def _fetch_readme_excerpt(username: str, repo: str, default_branch: str) -> str:
    for branch in dict.fromkeys([default_branch or "main", "main", "master"]):  # dedupe, keep order
        try:
            resp = httpx.get(f"https://raw.githubusercontent.com/{username}/{repo}/{branch}/README.md",
                              timeout=TIMEOUT)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text[:2000]
        except Exception:
            continue
    return ""


def fetch_github_profile(username: str, *, fetch_readmes: bool = True) -> dict:
    """Returns {"repos": [{"name", "description", "url", "language", "topics",
    "stars", "readme_excerpt"}, ...]}. Never raises; {"repos": []} on any
    failure. Skips forked repos with 10 or fewer stars (a fork the student
    hasn't meaningfully built on isn't their own work); keeps popular forks."""
    if not username or not username.strip():
        return {"repos": []}
    try:
        resp = httpx.get(
            f"{GITHUB_API}/users/{username}/repos",
            params={"sort": "updated", "per_page": 10},
            headers={"Accept": "application/vnd.github+json"},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("GitHub repos fetch for %r failed: HTTP %s", username, resp.status_code)
            return {"repos": []}
        repos_raw = resp.json()
    except Exception:
        logger.exception("GitHub repos fetch for %r failed", username)
        return {"repos": []}

    repos = []
    for r in repos_raw:
        if r.get("fork") and (r.get("stargazers_count") or 0) <= 10:
            continue
        name = r.get("name", "")
        repos.append({
            "name": name,
            "description": r.get("description") or "",
            "url": r.get("html_url", ""),
            "language": r.get("language"),
            "topics": r.get("topics") or [],
            "stars": r.get("stargazers_count", 0),
            "readme_excerpt": (
                _fetch_readme_excerpt(username, name, r.get("default_branch", "main"))
                if fetch_readmes else ""
            ),
        })
    return {"repos": repos}


def fetch_leetcode_stats(username: str) -> dict | None:
    """Returns {"total_solved": int, "ranking": int | None, "by_difficulty":
    {"Easy": n, "Medium": n, "Hard": n}} or None on any failure (unknown
    username, endpoint unreachable, unexpected response shape)."""
    if not username or not username.strip():
        return None
    try:
        resp = httpx.post(
            LEETCODE_GRAPHQL,
            json={"query": _LEETCODE_QUERY, "variables": {"username": username}},
            timeout=TIMEOUT,
            headers={"Content-Type": "application/json", "Referer": f"https://leetcode.com/{username}/"},
        )
        if resp.status_code != 200:
            logger.warning("LeetCode stats fetch for %r failed: HTTP %s", username, resp.status_code)
            return None
        matched = resp.json().get("data", {}).get("matchedUser")
        if not matched:
            return None
        counts = {e["difficulty"]: e["count"] for e in matched["submitStats"]["acSubmissionNum"]}
        total = counts.get("All", sum(v for k, v in counts.items() if k != "All"))
        return {
            "total_solved": total,
            "ranking": (matched.get("profile") or {}).get("ranking"),
            "by_difficulty": {k: v for k, v in counts.items() if k != "All"},
        }
    except Exception:
        logger.exception("LeetCode stats fetch for %r failed", username)
        return None
