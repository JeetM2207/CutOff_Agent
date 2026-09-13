"""Loads .env and exposes typed settings. MODE selects fake vs real adapters."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip() in ("1", "true", "True", "yes")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    mode: str
    demo_mode: bool

    llm_provider: str  # "anthropic" | "openrouter" | "gemini"
    anthropic_api_key: str
    openrouter_api_key: str
    openrouter_base_url: str
    gemini_api_key: str
    gemini_base_url: str
    llm_model: str
    llm_model_fast: str

    timezone: str
    db_path: str
    poll_interval_seconds: int

    google_client_secret_file: str
    google_token_file: str
    sheet_id: str
    calendar_id: str
    exam_calendar_id: str
    resume_folder_id: str

    # Dynamic resume generation (Section 6.4 extension): a local, hand-edited
    # Markdown file with every skill/project/metric the student has, never
    # uploaded anywhere. Missing or empty disables the feature entirely and
    # falls back to the static resume_* .pdf matching that already existed —
    # same "must never block planning" guarantee as everything else optional
    # here. generated_resume_dir/public_base_url are how a generated PDF gets
    # a real, clickable link (served by this app's own dashboard server)
    # without ever requesting Drive *write* access — deliberately staying
    # inside the project's existing readonly-only permission footprint.
    master_profile_path: str
    generated_resume_dir: str
    public_base_url: str

    telegram_bot_token: str
    telegram_chat_id: str

    gmail_api_base_url: str
    sheets_api_base_url: str
    calendar_api_base_url: str
    drive_api_base_url: str

    @property
    def is_fake(self) -> bool:
        return self.mode == "fake"

    @property
    def llm_api_key(self) -> str:
        if self.llm_provider == "openrouter":
            return self.openrouter_api_key
        if self.llm_provider == "gemini":
            return self.gemini_api_key
        return self.anthropic_api_key

    @property
    def llm_base_url(self) -> str | None:
        if self.llm_provider == "openrouter":
            return self.openrouter_base_url
        if self.llm_provider == "gemini":
            return self.gemini_base_url
        return None


def get_settings() -> Settings:
    """Reads current env vars fresh on every call — deliberately not baked
    into dataclass field defaults, so a key set mid-process (e.g. by a test,
    or right after `load_dotenv()` picks up an edited .env) is honored."""
    load_dotenv(override=False)  # fills in anything set in .env since import, without clobbering real env vars
    return Settings(
        mode=os.getenv("MODE", "fake"),
        demo_mode=_bool("DEMO_MODE", "0"),
        llm_provider=os.getenv("LLM_PROVIDER", "anthropic"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
        openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_base_url=os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        llm_model=os.getenv("LLM_MODEL", "claude-sonnet-5"),
        llm_model_fast=os.getenv("LLM_MODEL_FAST", "claude-haiku-4-5-20251001"),
        timezone=os.getenv("TIMEZONE", "Asia/Kolkata"),
        db_path=os.getenv("DB_PATH", "cutoff.db"),
        poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 15),
        google_client_secret_file=os.getenv("GOOGLE_CLIENT_SECRET_FILE", "credentials.json"),
        google_token_file=os.getenv("GOOGLE_TOKEN_FILE", "token.json"),
        sheet_id=os.getenv("SHEET_ID", ""),
        calendar_id=os.getenv("CALENDAR_ID", "primary"),
        exam_calendar_id=os.getenv("EXAM_CALENDAR_ID", ""),
        resume_folder_id=os.getenv("RESUME_FOLDER_ID", ""),
        master_profile_path=os.getenv("MASTER_PROFILE_PATH", "config/master_profile.yaml"),
        generated_resume_dir=os.getenv("GENERATED_RESUME_DIR", "generated_resumes"),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        gmail_api_base_url=os.getenv("GMAIL_API_BASE_URL", ""),
        sheets_api_base_url=os.getenv("SHEETS_API_BASE_URL", ""),
        calendar_api_base_url=os.getenv("CALENDAR_API_BASE_URL", ""),
        drive_api_base_url=os.getenv("DRIVE_API_BASE_URL", ""),
    )


PROJECT_ROOT = Path(__file__).resolve().parent.parent
