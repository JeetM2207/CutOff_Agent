"""The Section 16-extension demo endpoints (MODE=fake, DEMO_MODE=1's
dashboard demo controls) — Phase 5's originally-documented but never-built
promise (see cutoff.main's MODE=fake docstring and README.md), built and
tested here. Uses a stubbed extract_fn (never the real LLM, Section 0 rule
3) monkeypatched over cutoff.app.server._demo_extract_fn, so these test the
endpoints' own wiring — routing, drive resolution, chaos toggling, reset —
not extraction accuracy, which tests/test_prompts.py already covers live."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cutoff import db
from cutoff.models import Criteria, Evidence, Notice
from cutoff.pipeline.timeparse import resolve_deadline


def _stub_extract(msg, body_clean, quoted_history, attachment_text, **kwargs) -> Notice:
    """Company/role/deadline text are copied verbatim from the demo
    presets' own bodies (all built around "Meridian Robotics" / "Software
    Engineer") so the real grounding validator's near-verbatim check passes
    naturally, and `deadline` is computed with the same resolve_deadline()
    the real pipeline uses so its own dual-parse safety check doesn't null
    it out for disagreeing with `deadline_text` — these tests exercise the
    demo endpoints' own wiring (routing, drive resolution, chaos, reset),
    not extraction accuracy, which other tests already cover live."""
    if msg.subject.startswith("Re:"):
        notice_type, deadline_text = "REVISION", None
    elif msg.subject.startswith("Shortlist:"):
        notice_type, deadline_text = "SHORTLIST", None
    else:
        notice_type, deadline_text = "NEW_DRIVE", "tomorrow"

    evidence = [Evidence(field="company", quote="Meridian Robotics"), Evidence(field="role", quote="Software Engineer")]
    deadline = None
    if deadline_text:
        deadline = resolve_deadline(deadline_text, msg.received_at, "Asia/Kolkata")
        evidence.append(Evidence(field="deadline_text", quote=deadline_text))

    return Notice(
        notice_type=notice_type, company="Meridian Robotics", role="Software Engineer", role_category="SDE",
        criteria=Criteria(min_gpa=5.0, gpa_inclusive=True),
        deadline_text=deadline_text, deadline=deadline,
        form_url="https://forms.gle/stub",
        evidence=evidence,
    )


@pytest.fixture()
def demo_client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "demo.db")
    monkeypatch.setenv("MODE", "fake")
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DB_PATH", db_path)
    db.init_db(db_path)

    from cutoff.app import server
    monkeypatch.setattr(server, "_demo_extract_fn", _stub_extract)
    # Each test gets a fresh in-memory demo state, not whatever another test
    # (or another module's import of server.py) left behind.
    server._demo.clear()
    server._demo.update(server._build_demo_state())
    return TestClient(server.app)


def test_config_reports_demo_mode(demo_client):
    resp = demo_client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "fake", "demo_mode": True}


def test_demo_endpoints_require_fake_and_demo_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("MODE", "real")
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "real.db"))
    db.init_db(str(tmp_path / "real.db"))

    from cutoff.app import server
    client = TestClient(server.app)
    assert client.post("/api/demo/send", json={"preset": "new_drive_1"}).status_code == 403
    assert client.post("/api/demo/chaos", json={"target": "calendar"}).status_code == 403
    assert client.post("/api/demo/reset").status_code == 403


def test_demo_send_unknown_preset_is_400(demo_client):
    resp = demo_client.post("/api/demo/send", json={"preset": "nonexistent"})
    assert resp.status_code == 400


def test_demo_send_new_drive_appears_on_board(demo_client):
    resp = demo_client.post("/api/demo/send", json={"preset": "new_drive_1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["notice_type"] == "NEW_DRIVE"
    assert body["verdict"] == "ELIGIBLE"

    drives = demo_client.get("/api/drives").json()
    assert body["drive_id"] in [d["drive_id"] for d in drives["register"]]


def test_demo_send_scam_lands_in_watch_out(demo_client):
    resp = demo_client.post("/api/demo/send", json={"preset": "scam"})
    assert resp.status_code == 200
    assert resp.json()["notice_type"] == "SUSPICIOUS"

    drives = demo_client.get("/api/drives").json()
    assert len(drives["watch_out"]) == 1


def test_demo_send_correction_resolves_to_same_drive(demo_client):
    first = demo_client.post("/api/demo/send", json={"preset": "new_drive_1"}).json()
    second = demo_client.post("/api/demo/send", json={"preset": "correction"}).json()
    assert second["drive_id"] == first["drive_id"]
    assert second["notice_type"] == "REVISION"


def test_demo_send_shortlist_resolves_to_the_meridian_drive(demo_client):
    first = demo_client.post("/api/demo/send", json={"preset": "new_drive_1"}).json()
    second = demo_client.post("/api/demo/send", json={"preset": "shortlist"}).json()
    assert second["drive_id"] == first["drive_id"]
    assert second["notice_type"] == "SHORTLIST"


def test_demo_chaos_makes_the_next_calendar_write_fail(demo_client):
    # Runs real (not mocked) exponential backoff across MAX_ATTEMPTS=5
    # retries (~15s) before the action gives up as FAILED — with_retry()'s
    # `sleep=time.sleep` default is bound at function-definition time, so a
    # module-level monkeypatch can't intercept it without changing
    # executor.py itself, which is out of scope for a demo-panel test.
    demo_client.post("/api/demo/chaos", json={"target": "calendar"})
    result = demo_client.post("/api/demo/send", json={"preset": "new_drive_1"}).json()

    detail = demo_client.get(f"/api/drives/{result['drive_id']}").json()
    calendar_actions = [a for a in detail["actions"] if a["app"] == "calendar"]
    assert calendar_actions and calendar_actions[0]["status"] == "FAILED"
    assert "injected 500" in calendar_actions[0]["last_error"]


def test_demo_chaos_clear_stops_the_failures(demo_client):
    demo_client.post("/api/demo/chaos", json={"target": "calendar"})
    demo_client.post("/api/demo/chaos", json={"target": "clear"})
    result = demo_client.post("/api/demo/send", json={"preset": "new_drive_1"}).json()

    detail = demo_client.get(f"/api/drives/{result['drive_id']}").json()
    calendar_actions = [a for a in detail["actions"] if a["app"] == "calendar"]
    assert calendar_actions and calendar_actions[0]["status"] == "VERIFIED"


def test_demo_chaos_unknown_target_is_400(demo_client):
    resp = demo_client.post("/api/demo/chaos", json={"target": "nonexistent"})
    assert resp.status_code == 400


def test_demo_reset_clears_the_board(demo_client):
    demo_client.post("/api/demo/send", json={"preset": "new_drive_1"})
    assert demo_client.get("/api/drives").json()["register"]

    resp = demo_client.post("/api/demo/reset")
    assert resp.status_code == 200

    drives = demo_client.get("/api/drives").json()
    assert all(not v for v in drives.values())
