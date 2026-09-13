# CutOff

> The agent that never lets a student miss a campus recruiting drive they qualify for, or register for one they don't.

Built in a single 6.5-hour build window for the **Multi-App AI Agent Hackathon**, hosted by Lemma × Comma Capital and judged by the founders of Arga Labs.

---

## The problem, translated

In the US this is on-campus recruiting season — career fairs, OCR sign-ups, coffee chats, a portal you check once a week. In India it runs almost entirely through email, at a scale most outside the system never see: roughly a million and a half engineering students graduate every year, and nearly all of them go through some version of this.

Every college has a "career office" (locally, the "placement cell") that emails the entire student body — sometimes several times a day — about company recruiting drives: the role, a hard eligibility bar (minimum GPA, allowed majors, zero failed courses outstanding), and a registration deadline that's frequently the same night.

Then come the corrections. A deadline moves. A GPA cutoff quietly changes. A drive gets cancelled and re-announced under a slightly different subject line. A shortlist PDF lands with four hundred roll numbers in no particular order. And mixed in with all of it: real fraud — fake "registration processing fee" requests and lookalike-domain emails built to look exactly like the real career office, because at this scale, students are a large and soft target.

A student who misses one of these emails misses the drive. A student who registers for one they're not actually eligible for can, at many colleges, be barred from every drive for the rest of the season. There is no undo button on either mistake.

**CutOff is the agent that reads every one of those emails so the student doesn't have to — and refuses to press "submit" on their behalf.**

---

## What it actually does, end to end

CutOff runs continuously from a student's own Google and Telegram accounts. Here's one drive, start to finish:

