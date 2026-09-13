"""cutoff.pipeline.profile_synthesize: deterministic merge across an existing
profile, a freshly-parsed resume, and optional GitHub/LeetCode data. No LLM
call anywhere in this module (see its own docstring for why) — purely
Python, so every test here is a plain, deterministic assertion."""
from cutoff.models import MasterProfile, MasterProfileExperience, MasterProfileProject, ProfileLink
from cutoff.pipeline.profile_synthesize import merge_master_profile


def test_merge_with_everything_none_returns_an_empty_profile():
    merged = merge_master_profile()
    assert merged == MasterProfile()


def test_merge_unions_and_dedupes_skills_case_insensitively():
    existing = MasterProfile(skills=["Python", "SQL"])
    parsed = MasterProfile(skills=["python", "Django"])  # "python" should dedupe against existing "Python"

    merged = merge_master_profile(existing=existing, parsed_resume=parsed)

    assert merged.skills == ["Python", "SQL", "Django"]


def test_merge_dedupes_links_by_url():
    existing = MasterProfile(links=[ProfileLink(label="GitHub", url="https://github.com/riya/")])
    parsed = MasterProfile(links=[
        ProfileLink(label="GitHub", url="https://github.com/riya"),  # same URL, no trailing slash
        ProfileLink(label="LinkedIn", url="https://linkedin.com/in/riya"),
    ])

    merged = merge_master_profile(existing=existing, parsed_resume=parsed)

    assert len(merged.links) == 2
    assert {l.label for l in merged.links} == {"GitHub", "LinkedIn"}


def test_merge_matches_projects_by_title_and_unions_bullets():
    existing = MasterProfile(projects=[MasterProfileProject(title="Order Service", tech_stack=["Django"],
                                                              bullets=["Built the API."])])
    parsed = MasterProfile(projects=[MasterProfileProject(title="order service", tech_stack=["PostgreSQL"],
                                                            bullets=["Built the API.", "Added caching."])])

    merged = merge_master_profile(existing=existing, parsed_resume=parsed)

    assert len(merged.projects) == 1
    project = merged.projects[0]
    assert set(project.tech_stack) == {"Django", "PostgreSQL"}
    assert project.bullets == ["Built the API.", "Added caching."]  # union, no duplicate


def test_merge_matches_projects_by_shared_link_even_with_different_titles():
    existing = MasterProfile(projects=[
        MasterProfileProject(title="Campus Marketplace", bullets=["A marketplace app."], link="https://github.com/riya/marketplace"),
    ])
    github_data = {"repos": [{
        "name": "marketplace", "description": "A Django marketplace backend.", "url": "https://github.com/riya/marketplace",
        "language": "Python", "topics": ["django"], "stars": 5, "readme_excerpt": "",
    }]}

    merged = merge_master_profile(existing=existing, github_data=github_data)

    assert len(merged.projects) == 1  # matched by link, not duplicated despite the different title
    assert "A Django marketplace backend." in merged.projects[0].bullets


def test_merge_adds_github_repos_as_new_projects_when_unmatched():
    github_data = {"repos": [{
        "name": "cli-tool", "description": "A small CLI tool.", "url": "https://github.com/riya/cli-tool",
        "language": "Python", "topics": [], "stars": 2, "readme_excerpt": "",
    }]}

    merged = merge_master_profile(github_data=github_data)

    assert len(merged.projects) == 1
    assert merged.projects[0].title == "cli-tool"
    assert "A small CLI tool." in merged.projects[0].bullets


def test_merge_never_invents_a_bullet_for_a_repo_with_no_description_or_readme():
    github_data = {"repos": [{"name": "empty-repo", "description": None, "url": "https://github.com/riya/empty-repo",
                               "language": "Python", "topics": [], "stars": 0, "readme_excerpt": ""}]}

    merged = merge_master_profile(github_data=github_data)

    assert merged.projects[0].bullets == ["A Python project on GitHub."]  # honest placeholder, not a fabricated claim


def test_merge_adds_leetcode_stats_as_an_achievement():
    leetcode_data = {"total_solved": 450, "ranking": 25000, "by_difficulty": {"Easy": 200, "Medium": 200, "Hard": 50}}

    merged = merge_master_profile(leetcode_data=leetcode_data)

    assert len(merged.achievements) == 1
    assert "450" in merged.achievements[0]
    assert "25,000" in merged.achievements[0]


def test_merge_skips_leetcode_achievement_when_nothing_solved():
    merged = merge_master_profile(leetcode_data={"total_solved": 0, "ranking": None, "by_difficulty": {}})
    assert merged.achievements == []


def test_merge_dedupes_experience_by_title():
    existing = MasterProfile(experience=[MasterProfileExperience(title="Intern, Acme", bullets=["Did A."])])
    parsed = MasterProfile(experience=[MasterProfileExperience(title="Intern, Acme", bullets=["Did A."])])

    merged = merge_master_profile(existing=existing, parsed_resume=parsed)

    assert len(merged.experience) == 1


def test_merge_never_mutates_its_inputs():
    existing = MasterProfile(skills=["Python"])
    parsed = MasterProfile(skills=["Django"])

    merge_master_profile(existing=existing, parsed_resume=parsed)

    assert existing.skills == ["Python"]  # untouched
    assert parsed.skills == ["Django"]  # untouched
