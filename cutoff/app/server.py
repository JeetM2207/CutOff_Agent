"""FastAPI app: dashboard static files + JSON API (Section 16)."""
from __future__ import annotations

import io
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from cutoff import db, trace
from cutoff.adapters import faults
from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMailSource, FakeMessenger, FakeSheetStore
from cutoff.config import get_settings
from cutoff.llm.extract import extract_notice
from cutoff.models import AttachmentMeta, CollegePolicy, Drive, EmailMessage, ResumeFile, StudentProfile
from cutoff.pipeline import executor, run
from cutoff.pipeline.executor import Adapters

HEALTH_APPS = ["sheets", "calendar", "telegram"]

STATIC_DIR = Path(__file__).resolve().parent / "static"
EVAL_RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "eval" / "results"
# Dynamic resume generation (Section 6.4 extension) serves its output here —
# never uploaded to Drive, so no write scope is ever requested. Created and
# mounted eagerly since StaticFiles needs the directory to exist at import
# time; get_settings() is deliberately re-read (not cached) so a mid-session
# GENERATED_RESUME_DIR change in .env would need a restart to take effect,
# same as every other adapter-construction-time setting in this project.
GENERATED_RESUMES_DIR = Path(get_settings().generated_resume_dir)
GENERATED_RESUMES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="CutOff")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/generated_resumes", StaticFiles(directory=str(GENERATED_RESUMES_DIR)), name="generated_resumes")

# `main.py` already calls db.init_db() before starting uvicorn, but the API
# must not *depend* on that — importing this module directly (a test's
# TestClient, or `uvicorn cutoff.app.server:app`) is a legitimate way to run
# it too, and CREATE TABLE IF NOT EXISTS is idempotent, so calling it again
# here is free insurance, not a real double-init.
db.init_db(get_settings().db_path)

# Section 10's rule table, in bubble-strip display order.
BUBBLE_CRITERIA = [
    ("gpa", "GPA", "min_gpa"),
    ("branch", "Branch", "branches_allowed"),
    ("backlogs", "Backlogs", "max_active_backlogs"),
    ("batch", "Batch", "batch_years"),
    ("pct10", "10th %", "min_10th_pct"),
    ("pct12", "12th %", "min_12th_pct"),
]

@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


def _conn():
    return db.get_connection(get_settings().db_path)


def _all_drives() -> list[Drive]:
    rows = _conn().execute("SELECT blob FROM drives").fetchall()
    return [Drive.model_validate_json(r["blob"]) for r in rows]


def _deadline_countdown(drive: Drive) -> str | None:
    if drive.deadline is None:
        return None
    return drive.deadline.isoformat()


def _bubbles(drive: Drive) -> list[dict]:
    """Section 16's criteria bubble strip: filled=passed, hollow+slash=failed,
    half-filled=unclear, dotted=not stated. Derived from the verdict's reason
    codes rather than a full per-rule eligibility re-run (Section 0 rule 9 —
    the simpler option: `Verdict` already carries FAIL reason codes 1:1 with
    bubbles; NEEDS_REVIEW's `questions` aren't coded per-rule, so every
    *stated* criterion shows amber together rather than pinpointing which
    one — noted as a simplification in NOTES.md)."""
    criteria = drive.criteria
    verdict = drive.last_verdict

    bubbles = []
    for key, label, field in BUBBLE_CRITERIA:
        value = getattr(criteria, field, None)
        stated = bool(value) if isinstance(value, (list,)) else value is not None
        if not stated:
            state = "not_stated"
        elif verdict == "NOT_ELIGIBLE":
            state = "failed"
        elif verdict == "NEEDS_REVIEW":
            state = "unclear"
        else:
            state = "passed"
        bubbles.append({"key": key, "label": label, "state": state})
    return bubbles


def _column_for(drive: Drive) -> str:
    if drive.status == "CANCELLED":
        return "closed"
    if drive.registered:
        return "closed"
    verdict = drive.last_verdict
    if verdict == "ELIGIBLE":
        return "register"
    if verdict == "NEEDS_REVIEW":
        return "your_call"
    if verdict == "NOT_ELIGIBLE":
        return "not_for_you"
    return "your_call"


def _drive_summary(drive: Drive) -> dict:
    return {
        "drive_id": drive.drive_id,
        "company": drive.company,
        "role": drive.role,
        "version": drive.version,
        "status": drive.status,
        "verdict": drive.last_verdict,
        "deadline": _deadline_countdown(drive),
        "registered": drive.registered,
        "bubbles": _bubbles(drive),
    }


_WATCHOUT_RE = re.compile(r'Watch out: a message about "(.+?)" looks suspicious \((.+?)\)\.')


