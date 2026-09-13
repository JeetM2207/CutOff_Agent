"""Self-Healing add-on (Section 0's honesty culture, extended): turns an
already-known eval failure into a written diagnosis and a suggested fix —
never an automatic pull request. A human reads the report this writes and
decides whether to actually implement the fix and open a PR.

Works entirely from an already-committed eval/results/*.json file and the
fixture files it references. Never re-runs the pipeline, and never touches
the held-out split directly — it reads the *result* of a run that already
happened, so it cannot be used to iterate against held-out data the way
re-running eval/runner.py --split heldout repeatedly would (Section 15's
honesty guard around the held-out set stays intact).

    python scripts/self_heal.py                        # latest result, all failure clusters
    python scripts/self_heal.py --result <path> --top 3
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from cutoff.config import get_settings
from cutoff.llm.client import get_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = PROJECT_ROOT / "eval"
FIXTURES_ROOT = EVAL_ROOT / "fixtures"
RESULTS_DIR = EVAL_ROOT / "results"
REPORTS_DIR = EVAL_ROOT / "self_heal_reports"

# The one file most branch/deadline/eligibility judgment calls run through —
# handed to the LLM as grounding so its suggested fix references real code,
# not a guess about how the pipeline might work.
CONTEXT_FILES = ["cutoff/pipeline/eligibility.py", "cutoff/pipeline/timeparse.py"]

_SYSTEM = (
    "You are helping diagnose a failing test case in a campus-recruiting agent's eval harness. You will be "
    "given: the failure category, which metric it failed, the test email(s) and expected result, and the "
    "actual source code most likely responsible. Write a short, honest diagnosis grounded in that code — "
    "if you are not certain, say so rather than inventing a confident-sounding but wrong explanation. Then "
    "suggest ONE concrete fix (a specific file, and roughly what should change). Do not write a full patch; "
    "describe the fix clearly enough for an engineer to implement it. This suggestion will be reviewed by a "
    "human before anything is changed — you are drafting, not deciding."
)


def _latest_result() -> Path:
    results = sorted(RESULTS_DIR.glob("*.json"))
    if not results:
        raise SystemExit(f"No eval results found in {RESULTS_DIR}")
    return results[-1]


def _load_fixture(scenario_id: str) -> tuple[dict, dict, str]:
    for split in ("dev", "heldout"):
        d = FIXTURES_ROOT / split / scenario_id
        if d.exists():
            scenario = json.loads((d / "scenario.json").read_text(encoding="utf-8"))
            expected = json.loads((d / "expected.json").read_text(encoding="utf-8"))
            return scenario, expected, split
    raise SystemExit(f"Fixture not found for scenario_id={scenario_id!r}")


def _context_code() -> str:
    parts = []
    for rel in CONTEXT_FILES:
        path = PROJECT_ROOT / rel
        if path.exists():
            code = path.read_text(encoding="utf-8")
            parts.append(f"--- {rel} ---\n{code}")
    return "\n\n".join(parts)


def _draft_diagnosis(cluster: dict, scenario: dict, expected: dict, split: str, *, settings) -> str:
    user_text = (
        f"FAILURE CATEGORY: {cluster['category']}\n"
        f"FAILED METRIC: {cluster['failed_metric']}\n"
        f"AFFECTED SCENARIOS: {', '.join(cluster['scenario_ids'])} (split: {split})\n\n"
        f"TEST EMAIL(S):\n{json.dumps(scenario.get('messages', []), indent=2)}\n\n"
        f"EXPECTED RESULT:\n{json.dumps(expected, indent=2)}\n\n"
        f"RELEVANT SOURCE CODE:\n{_context_code()}"
    )
    client = get_client(settings.llm_provider, settings.llm_api_key, settings.llm_base_url)
    if settings.llm_provider == "anthropic":
        resp = client.messages.create(
            model=settings.llm_model, max_tokens=1024, temperature=0.2,
            system=_SYSTEM, messages=[{"role": "user", "content": user_text}],
        )
        return "".join(getattr(b, "text", "") for b in resp.content)
    resp = client.chat.completions.create(
        model=settings.llm_model, temperature=0.2, max_tokens=1024,
        messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user_text}],
    )
    return resp.choices[0].message.content or ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Draft a diagnosis + suggested fix for known eval failures.")
    parser.add_argument("--result", type=Path, default=None, help="Path to an eval/results/*.json file (default: latest).")
    parser.add_argument("--top", type=int, default=3, help="How many failure clusters to draft reports for.")
    args = parser.parse_args()

    settings = get_settings()
    result_path = args.result or _latest_result()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    clusters = result.get("top_failure_clusters", [])[: args.top]

    if not clusters:
        print(f"No failure clusters in {result_path.name} — nothing to draft.")
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written = []

    for i, cluster in enumerate(clusters):
        scenario_id = cluster["scenario_ids"][0]
        scenario, expected, split = _load_fixture(scenario_id)
        print(f"Drafting diagnosis for {cluster['category']} / {cluster['failed_metric']} ({scenario_id})...")
        diagnosis = _draft_diagnosis(cluster, scenario, expected, split, settings=settings)

        report_path = REPORTS_DIR / f"{timestamp}_{cluster['category']}.md"
        report_path.write_text(
            encoding="utf-8",
            data=(
            f"# Self-heal draft: {cluster['category']} / {cluster['failed_metric']}\n\n"
            f"**Source eval result:** `{result_path.name}`\n"
            f"**Affected scenarios:** {', '.join(cluster['scenario_ids'])} (`{split}` split)\n"
            f"**Status:** DRAFT — not reviewed, not implemented, no PR opened.\n\n"
            "## Diagnosis and suggested fix\n\n"
            f"{diagnosis}\n\n"
            "## Suggested PR (fill in once the fix above is actually implemented and reviewed)\n\n"
            f"**Title:** Fix {cluster['category'].replace('_', ' ')} handling\n\n"
            f"**Body:** Addresses a known eval failure ({cluster['failed_metric']} on "
            f"{', '.join(cluster['scenario_ids'])}). See diagnosis above for root cause.\n"
            ),
        )
        written.append(report_path)
        print(f"  -> {report_path}")

    print(f"\n{len(written)} draft report(s) written to {REPORTS_DIR}/. Nothing was changed and no PR was opened.")


if __name__ == "__main__":
    main()
