"""Resume selection by role category (Section 6.4), plus two optional layers
on top — this is a read, never ledgered, and must never block planning:

1. Dynamic generation: if a master profile (`config/master_profile.yaml`, a
   local file — Section 6.4 extension, built via the onboarding flow in
   cutoff.llm.profile_extract / cutoff.pipeline.profile_synthesize, or
   hand-edited) is configured, the LLM rewrites a job-description-tailored
   resume from it and a real PDF is compiled on the fly (`resume_pdf.py`, a
   fixed template — see its own docstring), served by this app's own
   dashboard server rather than uploaded anywhere (no Drive *write* scope
   needed).
2. Static JD-content match: the pre-existing behavior from before the
   runtime choice existed — download every resume on file, extract its
   text, have the LLM pick the best content match.
3. Deterministic category mapping (`select_resume`): the final fallback,
   used whenever neither of the above is configured, produces nothing
   usable, or fails outright.

When a master profile IS configured, the student is never left to a
silent automatic choice: they explicitly pick a path per drive, over a
Telegram button (`planner._plan_resume_choice`), and `resume_mode` below
keeps those two paths deliberately clean of each other's LLM calls —
"match" (Use my resume on file) never touches the LLM at all, not even
tier 2; "generate" falls straight to tier 3 on any failure, never into
tier 2 either, since that would be a second, unrequested LLM call at the
exact moment the student explicitly chose a path. Tier 2 (the static
JD-content match) is reachable ONLY via `resume_mode="auto"` — i.e. only
when no master profile is configured at all, so the choice card was never
shown in the first place; this keeps every caller from before this
feature existed working exactly as it always did."""
from __future__ import annotations

import hashlib
import logging
import os

from cutoff.adapters.base import FileStore
from cutoff.models import MasterProfile, ResumeFile, StudentProfile
from cutoff.pipeline.executor import with_retry
from cutoff.pipeline.ingest import extract_pdf_text

logger = logging.getLogger("cutoff.resume")

DEFAULT_RESUME_NAME = "resume_DEFAULT.pdf"
GENERATED_RESUME_NAME = "resume_GENERATED.pdf"


def select_resume(role_category: str | None, files: FileStore) -> ResumeFile | None:
    resumes = files.list_resumes()
    by_name = {r.name: r for r in resumes}

    if role_category:
        wanted = f"resume_{role_category}.pdf"
        if wanted in by_name:
            return by_name[wanted]

    return by_name.get(DEFAULT_RESUME_NAME)


def _write_generated_pdf(pdf_bytes: bytes, *, output_dir: str) -> str:
    """Writes to <output_dir>/<sha256-prefix>.pdf — content-addressed so
    re-generating the same tailored resume for the same drive is a no-op on
    disk, not a growing pile of near-duplicate files. Returns the filename
    (not the full path) for building the served URL."""
    token = hashlib.sha256(pdf_bytes).hexdigest()[:20]
    os.makedirs(output_dir, exist_ok=True)
    filename = f"{token}.pdf"
    path = os.path.join(output_dir, filename)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(pdf_bytes)
    return filename


def _try_generate_tailored_resume(
    jd_text: str, master_profile: MasterProfile, student_profile: StudentProfile,
    *, api_key: str, model: str, provider: str, base_url: str | None, db_path: str,
    output_dir: str, public_base_url: str,
) -> tuple[ResumeFile, str] | None:
    """Returns None on ANY failure (LLM call, PDF render, disk write) — the
    caller decides what happens next based on resume_mode: "generate" goes
    straight to the deterministic category default, "auto" falls through
    to the static JD-content-match tier first (see select_resume_smart)."""
    from cutoff.llm.resume_generate import generate_tailored_resume
    from cutoff.pipeline.resume_pdf import render_resume_pdf

    try:
        sections, reason = generate_tailored_resume(
            jd_text, master_profile, api_key=api_key, model=model, provider=provider,
            base_url=base_url, db_path=db_path,
        )
        pdf_bytes = render_resume_pdf(sections, student_profile=student_profile, master_profile=master_profile)
        filename = _write_generated_pdf(pdf_bytes, output_dir=output_dir)
    except Exception:
        logger.exception("dynamic resume generation failed; falling back to static resume matching")
        return None

    base = public_base_url.rstrip("/") if public_base_url else ""
    web_view_link = f"{base}/generated_resumes/{filename}"
    resume = ResumeFile(file_id=f"generated:{filename}", name=GENERATED_RESUME_NAME, web_view_link=web_view_link)
    return resume, reason


