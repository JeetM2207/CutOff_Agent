"""llm/form_answer.py's draft_answers (Section 6.5 extension). Regression
test for a live bug: using each question's raw text (spaces, "?", "(", ")")
as a JSON Schema *property name* made Gemini return no tool call at all.
Fixed with synthetic "qN" keys, real question text moved into each
property's description instead — this exercises that full round trip with
a fake OpenAI-compatible client, no real network call (Section 0 rule 3)."""
import json
from types import SimpleNamespace

from cutoff.llm import form_answer


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, name, arguments):
        self.function = _FakeFunction(name, arguments)


class _FakeCompletions:
    def __init__(self, tool_call_result: dict, expected_question_count: int):
        self._tool_call_result = tool_call_result
        self._expected_question_count = expected_question_count

    def create(self, **kwargs):
        # Confirms the tool schema sent to the model uses safe "qN" property
        # names, never the raw question text, for every question.
        schema = kwargs["tools"][0]["function"]["parameters"]
        assert set(schema["properties"].keys()) == {f"q{i}" for i in range(self._expected_question_count)}
        message = SimpleNamespace(tool_calls=[
            _FakeToolCall(form_answer.TOOL_NAME, json.dumps(self._tool_call_result))
        ])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeClient:
    def __init__(self, tool_call_result: dict, expected_question_count: int):
        self.chat = SimpleNamespace(completions=_FakeCompletions(tool_call_result, expected_question_count))


def test_draft_answers_maps_synthetic_keys_back_to_real_titles(monkeypatch):
    titles = ["Why ould we hire you?", "Your skills (comma seperated eg:CPP,Python,etc)"]
    fake_result = {"q0": "Backend experience matches the JD.", "q1": "Python, SQL, Git"}
    client = _FakeClient(fake_result, expected_question_count=2)
    monkeypatch.setattr(form_answer, "get_client", lambda provider, api_key, base_url: client)

    answers = form_answer.draft_answers(
        "resume text", "jd text", titles,
        api_key="x", model="m", provider="gemini", base_url=None,
    )

    assert answers == {
        "Why ould we hire you?": "Backend experience matches the JD.",
        "Your skills (comma seperated eg:CPP,Python,etc)": "Python, SQL, Git",
    }


def test_draft_answers_defaults_missing_keys_to_empty_string(monkeypatch):
    titles = ["Phone number"]
    client = _FakeClient({}, expected_question_count=1)  # model didn't include q0 at all
    monkeypatch.setattr(form_answer, "get_client", lambda provider, api_key, base_url: client)

    answers = form_answer.draft_answers(
        "resume text", "", titles, api_key="x", model="m", provider="gemini", base_url=None,
    )
    assert answers == {"Phone number": ""}


def test_draft_answers_returns_empty_dict_without_any_call_for_no_questions():
    calls = []

    class _ExplodingClient:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError("should never touch the client for zero questions")

    answers = form_answer.draft_answers(
        "resume", "jd", [], api_key="x", model="m", provider="gemini", base_url=None,
    )
    assert answers == {}
    assert calls == []


def test_tool_schema_uses_synthetic_keys_not_raw_question_text():
    schema = form_answer._tool_schema(["Why ould we hire you?", "Skills (CSV)"])
    assert set(schema["properties"].keys()) == {"q0", "q1"}
    assert schema["required"] == ["q0", "q1"]
    assert "Why ould we hire you?" in schema["properties"]["q0"]["description"]