def _watchouts() -> list[dict]:
    rows = _conn().execute(
        "SELECT idempotency_key, payload, created_at FROM actions "
        "WHERE app = 'telegram' AND kind = 'send_msg' ORDER BY created_at DESC"
    ).fetchall()
    items = []
    for r in rows:
        payload = json.loads(r["payload"])
        text = payload.get("text", "")
        m = _WATCHOUT_RE.match(text)
        if m:
            items.append({
                "key": r["idempotency_key"], "company_hint": m.group(1),
                "signals": m.group(2), "created_at": r["created_at"],
            })
    return items


@app.get("/api/drives")
def get_drives() -> dict:
    """Drives grouped by verdict (Section 16's five board columns)."""
    grouped: dict[str, list[dict]] = {"register": [], "your_call": [], "not_for_you": [], "watch_out": [], "closed": []}
    for drive in _all_drives():
        grouped[_column_for(drive)].append(_drive_summary(drive))
    grouped["watch_out"] = _watchouts()
    for key in ("register", "your_call", "not_for_you", "closed"):
        grouped[key].sort(key=lambda d: d["deadline"] or "", reverse=False)
    return grouped


@app.get("/api/drives/{drive_id}")
def get_drive_detail(drive_id: str) -> dict:
    conn = _conn()
    row = conn.execute("SELECT blob FROM drives WHERE drive_id = ?", (drive_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="drive not found")
    drive = Drive.model_validate_json(row["blob"])

    history_rows = conn.execute(
        "SELECT version, diff, source_message_id, timestamp FROM drive_history "
        "WHERE drive_id = ? ORDER BY id ASC", (drive_id,),
    ).fetchall()
    history = [
        {
            "version": h["version"], "source_message_id": h["source_message_id"],
            "timestamp": h["timestamp"], **json.loads(h["diff"]),
        }
        for h in history_rows
    ]

    action_rows = conn.execute(
        "SELECT action_id, idempotency_key, app, kind, tier, status, attempts, last_error, payload, result "
        "FROM actions WHERE drive_id = ? ORDER BY rowid ASC", (drive_id,),
    ).fetchall()
    actions = [
        {
            "action_id": a["action_id"], "idempotency_key": a["idempotency_key"], "app": a["app"],
            "kind": a["kind"], "tier": a["tier"], "status": a["status"], "attempts": a["attempts"],
            "last_error": a["last_error"], "payload": json.loads(a["payload"]),
            "result": json.loads(a["result"]) if a["result"] else None,
        }
        for a in action_rows
    ]

    prefilled_form_url = None
    for a in actions:
        if a["app"] == "telegram":
            text = a["payload"].get("text", "")
            for line in text.splitlines():
                if "Form (pre-filled" in line and "http" in line:
                    prefilled_form_url = line.split(":", 1)[1].strip()
                    break

    settings = get_settings()
    sheet_url = f"https://docs.google.com/spreadsheets/d/{settings.sheet_id}/edit" if settings.sheet_id else None

    return {
        "drive": _drive_summary(drive),
        "criteria": drive.criteria.model_dump(mode="json"),
        "form_url": drive.form_url,
        "prefilled_form_url": prefilled_form_url,
        "events": [e.model_dump(mode="json") for e in drive.events],
        "history": history,
        "actions": actions,
        "prep_intel": drive.prep_intel,
        "resolved_resume_pick": drive.resolved_resume_pick,
        "platform_links": {
            "sheet_url": sheet_url,
            "calendar_url": "https://calendar.google.com",
            "telegram_url": "https://t.me/hackathon_cutoff_bot",
        }
    }


@app.get("/api/runs/latest")
def get_latest_run() -> dict:
    run_id = trace.latest_run_id()
    if run_id is None:
        return {"run_id": None, "spans": []}
    return {"run_id": run_id, "spans": trace.list_spans(run_id)}


@app.get("/api/trace/{run_id}")
def get_trace(run_id: str) -> dict:
    spans = trace.list_spans(run_id)
    if not spans:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "spans": spans}


