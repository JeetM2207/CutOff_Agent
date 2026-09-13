"""Eval harness (Section 15): runs the real pipeline against fake apps and
grades the FINAL STATE of every fake app — calendar events, sheet rows,
Telegram messages/approvals — never the agent's own report. This is what
catches silent failures.

    python -m eval.runner --split dev --chaos none --label baseline
    python -m eval.runner --split dev --chaos kitchen_sink --label baseline-chaos
    python -m eval.runner --split heldout --chaos none --label after --i-promise-no-peeking
"""
from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from cutoff import db
from cutoff.adapters import faults
from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMailSource, FakeMessenger, FakeSheetStore
from cutoff.config import get_settings
from cutoff.llm.extract import extract_notice
from cutoff.main import SUSPICIOUS_QUERY_KEYWORDS
from cutoff.models import AttachmentMeta, CalendarEvent, CollegePolicy, EmailMessage, ResumeFile, StudentProfile
from cutoff.pipeline import executor, resolve, run, timeparse
from cutoff.pipeline.executor import Adapters, SimulatedCrash

from eval.metrics import ScenarioResult, summarize, top_failure_clusters

EVAL_ROOT = Path(__file__).resolve().parent
FIXTURES_ROOT = EVAL_ROOT / "fixtures"
PROFILES_DIR = EVAL_ROOT / "profiles"
RESULTS_DIR = EVAL_ROOT / "results"
CHAOS_PROFILES_PATH = EVAL_ROOT / "chaos_profiles.yaml"

DEFAULT_RESUMES = [
    ResumeFile(file_id="r1", name="resume_SDE.pdf", web_view_link="https://drive.example/r1"),
    ResumeFile(file_id="r2", name="resume_DATA.pdf", web_view_link="https://drive.example/r2"),
    ResumeFile(file_id="r3", name="resume_CORE.pdf", web_view_link="https://drive.example/r3"),
    ResumeFile(file_id="r4", name="resume_PRODUCT.pdf", web_view_link="https://drive.example/r5"),
    ResumeFile(file_id="r5", name="resume_BUSINESS.pdf", web_view_link="https://drive.example/r6"),
    ResumeFile(file_id="r6", name="resume_OTHER.pdf", web_view_link="https://drive.example/r7"),
    ResumeFile(file_id="r7", name="resume_DEFAULT.pdf", web_view_link="https://drive.example/r4"),
]

# Plain-text content per resume, distinct enough that an eval scenario can
# assert on JD-content resume matching actually picking the right one by
# what's written — not just by role_category. Rendered to real (tiny) PDFs
# below so extract_pdf_text() has something genuine to parse, exactly like
# eval/fixtures' own shortlist PDFs (scripts/make_pdfs.py).
DEFAULT_RESUME_TEXTS = {
    "r1": "Software engineer experienced in Python, Java, distributed systems, REST APIs, "
          "Kubernetes, microservices architecture.",
    "r2": "Data analyst skilled in SQL, Excel, Tableau dashboards, statistical analysis, "
          "A/B testing, Python pandas for data wrangling.",
    "r3": "Mechanical engineering graduate. CAD design, SolidWorks, manufacturing processes, "
          "thermodynamics, materials science.",
    "r4": "Product analyst experience: roadmap prioritization, user research, feature "
          "specs, cross-functional stakeholder alignment.",
    "r5": "Business analyst with experience in market research, financial modeling, "
          "stakeholder management, PowerPoint presentations.",
    "r6": "Generalist resume covering internships across different functions, adaptable "
          "to a wide range of entry-level roles.",
    "r7": "Generalist fresher resume. Internships, coursework projects, basic programming "
          "in C and Python.",
}


def _resume_pdf_bytes(text: str) -> bytes:
    import io

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    c.setFont("Helvetica", 11)
    c.drawString(50, height - 50, text)
    c.showPage()
    c.save()
    return buf.getvalue()


DEFAULT_RESUME_CONTENTS = {file_id: _resume_pdf_bytes(text) for file_id, text in DEFAULT_RESUME_TEXTS.items()}


def load_chaos_profiles() -> dict:
    return yaml.safe_load(CHAOS_PROFILES_PATH.read_text(encoding="utf-8"))


def list_scenarios(split: str) -> list[Path]:
    split_dir = FIXTURES_ROOT / split
    if not split_dir.exists():
        return []
    return sorted(p for p in split_dir.iterdir() if p.is_dir())


