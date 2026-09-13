# Self-heal draft: vague_branches / verdict_mismatch

**Source eval result:** `20260911T130354Z_after-heldout.json`
**Affected scenarios:** heldout_003_vague_branch_needs_review (`heldout` split)
**Status:** DRAFT — not reviewed, not implemented, no PR opened.

## Diagnosis and suggested fix

### Diagnosis
In `cutoff/pipeline/eligibility.py`, the check for vague branches looks like this:
```python
vague = any(w in (criteria.branches_text or "").lower() for w in VAGUE_BRANCH_WORDS)
```
The test email mentions `"Open to circuit branches and CSE."` The word `"circuit"` is inside `VAGUE_BRANCH_WORDS`. However, the branch check only triggers the vague branch `needs_review` path if `profile.branch not in criteria.branches_allowed`. If the student profile's branch is *not* in `criteria.branches_allowed` (or if `criteria.branches_allowed` doesn't explicitly list the specific student's branch), but the notice contains a vague branch descriptor like "circuit", it should return a question ("NEEDS_REVIEW") rather than failing outright (`NOT_ELIGIBLE`). Depending on how `criteria.branches_allowed` is populated by the extraction step, if a student's branch (e.g., ECE) isn't explicitly enumerated in `branches_allowed` even though "circuit branches" was specified, the engine evaluates it as a hard failure (`fails.append("BRANCH")`) instead of treating it as vague and surfacing a review question.

### Suggested Fix
In `cutoff/pipeline/eligibility.py`, update the branch evaluation block so that if `vague` is true, it always appends a review question to `questions` rather than marking a hard failure (`fails.append("BRANCH")`), regardless of whether `profile.branch` is strictly present in `criteria.branches_allowed`.

## Suggested PR (fill in once the fix above is actually implemented and reviewed)

**Title:** Fix vague branches handling

**Body:** Addresses a known eval failure (verdict_mismatch on heldout_003_vague_branch_needs_review). See diagnosis above for root cause.