@app.get("/api/health")
def get_health() -> dict:
    """Section 16 extension: makes the pipeline's own self-healing behavior
    visible rather than just claimed — a circuit breaker per app that now
    persists across polls (found live: it used to reset every 15 seconds,
    see NOTES.md), and a real query for actions that hit a transient
    failure and recovered on retry. Never fabricated — an empty list here
    honestly means nothing has needed to self-heal yet."""
    breaker = executor.shared_breaker(get_settings().db_path)
    circuits_by_app = breaker.snapshot()
    circuits = [
        {
            "app": app_name,
            "state": "open" if circuits_by_app.get(app_name, {}).get("open") else "closed",
            "consecutive_failures": circuits_by_app.get(app_name, {}).get("consecutive_failures", 0),
            "opened_until": circuits_by_app.get(app_name, {}).get("opened_until"),
        }
        for app_name in HEALTH_APPS
    ]

    rows = _conn().execute(
        "SELECT action_id, app, kind, drive_id, attempts, status, updated_at FROM actions "
        "WHERE attempts > 1 AND status IN ('VERIFIED', 'DONE') ORDER BY updated_at DESC LIMIT 10"
    ).fetchall()
    self_healed = [dict(r) for r in rows]

    return {"circuits": circuits, "self_healed": self_healed}


DEMO_STUDENT_PROFILE = StudentProfile(
    name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
    gpa=7.42, active_backlogs=0, pct_10th=91, pct_12th=86, batch_year=2026, placed_status="UNPLACED",
)
DEMO_COLLEGE_POLICY = CollegePolicy(
    college_domain="college.edu", career_office_senders=["careers.demo.college@gmail.com"],
    one_offer_rule=True, dream_multiplier=1.5,
)
DEMO_RESUMES = [
    ResumeFile(file_id="r1", name="resume_SDE.pdf", web_view_link="https://drive.example/r1"),
    ResumeFile(file_id="r2", name="resume_DATA.pdf", web_view_link="https://drive.example/r2"),
    ResumeFile(file_id="r3", name="resume_CORE.pdf", web_view_link="https://drive.example/r3"),
    ResumeFile(file_id="r4", name="resume_BUSINESS.pdf", web_view_link="https://drive.example/r4"),
    ResumeFile(file_id="r5", name="resume_DEFAULT.pdf", web_view_link="https://drive.example/r5"),
]


def _build_demo_state() -> dict:
    """MODE=fake has no live mail source of its own (main.py's worker thread
    just heartbeats), so these demo endpoints are how the dashboard gets fed
    without any real accounts — Section 0's Phase 5 promise. Kept as one
    mutable dict (not several module globals) so `/api/demo/reset` can swap
    every piece atomically, including rebuilding the FaultInjector wrappers
    around the *new* underlying fakes rather than the discarded ones."""
    calendar = FakeCalendarStore()
    sheets = FakeSheetStore(DEMO_STUDENT_PROFILE, DEMO_COLLEGE_POLICY)
    messenger = FakeMessenger()
    chaos_rules = {"calendar": {}, "telegram": {}, "sheets": {}}
    return {
        "mail": FakeMailSource(),
        "calendar": calendar,
        "sheets": sheets,
        "messenger": messenger,
        "files": FakeFileStore(DEMO_RESUMES),
        "chaos_rules": chaos_rules,
        # These wrapped versions (not the raw fakes above) are what's handed
        # to the pipeline/executor, so a chaos toggle actually bites; the raw
        # fakes stay reachable for anything that shouldn't fail (e.g. mail).
        "calendar_w": faults.FaultInjector(
            calendar, chaos_rules["calendar"], methods=("upsert_event", "cancel_event", "get_event", "list_exam_events")
        ),
        "telegram_w": faults.FaultInjector(messenger, chaos_rules["telegram"], methods=("send", "edit")),
        "sheets_w": faults.FaultInjector(
            sheets, chaos_rules["sheets"], methods=("upsert_drive_row", "read_drive_row", "read_profile", "read_policy")
        ),
    }


def _load_master_profile(settings):
    """Read fresh on every demo call (not cached at import time) so editing
    config/master_profile.yaml takes effect on the next demo click without a
    server restart -- useful mid-hackathon iteration. None disables dynamic
    resume generation entirely; the demo path then behaves exactly as it did
    before this feature existed."""
    from cutoff.pipeline.master_profile import load_master_profile

    return load_master_profile(settings.master_profile_path)


_demo = _build_demo_state()
# A module-level indirection (not a hardcoded `extract_notice` at the call
# site) so tests can monkeypatch this to a stub and drive /api/demo/send
# end-to-end without ever calling the real LLM (Section 0 rule 3).
_demo_extract_fn = extract_notice


class DemoSendRequest(BaseModel):
    preset: str


class DemoChaosRequest(BaseModel):
    target: str  # "calendar" | "telegram" | "sheets" | "clear"


def _require_demo_mode(settings) -> None:
    if not (settings.is_fake and settings.demo_mode):
        raise HTTPException(status_code=403, detail="demo endpoints require MODE=fake and DEMO_MODE=1")


