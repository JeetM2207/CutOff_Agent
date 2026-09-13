"""Dashboard API (Section 16): drives grouped into board columns, drive
detail with history/evidence/actions, live trace, and eval results."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cutoff import db, trace
from cutoff.models import Criteria, Drive
from cutoff.pipeline import executor, planner, resolve
from cutoff.models import Notice, StudentProfile, Verdict

NOW = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)


@pytest.fixture()
def dashboard_client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cutoff.db")
    monkeypatch.setenv("DB_PATH", db_path)
    db.init_db(db_path)
    trace.configure(db_path)

    # server.py's module-level app is already imported elsewhere in the test
    # session with a different DB_PATH baked into its own init call; re-import
    # is unnecessary since every route reads get_settings() fresh per request.
    from cutoff.app.server import app
    return TestClient(app), db_path


def profile() -> StudentProfile:
    return StudentProfile(
        name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
        gpa=7.42, active_backlogs=0, batch_year=2026,
    )


def _seed_drive(db_path, **overrides) -> Drive:
    base = dict(
        drive_id="zentrix:data-analyst:2026", company="Zentrix", role="Data Analyst",
        version=1, criteria=Criteria(min_gpa=7.0, branches_allowed=["CSE"]),
        deadline=NOW.replace(day=20), last_verdict="ELIGIBLE",
    )
    base.update(overrides)
    drive = Drive(**base)
    resolve.save_drive(db_path, drive)
    return drive


def test_drives_grouped_by_verdict_into_board_columns(dashboard_client):
    client, db_path = dashboard_client
    _seed_drive(db_path, drive_id="d1:role:2026", company="Eligible Co", last_verdict="ELIGIBLE")
    _seed_drive(db_path, drive_id="d2:role:2026", company="Review Co", last_verdict="NEEDS_REVIEW")
    _seed_drive(db_path, drive_id="d3:role:2026", company="Rejected Co", last_verdict="NOT_ELIGIBLE")
    _seed_drive(db_path, drive_id="d4:role:2026", company="Cancelled Co", status="CANCELLED")
    _seed_drive(db_path, drive_id="d5:role:2026", company="Registered Co", registered=True)

    resp = client.get("/api/drives")
    assert resp.status_code == 200
    body = resp.json()

    assert [d["company"] for d in body["register"]] == ["Eligible Co"]
    assert [d["company"] for d in body["your_call"]] == ["Review Co"]
    assert [d["company"] for d in body["not_for_you"]] == ["Rejected Co"]
    assert {d["company"] for d in body["closed"]} == {"Cancelled Co", "Registered Co"}


def test_drive_bubbles_reflect_stated_vs_unstated_criteria(dashboard_client):
    client, db_path = dashboard_client
    _seed_drive(
        db_path, drive_id="d1:role:2026",
        criteria=Criteria(min_gpa=7.0, branches_allowed=["CSE"]),  # backlogs/batch/pct not stated
        last_verdict="ELIGIBLE",
    )
    resp = client.get("/api/drives")
    bubbles = {b["key"]: b["state"] for b in resp.json()["register"][0]["bubbles"]}
    assert bubbles["gpa"] == "passed"
    assert bubbles["branch"] == "passed"
    assert bubbles["backlogs"] == "not_stated"
    assert bubbles["batch"] == "not_stated"


def test_drive_detail_includes_history_evidence_and_actions(dashboard_client):
    client, db_path = dashboard_client
    drive = _seed_drive(db_path)
    resolve.record_history(db_path, drive.drive_id, 1, {
        "changed_fields": ["criteria"], "notice_type": "NEW_DRIVE",
        "evidence": [{"field": "company", "quote": "Zentrix"}],
        "verdict": {"result": "ELIGIBLE", "reasons": [], "questions": []},
    }, "m1")
    planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=Notice(notice_type="NEW_DRIVE"),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )

    resp = client.get(f"/api/drives/{drive.drive_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["drive"]["company"] == "Zentrix"
    assert len(body["history"]) == 1
    assert body["history"][0]["evidence"] == [{"field": "company", "quote": "Zentrix"}]
    assert len(body["actions"]) >= 1
    assert any(a["app"] == "telegram" for a in body["actions"])


def test_drive_detail_404_for_unknown_drive(dashboard_client):
    client, _ = dashboard_client
    resp = client.get("/api/drives/does-not-exist:role:2026")
    assert resp.status_code == 404


def test_watch_out_column_surfaces_suspicious_messages(dashboard_client):
    client, db_path = dashboard_client
    planner.plan_suspicious(
        db_path, key="tg:suspicious:m1", drive=None, company_hint="Scammy Corp",
        signals=["FEE_REQUEST"],
    )
    resp = client.get("/api/drives")
    watch_out = resp.json()["watch_out"]
    assert len(watch_out) == 1
    assert watch_out[0]["company_hint"] == "Scammy Corp"
    assert "FEE_REQUEST" in watch_out[0]["signals"]


def test_runs_latest_and_trace_endpoints(dashboard_client):
    client, db_path = dashboard_client
    with trace.run("run-1"):
        with trace.span("ingest", message_id="m1"):
            pass

    resp = client.get("/api/runs/latest")
    assert resp.status_code == 200
    assert resp.json()["run_id"] == "run-1"

    resp2 = client.get("/api/trace/run-1")
    assert resp2.status_code == 200
    assert resp2.json()["spans"][0]["name"] == "ingest"

    resp3 = client.get("/api/trace/nonexistent-run")
    assert resp3.status_code == 404


def test_health_endpoint_reports_no_issues_on_a_fresh_db(dashboard_client):
    client, db_path = dashboard_client
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert {c["app"] for c in data["circuits"]} == {"sheets", "calendar", "telegram"}
    assert all(c["state"] == "closed" for c in data["circuits"])
    assert data["self_healed"] == []


def test_health_endpoint_reports_an_open_circuit_and_a_self_healed_action(dashboard_client):
    from cutoff.models import Action
    from cutoff.pipeline.executor import CIRCUIT_FAILURE_THRESHOLD

    client, db_path = dashboard_client

    breaker = executor.shared_breaker(db_path)
    state = breaker.state_for("sheets")
    # snapshot()/is_open() check against real wall-clock time, so the
    # failures must be "now", not the fixture's fixed historical NOW.
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        state.record_failure(datetime.now(timezone.utc))

    action = Action(
        action_id="a1", idempotency_key="cal:d1:deadline", drive_id="d1", drive_version=1,
        app="calendar", kind="upsert_event", tier="T1_REVERSIBLE", payload={},
    )
    planned = executor.plan_action(db_path, action)
    planned.attempts = 2
    planned.status = "VERIFIED"
    executor._save_action(db_path, planned)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    sheets_circuit = next(c for c in data["circuits"] if c["app"] == "sheets")
    assert sheets_circuit["state"] == "open"
    assert sheets_circuit["consecutive_failures"] == CIRCUIT_FAILURE_THRESHOLD

    assert len(data["self_healed"]) == 1
    assert data["self_healed"][0]["action_id"] == "a1"
    assert data["self_healed"][0]["attempts"] == 2


def test_eval_latest_reads_real_committed_results(dashboard_client):
    client, _ = dashboard_client
    resp = client.get("/api/eval/latest")
    assert resp.status_code == 200
    labels = {r["label"] for r in resp.json()["runs"]}
    # These are the project's own committed eval/results/*.json files
    # (Section 15.6/19) — not fixtures, the real Phase 2/4 history.
    assert "baseline" in labels
    assert "after" in labels
