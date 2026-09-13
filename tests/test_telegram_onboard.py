"""Section 6.4 onboarding extension: /onboard, /sync, and resume-PDF upload
handling in TelegramBotLoop. Never calls a real LLM, GitHub/LeetCode API, or
Telegram network endpoint (Section 0 rule 3 extends to every external
service this project talks to) — every boundary is stubbed."""
from types import SimpleNamespace

from cutoff import db
from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMessenger, FakeSheetStore
from cutoff.bot import telegram_loop as telegram_loop_mod
from cutoff.bot.telegram_loop import TelegramBotLoop
from cutoff.models import MasterProfile, MasterProfileProject
from cutoff.pipeline import master_profile
from cutoff.pipeline.executor import Adapters


class _FakeHttpResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")


def _bot(tmp_path, *, files=None) -> tuple[TelegramBotLoop, Adapters]:
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    adapters = Adapters(
        sheets=FakeSheetStore(None, None), calendar=FakeCalendarStore(), messenger=FakeMessenger(), files=files,
    )
    settings = SimpleNamespace(
        llm_api_key="unused", llm_model="stub", llm_provider="anthropic", llm_base_url=None,
        generated_resume_dir=str(tmp_path / "generated"), public_base_url="http://127.0.0.1:8000",
        master_profile_path=str(tmp_path / "master_profile.yaml"),
    )
    return TelegramBotLoop("token", "chat1", db_path, adapters, settings=settings), adapters, db_path


def _message(text=None, document=None, chat_id="chat1") -> dict:
    m = {"chat": {"id": chat_id}}
    if text is not None:
        m["text"] = text
    if document is not None:
        m["document"] = document
    return m


def test_message_from_untrusted_chat_is_ignored(tmp_path):
    bot, adapters, _ = _bot(tmp_path)
    bot._handle_message(_message(text="/onboard", chat_id="someone-elses-chat"))
    assert adapters.messenger.calls == []


def test_onboard_command_sends_help_text(tmp_path):
    bot, adapters, _ = _bot(tmp_path)
    bot._handle_message(_message(text="/onboard"))
    sends = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert len(sends) == 1
    assert "resume" in sends[0][1]["text"].lower()


def test_sync_command_with_no_args_shows_usage(tmp_path):
    bot, adapters, _ = _bot(tmp_path)
    bot._handle_message(_message(text="/sync"))
    sends = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert "Usage" in sends[0][1]["text"]


def test_sync_command_stages_and_sends_preview(tmp_path, monkeypatch):
    bot, adapters, db_path = _bot(tmp_path)
    monkeypatch.setattr(
        "cutoff.adapters.developer_footprint.fetch_github_profile",
        lambda username, **k: {"repos": [{"name": "cli-tool", "description": "A CLI.",
                                           "url": "https://github.com/x/cli-tool", "language": "Python",
                                           "topics": [], "stars": 3, "readme_excerpt": ""}]},
    )
    monkeypatch.setattr("cutoff.adapters.developer_footprint.fetch_leetcode_stats", lambda username, **k: None)

    bot._handle_message(_message(text="/sync some-github-user"))

    sends = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert len(sends) == 1
    text, buttons = sends[0][1]["text"], sends[0][1]["buttons"]
    assert "cli-tool" in text
    assert {b.callback_data.split(":")[2] for b in buttons} == {"approve", "discard"}
    token = buttons[0].callback_data.split(":")[1]
    row = db.get_connection(db_path).execute(
        "SELECT * FROM profile_imports WHERE token = ?", (token,)
    ).fetchone()
    assert row is not None
    assert not (tmp_path / "master_profile.yaml").exists()  # staged, not yet written to the real path


def test_sync_command_reports_when_nothing_found(tmp_path, monkeypatch):
    bot, adapters, _ = _bot(tmp_path)
    monkeypatch.setattr("cutoff.adapters.developer_footprint.fetch_github_profile",
                        lambda username, **k: {"repos": []})
    monkeypatch.setattr("cutoff.adapters.developer_footprint.fetch_leetcode_stats", lambda username, **k: None)

    bot._handle_message(_message(text="/sync nonexistent-user-xyz"))

    sends = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert "couldn't find" in sends[0][1]["text"].lower()


def test_resume_upload_rejects_non_pdf_filename(tmp_path):
    bot, adapters, _ = _bot(tmp_path)
    bot._handle_message(_message(document={"file_name": "resume.docx", "file_id": "f1", "file_size": 100}))
    sends = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert "PDF" in sends[0][1]["text"]


def test_resume_upload_rejects_oversized_file(tmp_path):
    bot, adapters, _ = _bot(tmp_path)
    bot._handle_message(_message(document={"file_name": "resume.pdf", "file_id": "f1", "file_size": 999_999_999}))
    sends = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert "too large" in sends[0][1]["text"].lower()