def _make_demo_email(
    subject: str, body: str, thread_key: str, *,
    from_addr: str = "Career Office <careers.demo.college@gmail.com>",
    attachment: tuple[str, bytes] | None = None,
) -> tuple[EmailMessage, dict[str, bytes]]:
    # A fixed thread_id per company (not a random one) is what lets a
    # "correction" or "shortlist" preset resolve against the same drive a
    # "new_drive" preset already created, regardless of how much real time
    # passes between demo button clicks.
    attachments_meta, attachments_bytes = [], {}
    if attachment:
        filename, data = attachment
        attachments_meta.append(AttachmentMeta(attachment_id=filename, filename=filename, mime_type="application/pdf", size_bytes=len(data)))
        attachments_bytes[filename] = data
    msg = EmailMessage(
        message_id=uuid.uuid4().hex, thread_id=f"demo-{thread_key}", from_addr=from_addr,
        subject=subject, body_text=body, received_at=datetime.now(timezone.utc), attachments=attachments_meta,
    )
    return msg, attachments_bytes


def _shortlist_pdf_bytes() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    y = height - 50
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, y, "Meridian Robotics - Software Engineer Shortlist")
    y -= 30
    c.setFont("Helvetica", 11)
    for line in [
        "Roll No          Name                Branch",
        "21BCS012         Aman Verma          CSE",
        f"{DEMO_STUDENT_PROFILE.roll_no}         {DEMO_STUDENT_PROFILE.name}          {DEMO_STUDENT_PROFILE.branch}",
        "21BCS078         Kabir Singh         CSE",
    ]:
        c.drawString(50, y, line)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


def _demo_preset(key: str) -> tuple[EmailMessage, dict[str, bytes]]:
    if key == "new_drive_1":
        return _make_demo_email(
            "Campus Drive: Meridian Robotics - Software Engineer (2026 Batch)",
            "Dear Students,\n\nMeridian Robotics is visiting campus for the Software Engineer role "
            "(CTC 11 LPA).\nEligibility: B.Tech CSE, IT, ECE | CGPA 7.0 and above | No active backlogs.\n"
            "Register here: https://forms.gle/demoMeridian by 11:59 PM, tomorrow.\n\nRegards,\nCareer Office",
            "meridian",
        )
    if key == "new_drive_2":
        # Deliberately a NEEDS_REVIEW case for demo variety: a generic
        # aggregate percentage with no 10th/12th mention always needs a
        # human, since the eligibility engine has no field for it to check.
        return _make_demo_email(
            "Campus Drive: Vantage Retail - Business Analyst (2026 Batch)",
            "Dear Students,\n\nVantage Retail is hiring Business Analysts (CTC 9 LPA).\n"
            "Eligibility: CSE, IT | Minimum 60% aggregate throughout academics | No active backlogs.\n"
            "Register here: https://forms.gle/demoVantage by 11:59 PM, tomorrow.\n\nRegards,\nCareer Office",
            "vantage",
        )
    if key == "new_drive_3":
        return _make_demo_email(
            "Campus Drive: Kestrel Robotics - Firmware Engineer (2026 Batch)",
            "Dear Students,\n\nKestrel Robotics is hiring Firmware Engineers (CTC 14 LPA).\n"
            "Eligibility: ECE, EEE only | CGPA 7.0 and above | No active backlogs.\n"
            "Register here: https://forms.gle/demoKestrel by 11:59 PM, tomorrow.\n\nRegards,\nCareer Office",
            "kestrel",
        )
    if key == "correction":
        return _make_demo_email(
            "Re: Campus Drive: Meridian Robotics - Software Engineer (2026 Batch)",
            "Dear Students,\n\nCorrection: the Meridian Robotics Software Engineer drive is now open to "
            "CSE and IT only -- ECE is no longer eligible. All other eligibility, deadline, and "
            "registration details are unchanged.\n\nRegards,\nCareer Office",
            "meridian",
        )
    if key == "scam":
        return _make_demo_email(
            "CONGRATULATIONS! Pay registration fee to confirm your seat",
            "You have been PRE-SELECTED for the Software Engineer role. Pay a refundable registration "
            "fee of Rs 999 via UPI to confirm@fastpay-verify.example within 1 hour or lose this "
            "opportunity. Selection guaranteed.",
            "scam", from_addr="HR Team <hr@fastpay-verify.example>",
        )
    if key == "shortlist":
        return _make_demo_email(
            "Shortlist: Meridian Robotics - Software Engineer",
            "Dear Students,\n\nPlease find attached the shortlist for the Meridian Robotics Software "
            "Engineer drive.\n\nRegards,\nCareer Office",
            "meridian", attachment=("shortlist.pdf", _shortlist_pdf_bytes()),
        )
    raise HTTPException(status_code=400, detail=f"unknown demo preset: {key!r}")


