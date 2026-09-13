"""cutoff.adapters.web_research: the DuckDuckGo-backed interview-prep
search adapter. Never calls the real network (Section 0 rule 3 extends to
every external service this project talks to) — DDGS itself is mocked."""
from cutoff.adapters import web_research


class _FakeDDGS:
    def __init__(self, results=None, raise_exc=None):
        self._results = results or []
        self._raise_exc = raise_exc
        self.calls = []

    def __call__(self, timeout=None):
        self._timeout = timeout
        return self

    def text(self, query, max_results=None):
        self.calls.append({"query": query, "max_results": max_results})
        if self._raise_exc:
            raise self._raise_exc
        return self._results


def test_fetch_interview_experiences_returns_empty_for_blank_company(monkeypatch):
    fake = _FakeDDGS()
    monkeypatch.setattr(web_research, "DDGS", fake)
    assert web_research.fetch_interview_experiences("", "Software Engineer") == []
    assert fake.calls == []  # never even attempted a search


def test_fetch_interview_experiences_parses_results(monkeypatch):
    fake = _FakeDDGS(results=[
        {"title": "Zentrix Analytics Interview Experience", "body": "Asked about graph traversal and SQL.",
         "href": "https://leetcode.com/discuss/12345"},
        {"title": "Zentrix OA Review", "body": "DP problem, medium difficulty.",
         "href": "https://www.geeksforgeeks.org/zentrix-oa"},
    ])
    monkeypatch.setattr(web_research, "DDGS", fake)

    results = web_research.fetch_interview_experiences("Zentrix Analytics", "Software Engineer")

    assert len(results) == 2
    assert results[0] == {
        "title": "Zentrix Analytics Interview Experience",
        "snippet": "Asked about graph traversal and SQL.",
        "url": "https://leetcode.com/discuss/12345",
    }


def test_fetch_interview_experiences_builds_a_domain_scoped_query(monkeypatch):
    fake = _FakeDDGS(results=[])
    monkeypatch.setattr(web_research, "DDGS", fake)

    web_research.fetch_interview_experiences("Zentrix Analytics", "Software Engineer")

    query = fake.calls[0]["query"]
    assert "Zentrix Analytics" in query
    assert "Software Engineer" in query
    for domain in web_research.TARGET_DOMAINS:
        assert domain in query


def test_fetch_interview_experiences_uses_the_configured_timeout(monkeypatch):
    fake = _FakeDDGS(results=[])
    monkeypatch.setattr(web_research, "DDGS", fake)

    web_research.fetch_interview_experiences("Zentrix Analytics", "Software Engineer")

    assert fake._timeout == web_research.SEARCH_TIMEOUT_SECONDS


def test_fetch_interview_experiences_skips_results_with_no_url(monkeypatch):
    fake = _FakeDDGS(results=[{"title": "No URL here", "body": "..."}])
    monkeypatch.setattr(web_research, "DDGS", fake)
    assert web_research.fetch_interview_experiences("Zentrix Analytics", "Software Engineer") == []


def test_fetch_interview_experiences_degrades_gracefully_on_timeout(monkeypatch):
    fake = _FakeDDGS(raise_exc=TimeoutError("search timed out"))
    monkeypatch.setattr(web_research, "DDGS", fake)
    assert web_research.fetch_interview_experiences("Zentrix Analytics", "Software Engineer") == []


def test_fetch_interview_experiences_degrades_gracefully_on_rate_limit(monkeypatch):
    fake = _FakeDDGS(raise_exc=RuntimeError("rate limited"))
    monkeypatch.setattr(web_research, "DDGS", fake)
    assert web_research.fetch_interview_experiences("Zentrix Analytics", "Software Engineer") == []


def test_fetch_interview_experiences_degrades_gracefully_on_any_other_exception(monkeypatch):
    fake = _FakeDDGS(raise_exc=ConnectionError("no network"))
    monkeypatch.setattr(web_research, "DDGS", fake)
    assert web_research.fetch_interview_experiences("Zentrix Analytics", "Software Engineer") == []
