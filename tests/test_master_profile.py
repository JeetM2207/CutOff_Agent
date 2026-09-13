"""cutoff.pipeline.master_profile: loading, saving, backing up, and diffing
the student's master profile (Section 6.4 / onboarding extensions). No LLM
or network involved anywhere in this module."""
from cutoff.models import EducationEntry, MasterProfile, MasterProfileProject, ProfileLink
from cutoff.pipeline import master_profile


def test_load_master_profile_returns_none_when_file_missing(tmp_path):
    assert master_profile.load_master_profile(tmp_path / "nope.yaml") is None


def test_load_master_profile_returns_none_for_empty_or_malformed_file(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert master_profile.load_master_profile(empty) is None

    malformed = tmp_path / "bad.yaml"
    malformed.write_text("not: valid: yaml: [", encoding="utf-8")
    assert master_profile.load_master_profile(malformed) is None


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "master_profile.yaml"
    profile = MasterProfile(
        phone="+91-9999999999",
        links=[ProfileLink(label="GitHub", url="https://github.com/riya")],
        education=[EducationEntry(degree="B.Tech CSE", institution="Demo College", cgpa="7.42", batch_year=2026)],
        skills=["Python", "Django"],
        projects=[MasterProfileProject(title="X", tech_stack=["Django"], bullets=["Did X."], link="https://x.example")],
        achievements=["Runner-up, hackathon."],
    )

    master_profile.save_master_profile(profile, path)
    loaded = master_profile.load_master_profile(path)

    assert loaded == profile


def test_save_master_profile_omits_empty_sections(tmp_path):
    """A thin profile's yaml shouldn't be cluttered with empty lists for
    every section that has nothing in it — easier for a student to hand-edit."""
    path = tmp_path / "master_profile.yaml"
    master_profile.save_master_profile(MasterProfile(skills=["Python"]), path)
    text = path.read_text(encoding="utf-8")
    assert "skills" in text
    assert "links" not in text
    assert "projects" not in text


def test_backup_master_profile_returns_none_when_nothing_to_back_up(tmp_path):
    assert master_profile.backup_master_profile(tmp_path / "nope.yaml") is None


def test_backup_master_profile_copies_existing_file(tmp_path):
    path = tmp_path / "master_profile.yaml"
    path.write_text("skills: [Python]", encoding="utf-8")

    backup_path = master_profile.backup_master_profile(path)

    assert backup_path is not None
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == "skills: [Python]"
    assert path.exists()  # the original is untouched, only copied


def test_generate_profile_diff_summary_reports_additions():
    old = MasterProfile(skills=["Python"], projects=[MasterProfileProject(title="X", bullets=["did x"])])
    new = MasterProfile(
        skills=["Python", "Django"],
        projects=[
            MasterProfileProject(title="X", bullets=["did x"]),
            MasterProfileProject(title="Y", bullets=["did y"]),
        ],
        achievements=["Won a hackathon."],
    )

    summary = master_profile.generate_profile_diff_summary(old, new)

    assert "Django" in summary
    new_projects_line = next(line for line in summary.splitlines() if "new projects" in line)
    assert "Y" in new_projects_line
    assert "X" not in new_projects_line  # X already existed in `old`, only Y is new
    assert "Won a hackathon." in summary


def test_generate_profile_diff_summary_detects_updated_projects():
    old = MasterProfile(projects=[MasterProfileProject(title="X", bullets=["did x"])])
    new = MasterProfile(projects=[MasterProfileProject(title="X", bullets=["did x", "did x better"])])

    summary = master_profile.generate_profile_diff_summary(old, new)

    assert "projects updated" in summary
    assert "X" in summary


def test_generate_profile_diff_summary_handles_no_previous_profile():
    new = MasterProfile(skills=["Python"])
    summary = master_profile.generate_profile_diff_summary(None, new)
    assert "Python" in summary


def test_generate_profile_diff_summary_reports_no_changes():
    profile = MasterProfile(skills=["Python"])
    assert master_profile.generate_profile_diff_summary(profile, profile) == "No changes."
