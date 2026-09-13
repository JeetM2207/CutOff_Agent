"""Metrics (Section 15.5) computed from a list of ScenarioResult, produced by
grading the real pipeline's effect on fake-app final state — never from the
agent's own report."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScenarioResult:
    scenario_id: str
    category: str
    passed: bool
    error: str | None = None

    has_gold_verdict: bool = False
    verdict_correct: bool = False
    dangerous_error: bool = False       # predicted ELIGIBLE, gold NOT_ELIGIBLE
    missed_opportunity: bool = False    # predicted NOT_ELIGIBLE, gold ELIGIBLE

    has_gold_deadline: bool = False
    deadline_correct: bool = False

    field_matches: dict[str, bool] = field(default_factory=dict)  # field -> correct?

    duplicate_effects: int = 0
    forbidden_effects_hit: list[dict] = field(default_factory=list)
    required_effects_total: int = 0
    required_effects_present: int = 0

    false_shortlisted: bool = False

    is_scam_scenario: bool = False
    flagged_suspicious: bool = False  # did the run raise a SUSPICIOUS signal anywhere?

    grounding_catches: list[str] = field(default_factory=list)

    chaos_profile: str = "none"


def summarize(results: list[ScenarioResult]) -> dict:
    total = len(results)
    with_gold_verdict = [r for r in results if r.has_gold_verdict]
    with_gold_deadline = [r for r in results if r.has_gold_deadline]
    scam_scenarios = [r for r in results if r.is_scam_scenario]
    non_scam_scenarios = [r for r in results if not r.is_scam_scenario]

    field_totals: dict[str, list[int]] = {}
    for r in results:
        for field_name, correct in r.field_matches.items():
            totals = field_totals.setdefault(field_name, [0, 0])
            totals[0] += 1
            totals[1] += int(correct)

    return {
        "total_scenarios": total,
        "passed": sum(1 for r in results if r.passed),
        "verdict_accuracy": _ratio(sum(r.verdict_correct for r in with_gold_verdict), len(with_gold_verdict)),
        "dangerous_errors": sum(1 for r in results if r.dangerous_error),
        "missed_opportunities": sum(1 for r in results if r.missed_opportunity),
        "deadline_accuracy": _ratio(sum(r.deadline_correct for r in with_gold_deadline), len(with_gold_deadline)),
        "field_accuracy": {
            name: _ratio(correct, seen) for name, (seen, correct) in field_totals.items()
        },
        "duplicate_effects": sum(r.duplicate_effects for r in results),
        "forbidden_effects": sum(1 for r in results if r.forbidden_effects_hit),
        "required_effects": _ratio(
            sum(r.required_effects_present for r in results),
            sum(r.required_effects_total for r in results),
        ),
        "false_shortlisted": sum(1 for r in results if r.false_shortlisted),
        "scam_recall": _ratio(sum(r.flagged_suspicious for r in scam_scenarios), len(scam_scenarios)),
        "scam_false_alarms": sum(1 for r in non_scam_scenarios if r.flagged_suspicious),
        "grounding_catches": sum(len(r.grounding_catches) for r in results),
        "chaos_recovery_by_profile": _chaos_recovery(results),
        "errors": [{"scenario_id": r.scenario_id, "error": r.error} for r in results if r.error],
    }


def _chaos_recovery(results: list[ScenarioResult]) -> dict:
    profiles: dict[str, list[bool]] = {}
    for r in results:
        profiles.setdefault(r.chaos_profile, []).append(r.passed)
    return {profile: _ratio(sum(passes), len(passes)) for profile, passes in profiles.items()}


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def top_failure_clusters(results: list[ScenarioResult], limit: int = 5) -> list[dict]:
    """Groups failures by (category, first failed-metric reason) — the
    'fix clusters, not individual cases' view (Section 15.6)."""
    clusters: dict[tuple[str, str], list[str]] = {}
    for r in results:
        if r.passed:
            continue
        reason = _failure_reason(r)
        clusters.setdefault((r.category, reason), []).append(r.scenario_id)

    ranked = sorted(clusters.items(), key=lambda kv: -len(kv[1]))
    return [
        {"category": cat, "failed_metric": reason, "count": len(ids), "scenario_ids": ids}
        for (cat, reason), ids in ranked[:limit]
    ]


def _failure_reason(r: ScenarioResult) -> str:
    if r.error:
        return "error"
    if r.has_gold_verdict and not r.verdict_correct:
        return "dangerous_error" if r.dangerous_error else ("missed_opportunity" if r.missed_opportunity else "verdict_mismatch")
    if r.has_gold_deadline and not r.deadline_correct:
        return "deadline_mismatch"
    if r.forbidden_effects_hit:
        return "forbidden_effect"
    if r.required_effects_present < r.required_effects_total:
        return "missing_required_effect"
    if r.false_shortlisted:
        return "false_shortlisted"
    if r.is_scam_scenario and not r.flagged_suspicious:
        return "missed_scam"
    if not r.is_scam_scenario and r.flagged_suspicious:
        return "false_alarm"
    return "other"
