"""Exercises the eval harness itself (Section 15) against the dev_014 fixture
with a stubbed extract_fn — never the real LLM. Confirms runner.py's grading
(drive state + required/forbidden effects) agrees with what
test_pipeline_dev014.py already proved about the pipeline directly, and that
grading survives the crash_mid_run chaos profile."""
import json

from cutoff.models import Criteria, EmailMessage, Evidence, Notice
from eval.metrics import ScenarioResult, summarize, top_failure_clusters
from eval.runner import FIXTURES_ROOT, load_chaos_profiles, run_scenario
from tests.test_pipeline_dev014 import _stub_extract

SCENARIO_DIR = FIXTURES_ROOT / "dev" / "dev_014_revision_branch_removed"


def _fail_if_called(*args, **kwargs):
    raise AssertionError("extract_fn should never be called for a non-allowlisted, unrecognized sender")


def test_dev_014_passes_end_to_end_through_the_eval_harness():
    result = run_scenario(SCENARIO_DIR, chaos_name="none", chaos_rules={}, use_cache=False, extract_fn=_stub_extract)
    assert result.error is None
    assert result.has_gold_verdict
    assert result.verdict_correct
    assert not result.dangerous_error
    assert result.required_effects_present == result.required_effects_total > 0
    assert result.forbidden_effects_hit == []
    assert result.duplicate_effects == 0
    assert result.passed


def test_dev_014_survives_crash_mid_run_chaos():
    chaos_rules = load_chaos_profiles()["crash_mid_run"]
    result = run_scenario(SCENARIO_DIR, chaos_name="crash_mid_run", chaos_rules=chaos_rules,
                           use_cache=False, extract_fn=_stub_extract)
    assert result.error is None
    assert result.passed
    assert result.duplicate_effects == 0


def test_dev_014_survives_kitchen_sink_chaos():
    chaos_rules = load_chaos_profiles()["kitchen_sink"]
    result = run_scenario(SCENARIO_DIR, chaos_name="kitchen_sink", chaos_rules=chaos_rules,
                           use_cache=False, extract_fn=_stub_extract)
    assert result.error is None
    assert result.passed


def _dev_023_stub_extract(msg: EmailMessage, body_clean: str, quoted_history: str, attachment_text: str, **kwargs) -> Notice:
    if msg.message_id == "m1":
        return Notice(
            notice_type="NEW_DRIVE", company="Solberg Systems", role="QA Engineer",
            criteria=Criteria(min_gpa=7.0, gpa_inclusive=True, branches_allowed=["CSE", "IT"]),
            evidence=[
                Evidence(field="company", quote="Solberg Systems"),
                Evidence(field="role", quote="QA Engineer"),
                Evidence(field="criteria.min_gpa", quote="CGPA 7.0 and above"),
                Evidence(field="criteria.gpa_inclusive", quote="CGPA 7.0 and above"),
                Evidence(field="criteria.branches_allowed", quote="CSE, IT"),
            ],
        )
    if msg.message_id == "m2":
        from datetime import datetime

        from cutoff.models import DriveEvent

        return Notice(
            notice_type="SCHEDULE",
            events=[DriveEvent(
                kind="TEST",
                start=datetime.fromisoformat("2026-09-18T10:00:00+05:30"),
                end=datetime.fromisoformat("2026-09-18T12:00:00+05:30"),
                location_or_link="https://meet.example.com/solberg-test",
            )],
            evidence=[Evidence(field="events", quote="10:00 AM to 12:00 PM, 18 September 2026")],
        )
    raise AssertionError(f"unexpected message_id {msg.message_id!r}")


def test_dev_023_clash_scenario_detects_the_seeded_exam_conflict():
    scenario_dir = FIXTURES_ROOT / "dev" / "dev_023_test_slot_clashes_with_exam"
    result = run_scenario(scenario_dir, chaos_name="none", chaos_rules={}, use_cache=False,
                           extract_fn=_dev_023_stub_extract)
    assert result.error is None
    assert result.passed


