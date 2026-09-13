"""eval/compare.py (Section 15.6's before/after table)."""
import json

import pytest

import eval.compare as compare_mod


@pytest.fixture()
def results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(compare_mod, "RESULTS_DIR", tmp_path)
    return tmp_path


def _write_result(results_dir, timestamp: str, label: str, summary: dict):
    path = results_dir / f"{timestamp}_{label}.json"
    path.write_text(json.dumps({"label": label, "summary": summary}))
    return path


def test_compare_prints_both_labels_side_by_side(results_dir):
    _write_result(results_dir, "20260910T000000Z", "baseline", {
        "verdict_accuracy": 0.7, "dangerous_errors": 2, "missed_opportunities": 1,
        "deadline_accuracy": 0.9, "duplicate_effects": 0, "forbidden_effects": 1,
        "required_effects": 0.8, "false_shortlisted": 0, "scam_recall": 1.0,
        "scam_false_alarms": 0, "grounding_catches": 3,
    })
    _write_result(results_dir, "20260910T010000Z", "after", {
        "verdict_accuracy": 0.95, "dangerous_errors": 0, "missed_opportunities": 0,
        "deadline_accuracy": 1.0, "duplicate_effects": 0, "forbidden_effects": 0,
        "required_effects": 1.0, "false_shortlisted": 0, "scam_recall": 1.0,
        "scam_false_alarms": 0, "grounding_catches": 3,
    })

    table = compare_mod.compare("baseline", "after")
    assert "70.0%" in table
    assert "95.0%" in table
    assert "| Dangerous errors | 2 | 0 |" in table


def test_compare_uses_the_latest_result_for_a_label(results_dir):
    _write_result(results_dir, "20260910T000000Z", "baseline", {"verdict_accuracy": 0.5})
    _write_result(results_dir, "20260911T000000Z", "baseline", {"verdict_accuracy": 0.9})
    _write_result(results_dir, "20260911T010000Z", "after", {"verdict_accuracy": 0.9})

    table = compare_mod.compare("baseline", "after")
    assert "50.0%" not in table
    assert "90.0%" in table


def test_compare_raises_a_clear_error_for_a_missing_label(results_dir):
    with pytest.raises(SystemExit):
        compare_mod.compare("nonexistent", "also-nonexistent")