def select_resume_smart(
    role_category: str | None,
    jd_text: str,
    files: FileStore,
    *, api_key: str, model: str, provider: str, base_url: str | None, db_path: str,
    student_profile: StudentProfile | None = None,
    master_profile: MasterProfile | None = None,
    generated_resume_dir: str | None = None,
    public_base_url: str = "",
    resume_mode: str = "auto",  # "auto" | "generate" | "match" — see module docstring
) -> tuple[ResumeFile | None, str | None]:
    """Returns (resume, reason). `reason` is None whenever the deterministic
    category fallback was used instead of an actual JD-content match or a
    dynamically generated one.

    `resume_mode` — the two explicit runtime-choice paths (Section 6.4) are
    kept deliberately clean, never crossing into each other's LLM calls:

      "auto"     — no explicit student choice involved (no master profile
                   configured at all, so the choice card was never shown).
                   Original pre-choice-card behavior, unchanged: dynamic
                   generation if configured, else the static JD-content
                   match (an LLM picks the best of several resumes on
                   file), else category default.
      "match"    — the student tapped "Use my resume on file". Bypasses
                   the LLM ENTIRELY — including the JD-content-match tier
                   below — and goes straight to the deterministic category
                   lookup. Fast, free, and exactly what the student asked
                   for: no AI involved in this path at all.
      "generate" — the student tapped "Generate tailored resume". Attempts
                   generation; on ANY failure, falls straight to the
                   deterministic category default — never the JD-content-
                   match tier, which is a second, unrequested LLM call the
                   student never asked for and would silently spend tokens
                   on at exactly the moment they explicitly chose a path.

    Dynamic generation only ever activates when the caller explicitly
    supplies student_profile, master_profile, AND generated_resume_dir —
    every existing caller that doesn't pass these (including every test
    written before this feature existed) gets the exact prior "auto"
    behavior, unchanged."""
    if resume_mode == "match":
        return select_resume(role_category, files), None

    try:
        resumes = files.list_resumes()
    except Exception:
        logger.exception("files.list_resumes failed; falling back to default resume selection")
        return select_resume(role_category, files), None

    if not jd_text.strip():
        return select_resume(role_category, files), None

    generation_configured = student_profile is not None and master_profile is not None and generated_resume_dir
    if generation_configured:
        generated = _try_generate_tailored_resume(
            jd_text, master_profile, student_profile,
            api_key=api_key, model=model, provider=provider, base_url=base_url, db_path=db_path,
            output_dir=generated_resume_dir, public_base_url=public_base_url,
        )
        if generated is not None:
            return generated

    if resume_mode == "generate":
        # An explicit "generate" request failed outright (or generation
        # wasn't even configured, which shouldn't normally happen given
        # the caller's own guards) -- never fall into the JD-content-match
        # tier below; that's a different, unrequested LLM call.
        return select_resume(role_category, files), None

    if not resumes:
        return select_resume(role_category, files), None

    texts: list[tuple[str, str]] = []
    for r in resumes:
        ok, data, err = with_retry(lambda r=r: files.get_resume_content(r.file_id))
        if not ok:
            logger.warning("couldn't fetch resume %r for JD matching: %s", r.name, err)
            continue
        try:
            text = extract_pdf_text(data)
        except Exception:
            logger.warning("couldn't extract text from resume %r for JD matching", r.name)
            continue
        if text.strip():
            texts.append((r.name, text))

    if not texts:
        return select_resume(role_category, files), None

    from cutoff.llm.resume_match import select_best_resume

    try:
        chosen_name, reason = select_best_resume(
            jd_text, texts, api_key=api_key, model=model, provider=provider,
            base_url=base_url, db_path=db_path,
        )
    except Exception:
        logger.exception("resume JD-matching failed; falling back to role-category selection")
        return select_resume(role_category, files), None

    by_name = {r.name: r for r in resumes}
    chosen = by_name.get(chosen_name)
    return (chosen, reason) if chosen is not None else (select_resume(role_category, files), None)