def _build_email(m: dict, attachments_dir: Path) -> tuple[EmailMessage, dict[str, bytes]]:
    attachments, attachment_bytes = [], {}
    for att in m.get("attachments", []):
        attachments.append(AttachmentMeta(
            attachment_id=att["attachment_id"], filename=att["filename"],
            mime_type=att.get("mime_type", "application/pdf"),
        ))
        attachment_bytes[att["attachment_id"]] = (attachments_dir / att["filename"]).read_bytes()
    msg = EmailMessage(
        message_id=m["message_id"], thread_id=m["thread_id"], from_addr=m["from"],
        subject=m["subject"], body_text=m["body"], received_at=datetime.fromisoformat(m["received_at"]),
        attachments=attachments,
    )
    return msg, attachment_bytes


def _had_any_approval(db_path: str, drive_id: str) -> bool:
    conn = db.get_connection(db_path)
    row = conn.execute("SELECT 1 FROM approvals WHERE drive_id = ? LIMIT 1", (drive_id,)).fetchone()
    return row is not None


def _distinct_calendar_action_keys(db_path: str) -> int:
    conn = db.get_connection(db_path)
    row = conn.execute(
        "SELECT COUNT(DISTINCT idempotency_key) AS n FROM actions WHERE app = 'calendar' AND kind = 'upsert_event'"
    ).fetchone()
    return row["n"] or 0


def _check_effect(effect: dict, db_path: str, sheets, calendar, messenger) -> bool:
    app, kind = effect["app"], effect["kind"]

    if app == "telegram" and kind == "approval_voided":
        return executor.get_pending_approval(db_path, effect["drive_id"]) is None and _had_any_approval(db_path, effect["drive_id"])
    if app == "telegram" and kind == "active_approval":
        pending = executor.get_pending_approval(db_path, effect["drive_id"])
        if pending is None:
            return False
        # The `approvals` table is a generic "short token -> drive" lookup
        # shared by both real approvals (Approve/Skip) and NEEDS_REVIEW
        # questions (Yes/No) since Phase 3 — a pending row alone doesn't mean
        # an *approval* is active; it could just be the legitimate open
        # question. Only count it as a forbidden active approval when the
        # drive's current verdict is actually ELIGIBLE.
        row = sheets.read_drive_row(effect["drive_id"])
        return row is not None and row.get("verdict") == "ELIGIBLE"
    if app == "sheets" and kind == "row":
        row = sheets.read_drive_row(effect["drive_id"])
        if row is None:
            return False
        return all(row.get(k) == v for k, v in effect.items() if k not in ("app", "kind", "drive_id"))
    if app == "calendar" and kind == "active_event":
        real_id = executor.event_id_for(f"cal:{effect['drive_id']}:{effect.get('event', 'deadline')}")
        got = calendar.get_event(real_id)
        return got is not None and got.status == "confirmed"
    if app == "calendar" and kind == "cancelled_event":
        real_id = executor.event_id_for(f"cal:{effect['drive_id']}:{effect.get('event', 'deadline')}")
        got = calendar.get_event(real_id)
        return got is not None and got.status == "cancelled"
    if app == "calendar" and kind == "event_description_contains":
        real_id = executor.event_id_for(f"cal:{effect['drive_id']}:{effect.get('event', 'deadline')}")
        got = calendar.get_event(real_id)
        return got is not None and got.description is not None and effect["text"] in got.description
    if app == "telegram" and kind == "message_sent_containing":
        return any(c[0] == "send" and effect["text"] in c[1].get("text", "") for c in messenger.calls)
    if app == "calendar" and kind == "duplicate_event":
        return len(calendar._events) > _distinct_calendar_action_keys(db_path)

    raise ValueError(f"unrecognized effect kind: {app}/{kind}")


