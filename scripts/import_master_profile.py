"""Bootstraps config/master_profile.yaml from a resume a student already has
(Section 6.4 onboarding extension), instead of typing every skill/project by
hand into the YAML template. Parses the resume PDF (cutoff.llm.profile_extract,
forced tool-use, never invents beyond what's written), optionally enriches it
with public GitHub repos and/or LeetCode stats
(cutoff.adapters.developer_footprint, read-only, best-effort), merges
everything deterministically with whatever's already in master_profile.yaml
(cutoff.pipeline.profile_synthesize — no LLM call in the merge itself),
shows a diff, backs up the old file, and writes the new one. Never silent:
prints exactly what changed before/after writing.

    python -m scripts.import_master_profile path/to/resume.pdf
    python -m scripts.import_master_profile path/to/resume.pdf --github your-username
    python -m scripts.import_master_profile path/to/resume.pdf --github your-username --leetcode your-username
    python -m scripts.import_master_profile path/to/resume.pdf --dry-run   # parse + show the diff, write nothing
"""
from __future__ import annotations

import argparse
from pathlib import Path

from cutoff.adapters.developer_footprint import fetch_github_profile, fetch_leetcode_stats
from cutoff.config import get_settings
from cutoff.llm.profile_extract import extract_profile_from_resume_text
from cutoff.pipeline.ingest import extract_pdf_text
from cutoff.pipeline.master_profile import (
    backup_master_profile, generate_profile_diff_summary, load_master_profile, save_master_profile,
)
from cutoff.pipeline.profile_synthesize import merge_master_profile

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("resume_pdf", help="Path to an existing resume PDF")
    parser.add_argument("--github", default=None, help="Public GitHub username to enrich the profile with")
    parser.add_argument("--leetcode", default=None, help="Public LeetCode username to enrich the profile with")
    parser.add_argument("--dry-run", action="store_true", help="Parse and show the diff, but write nothing")
    args = parser.parse_args()

    settings = get_settings()
    target_path = Path(settings.master_profile_path)
    if not target_path.is_absolute():
        target_path = PROJECT_ROOT / target_path

    resume_bytes = Path(args.resume_pdf).read_bytes()
    resume_text = extract_pdf_text(resume_bytes)
    if not resume_text.strip():
        raise SystemExit(f"Couldn't extract any text from {args.resume_pdf!r} — is it a scanned image PDF?")

    print(f"Extracting profile from {args.resume_pdf} ...")
    parsed_resume = extract_profile_from_resume_text(
        resume_text, api_key=settings.llm_api_key, model=settings.llm_model,
        provider=settings.llm_provider, base_url=settings.llm_base_url, db_path=settings.db_path,
    )
    print(f"  -> {len(parsed_resume.skills)} skills, {len(parsed_resume.projects)} projects, "
          f"{len(parsed_resume.experience)} experience entries, {len(parsed_resume.achievements)} achievements")

    github_data = None
    if args.github:
        print(f"Fetching public GitHub repos for {args.github} ...")
        github_data = fetch_github_profile(args.github)
        print(f"  -> {len(github_data['repos'])} repos")

    leetcode_data = None
    if args.leetcode:
        print(f"Fetching public LeetCode stats for {args.leetcode} ...")
        leetcode_data = fetch_leetcode_stats(args.leetcode)
        if leetcode_data is None:
            print("  -> couldn't fetch LeetCode stats (unreachable, unknown user, or rate-limited) -- skipping")
        else:
            print(f"  -> {leetcode_data['total_solved']} problems solved")

    existing = load_master_profile(target_path)
    merged = merge_master_profile(
        existing=existing, parsed_resume=parsed_resume, github_data=github_data, leetcode_data=leetcode_data,
    )

    print(f"\n=== Changes to {target_path} ===")
    print(generate_profile_diff_summary(existing, merged))

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    backup_path = backup_master_profile(target_path)
    if backup_path:
        print(f"\nBacked up previous profile to {backup_path}")
    save_master_profile(merged, target_path)
    print(f"Wrote {target_path}")
    print("Review it and hand-edit anything the import got wrong before it's used for real.")


if __name__ == "__main__":
    main()
