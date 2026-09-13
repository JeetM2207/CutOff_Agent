"""Loads and formats the student's master profile (Section 6.4 extension) — a
local, hand-edited YAML file listing everything they have: education, skills,
projects, experience, links (GitHub/LinkedIn/portfolio/etc.), achievements.
This is the ONLY source of truth dynamic resume generation is allowed to draw
from — see cutoff.llm.resume_generate's grounding rules."""
from __future__ import annotations

from pathlib import Path

import yaml

from cutoff.models import MasterProfile


def load_master_profile(path: str | Path) -> MasterProfile | None:
    """None disables dynamic resume generation entirely — a missing, empty,
    or malformed file is "not configured yet," never an error that blocks
    planning, same treatment as an unset RESUME_FOLDER_ID."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not raw:
            return None
        return MasterProfile(**raw)
    except Exception:
        return None


def format_for_prompt(profile: MasterProfile) -> str:
    """Plain-text rendering of the whole profile for the LLM prompt — the
    LLM only ever selects and rephrases from this, never adds beyond it."""
    lines: list[str] = []
    if profile.skills:
        lines.append("SKILLS: " + ", ".join(profile.skills))
    for edu in profile.education:
        bits = [edu.degree, edu.institution]
        if edu.cgpa:
            bits.append(f"CGPA {edu.cgpa}")
        if edu.batch_year:
            bits.append(f"Batch {edu.batch_year}")
        line = "EDUCATION: " + " | ".join(bits)
        if edu.notes:
            line += f" ({edu.notes})"
        lines.append(line)
    for proj in profile.projects:
        header = f"PROJECT: {proj.title}"
        if proj.tech_stack:
            header += f" [{', '.join(proj.tech_stack)}]"
        lines.append(header)
        for bullet in proj.bullets:
            lines.append(f"  - {bullet}")
    for exp in profile.experience:
        lines.append(f"EXPERIENCE: {exp.title}")
        for bullet in exp.bullets:
            lines.append(f"  - {bullet}")
    if profile.achievements:
        lines.append("ACHIEVEMENTS: " + "; ".join(profile.achievements))
    return "\n".join(lines)