def test_dev_035_lookalike_sender_is_only_reachable_via_the_suspicious_query():
    """Section 6.1/12 reachability: dev_035's sender is a homoglyph lookalike
    of college.edu that's NOT in career_office_senders, so run_scenario's
    use_polling branch must route it through list_suspicious (keyword match
    on "Campus Drive"/"Career Office"), not list_new, to ever reach
    process_message at all. A stub extract_fn that raises if called proves
    the LLM is genuinely never invoked (security.check_sender rejects the
    message before extraction, per run.py)."""
    scenario_dir = FIXTURES_ROOT / "dev" / "dev_035_lookalike_reaches_via_suspicious_query"
    result = run_scenario(scenario_dir, chaos_name="none", chaos_rules={}, use_cache=False,
                           extract_fn=_fail_if_called)
    assert result.error is None
    assert result.passed
    assert result.flagged_suspicious
    assert result.forbidden_effects_hit == []


def _write_scenario(tmp_path, *, from_addr: str, subject: str, body: str, use_polling: bool):
    scenario_dir = tmp_path / "scenario"
    scenario_dir.mkdir()
    scenario = {
        "id": "synthetic", "category": "reachability_probe", "profile": "riya.json",
        "use_polling": use_polling, "now": "2026-09-05T09:00:00+05:30",
        "messages": [{
            "message_id": "m1", "thread_id": "t1", "from": from_addr,
            "received_at": "2026-09-04T11:00:00+05:30", "subject": subject, "body": body,
        }],
    }
    (scenario_dir / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    (scenario_dir / "expected.json").write_text(json.dumps({"drives": [], "required_effects": [],
                                                             "forbidden_effects": []}), encoding="utf-8")
    return scenario_dir


def test_use_polling_true_never_reaches_a_non_allowlisted_sender_with_no_keyword_match(tmp_path):
    """The true-negative complement to dev_035: a non-allowlisted sender whose
    content matches NEITHER list_new's allowlist NOR list_suspicious's
    keywords must never be processed at all under use_polling -- proving the
    routing is a genuine reachability gate, not a rubber stamp that lets
    everything through regardless."""
    scenario_dir = _write_scenario(
        tmp_path,
        from_addr="Someone <person@unrelated-domain.example>",
        subject="Lunch plans", body="Want to grab lunch later today?",
        use_polling=True,
    )
    result = run_scenario(scenario_dir, chaos_name="none", chaos_rules={}, use_cache=False,
                           extract_fn=_fail_if_called)
    assert result.error is None
    assert result.flagged_suspicious is False
    assert result.forbidden_effects_hit == []


def test_load_chaos_profiles_has_all_named_profiles():
    profiles = load_chaos_profiles()
    assert set(profiles) == {
        "none", "gmail_throttle", "calendar_flaky", "telegram_timeout",
        "duplicate_delivery", "crash_mid_run", "kitchen_sink",
    }


def test_summarize_computes_dangerous_errors_and_verdict_accuracy():
    results = [
        ScenarioResult(scenario_id="a", category="eligible", passed=True, has_gold_verdict=True, verdict_correct=True),
        ScenarioResult(scenario_id="b", category="eligible", passed=False, has_gold_verdict=True, verdict_correct=False, dangerous_error=True),
    ]
    summary = summarize(results)
    assert summary["verdict_accuracy"] == 0.5
    assert summary["dangerous_errors"] == 1


def test_top_failure_clusters_groups_by_category_and_reason():
    results = [
        ScenarioResult(scenario_id="a", category="gpa", passed=False, has_gold_verdict=True, verdict_correct=False, dangerous_error=True),
        ScenarioResult(scenario_id="b", category="gpa", passed=False, has_gold_verdict=True, verdict_correct=False, dangerous_error=True),
        ScenarioResult(scenario_id="c", category="branch", passed=True),
    ]
    clusters = top_failure_clusters(results)
    assert clusters[0]["category"] == "gpa"
    assert clusters[0]["failed_metric"] == "dangerous_error"
    assert set(clusters[0]["scenario_ids"]) == {"a", "b"}