1. **Reads** the student's Gmail for career-office mail — new drives, corrections, cancellations, reminders, shortlist PDFs — and always resolves to the *latest* version of a drive, never a stale one, even across three or four revisions of the same posting.
2. **Extracts facts, not judgment.** An LLM turns the email into structured data (company, role, GPA cutoff, allowed majors, deadline...) — forced tool-use only, and every single field it fills in must come with a verbatim quote from the email as evidence. It never gets to decide anything.
3. **Checks eligibility deterministically.** Plain Python compares those extracted facts against the student's real profile and the college's actual policy (both live in a Google Sheet the student controls) — GPA, major, backlog count, batch year, one-offer rules. No model in the loop, no hallucinated "you're probably fine."
4. **Keeps Google Calendar and the tracker Sheet in sync** as the drive evolves, and flags a test or interview slot that clashes with the student's own exam timetable.
5. **Tailors a resume for this specific role** — either the closest existing resume on file, or a freshly generated one built from a "master profile" (assembled once, from the student's existing resume plus a live pull of their GitHub and LeetCode activity), swapping in whichever real projects and skills actually match what the job description asks for.
6. **Drafts everything a human would have to write by hand**: the open-ended registration-form answers, and a company- and role-specific interview-prep briefing pulled from real, live web search — likely question patterns, what past candidates reported, where to look next.
7. **Sends it all to the student on Telegram as one approval card, and waits.** The student can approve, ask for a different resume, or ignore it. CutOff never submits the form itself — the one genuinely irreversible step in this entire pipeline always ends with a human's own thumb on the button.
8. **Reports back.** When a shortlist PDF arrives, it's scanned for the student's roll number, and the result comes with the exact page it was found on — not just a verdict.

Everywhere along the way, it's also watching for fraud: fee-request scams and lookalike-domain phishing that impersonates the real career office. It never acts on either.

---

## Why this is hard (and why it's built the way it is)

The team judging this — Arga Labs — builds sandboxes with stateful "twins" of real APIs and grades agents on **final service state** and **forbidden effects**, not on whether the transcript sounds confident. So that's exactly how CutOff grades itself:

- **Every side effect goes through one action ledger** (`planned → approved → executing → done → verified | failed | voided`) with an idempotency key, retry with backoff, and a circuit breaker. Nothing writes to Sheets, Calendar, Drive, or Telegram from anywhere else in the codebase.
- **A verifier re-reads the real app state after every write.** An action isn't "done" because the code that sent it didn't throw — it's done because CutOff read it back and confirmed it's actually there.
- **A labeled eval harness grades outcomes, not intentions** — including a held-out split the team deliberately never peeked at while iterating, and a chaos mode that injects 429s, 500s, and mid-run crash/restarts to see whether the ledger actually recovers or just says it does.
- **The LLM is sandboxed to exactly three narrow jobs** — extract facts (with evidence), tailor/match a resume, draft a form answer — and is never allowed to trigger a side effect or decide eligibility directly.
- **Every real bug found during testing is written down, not quietly fixed.** [`NOTES.md`](NOTES.md) is a dated build log of every live bug (root cause and fix), every deliberate deviation from the original spec, and why — nothing here is retouched after the fact to look cleaner than it was.

## Architecture

```
                ┌──────────────────────────────────────────────────────────┐
                │                         CutOff                           │
 Gmail ──poll──►│ INGEST ─► EXTRACT (LLM) ─► VALIDATE ─► RESOLVE ─► DIFF   │
 (readonly)     │   │        one tool:        grounding   drive id   v1→v2 │
                │   │        record_notice    + dates     + version        │
                │   ▼                                         │            │
                │ SECURITY (allowlist, lookalike, fee, injection)          │
                │                                             ▼            │
 Sheets ◄──────►│ ELIGIBILITY + POLICY (deterministic) ─► PLANNER          │
 (profile,      │                                             │            │
  policy,       │                                             ▼            │
  tracker)      │                    ┌──────── ACTION LEDGER (SQLite) ───┐ │
                │                    │ planned → approved → executing →  │ │
 Calendar ◄────►│  EXECUTOR ◄────────┤ done → verified | failed | voided │ │
 Drive ◄────────│  (retry, backoff,  └───────────────────────────────────┘ │
 Telegram ◄────►│   circuit breaker)  ─► VERIFIER (re-read app state)      │
                │                                                          │
                │ TRACE (every step, tool call, latency, error) ─► UI      │
                └──────────────────────────────────────────────────────────┘
                     ▲
                     │ every adapter can be wrapped by FaultInjector (chaos)
                     │ and swapped real ⇄ fake via config
```

Every external app sits behind a Python `Protocol` with a `real` and a `fake` (in-memory) implementation ([`cutoff/adapters/`](cutoff/adapters/)); the pipeline only ever sees the Protocol, so the entire system runs and is tested against fakes with zero network access. One process (`python -m cutoff.main`) runs three things: a FastAPI dashboard + JSON API, a worker thread that polls Gmail and drives the pipeline, and a Telegram thread that long-polls for button presses.

Two subsystems sit on top of that core loop:

- **Onboarding** (`cutoff/pipeline/profile_synthesize.py`, `cutoff/adapters/developer_footprint.py`) — a one-time Telegram flow that parses a resume PDF, pulls GitHub/LeetCode activity, and deterministically merges them into a `master_profile.yaml` the student can hand-edit. Deliberately *not* an LLM merge — reconciling structured facts is exactly the kind of judgment call kept out of the model's hands here too.
- **Prep intel** (`cutoff/adapters/web_research.py`, `cutoff/llm/intel_synthesize.py`) — opt-in, real web search for the specific company and role, synthesized into a short prep strategy attached to the approval card.

## Try it in under a minute (no real accounts needed)

```bash
pip install -e ".[dev]"
cp .env.example .env        # add one free LLM key — see Setup below
python -m cutoff.main
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). In fake mode with `DEMO_MODE=1`, the dashboard has demo controls — send a test email, inject a correction, trigger chaos — with no Google or Telegram account required.

## Setup

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
cp .env.example .env
```

Fill in `.env`. At minimum, for fake/demo mode, you need an LLM key — `GEMINI_API_KEY` is the easiest to get for free (no card) at [aistudio.google.com/apikey](https://aistudio.google.com/apikey); set `LLM_PROVIDER=gemini` and `LLM_MODEL` to match. See the comments in `.env.example` for the Anthropic and OpenRouter alternatives.

For `MODE=real` (actual Gmail/Sheets/Calendar/Drive/Telegram), you additionally need:

- A Google Cloud OAuth client (`credentials.json`) with Gmail (readonly), Sheets, Calendar, and Drive (readonly) scopes enabled, then run:
  ```bash
  python scripts/google_auth.py
  ```
- A Google Sheet with Profile/Policy/Drives/Log/FormTemplates tabs — created and seeded automatically:
  ```bash
  python scripts/setup_sheet.py
  ```
- A Telegram bot token (`@BotFather`) and your chat ID, in `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
- Optionally, `python scripts/import_master_profile.py <resume.pdf>` to build the onboarding profile from the CLI instead of via Telegram.

## Running

```bash
python -m cutoff.main
```

In `MODE=real`, it polls the real Gmail inbox every `POLL_INTERVAL_SECONDS`.

To send a real test email to the account CutOff watches (from a separate "career office" sender account, never the agent's own credentials — which are `gmail.readonly` only):

```bash
python scripts/seed_inbox.py --to <student-email> --preset new_drive
```

## Testing

```bash
pytest
```

374 unit/integration tests, all against fake adapters and stubbed LLM responses — no test ever calls a real API or a real LLM.

The eval harness grades the full pipeline against fake apps' **final state** (not intermediate reasoning), including forbidden effects — things that shouldn't have happened:

```bash
python -m eval.runner --split dev --chaos none --label mylabel          # 30 labeled dev scenarios
python -m eval.runner --split dev --chaos kitchen_sink --label mylabel  # + injected 429s/500s/crash-restart
python -m eval.runner --split heldout --chaos none --i-promise-no-peeking --label mylabel  # 10 scenarios, don't run casually
```

Results are written to `eval/results/*.json` and never fabricated or hand-edited — see [`docs/RELIABILITY_BRIEF.md`](docs/RELIABILITY_BRIEF.md) for the current numbers and what they mean.

`scripts/self_heal.py` reads the latest eval failures and drafts a root-cause diagnosis with an LLM, purely for a human to review — it never opens a PR or applies a fix on its own.

## Results

Full numbers, the real fix-by-fix arc, and every real bug found along the way live in [`docs/RELIABILITY_BRIEF.md`](docs/RELIABILITY_BRIEF.md) — this is the short version, from the core eligibility/action pipeline eval:

| Run | Verdict acc. | Dangerous errors | Forbidden effects | Required effects | Scam recall |
|---|---|---|---|---|---|
| Baseline (dev, 30 scenarios) | 90.9% | 0 | 0 | 48.7% | 100% |
| **Final (dev, 34 scenarios)** | **100%** | 0 | **0** | **100%** | 100% |
| **Final (held-out, 10 scenarios)** | **100%** | 0 | **0** | **100%** | 100% |
| **Final (dev, kitchen_sink chaos)** | **100%** | 0 | **0** | **100%** | 100% |

The dev split includes 3 scenarios added specifically to be hard to pass — a multi-revision chain, two contradictory corrections in one thread, and a genuinely ambiguous two-drive resolution. All 3 pass.

The onboarding/resume-generation/prep-intel subsystems built after this eval sat were validated live against real Gmail, Sheets, Calendar, and Telegram accounts (not the fake-adapter eval), and are covered by the unit-test suite above — see `NOTES.md` for the live-run details, including two real bugs found and fixed that way (a PDF hyperlink-extraction gap, and a malformed Sheet cell producing an unreadable crash).

## Project layout

```
cutoff/
  adapters/    real + fake implementations of every external app
  llm/         extraction / resume-matching / form-answer / prep-intel prompts
  pipeline/    eligibility engine, planner, executor, form autofill, resume
               generation, onboarding synthesis
  app/         FastAPI server + dashboard (static/)
  bot/         Telegram long-poll handler + onboarding flow
  main.py      entrypoint: server + worker + Telegram threads
eval/          scenarios (dev + held-out), fixtures, runner, results, self-heal reports
scripts/       one-off setup and ops scripts (OAuth, sheet seeding, test-email sending,
               onboarding import, demo reset)
tests/         pytest suite
docs/          reliability brief
config/        master_profile.example.yaml template (the real one is gitignored)
```

## Glossary

Indian recruiting terms that show up in real emails and some fixtures — the UI and the rest of this repo use the right-hand column throughout.

| Indian term | Say this instead |
|---|---|
| Placement cell / T&P cell | Career office |
| Placement drive | Campus recruiting drive |
| CGPA (10-point) | GPA (10-point scale) |
| Active backlog | Failed course not yet cleared |
| Branch | Major / department |
| LPA (lakhs per annum) | Annual salary (1 lakh = ₹100,000) |
| One-offer / dream rule | College rule limiting applications after you receive an offer |
| Debarred | Barred from future drives |

## Scope

**In scope:** Gmail ingest with sender allowlisting and dedup; LLM extraction with evidence-quote grounding; drive identity/versioning across new/revision/cancellation/reminder/shortlist; a deterministic `ELIGIBLE` / `NOT_ELIGIBLE` / `NEEDS_REVIEW` eligibility engine; an action ledger with retry, verification, crash-resume, and compensation; Sheets tracker, Calendar reminders with exam-clash detection, Telegram approval flow; shortlist PDF checking by roll number; scam/lookalike/prompt-injection detection; one-time onboarding (resume + GitHub/LeetCode) into a master profile; dynamic, JD-tailored resume generation; opt-in interview-prep intel.

**Out of scope:** auto-submitting the registration form (the student always does this — it's the approval gate); sending email from the student's account; OCR of scanned PDFs; multi-user support or deployment.

## Team & context

Built solo, end to end, in the 6.5-hour build window of the Multi-App AI Agent Hackathon (Lemma × Comma Capital, judged by the founders of Arga Labs). Full build spec and design rationale: [`CUTOFF_SPEC.md`](CUTOFF_SPEC.md).

## Demo video

📺 **[Watch the 2-minute demo](ADD_DEMO_VIDEO_LINK_HERE)**
