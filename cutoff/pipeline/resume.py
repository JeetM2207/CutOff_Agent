"""Resume selection by role category (Section 6.4), plus two optional layers
on top, each falling back to the one below it the instant anything goes
wrong — this is a read, never ledgered, and must never block planning:

1. Dynamic generation: if a master profile (`config/master_profile.md`, a
   local, hand-edited file — Section 6.4 extension) is configured, the LLM
   rewrites a job-description-tailored resume from it and a real PDF is
   compiled on the fly (`resume_pdf.py`), served by this app's own dashboard
   server rather than uploaded anywhere (no Drive *write* scope needed).
2. Static JD-content match: the original behavior — download every resume
   on file, extract its text, have the LLM pick the best content match.
3. Deterministic category mapping (`select_resume`): the final fallback,
   used whenever neither of the above is configured, produces nothing
   usable, or fails outright."""
from __future__ import annotations

import hashlib
import logging
import os

from cutoff.adapters.base import FileStore
from cutoff.models import ResumeFile, StudentProfile
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
    jd_text: str, master_profile_md: str, student_profile: StudentProfile,
    *, api_key: str, model: str, provider: str, base_url: str | None, db_path: str,
    output_dir: str, public_base_url: str,
) -> tuple[ResumeFile, str] | None:
    """Returns None on ANY failure (LLM call, PDF render, disk write) —
    caller falls back to static JD matching, exactly like a failed
    select_best_resume call already does."""
    from cutoff.llm.resume_generate import generate_tailored_resume
    from cutoff.pipeline.resume_pdf import render_resume_pdf

    try:
        sections, reason = generate_tailored_resume(
            jd_text, master_profile_md, api_key=api_key, model=model, provider=provider,
            base_url=base_url, db_path=db_path,
        )
        pdf_bytes = render_resume_pdf(
            sections, student_name=student_profile.name, student_roll_no=student_profile.roll_no,
            student_email=student_profile.email,
        )
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
    master_profile_md: str | None = None,
    generated_resume_dir: str | None = None,
    public_base_url: str = "",
) -> tuple[ResumeFile | None, str | None]:
    """Returns (resume, reason). `reason` is None whenever the deterministic
    category fallback was used instead of an actual JD-content match or a
    dynamically generated one.

    Dynamic generation (see module docstring) only ever activates when the
    caller explicitly supplies student_profile, master_profile_md, AND
    generated_resume_dir — every existing caller that doesn't pass these
    (including every test written before this feature existed) gets the
    exact prior behavior, unchanged."""
    resumes = files.list_resumes()
    if not jd_text.strip():
        return select_resume(role_category, files), None

    if student_profile is not None and master_profile_md and master_profile_md.strip() and generated_resume_dir:
        generated = _try_generate_tailored_resume(
            jd_text, master_profile_md, student_profile,
            api_key=api_key, model=model, provider=provider, base_url=base_url, db_path=db_path,
            output_dir=generated_resume_dir, public_base_url=public_base_url,
        )
        if generated is not None:
            return generated

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