def _grade(scenario_id: str, scenario: dict, expected: dict, db_path: str, sheets, calendar, messenger,
           chaos_name: str, error: str | None, unverified_fields: list[str] | None = None) -> ScenarioResult:
    category = scenario.get("category", "uncategorized")
    result = ScenarioResult(scenario_id=scenario_id, category=category, passed=False, error=error, chaos_profile=chaos_name)
    # "scam" categories (fee/lookalike) are meant to be flagged SUSPICIOUS and
    # blocked — that's what scam_recall measures. "injection" categories are
    # the opposite test: an allowlisted email with an embedded instruction
    # should be processed NORMALLY, with the injected text ignored, not
    # blocked outright (Section 12) — counting it in scam_recall's denominator
    # would penalize the pipeline for correctly *not* raising a scam warning.
    result.is_scam_scenario = "scam" in category
    result.grounding_catches = list(dict.fromkeys(unverified_fields or []))  # dedupe, keep order

    sent_texts = [c[1].get("text", "") for c in messenger.calls if c[0] == "send"]
    result.flagged_suspicious = any(t.startswith("Watch out:") for t in sent_texts)

    if error:
        return result

    ok = True

    for gold_drive in expected.get("drives", []):
        drive_id = gold_drive["drive_id"]
        row = sheets.read_drive_row(drive_id)
        if row is None:
            ok = False
            continue

        if "verdict" in gold_drive:
            result.has_gold_verdict = True
            actual = row.get("verdict")
            correct = actual == gold_drive["verdict"]
            result.verdict_correct = correct
            ok = ok and correct
            if not correct:
                if gold_drive["verdict"] == "NOT_ELIGIBLE" and actual == "ELIGIBLE":
                    result.dangerous_error = True
                elif gold_drive["verdict"] == "ELIGIBLE" and actual == "NOT_ELIGIBLE":
                    result.missed_opportunity = True

        for key in ("version", "status"):
            if key in gold_drive:
                correct = row.get(key) == gold_drive[key]
                result.field_matches[f"{drive_id}:{key}"] = correct
                ok = ok and correct

        if "reasons" in gold_drive:
            actual_reasons = set(filter(None, (row.get("reasons") or "").split(",")))
            correct = actual_reasons == set(gold_drive["reasons"])
            result.field_matches[f"{drive_id}:reasons"] = correct
            ok = ok and correct

        if "deadline" in gold_drive:
            result.has_gold_deadline = True
            gold_dt = datetime.fromisoformat(gold_drive["deadline"]) if gold_drive["deadline"] else None
            actual_dt = datetime.fromisoformat(row["deadline"]) if row.get("deadline") else None
            correct = timeparse.deadlines_agree(gold_dt, actual_dt)
            result.deadline_correct = correct
            ok = ok and correct

        extracted = gold_drive.get("extracted_fields")
        if extracted:
            drive = resolve.get_drive(db_path, drive_id)
            criteria = drive.criteria if drive else None
            for field_name, gold_value in extracted.items():
                actual_value = getattr(criteria, field_name, None) if criteria else None
                if isinstance(gold_value, list):
                    correct = set(actual_value or []) == set(gold_value)
                else:
                    correct = actual_value == gold_value
                result.field_matches[field_name] = correct
                ok = ok and correct

        shortlist = gold_drive.get("shortlisted")
        if shortlist is False:
            # Exact match only — "SHORTLIST_REVIEW" (a name-only match still
            # needing a human look) must never be confused with the actual
            # "SHORTLISTED" claim this metric exists to catch.
            if (row.get("status") or "") == "SHORTLISTED":
                result.false_shortlisted = True
                ok = False

    for eff in expected.get("required_effects", []):
        result.required_effects_total += 1
        if _check_effect(eff, db_path, sheets, calendar, messenger):
            result.required_effects_present += 1
        else:
            ok = False

    for eff in expected.get("forbidden_effects", []):
        if _check_effect(eff, db_path, sheets, calendar, messenger):
            result.forbidden_effects_hit.append(eff)
            ok = False

    for forbidden_drive_id in expected.get("forbidden_drives", []):
        if resolve.get_drive(db_path, forbidden_drive_id) is not None:
            result.forbidden_effects_hit.append({"app": "internal", "kind": "unexpected_drive", "drive_id": forbidden_drive_id})
            ok = False

    result.duplicate_effects = max(0, len(calendar._events) - _distinct_calendar_action_keys(db_path))
    if result.duplicate_effects:
        ok = False

    result.passed = ok
    return result


