# Self-heal draft: deadline_already_passed / missing_required_effect

**Source eval result:** `20260911T130354Z_after-heldout.json`
**Affected scenarios:** heldout_007_deadline_passed (`heldout` split)
**Status:** DRAFT — not reviewed, not implemented, no PR opened.

## Diagnosis and suggested fix

### Diagnosis
Based on the provided test case, the email states that the drive already concluded and closed on September 5, 2026, while the test email was received on September 11, 2026. Therefore, the deadline has passed relative to the evaluation time. 

However, looking at the provided source code, `cutoff/pipeline/eligibility.py` and `cutoff/pipeline/timeparse.py` handle eligibility evaluation and deadline state computation, but the source code for the overall pipeline orchestrator or status-handling logic (where `deadline_state` is checked to determine whether a drive is marked `OPEN` vs `PASSED` or whether it filters out of Google Sheets) is not fully shown. 

Specifically, when a deadline has passed (`deadline_state == "PASSED"`), the system should typically mark the drive status as `PASSED` rather than `OPEN`, and skip adding it to sheets or sending certain effects. If the pipeline incorrectly labels a passed deadline as `OPEN` or fails to suppress the required effect when a deadline has passed, the test fails. Because the orchestrator code is omitted, we cannot pinpoint the exact file or function, but the bug lies in how the agent orchestrates the workflow when `deadline_state` returns `"PASSED"`.

### Suggested Fix
In the main pipeline or orchestrator file (typically `cutoff/pipeline/run.py` or similar workflow runner), check the result of `deadline_state(deadline, now)`. If the deadline state is `"PASSED"`, ensure the drive's status is set to `"PASSED"` and that it is excluded from generating a row effect in Google Sheets (since expired drives should not be processed for applications).

## Suggested PR (fill in once the fix above is actually implemented and reviewed)

**Title:** Fix deadline already passed handling

**Body:** Addresses a known eval failure (missing_required_effect on heldout_007_deadline_passed). See diagnosis above for root cause.
