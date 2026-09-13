"""python -m eval.compare <label_a> <label_b> — prints the before/after table
(Section 15.6). Reads the latest results/*_<label>.json for each label; never
recomputes or edits anything, just reports what runner.py already wrote."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"

METRIC_KEYS = [
    ("verdict_accuracy", "Verdict accuracy", True),
    ("dangerous_errors", "Dangerous errors", False),
    ("missed_opportunities", "Missed opportunities", False),
    ("deadline_accuracy", "Deadline accuracy", True),
    ("duplicate_effects", "Duplicate effects", False),
    ("forbidden_effects", "Forbidden effects", False),
    ("required_effects", "Required effects", True),
    ("false_shortlisted", "False shortlisted", False),
    ("scam_recall", "Scam recall", True),
    ("scam_false_alarms", "Scam false alarms", False),
    ("grounding_catches", "Grounding catches", False),
]


def _latest_result_for_label(label: str) -> dict:
    matches = sorted(RESULTS_DIR.glob(f"*_{label}.json"))
    if not matches:
        raise SystemExit(f"no results file found for label {label!r} in {RESULTS_DIR}")
    return json.loads(matches[-1].read_text())


def _fmt(value, is_ratio: bool) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%" if is_ratio else str(value)


def compare(label_a: str, label_b: str) -> str:
    a, b = _latest_result_for_label(label_a), _latest_result_for_label(label_b)
    sa, sb = a["summary"], b["summary"]

    lines = [f"| Metric | {label_a} | {label_b} |", "|---|---|---|"]
    for key, name, is_ratio in METRIC_KEYS:
        lines.append(f"| {name} | {_fmt(sa.get(key), is_ratio)} | {_fmt(sb.get(key), is_ratio)} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two eval result labels (Section 15.6)")
    parser.add_argument("label_a")
    parser.add_argument("label_b")
    args = parser.parse_args()
    print(compare(args.label_a, args.label_b))


if __name__ == "__main__":
    main()
