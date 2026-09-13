"""cutoff.llm.intel_synthesize: distilling search snippets into a prep
strategy, via a stubbed provider client — never a real LLM call (Section 0
rule 3). Grounding enforced at the schema level (enum-constrained URLs),
same discipline as resume_generate.py/profile_extract.py."""
from cutoff.llm import intel_synthesize
from cutoff.llm.intel_synthesize import TOOL_NAME


class _FakeAnthropicToolUseBlock:
    def __init__(self, name, input_):
        self.type = "tool_use"
        self.name = name
        self.input = input_


class _FakeAnthropicResponse:
    def __init__(self, content):
        self.content = content


class _FakeAnthropicMessages:
    def __init__(self, result: dict):
        self._result = result
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeAnthropicResponse([_FakeAnthropicToolUseBlock(TOOL_NAME, self._result)])


class _FakeAnthropicClient:
    def __init__(self, result: dict):
        self.messages = _FakeAnthropicMessages(result)


_RESULTS = [
    {"title": "Zentrix Analytics Interview Experience", "snippet": "Graph traversal and SQL window functions came up.",
     "url": "https://leetcode.com/discuss/12345"},
    {"title": "Zentrix OA Review", "snippet": "Medium-difficulty DP problem in the OA.",
     "url": "https://www.geeksforgeeks.org/zentrix-oa"},
]


def _valid_result(**overrides):
    base = {
        "strategy_summary": "Historically, Zentrix focuses on graph traversals and SQL window functions.",
        "top_reference_links": ["https://leetcode.com/discuss/12345"],
    }
    base.update(overrides)
    return base


def test_synthesize_prep_strategy_returns_none_for_empty_search_results():
    assert intel_synthesize.synthesize_prep_strategy(
        "Zentrix Analytics", "Software Engineer", [],
        api_key="x", model="m", provider="anthropic", base_url=None,
    ) is None


def test_synthesize_prep_strategy_returns_the_distilled_intel(monkeypatch):
    fake_client = _FakeAnthropicClient(_valid_result())
    monkeypatch.setattr(intel_synthesize, "get_client", lambda *a, **k: fake_client)

    intel = intel_synthesize.synthesize_prep_strategy(
        "Zentrix Analytics", "Software Engineer", _RESULTS,
        api_key="x", model="m", provider="anthropic", base_url=None,
    )

    assert "graph traversals" in intel["strategy_summary"].lower()
    assert intel["top_reference_links"] == ["https://leetcode.com/discuss/12345"]
    # forced tool use, not freeform text
    call = fake_client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": TOOL_NAME}


def test_synthesize_prep_strategy_schema_constrains_links_to_real_search_urls(monkeypatch):
    fake_client = _FakeAnthropicClient(_valid_result())
    monkeypatch.setattr(intel_synthesize, "get_client", lambda *a, **k: fake_client)

    intel_synthesize.synthesize_prep_strategy(
        "Zentrix Analytics", "Software Engineer", _RESULTS,
        api_key="x", model="m", provider="anthropic", base_url=None,
    )

    call = fake_client.messages.calls[0]
    schema = call["tools"][0]["input_schema"]
    assert set(schema["properties"]["top_reference_links"]["items"]["enum"]) == {
        "https://leetcode.com/discuss/12345", "https://www.geeksforgeeks.org/zentrix-oa",
    }


def test_synthesize_prep_strategy_raises_when_link_not_in_search_results(monkeypatch):
    fake_client = _FakeAnthropicClient(_valid_result(top_reference_links=["https://example.com/invented"]))
    monkeypatch.setattr(intel_synthesize, "get_client", lambda *a, **k: fake_client)

    try:
        intel_synthesize.synthesize_prep_strategy(
            "Zentrix Analytics", "Software Engineer", _RESULTS,
            api_key="x", model="m", provider="anthropic", base_url=None,
        )
        assert False, "expected ValueError for a link not in the search results"
    except ValueError as e:
        assert "invented" in str(e)


def test_synthesize_prep_strategy_raises_when_no_links_returned(monkeypatch):
    fake_client = _FakeAnthropicClient(_valid_result(top_reference_links=[]))
    monkeypatch.setattr(intel_synthesize, "get_client", lambda *a, **k: fake_client)

    try:
        intel_synthesize.synthesize_prep_strategy(
            "Zentrix Analytics", "Software Engineer", _RESULTS,
            api_key="x", model="m", provider="anthropic", base_url=None,
        )
        assert False, "expected ValueError for no reference links"
    except ValueError:
        pass