def run_scenario(scenario_dir: Path, *, chaos_name: str, chaos_rules: dict, use_cache: bool,
                  extract_fn=extract_notice) -> ScenarioResult:
    # explicit encoding="utf-8": Windows' Path.read_text() otherwise defaults
    # to the console locale codepage (cp1252), which crashes on any fixture
    # containing real non-ASCII bytes -- e.g. a homoglyph domain -- found
    # live while adding dev_035 below.
    scenario = json.loads((scenario_dir / "scenario.json").read_text(encoding="utf-8"))
    expected = json.loads((scenario_dir / "expected.json").read_text(encoding="utf-8"))

    if scenario.get("skip_reason"):
        return ScenarioResult(scenario_id=scenario_dir.name, category=scenario.get("category", ""),
                               passed=True, chaos_profile=chaos_name)

    profile_data = json.loads((PROFILES_DIR / scenario["profile"]).read_text(encoding="utf-8"))
    profile_data.update(scenario.get("profile_overrides", {}))
    policy_data = json.loads((PROFILES_DIR / "policy.json").read_text(encoding="utf-8"))
    policy_data.update(scenario.get("policy_overrides", {}))

    profile = StudentProfile(**profile_data)
    policy = CollegePolicy(**policy_data)
    now = datetime.fromisoformat(scenario["now"])

    settings = get_settings()
    tmp_dir = tempfile.mkdtemp(prefix=f"cutoff_eval_{scenario_dir.name}_")
    db_path = str(Path(tmp_dir) / "cutoff.db")
    db.init_db(db_path)

    exam_events = [
        CalendarEvent(
            event_id=f"exam{i}", title=e["title"],
            start=datetime.fromisoformat(e["start"]), end=datetime.fromisoformat(e["end"]),
        )
        for i, e in enumerate(scenario.get("exam_events", []))
    ]

    # Grading (`_grade` below) must read the fakes' *real* final state, never
    # through the chaos wrapper — a FaultInjector on the verification calls
    # themselves would mean the harness's own grading step can randomly
    # crash on injected faults that have nothing to do with the pipeline
    # being tested (found live: kitchen_sink's calendar 500-rate crashed
    # `_check_effect`'s own `get_event` call). Keep raw references for
    # grading; only the pipeline's own adapters go through `faults.wrap_*`.
    raw_calendar = FakeCalendarStore(exam_events=exam_events)
    raw_messenger = FakeMessenger()

    mail = faults.wrap_mail(FakeMailSource(), chaos_rules.get("gmail", {}))
    sheets = FakeSheetStore(profile, policy)
    calendar = faults.wrap_calendar(raw_calendar, chaos_rules.get("calendar", {}))
    files = FakeFileStore(DEFAULT_RESUMES, contents=DEFAULT_RESUME_CONTENTS)
    messenger = faults.wrap_telegram(raw_messenger, chaos_rules.get("telegram", {}))

    ctx = run.PipelineContext(
        mail=mail, files=files, extract_fn=extract_fn, api_key=settings.llm_api_key,
        model=settings.llm_model, timezone_name=settings.timezone, db_path=db_path, use_llm_cache=use_cache,
        provider=settings.llm_provider, base_url=settings.llm_base_url,
        calendar=calendar,
    )

    error = None
    unverified_fields: list[str] = []
    try:
        attachments_dir = scenario_dir / "attachments"
        delivered: dict[str, EmailMessage] = {}
        for m in scenario["messages"]:
            msg, attachment_bytes = _build_email(m, attachments_dir)
            mail.deliver(msg, attachment_bytes)
            delivered[msg.message_id] = msg

        if scenario.get("use_polling"):
            # Section 6.1/12 reachability test: route delivered messages
            # through the SAME list_new/list_suspicious polling main.py's
            # worker loop actually uses, instead of handing every message
            # straight to process_message() regardless of sender. Proves a
            # message is reachable at all -- not just that process_message()
            # handles it correctly once handed one. Opt-in via
            # scenario["use_polling"] so the other ~30 scenarios (which never
            # cared about reachability, only about correct handling) keep
            # their exact prior behavior untouched.
            since = min(msg.received_at for msg in delivered.values()) - timedelta(days=1)
            reachable_ids: list[str] = []
            seen_ids: set[str] = set()
            for ref in mail.list_new(policy.career_office_senders, since):
                seen_ids.add(ref.message_id)
                reachable_ids.append(ref.message_id)
            for ref in mail.list_suspicious(SUSPICIOUS_QUERY_KEYWORDS, since):
                if ref.message_id not in seen_ids:
                    seen_ids.add(ref.message_id)
                    reachable_ids.append(ref.message_id)
            for mid in reachable_ids:
                msg = mail.get(mid)
                run_result = run.process_message(msg, ctx, profile=profile, policy=policy, now=now)
                unverified_fields.extend(run_result.unverified_fields)
        else:
            for msg in delivered.values():
                run_result = run.process_message(msg, ctx, profile=profile, policy=policy, now=now)
                unverified_fields.extend(run_result.unverified_fields)

        adapters = Adapters(sheets=sheets, calendar=calendar, messenger=messenger)
        raise_after = chaos_rules.get("executor", {}).get("raise_after_actions")
        try:
            executor.run_pending(db_path, adapters, raise_after_actions=raise_after,
                                  lease_seconds=0 if raise_after is not None else executor.LEASE_SECONDS)
        except SimulatedCrash:
            # "the runner discards all in-memory objects, then constructs a fresh
            # pipeline on the same SQLite file and fake-app state, and continues"
            # (Section 15.4) — the fakes (external services) persist; only a new
            # CircuitBreaker is created, which run_pending already does internally.
            executor.run_pending(db_path, adapters, lease_seconds=0)
    except Exception as e:  # noqa: BLE001 — a scenario blowing up is itself a finding
        error = f"{type(e).__name__}: {e}"

    return _grade(scenario_dir.name, scenario, expected, db_path, sheets, raw_calendar, raw_messenger, chaos_name,
                  error, unverified_fields=unverified_fields)


