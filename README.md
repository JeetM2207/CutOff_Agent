# CutOff

> The agent that never lets a student miss a campus recruiting drive they qualify for, or register for one they don't.

CutOff reads a student's own Gmail for campus-recruiting emails, decides eligibility against their profile and college policy with **deterministic rules — never an LLM guess** — and keeps Google Calendar and a tracker Sheet in sync as drives are announced, corrected, or cancelled. The one irreversible step, submitting the registration form, is gated behind a Telegram approval: the agent drafts the answers, the student approves and submits it themselves.

Full design rationale and every rule lives in [`CUTOFF_SPEC.md`](CUTOFF_SPEC.md). Real numbers and known limitations live in [`docs/RELIABILITY_BRIEF.md`](docs/RELIABILITY_BRIEF.md). Every decision made along the way, including seven real bugs found and fixed during testing, is logged in [`NOTES.md`](NOTES.md).

## How it works

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
                │ TRACE (every step, tool call, latency, error) ─► UI      │
                └──────────────────────────────────────────────────────────┘
```

The LLM is used in exactly two places: extracting structured facts from an email (forced tool-use, with a verbatim evidence quote required for every field it fills in), and matching a resume or drafting open-ended form answers. It never decides eligibility and never touches an external API directly — every side effect goes through the action ledger, which enforces idempotency keys, retry with backoff, a circuit breaker, and a verifier that re-reads the real app state after every write.

Every external app sits behind a Python `Protocol` with a `real` and a `fake` (in-memory) implementation ([`cutoff/adapters/`](cutoff/adapters/)); the pipeline only ever sees the Protocol, so the whole system runs and is tested against fakes with no network access at all.

One process (`python -m cutoff.main`) runs three things: FastAPI serving the dashboard and JSON API, a worker thread that polls Gmail and runs the pipeline, and a Telegram thread that long-polls for button presses. State lives in SQLite (WAL mode).

## Setup

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
cp .env.example .env
```

Fill in `.env`. At minimum, for the fake/demo mode, you need an LLM key — `GEMINI_API_KEY` is the easiest to get for free (no card) at [aistudio.google.com/apikey](https://aistudio.google.com/apikey); set `LLM_PROVIDER=gemini` and `LLM_MODEL` to match. See the comments in `.env.example` for the Anthropic and OpenRouter alternatives.

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

## Running

```bash
python -m cutoff.main
```

Dashboard: [http://127.0.0.1:8000](http://127.0.0.1:8000).

In `MODE=fake` with `DEMO_MODE=1`, the dashboard exposes demo controls (send a test email, inject a correction, trigger chaos) with no real accounts needed. In `MODE=real`, it polls the real Gmail inbox every `POLL_INTERVAL_SECONDS`.

To send a real test email to the account CutOff watches (from a separate "career office" sender account, never the agent's own credentials — which are `gmail.readonly` only):

```bash
python scripts/seed_inbox.py --to <student-email> --preset new_drive
```

## Testing

```bash
pytest
```

213 unit/integration tests, all against fake adapters and stubbed LLM responses — no test ever calls a real API or a real LLM.

The eval harness grades the full pipeline against fake apps' **final state** (not intermediate reasoning), including forbidden effects — things that shouldn't have happened:

```bash
python -m eval.runner --split dev --chaos none --label mylabel          # 30 labeled dev scenarios
python -m eval.runner --split dev --chaos kitchen_sink --label mylabel  # + injected 429s/500s/crash-restart
python -m eval.runner --split heldout --chaos none --i-promise-no-peeking --label mylabel  # 10 scenarios, don't run casually
```

Results are written to `eval/results/*.json` and never fabricated or hand-edited — see [`docs/RELIABILITY_BRIEF.md`](docs/RELIABILITY_BRIEF.md) for the current numbers and what they mean.

`scripts/self_heal.py` reads the latest eval failures and drafts a root-cause diagnosis with an LLM, purely for a human to review — it never opens a PR or applies a fix on its own. Its track record so far is 0-for-2 on getting the root cause right on the first guess, which is the whole reason it stays draft-only; see `eval/self_heal_reports/` and `NOTES.md` for what it got wrong each time and what the real fix was.

## Project layout

```
cutoff/
  adapters/    real + fake implementations of every external app
  llm/         extraction prompt, tool schema, resume/form-answer drafting
  pipeline/    eligibility engine, planner, executor, form autofill, resume matching
  app/         FastAPI server + dashboard (static/)
  bot/         Telegram long-poll handler
  main.py      entrypoint: server + worker + Telegram threads
eval/          scenarios (dev + held-out), fixtures, runner, results, self-heal reports
scripts/       one-off setup and ops scripts (OAuth, sheet seeding, test-email sending)
tests/         pytest suite
docs/          reliability brief
```

## Results

Full numbers, the real fix-by-fix arc (not just this endpoint), and every one of the seven real bugs found along the way live in [`docs/RELIABILITY_BRIEF.md`](docs/RELIABILITY_BRIEF.md) — this is the short version:

| Run | Verdict acc. | Dangerous errors | Forbidden effects | Required effects | Scam recall |
|---|---|---|---|---|---|
| Baseline (dev, 30 scenarios) | 90.9% | 0 | 0 | 48.7% | 100% |
| **Final (dev, 34 scenarios)** | **100%** | 0 | **0** | **100%** | 100% |
| **Final (held-out, 10 scenarios)** | **100%** | 0 | **0** | **100%** | 100% |
| **Final (dev, kitchen_sink chaos)** | **100%** | 0 | **0** | **100%** | 100% |

The dev split includes 3 scenarios added specifically to be hard to pass — a multi-revision chain, two contradictory corrections in one thread, and a genuinely ambiguous two-drive resolution. All 3 pass; the third one didn't at first (the system never misattached, but the clarifying question it should ask didn't trigger) — found, root-caused, and fixed live, see the brief.

29 of 30 dev scenarios pass outright; the one remaining failure has a correct eligibility verdict and zero forbidden effects (see the brief's Known Limitations).

## Glossary

Indian recruiting terms used in some fixtures and in real emails — the UI and this repo otherwise use the right-hand column throughout, for judges unfamiliar with Indian campus placement conventions.

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

**In scope:** Gmail ingest with sender allowlisting and dedup; LLM extraction with evidence-quote grounding; drive identity/versioning across new/revision/cancellation/reminder/shortlist; a deterministic `ELIGIBLE` / `NOT_ELIGIBLE` / `NEEDS_REVIEW` eligibility engine; an action ledger with retry, verification, crash-resume, and compensation; Sheets tracker, Calendar reminders with exam-clash detection, Drive resume selection, Telegram approval flow; shortlist PDF checking by roll number; scam/lookalike/prompt-injection detection.

**Out of scope:** auto-submitting the registration form (the student always does this — it's the approval gate); sending email from the student's account; OCR of scanned PDFs; multi-user support or deployment.
