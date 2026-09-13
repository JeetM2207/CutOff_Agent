"""Loads and formats the student's master profile (Section 6.4 extension) — a
local YAML file listing everything they have: education, skills, projects,
experience, links (GitHub/LinkedIn/portfolio/etc.), achievements. This is the
ONLY source of truth dynamic resume generation is allowed to draw from — see
cutoff.llm.resume_generate's grounding rules.

Also holds the file-management helpers for the onboarding-import flow
(Section 6.4 onboarding extension: cutoff.llm.profile_extract +
cutoff.pipeline.profile_synthesize) — saving a freshly-built profile,
backing up whatever was there before overwriting it, and summarizing what
changed, so an import is never a silent, unreviewable overwrite."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
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


def backup_master_profile(path: str | Path) -> Path | None:
    """Copies the existing file to a timestamped `.bak` sibling before an
    import overwrites it. Returns the backup path, or None if there was
    nothing to back up (first-ever import)."""
    p = Path(path)
    if not p.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = p.with_name(f"{p.stem}.{stamp}.bak{p.suffix}")
    shutil.copy2(p, backup_path)
    return backup_path


def save_master_profile(profile: MasterProfile, path: str | Path) -> None:
    """Writes `profile` as clean, human-editable YAML — the student should
    always be able to open this file afterward and hand-correct anything
    the import got wrong. Does NOT back up the previous file itself; call
    backup_master_profile first if that matters to the caller."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = profile.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
    # Drop empty top-level lists/None so a thin profile's yaml isn't cluttered
    # with "links: []" for every section that has nothing in it yet.
    data = {k: v for k, v in data.items() if v not in (None, [], "")}
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False),
                 encoding="utf-8")


def generate_profile_diff_summary(old: MasterProfile | None, new: MasterProfile) -> str:
    """A concise Markdown summary of what an import would change — added
    skills, new or updated projects, new achievements — shown to the
    student before anything is written (never silent)."""
    old = old or MasterProfile()
    lines: list[str] = []

    old_skills = {s.lower() for s in old.skills}
    added_skills = [s for s in new.skills if s.lower() not in old_skills]
    if added_skills:
        lines.append(f"**+{len(added_skills)} skills:** {', '.join(added_skills)}")

    old_projects = {p.title.lower(): p for p in old.projects}
    new_project_titles, updated_project_titles = [], []
    for proj in new.projects:
        key = proj.title.lower()
        if key not in old_projects:
            new_project_titles.append(proj.title)
        elif old_projects[key].model_dump() != proj.model_dump():
            updated_project_titles.append(proj.title)
    if new_project_titles:
        lines.append(f"**+{len(new_project_titles)} new projects:** {', '.join(new_project_titles)}")
    if updated_project_titles:
        lines.append(f"**{len(updated_project_titles)} projects updated:** {', '.join(updated_project_titles)}")

    old_achievements = set(old.achievements)
    added_achievements = [a for a in new.achievements if a not in old_achievements]
    if added_achievements:
        lines.append(f"**+{len(added_achievements)} achievements:** {'; '.join(added_achievements)}")

    old_links = {link.url.rstrip("/").lower() for link in old.links}
    added_links = [link for link in new.links if link.url.rstrip("/").lower() not in old_links]
    if added_links:
        lines.append(f"**+{len(added_links)} links:** {', '.join(f'{l.label}: {l.url}' for l in added_links)}")

    if not lines:
        return "No changes."
    return "\n".join(lines)
