"""cutoff.llm.resume_generate: the forced tool-use call itself, stubbing the
provider client directly (never a real network/LLM call — Section 0 rule
3), same shape as test_extract_retry.py does for extract.py's client calls."""
from cutoff.llm import resume_generate
from cutoff.llm.resume_generate import TOOL_NAME


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


def _valid_result(**overrides):
    base = {
        "headline": "Backend-focused CSE student.",
        "skills": ["Python", "Django"],
        "highlighted_projects": [{"title": "Order Service", "bullets": ["Built a Django REST API."]}],
        "match_reason": "Emphasized Django experience matching the JD's backend stack.",
    }
    base.update(overrides)
    return base


def test_generate_tailored_resume_returns_sections_and_reason(monkeypatch):
    fake_client = _FakeAnthropicClient(_valid_result())
    monkeypatch.setattr(resume_generate, "get_client", lambda *a, **k: fake_client)

    sections, reason = resume_generate.generate_tailored_resume(
        "We need a backend engineer with Django experience.", "## Skills\n- Python\n- Django",
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:", use_cache=False,
    )
    assert sections["skills"] == ["Python", "Django"]
    assert reason == "Emphasized Django experience matching the JD's backend stack."
    # forced tool use, not freeform text -- same discipline as resume_match.py/extract.py
    call = fake_client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": TOOL_NAME}


def test_generate_tailored_resume_raises_when_no_highlighted_projects(monkeypatch):
    fake_client = _FakeAnthropicClient(_valid_result(highlighted_projects=[]))
    monkeypatch.setattr(resume_generate, "get_client", lambda *a, **k: fake_client)

    try:
        resume_generate.generate_tailored_resume(
            "JD text", "master profile", api_key="x", model="m", provider="anthropic",
            base_url=None, db_path=":memory:", use_cache=False,
        )
        assert False, "expected ValueError for empty highlighted_projects"
    except ValueError:
        pass


def test_generate_tailored_resume_raises_when_project_missing_bullets(monkeypatch):
    fake_client = _FakeAnthropicClient(_valid_result(highlighted_projects=[{"title": "X", "bullets": []}]))
    monkeypatch.setattr(resume_generate, "get_client", lambda *a, **k: fake_client)

    try:
        resume_generate.generate_tailored_resume(
            "JD text", "master profile", api_key="x", model="m", provider="anthropic",
            base_url=None, db_path=":memory:", use_cache=False,
        )
        assert False, "expected ValueError for a project with no bullets"
    except ValueError:
        pass


def test_generate_tailored_resume_uses_cache_on_second_call(monkeypatch, tmp_path):
    from cutoff import db

    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    fake_client = _FakeAnthropicClient(_valid_result())
    monkeypatch.setattr(resume_generate, "get_client", lambda *a, **k: fake_client)

    args = ("We need a backend engineer.", "## Skills\n- Python")
    kwargs = dict(api_key="x", model="m", provider="anthropic", base_url=None, db_path=db_path, use_cache=True)

    resume_generate.generate_tailored_resume(*args, **kwargs)
    resume_generate.generate_tailored_resume(*args, **kwargs)

    assert len(fake_client.messages.calls) == 1  # second call served from cache, no second LLM call
