"""Deterministic merge of a master profile from multiple optional sources
(Section 6.4 onboarding extension): an existing config/master_profile.yaml,
a resume freshly parsed by cutoff.llm.profile_extract, and/or public GitHub
repos / LeetCode stats (cutoff.adapters.developer_footprint).

Deliberately NOT an LLM call, unlike profile_extract.py: every input here is
either already a validated MasterProfile (existing/parsed_resume) or simple
structured data (github repos, leetcode stats) — a deterministic
dedupe-and-union is both simpler and strictly safer than asking an LLM to
"reconcile" several sources into one output. Nothing here can invent a
skill or project that wasn't already in one of the inputs, because nothing
here is generated — only copied, deduplicated, and merged by matching key."""
from __future__ import annotations

from cutoff.models import MasterProfile, MasterProfileProject, ProfileLink


def _dedupe_skills(*skill_lists: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for skills in skill_lists:
        for s in skills:
            key = s.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(s.strip())
    return out


def _dedupe_links(*link_lists: list[ProfileLink]) -> list[ProfileLink]:
    seen: set[str] = set()
    out: list[ProfileLink] = []
    for links in link_lists:
        for link in links:
            key = link.url.strip().lower().rstrip("/")
            if key and key not in seen:
                seen.add(key)
                out.append(link)
    return out


def _merge_project(a: MasterProfileProject, b: MasterProfileProject) -> MasterProfileProject:
    """Keeps `a`'s title (whichever source was seen first); unions
    tech_stack/bullets (order-preserving dedupe); fills in a missing link
    from either side."""
    tech_stack = list(dict.fromkeys([*a.tech_stack, *b.tech_stack]))
    bullets = list(dict.fromkeys([*a.bullets, *b.bullets]))
    return MasterProfileProject(title=a.title, tech_stack=tech_stack, bullets=bullets, link=a.link or b.link)


def _github_repo_to_project(repo: dict) -> MasterProfileProject:
    bullets = [repo["description"]] if repo.get("description") else []
    readme = repo.get("readme_excerpt") or ""
    first_line = next(
        (line.strip() for line in readme.splitlines() if line.strip() and not line.strip().startswith("#")), "",
    )
    if first_line and first_line not in bullets:
        bullets.append(first_line[:300])
    if not bullets:
        bullets = [f"A {repo.get('language') or 'software'} project on GitHub."]
    tech_stack = ([repo["language"]] if repo.get("language") else []) + list(repo.get("topics") or [])
    return MasterProfileProject(title=repo.get("name", "Untitled"), tech_stack=tech_stack, bullets=bullets,
                                 link=repo.get("url"))


def merge_master_profile(
    *, existing: MasterProfile | None = None, parsed_resume: MasterProfile | None = None,
    github_data: dict | None = None, leetcode_data: dict | None = None,
) -> MasterProfile:
    """Every argument is optional — whatever's missing simply isn't merged
    in. Returns a NEW MasterProfile; never mutates the inputs. Projects are
    matched across sources by normalized title OR by a shared link (a resume
    project and its GitHub repo are the same project even if titled
    slightly differently, as long as the link matches); when matched, the
    richer bullets/tech_stack win rather than being listed twice."""
    profile_sources = [p for p in (existing, parsed_resume) if p is not None]

    phone = next((p.phone for p in profile_sources if p.phone), None)
    links = _dedupe_links(*(p.links for p in profile_sources))
    education = list({(e.degree, e.institution): e for p in profile_sources for e in p.education}.values())
    skills = _dedupe_skills(*(p.skills for p in profile_sources))
    experience = list({e.title: e for p in profile_sources for e in p.experience}.values())
    achievements = list(dict.fromkeys(a for p in profile_sources for a in p.achievements))

    projects_by_key: dict[str, MasterProfileProject] = {}
    project_order: list[str] = []

    def _add_project(candidate: MasterProfileProject) -> None:
        title_key = candidate.title.strip().lower()
        link_key = next(
            (k for k, v in projects_by_key.items()
             if v.link and candidate.link and v.link.rstrip("/").lower() == candidate.link.rstrip("/").lower()),
            None,
        )
        key = link_key or (title_key if title_key in projects_by_key else None)
        if key:
            projects_by_key[key] = _merge_project(projects_by_key[key], candidate)
        else:
            projects_by_key[title_key] = candidate
            project_order.append(title_key)

    for p in profile_sources:
        for proj in p.projects:
            _add_project(proj)
    for repo in (github_data or {}).get("repos", []):
        _add_project(_github_repo_to_project(repo))

    if leetcode_data and leetcode_data.get("total_solved"):
        line = f"Solved {leetcode_data['total_solved']}+ problems on LeetCode"
        if leetcode_data.get("ranking"):
            line += f" (global rank ~{leetcode_data['ranking']:,})"
        line += "."
        if line not in achievements:
            achievements.append(line)

    return MasterProfile(
        phone=phone, links=links, education=education, skills=skills,
        projects=[projects_by_key[k] for k in project_order], experience=experience, achievements=achievements,
    )