@app.get("/api/config")
def get_config() -> dict:
    """So the frontend knows whether to render the demo controls panel at
    all — never assume, always ask, since MODE/DEMO_MODE are server-side."""
    settings = get_settings()
    return {"mode": settings.mode, "demo_mode": settings.demo_mode}


@app.post("/api/demo/send")
def demo_send(body: DemoSendRequest) -> dict:
    """Feeds one fixture email through the real pipeline — real LLM
    extraction included, not a canned response — exactly like a live Gmail
    poll would, but against the in-memory demo fakes instead of real
    accounts. This is Phase 5's missing demo-endpoint promise (see
    cutoff.main's MODE=fake docstring)."""
    settings = get_settings()
    _require_demo_mode(settings)

    msg, attachments = _demo_preset(body.preset)
    _demo["mail"].deliver(msg, attachments)

    ctx = run.PipelineContext(
        mail=_demo["mail"], files=_demo["files"], extract_fn=_demo_extract_fn, api_key=settings.llm_api_key,
        model=settings.llm_model, timezone_name=settings.timezone, db_path=settings.db_path,
        provider=settings.llm_provider, base_url=settings.llm_base_url,
        calendar=_demo["calendar_w"], sheets=_demo["sheets_w"],
        master_profile=_load_master_profile(settings),
        generated_resume_dir=settings.generated_resume_dir, public_base_url=settings.public_base_url,
        enable_prep_intel=settings.enable_prep_intel,
    )
    result = run.process_message(msg, ctx, profile=DEMO_STUDENT_PROFILE, policy=DEMO_COLLEGE_POLICY, now=datetime.now(timezone.utc))

    exec_adapters = Adapters(
        sheets=_demo["sheets_w"], calendar=_demo["calendar_w"], messenger=_demo["telegram_w"], files=_demo["files"],
    )
    breaker = executor.shared_breaker(settings.db_path)
    executor.run_pending(settings.db_path, exec_adapters, breaker)

    return {
        "run_id": result.run_id, "notice_type": result.notice_type,
        "drive_id": result.drive_id, "verdict": result.verdict, "note": result.note,
    }


@app.post("/api/demo/chaos")
def demo_chaos(body: DemoChaosRequest) -> dict:
    """Toggles fault injection on the demo calendar/telegram/sheets fakes so
    the next action(s) through them fail with a real (injected) 500 —
    driving the same retry/backoff/circuit-breaker path the Self-Healing tab
    already reports on, live, on demand. `target="clear"` turns it back off."""
    settings = get_settings()
    _require_demo_mode(settings)

    rules = _demo["chaos_rules"]
    if body.target == "clear":
        for r in rules.values():
            r.clear()
    elif body.target in rules:
        for r in rules.values():
            r.clear()
        rules[body.target]["http_500_rate"] = 1.0
    else:
        raise HTTPException(status_code=400, detail=f"unknown chaos target: {body.target!r}")

    return {"chaos_rules": {k: dict(v) for k, v in rules.items()}}


@app.post("/api/demo/reset")
def demo_reset() -> dict:
    """Clears every drive/action/trace this demo session created, plus the
    in-memory fakes, so the board starts empty again before a take — the
    fake-mode analogue of scripts/reset_demo_state.py (which touches the
    real Sheet/Calendar and has no place being called from MODE=fake)."""
    settings = get_settings()
    _require_demo_mode(settings)

    conn = _conn()
    for table in ("processed_messages", "drives", "drive_threads", "drive_history", "actions", "approvals", "traces"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    _demo.clear()
    _demo.update(_build_demo_state())
    return {"ok": True}


@app.get("/api/eval/latest")
def get_latest_eval() -> dict:
    if not EVAL_RESULTS_DIR.exists():
        return {"runs": []}
    runs = []
    by_label: dict[str, Path] = {}
    for path in sorted(EVAL_RESULTS_DIR.glob("*.json")):
        label = path.stem.split("_", 1)[1] if "_" in path.stem else path.stem
        by_label[label] = path  # later (sorted-by-timestamp) file wins per label
    for label, path in by_label.items():
        data = json.loads(path.read_text())
        runs.append({
            "label": label, "split": data.get("split"), "chaos": data.get("chaos"),
            "timestamp": data.get("timestamp"), "summary": data.get("summary"),
            "top_failure_clusters": data.get("top_failure_clusters", []),
        })
    runs.sort(key=lambda r: r["timestamp"] or "")
    return {"runs": runs}
