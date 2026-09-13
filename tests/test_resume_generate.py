"""cutoff.llm.resume_generate: the forced tool-use call itself, stubbing the
provider client directly (never a real network/LLM call — Section 0 rule 3).
Grounding is enforced at the schema level (enum-constrained skills/titles)
plus a Python-side re-check, same double-layer discipline resume_match.py's
chosen_filename already uses."""
from cutoff.llm import resume_generate
from cutoff.llm.resume_generate import TOOL_NAME
from cutoff.models import MasterProfile, MasterProfileProject


def _profile(**overrides):
    base = dict(
        skills=["Python", "Django", "PostgreSQL"],
        projects=[
            MasterProfileProject(title="Order Service", bullets=["Built a Django REST API."]),
            MasterProfileProject(title="CLI Tool", bullets=["Wrote a small CLI in Python."]),
        ],
    )
    base.update(overrides)
    return MasterProfile(**base)


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
        "We need a backend engineer with Django experience.", _profile(),
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:", use_cache=False,
    )
    assert sections["skills"] == ["Python", "Django"]
    assert reason == "Emphasized Django experience matching the JD's backend stack."
    # forced tool use, not freeform text -- same discipline as resume_match.py/extract.py
    call = fake_client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    # schema-level grounding: skills/titles are enum-constrained to the real profile
    schema = call["tools"][0]["input_schema"]
    assert schema["properties"]["skills"]["items"]["enum"] == ["Python", "Django", "PostgreSQL"]
    title_schema = schema["properties"]["highlighted_projects"]["items"]["properties"]["title"]
    assert set(title_schema["enum"]) == {"Order Service", "CLI Tool"}


def test_generate_tailored_resume_raises_when_a_skill_is_not_in_the_master_profile(monkeypatch):
    """Defense in depth beyond the schema enum -- providers don't always
    enforce enum constraints strictly (found live elsewhere in this
    codebase, hence resume_match.py's own chosen_filename re-check)."""
    fake_client = _FakeAnthropicClient(_valid_result(skills=["Python", "Rust"]))  # Rust isn't in the profile
    monkeypatch.setattr(resume_generate, "get_client", lambda *a, **k: fake_client)

    try:
        resume_generate.generate_tailored_resume(
            "JD text", _profile(), api_key="x", model="m", provider="anthropic",
            base_url=None, db_path=":memory:", use_cache=False,
        )
        assert False, "expected ValueError for a skill not in the master profile"
    except ValueError as e:
        assert "Rust" in str(e)


def test_generate_tailored_resume_raises_when_project_title_is_not_in_the_master_profile(monkeypatch):
    fake_client = _FakeAnthropicClient(
        _valid_result(highlighted_projects=[{"title": "Invented Project", "bullets": ["Did something."]}])
    )
    monkeypatch.setattr(resume_generate, "get_client", lambda *a, **k: fake_client)

    try:
        resume_generate.generate_tailored_resume(
            "JD text", _profile(), api_key="x", model="m", provider="anthropic",
            base_url=None, db_path=":memory:", use_cache=False,
        )
        assert False, "expected ValueError for a project title not in the master profile"
    except ValueError as e:
        assert "Invented Project" in str(e)


def test_generate_tailored_resume_raises_when_no_highlighted_projects(monkeypatch):
    fake_client = _FakeAnthropicClient(_valid_result(highlighted_projects=[]))
    monkeypatch.setattr(resume_generate, "get_client", lambda *a, **k: fake_client)

    try:
        resume_generate.generate_tailored_resume(
            "JD text", _profile(), api_key="x", model="m", provider="anthropic",
            base_url=None, db_path=":memory:", use_cache=False,
        )
        assert False, "expected ValueError for empty highlighted_projects"
    except ValueError:
        pass


def test_generate_tailored_resume_raises_when_project_missing_bullets(monkeypatch):
    fake_client = _FakeAnthropicClient(
        _valid_result(highlighted_projects=[{"title": "Order Service", "bullets": []}])
    )
    monkeypatch.setattr(resume_generate, "get_client", lambda *a, **k: fake_client)

    try:
        resume_generate.generate_tailored_resume(
            "JD text", _profile(), api_key="x", model="m", provider="anthropic",
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

    profile = _profile()
    kwargs = dict(api_key="x", model="m", provider="anthropic", base_url=None, db_path=db_path, use_cache=True)

    resume_generate.generate_tailored_resume("We need a backend engineer.", profile, **kwargs)
    resume_generate.generate_tailored_resume("We need a backend engineer.", profile, **kwargs)

    assert len(fake_client.messages.calls) == 1  # second call served from cache, no second LLM call