def test_resume_upload_happy_path_stages_and_sends_preview(tmp_path, monkeypatch):
    bot, adapters, db_path = _bot(tmp_path)

    def fake_get(url, **kwargs):
        if "getFile" in url:
            return _FakeHttpResponse(200, {"result": {"file_path": "documents/resume.pdf"}})
        return _FakeHttpResponse(200, content=b"%PDF-1.4 fake bytes")

    monkeypatch.setattr(telegram_loop_mod.httpx, "get", fake_get)
    monkeypatch.setattr("cutoff.pipeline.ingest.extract_pdf_text", lambda data: "Riya Mehta\nSkills: Python, Django")
    monkeypatch.setattr("cutoff.pipeline.ingest.extract_pdf_hyperlinks", lambda data: ["https://github.com/riya"])
    monkeypatch.setattr(
        "cutoff.llm.profile_extract.extract_profile_from_resume_text",
        lambda text, **k: MasterProfile(skills=["Python", "Django"]),
    )

    bot._handle_message(_message(document={"file_name": "resume.pdf", "file_id": "f1", "file_size": 1000}))

    sends = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert len(sends) == 1
    assert "Django" in sends[0][1]["text"]
    row = db.get_connection(db_path).execute("SELECT * FROM profile_imports").fetchone()
    assert row is not None


def test_resume_upload_handles_extraction_failure_without_crashing(tmp_path, monkeypatch):
    bot, adapters, _ = _bot(tmp_path)

    def fake_get(url, **kwargs):
        if "getFile" in url:
            return _FakeHttpResponse(200, {"result": {"file_path": "documents/resume.pdf"}})
        return _FakeHttpResponse(200, content=b"%PDF-1.4 fake bytes")

    monkeypatch.setattr(telegram_loop_mod.httpx, "get", fake_get)
    monkeypatch.setattr("cutoff.pipeline.ingest.extract_pdf_text", lambda data: "some text")
    monkeypatch.setattr("cutoff.pipeline.ingest.extract_pdf_hyperlinks", lambda data: [])

    def broken_extract(text, **k):
        raise RuntimeError("rate limited")
    monkeypatch.setattr("cutoff.llm.profile_extract.extract_profile_from_resume_text", broken_extract)

    bot._handle_message(_message(document={"file_name": "resume.pdf", "file_id": "f1", "file_size": 1000}))

    sends = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert len(sends) == 1
    assert "went wrong" in sends[0][1]["text"].lower()


def test_approve_callback_promotes_staged_profile_and_backs_up_the_old_one(tmp_path):
    bot, adapters, db_path = _bot(tmp_path)
    target_path = tmp_path / "master_profile.yaml"
    master_profile.save_master_profile(MasterProfile(skills=["Python"]), target_path)  # an existing profile

    new_profile = MasterProfile(skills=["Python", "Django"])
    staged_path = tmp_path / ".staged_abc123.yaml"
    master_profile.save_master_profile(new_profile, staged_path)
    conn = db.get_connection(db_path)
    conn.execute("INSERT INTO profile_imports (token, staged_path, diff_summary, created_at) VALUES (?, ?, ?, ?)",
                 ("abc123", str(staged_path), "+1 skill", "2026-01-01T00:00:00+00:00"))
    conn.commit()

    bot._handle_onboard_callback("abc123", "approve")

    loaded = master_profile.load_master_profile(target_path)
    assert loaded.skills == ["Python", "Django"]
    assert not staged_path.exists()
    backups = list(tmp_path.glob("master_profile.*.bak.yaml"))
    assert len(backups) == 1
    assert master_profile.load_master_profile(backups[0]).skills == ["Python"]
    sends = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert "updated" in sends[0][1]["text"].lower()


def test_discard_callback_deletes_staged_file_and_leaves_existing_profile_untouched(tmp_path):
    bot, adapters, db_path = _bot(tmp_path)
    target_path = tmp_path / "master_profile.yaml"
    master_profile.save_master_profile(MasterProfile(skills=["Python"]), target_path)

    staged_path = tmp_path / ".staged_abc123.yaml"
    master_profile.save_master_profile(MasterProfile(skills=["Python", "Rust"]), staged_path)
    conn = db.get_connection(db_path)
    conn.execute("INSERT INTO profile_imports (token, staged_path, diff_summary, created_at) VALUES (?, ?, ?, ?)",
                 ("abc123", str(staged_path), "+1 skill", "2026-01-01T00:00:00+00:00"))
    conn.commit()

    bot._handle_onboard_callback("abc123", "discard")

    assert not staged_path.exists()
    assert master_profile.load_master_profile(target_path).skills == ["Python"]  # unchanged
    sends = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert "discarded" in sends[0][1]["text"].lower()


def test_onboard_callback_is_a_no_op_for_an_unknown_or_already_resolved_token(tmp_path):
    bot, adapters, _ = _bot(tmp_path)
    bot._handle_onboard_callback("does-not-exist", "approve")
    assert adapters.messenger.calls == []
