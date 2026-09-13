"""cutoff.llm.profile_extract: extracting a MasterProfile from raw resume
text, via a stubbed provider client — never a real LLM call (Section 0 rule
3)."""
from cutoff.llm import profile_extract
from cutoff.llm.profile_extract import TOOL_NAME
from cutoff.models import MasterProfile


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
        "phone": None,
        "links": [{"label": "GitHub", "url": "https://github.com/riya"}],
        "education": [{"degree": "B.Tech CSE", "institution": "Demo College", "cgpa": "7.42",
                        "batch_year": 2026, "notes": None}],
        "skills": ["Python", "Django"],
        "projects": [{"title": "Order Service", "tech_stack": ["Django"], "bullets": ["Built a REST API."],
                       "link": None}],
        "experience": [],
        "achievements": ["Runner-up, hackathon."],
    }
    base.update(overrides)
    return base


def test_extract_profile_from_resume_text_returns_a_master_profile(monkeypatch):
    fake_client = _FakeAnthropicClient(_valid_result())
    monkeypatch.setattr(profile_extract, "get_client", lambda *a, **k: fake_client)

    profile = profile_extract.extract_profile_from_resume_text(
        "Riya Mehta\nSkills: Python, Django\nEducation: B.Tech CSE, Demo College, CGPA 7.42, 2026\n"
        "Projects: Order Service (Django) -- Built a REST API.\nAchievements: Runner-up, hackathon.",
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:", use_cache=False,
    )

    assert isinstance(profile, MasterProfile)
    assert profile.skills == ["Python", "Django"]
    assert profile.education[0].institution == "Demo College"
    assert profile.projects[0].title == "Order Service"
    # forced tool use, not freeform text -- same discipline as resume_generate.py
    call = fake_client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": TOOL_NAME}


def test_extract_profile_from_resume_text_includes_hyperlinks_in_the_prompt(monkeypatch):
    """The real fix for the "link URL got saved as the literal word GitHub"
    bug: plain PDF text only ever shows a link's visible label, never its
    target -- the real URLs (from cutoff.pipeline.ingest.extract_pdf_hyperlinks)
    must be threaded into the prompt for the model to have any chance of
    getting the real URL right instead of echoing back the label."""
    fake_client = _FakeAnthropicClient(_valid_result())
    monkeypatch.setattr(profile_extract, "get_client", lambda *a, **k: fake_client)

    profile_extract.extract_profile_from_resume_text(
        "GitHub\nLinkedIn", api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
        use_cache=False, hyperlinks=["https://github.com/JeetM2207", "https://linkedin.com/in/jeet-manseta"],
    )

    call = fake_client.messages.calls[0]
    user_text = call["messages"][0]["content"]
    assert "https://github.com/JeetM2207" in user_text
    assert "https://linkedin.com/in/jeet-manseta" in user_text


def test_extract_profile_from_resume_text_works_without_hyperlinks(monkeypatch):
    """hyperlinks is optional -- every existing caller that doesn't pass it
    (including every test written before this fix) must keep working."""
    fake_client = _FakeAnthropicClient(_valid_result())
    monkeypatch.setattr(profile_extract, "get_client", lambda *a, **k: fake_client)

    profile = profile_extract.extract_profile_from_resume_text(
        "some resume text", api_key="x", model="m", provider="anthropic", base_url=None,
        db_path=":memory:", use_cache=False,
    )
    assert isinstance(profile, MasterProfile)


def test_extract_profile_from_resume_text_raises_on_empty_input():
    try:
        profile_extract.extract_profile_from_resume_text(
            "   ", api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
        )
        assert False, "expected ValueError for empty resume text"
    except ValueError:
        pass


def test_extract_profile_from_resume_text_raises_on_malformed_model_response(monkeypatch):
    """A response with a structurally wrong project (missing the required
    `bullets` field) must surface as an error, not a silently corrupted
    profile — Pydantic validation on MasterProfile(**result) is the safety
    net, same as resume_generate.py's schema-validation discipline."""
    broken_result = _valid_result(projects=[{"title": "Order Service", "tech_stack": ["Django"]}])  # no "bullets"
    fake_client = _FakeAnthropicClient(broken_result)
    monkeypatch.setattr(profile_extract, "get_client", lambda *a, **k: fake_client)

    try:
        profile_extract.extract_profile_from_resume_text(
            "some resume text", api_key="x", model="m", provider="anthropic", base_url=None,
            db_path=":memory:", use_cache=False,
        )
        assert False, "expected a validation error for a project missing required bullets"
    except Exception as e:
        assert "bullets" in str(e).lower() or "validation" in str(type(e)).lower()


def test_extract_profile_from_resume_text_uses_cache_on_second_call(tmp_path, monkeypatch):
    from cutoff import db

    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    fake_client = _FakeAnthropicClient(_valid_result())
    monkeypatch.setattr(profile_extract, "get_client", lambda *a, **k: fake_client)

    kwargs = dict(api_key="x", model="m", provider="anthropic", base_url=None, db_path=db_path, use_cache=True)
    profile_extract.extract_profile_from_resume_text("same resume text", **kwargs)
    profile_extract.extract_profile_from_resume_text("same resume text", **kwargs)

    assert len(fake_client.messages.calls) == 1  # second call served from cache
