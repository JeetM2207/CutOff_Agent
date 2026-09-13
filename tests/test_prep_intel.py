"""cutoff.pipeline.prep_intel: the never-raises orchestration wrapper
around web_research + intel_synthesize. Never calls a real search or LLM
here — both steps are stubbed."""
from cutoff.pipeline import prep_intel


def test_fetch_prep_intel_returns_not_found_when_search_finds_nothing(monkeypatch):
    monkeypatch.setattr("cutoff.adapters.web_research.fetch_interview_experiences", lambda c, r: [])

    result = prep_intel.fetch_prep_intel(
        "Zentrix Analytics", "Software Engineer", api_key="x", model="m", provider="anthropic", base_url=None,
    )

    assert result == {"attempted": True, "strategy_summary": None, "top_reference_links": []}


def test_fetch_prep_intel_returns_the_distilled_intel_on_success(monkeypatch):
    monkeypatch.setattr(
        "cutoff.adapters.web_research.fetch_interview_experiences",
        lambda c, r: [{"title": "t", "snippet": "s", "url": "https://leetcode.com/discuss/1"}],
    )
    monkeypatch.setattr(
        "cutoff.llm.intel_synthesize.synthesize_prep_strategy",
        lambda *a, **k: {"strategy_summary": "Expect graph questions.", "top_reference_links": ["https://leetcode.com/discuss/1"]},
    )

    result = prep_intel.fetch_prep_intel(
        "Zentrix Analytics", "Software Engineer", api_key="x", model="m", provider="anthropic", base_url=None,
    )

    assert result == {
        "attempted": True, "strategy_summary": "Expect graph questions.",
        "top_reference_links": ["https://leetcode.com/discuss/1"],
    }


def test_fetch_prep_intel_returns_not_found_when_synthesis_returns_none(monkeypatch):
    monkeypatch.setattr(
        "cutoff.adapters.web_research.fetch_interview_experiences",
        lambda c, r: [{"title": "t", "snippet": "s", "url": "https://leetcode.com/discuss/1"}],
    )
    monkeypatch.setattr("cutoff.llm.intel_synthesize.synthesize_prep_strategy", lambda *a, **k: None)

    result = prep_intel.fetch_prep_intel(
        "Zentrix Analytics", "Software Engineer", api_key="x", model="m", provider="anthropic", base_url=None,
    )

    assert result == {"attempted": True, "strategy_summary": None, "top_reference_links": []}


def test_fetch_prep_intel_never_raises_when_search_itself_blows_up(monkeypatch):
    def broken_search(c, r):
        raise RuntimeError("ddgs exploded")
    monkeypatch.setattr("cutoff.adapters.web_research.fetch_interview_experiences", broken_search)

    result = prep_intel.fetch_prep_intel(
        "Zentrix Analytics", "Software Engineer", api_key="x", model="m", provider="anthropic", base_url=None,
    )

    assert result == {"attempted": True, "strategy_summary": None, "top_reference_links": []}


def test_fetch_prep_intel_never_raises_when_synthesis_blows_up(monkeypatch):
    monkeypatch.setattr(
        "cutoff.adapters.web_research.fetch_interview_experiences",
        lambda c, r: [{"title": "t", "snippet": "s", "url": "https://leetcode.com/discuss/1"}],
    )

    def broken_synthesize(*a, **k):
        raise RuntimeError("rate limited")
    monkeypatch.setattr("cutoff.llm.intel_synthesize.synthesize_prep_strategy", broken_synthesize)

    result = prep_intel.fetch_prep_intel(
        "Zentrix Analytics", "Software Engineer", api_key="x", model="m", provider="anthropic", base_url=None,
    )

    assert result == {"attempted": True, "strategy_summary": None, "top_reference_links": []}
