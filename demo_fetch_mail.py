"""demo_fetch_mail.py: Helper script for the hackathon demo video.
Fetches the campus placement drive email from Gmail, evaluates eligibility
against the student's Google Sheet profile, updates the web dashboard,
and sends the interactive Telegram choice card to the student.
"""
from datetime import datetime, timezone
from cutoff.config import get_settings
from cutoff.llm.extract import extract_notice
from cutoff.main import _build_adapters, _load_master_profile
from cutoff.pipeline import executor, form_autofill, run

def main():
    print("[1/4] Loading Cutoff settings and adapters...")
    settings = get_settings()
    adapters = _build_adapters(settings)
    
    print("[2/4] Reading Student Profile & Policy from Google Sheets...")
    profile = adapters.sheets.read_profile()
    policy = adapters.sheets.read_policy()
    print(f"      Student: {profile.name} ({profile.roll_no}, {profile.branch}) | CGPA: {profile.gpa}")

    ctx = run.PipelineContext(
        mail=adapters.mail,
        files=adapters.files,
        extract_fn=extract_notice,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timezone_name=settings.timezone,
        db_path=settings.db_path,
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        calendar=adapters.calendar,
        sheets=adapters.sheets,
        autofill_form_fn=form_autofill.build_autofilled_url,
        master_profile=_load_master_profile(settings),
        generated_resume_dir=settings.generated_resume_dir,
        public_base_url=settings.public_base_url,
        enable_prep_intel=settings.enable_prep_intel,
    )

    print("[3/4] Fetching latest campus drive email from Gmail...")
    # Get the latest placement email (Meta campus drive)
    msg_id = "1a09c687ae891e89"
    msg = adapters.mail.get(msg_id)
    print(f"      Subject: {msg.subject}")
    print(f"      From:    {msg.from_addr}")

    print("[4/4] Ingesting through Cutoff zero-hallucination pipeline...")
    now = datetime.now(timezone.utc)
    res = run.process_message(msg, ctx, profile=profile, policy=policy, now=now)
    print(f"      Pipeline result: run_id={res.run_id}")

    exec_adapters = executor.Adapters(
        sheets=adapters.sheets,
        calendar=adapters.calendar,
        messenger=adapters.messenger,
        files=adapters.files,
    )
    breaker = executor.shared_breaker(settings.db_path)
    executor.run_pending(settings.db_path, exec_adapters, breaker)
    print("\nSUCCESS! Placement card delivered to Telegram (@hackathon_cutoff_bot) & synced to Web Dashboard!")

if __name__ == "__main__":
    main()
