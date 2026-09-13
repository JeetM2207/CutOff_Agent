"""cutoff.adapters.developer_footprint: GitHub/LeetCode fetchers. Every
httpx call is mocked here — never a real network request (Section 0 rule 3
extends to any external API this project talks to, not just the LLM)."""
from cutoff.adapters import developer_footprint


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data


def test_fetch_github_profile_returns_empty_for_blank_username():
    assert developer_footprint.fetch_github_profile("") == {"repos": []}


def test_fetch_github_profile_parses_repos(monkeypatch):
    repos_json = [
        {"name": "marketplace", "description": "A Django marketplace.", "html_url": "https://github.com/riya/marketplace",
         "language": "Python", "topics": ["django"], "stargazers_count": 12, "fork": False, "default_branch": "main"},
    ]
    monkeypatch.setattr(developer_footprint.httpx, "get", lambda url, **k: _FakeResponse(200, repos_json))

    result = developer_footprint.fetch_github_profile("riya", fetch_readmes=False)

    assert len(result["repos"]) == 1
    repo = result["repos"][0]
    assert repo["name"] == "marketplace"
    assert repo["description"] == "A Django marketplace."
    assert repo["language"] == "Python"
    assert repo["stars"] == 12


def test_fetch_github_profile_skips_low_star_forks(monkeypatch):
    repos_json = [
        {"name": "forked-repo", "description": "", "html_url": "https://github.com/riya/forked-repo",
         "language": "Python", "topics": [], "stargazers_count": 3, "fork": True, "default_branch": "main"},
        {"name": "popular-fork", "description": "", "html_url": "https://github.com/riya/popular-fork",
         "language": "Python", "topics": [], "stargazers_count": 50, "fork": True, "default_branch": "main"},
    ]
    monkeypatch.setattr(developer_footprint.httpx, "get", lambda url, **k: _FakeResponse(200, repos_json))

    result = developer_footprint.fetch_github_profile("riya", fetch_readmes=False)

    names = {r["name"] for r in result["repos"]}
    assert names == {"popular-fork"}  # the low-star fork is filtered out, the popular one isn't


def test_fetch_github_profile_degrades_gracefully_on_http_error(monkeypatch):
    monkeypatch.setattr(developer_footprint.httpx, "get", lambda url, **k: _FakeResponse(404))
    assert developer_footprint.fetch_github_profile("nonexistent-user-xyz") == {"repos": []}


def test_fetch_github_profile_degrades_gracefully_on_network_exception(monkeypatch):
    def raise_error(url, **k):
        raise ConnectionError("no network")
    monkeypatch.setattr(developer_footprint.httpx, "get", raise_error)
    assert developer_footprint.fetch_github_profile("riya") == {"repos": []}


def test_fetch_github_profile_fetches_readme_excerpt(monkeypatch):
    repos_json = [{"name": "cli-tool", "description": "", "html_url": "https://github.com/riya/cli-tool",
                    "language": "Python", "topics": [], "stargazers_count": 1, "fork": False, "default_branch": "main"}]

    def fake_get(url, **k):
        if "api.github.com" in url:
            return _FakeResponse(200, repos_json)
        if "README.md" in url:
            return _FakeResponse(200, text="# CLI Tool\n\nA command-line tool for doing X.")
        return _FakeResponse(404)

    monkeypatch.setattr(developer_footprint.httpx, "get", fake_get)

    result = developer_footprint.fetch_github_profile("riya", fetch_readmes=True)
    assert "A command-line tool for doing X." in result["repos"][0]["readme_excerpt"]


def test_fetch_leetcode_stats_returns_none_for_blank_username():
    assert developer_footprint.fetch_leetcode_stats("") is None


def test_fetch_leetcode_stats_parses_counts(monkeypatch):
    payload = {
        "data": {"matchedUser": {
            "submitStats": {"acSubmissionNum": [
                {"difficulty": "All", "count": 450}, {"difficulty": "Easy", "count": 200},
                {"difficulty": "Medium", "count": 200}, {"difficulty": "Hard", "count": 50},
            ]},
            "profile": {"ranking": 25000},
        }},
    }
    monkeypatch.setattr(developer_footprint.httpx, "post", lambda url, **k: _FakeResponse(200, payload))

    result = developer_footprint.fetch_leetcode_stats("riya")

    assert result["total_solved"] == 450
    assert result["ranking"] == 25000
    assert result["by_difficulty"] == {"Easy": 200, "Medium": 200, "Hard": 50}


def test_fetch_leetcode_stats_returns_none_for_unknown_user(monkeypatch):
    monkeypatch.setattr(developer_footprint.httpx, "post",
                        lambda url, **k: _FakeResponse(200, {"data": {"matchedUser": None}}))
    assert developer_footprint.fetch_leetcode_stats("nonexistent-user-xyz") is None


def test_fetch_leetcode_stats_degrades_gracefully_on_http_error(monkeypatch):
    monkeypatch.setattr(developer_footprint.httpx, "post", lambda url, **k: _FakeResponse(429))
    assert developer_footprint.fetch_leetcode_stats("riya") is None


def test_fetch_leetcode_stats_degrades_gracefully_on_malformed_response(monkeypatch):
    monkeypatch.setattr(developer_footprint.httpx, "post", lambda url, **k: _FakeResponse(200, {"unexpected": "shape"}))
    assert developer_footprint.fetch_leetcode_stats("riya") is None


def test_fetch_leetcode_stats_degrades_gracefully_on_network_exception(monkeypatch):
    def raise_error(url, **k):
        raise ConnectionError("no network")
    monkeypatch.setattr(developer_footprint.httpx, "post", raise_error)
    assert developer_footprint.fetch_leetcode_stats("riya") is None