def run_split(split: str, chaos_name: str, *, use_cache: bool = True, extract_fn=extract_notice) -> list[ScenarioResult]:
    chaos_rules = load_chaos_profiles().get(chaos_name, {})
    return [
        run_scenario(d, chaos_name=chaos_name, chaos_rules=chaos_rules, use_cache=use_cache, extract_fn=extract_fn)
        for d in list_scenarios(split)
    ]


def _print_markdown_table(summary: dict, label: str) -> None:
    print(f"\n## Eval results — {label}\n")
    rows = [
        ("Scenarios", f"{summary['passed']}/{summary['total_scenarios']}"),
        ("Verdict accuracy", _pct(summary["verdict_accuracy"])),
        ("Dangerous errors", summary["dangerous_errors"]),
        ("Missed opportunities", summary["missed_opportunities"]),
        ("Deadline accuracy", _pct(summary["deadline_accuracy"])),
        ("Duplicate effects", summary["duplicate_effects"]),
        ("Forbidden effects", summary["forbidden_effects"]),
        ("Required effects", _pct(summary["required_effects"])),
        ("False shortlisted", summary["false_shortlisted"]),
        ("Scam recall", _pct(summary["scam_recall"])),
        ("Scam false alarms", summary["scam_false_alarms"]),
        ("Grounding catches", summary["grounding_catches"]),
    ]
    print("| Metric | Value |")
    print("|---|---|")
    for name, value in rows:
        print(f"| {name} | {value} |")
    if summary["chaos_recovery_by_profile"]:
        print("\n| Chaos profile | Recovery |")
        print("|---|---|")
        for profile, ratio in summary["chaos_recovery_by_profile"].items():
            print(f"| {profile} | {_pct(ratio)} |")


def _pct(ratio: float | None) -> str:
    return "n/a" if ratio is None else f"{ratio * 100:.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="CutOff eval runner (Section 15)")
    parser.add_argument("--split", choices=["dev", "heldout"], default="dev")
    parser.add_argument("--chaos", default="none")
    parser.add_argument("--label", required=True)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--i-promise-no-peeking", action="store_true")
    args = parser.parse_args()

    if args.split == "heldout" and not args.i_promise_no_peeking:
        raise SystemExit(
            "Refusing --split heldout without --i-promise-no-peeking. "
            "The held-out set exists to catch overfitting — don't look at it while iterating."
        )

    results = run_split(args.split, args.chaos, use_cache=not args.no_cache)
    summary = summarize(results)
    clusters = top_failure_clusters(results)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"{timestamp}_{args.label}.json"
    out_path.write_text(json.dumps({
        "label": args.label, "split": args.split, "chaos": args.chaos, "timestamp": timestamp,
        "summary": summary, "top_failure_clusters": clusters,
        "scenarios": [
            {
                "scenario_id": r.scenario_id, "category": r.category, "passed": r.passed, "error": r.error,
                "verdict_correct": r.verdict_correct if r.has_gold_verdict else None,
                "dangerous_error": r.dangerous_error, "missed_opportunity": r.missed_opportunity,
                "forbidden_effects_hit": r.forbidden_effects_hit,
                "duplicate_effects": r.duplicate_effects,
            }
            for r in results
        ],
    }, indent=2, default=str))

    _print_markdown_table(summary, args.label)
    if clusters:
        print(f"\n## Top failure clusters — {args.label}\n")
        for c in clusters:
            print(f"- **{c['category']} / {c['failed_metric']}** ({c['count']}): {', '.join(c['scenario_ids'])}")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
