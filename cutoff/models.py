"""Pydantic v2 data model (Section 7). All datetimes are stored/passed as UTC;
convert to the profile's timezone only for display."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# --- Mail -------------------------------------------------------------------

class AttachmentMeta(BaseModel):
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int = 0


class EmailRef(BaseModel):
    message_id: str
    thread_id: str
    from_addr: str
    subject: str
    received_at: datetime


class EmailMessage(BaseModel):
    message_id: str
    thread_id: str
    from_addr: str
    subject: str
    body_text: str
    received_at: datetime
    attachments: list[AttachmentMeta] = Field(default_factory=list)


# --- Extraction ---------------------------------------------------------------

class Criteria(BaseModel):
    min_gpa: float | None = None
    gpa_inclusive: bool | None = None  # None = wording unclear
    branches_allowed: list[str] = Field(default_factory=list)
    branches_text: str | None = None  # raw wording kept for review
    max_active_backlogs: int | None = None
    min_10th_pct: float | None = None
    min_12th_pct: float | None = None
    batch_years: list[int] = Field(default_factory=list)
    other_conditions: list[str] = Field(default_factory=list)


class DriveEvent(BaseModel):
    kind: Literal["TEST", "INTERVIEW", "TALK", "OTHER"]
    start: datetime
    end: datetime | None = None
    location_or_link: str | None = None


class Evidence(BaseModel):
    field: str
    quote: str


class Notice(BaseModel):
    notice_type: Literal[
        "NEW_DRIVE", "REVISION", "CANCELLATION", "SHORTLIST",
        "SCHEDULE", "REMINDER", "NON_DRIVE", "SUSPICIOUS",
    ]
    company: str | None = None
    role: str | None = None
    role_category: Literal["SDE", "DATA", "CORE", "PRODUCT", "BUSINESS", "OTHER"] | None = None
    salary_lpa: float | None = None
    criteria: Criteria = Field(default_factory=Criteria)
    deadline_text: str | None = None
    deadline: datetime | None = None
    form_url: str | None = None
    events: list[DriveEvent] = Field(default_factory=list)
    change_summary: str | None = None  # for REVISION / CANCELLATION
    suspicion_signals: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    unverified_fields: list[str] = Field(default_factory=list)  # filled by the grounding validator


# --- Canonical drive ----------------------------------------------------------

class Drive(BaseModel):
    drive_id: str  # slug: "zentrix-analytics:data-analyst:2026"
    company: str
    role: str
    version: int = 1
    status: Literal["OPEN", "CLOSED", "CANCELLED"] = "OPEN"
    criteria: Criteria = Field(default_factory=Criteria)
    deadline: datetime | None = None
    form_url: str | None = None
    events: list[DriveEvent] = Field(default_factory=list)
    source_message_ids: list[str] = Field(default_factory=list)
    history: list[dict] = Field(default_factory=list)
    # Not listed explicitly in Section 7's Drive model, but needed across revisions
    # for the policy one-offer check and resume selection (see NOTES.md, Phase 1).
    salary_lpa: float | None = None
    role_category: Literal["SDE", "DATA", "CORE", "PRODUCT", "BUSINESS", "OTHER"] | None = None
    registered: bool = False
    last_verdict: Literal["ELIGIBLE", "NOT_ELIGIBLE", "NEEDS_REVIEW"] | None = None
    # Dynamic resume generation (Section 6.4 extension). jd_text is captured
    # once at extraction time so a resume choice made later, asynchronously,
    # from a Telegram callback (long after the original email's attachment
    # text is out of scope) still has the job description to generate from.
    # resolved_resume_pick records the student's answer to "use my resume on
    # file, or generate one?" (a plain dict, not a ResumeFile, to keep Drive's
    # own schema dependency-free) so a later revision to the same drive shows
    # the same resume instead of asking again.
    jd_text: str | None = None
    resolved_resume_pick: dict | None = None
    # Interview-prep intel (new extension: cutoff.pipeline.prep_intel). None
    # means "never attempted yet"; once attempted (success or not), always a
    # dict with an "attempted" marker — fetched at most once per drive, not
    # re-searched on every subsequent revision/reminder replan.
    prep_intel: dict | None = None


# --- Student / policy -----------------------------------------------------------

class StudentProfile(BaseModel):
    name: str
    roll_no: str
    email: str
    branch: str
    gpa: float
    gpa_scale: float = 10.0
    active_backlogs: int
    pct_10th: float | None = None
    pct_12th: float | None = None
    batch_year: int
    placed_status: Literal["UNPLACED", "PLACED"] = "UNPLACED"
    current_offer_lpa: float | None = None
    timezone: str = "Asia/Kolkata"


# --- Master profile (Section 6.4 extension: dynamic resume generation) -------
# A local, hand-edited file (config/master_profile.yaml, gitignored) is the
# ONLY source of truth for dynamic resume generation — see
# cutoff.llm.resume_generate's grounding rules. Education/experience/
# achievements/links are rendered onto the generated resume deterministically
# (never re-decided by the LLM); only headline/skills-subset/project-subset
# are LLM-tailored per job description.

class ProfileLink(BaseModel):
    label: str  # "GitHub", "LinkedIn", "Portfolio", etc.
    url: str


class EducationEntry(BaseModel):
    degree: str  # e.g. "B.Tech, Computer Science and Engineering"
    institution: str
    cgpa: str | None = None
    batch_year: int | None = None
    notes: str | None = None  # relevant coursework, honors, etc.


class MasterProfileProject(BaseModel):
    title: str
    tech_stack: list[str] = Field(default_factory=list)
    bullets: list[str]
    link: str | None = None  # the project's repo (e.g. GitHub) URL
    demo_link: str | None = None  # a live/hosted demo URL, shown alongside `link` if present


class MasterProfileExperience(BaseModel):
    title: str  # "Backend Engineering Intern, Company, Summer 2025"
    bullets: list[str]


class MasterProfile(BaseModel):
    phone: str | None = None
    links: list[ProfileLink] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    projects: list[MasterProfileProject] = Field(default_factory=list)
    # Always shown in full on a generated resume, never LLM-selected — real
    # resumes don't usually need "tailoring" of which internships to list.
    experience: list[MasterProfileExperience] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)


class CollegePolicy(BaseModel):
    career_office_senders: list[str] = Field(default_factory=list)
    college_domain: str
    one_offer_rule: bool = False
    dream_multiplier: float = 1.0
    no_show_penalty_text: str = ""
    allowed_form_domains: list[str] = Field(
        default_factory=lambda: ["docs.google.com", "forms.gle"]
    )


class Verdict(BaseModel):
    result: Literal["ELIGIBLE", "NOT_ELIGIBLE", "NEEDS_REVIEW"]
    reasons: list[str] = Field(default_factory=list)  # GPA, BRANCH, BACKLOGS, PCT_10, PCT_12, BATCH, POLICY_ONE_OFFER
    questions: list[str] = Field(default_factory=list)  # for NEEDS_REVIEW


# --- Actions --------------------------------------------------------------------

class Action(BaseModel):
    action_id: str  # uuid4
    idempotency_key: str  # UNIQUE, e.g. "cal:<drive_id>:deadline"
    drive_id: str
    drive_version: int
    app: Literal["sheets", "calendar", "telegram", "drive"]
    kind: str  # upsert_row | upsert_event | cancel_event | send_msg | edit_msg
    tier: Literal["T1_REVERSIBLE", "T2_NEEDS_HUMAN"]
    payload: dict = Field(default_factory=dict)
    status: Literal[
        "PLANNED", "AWAITING_APPROVAL", "APPROVED", "EXECUTING",
        "DONE", "VERIFIED", "FAILED", "VOIDED",
    ] = "PLANNED"
    attempts: int = 0
    last_error: str | None = None
    lease_until: datetime | None = None
    result: dict | None = None


# --- Adapter-facing helper types ---------------------------------------------

class CalendarEvent(BaseModel):
    event_id: str
    title: str
    start: datetime
    end: datetime | None = None
    description: str | None = None
    location: str | None = None
    status: Literal["confirmed", "cancelled", "tentative"] = "confirmed"


class ResumeFile(BaseModel):
    file_id: str
    name: str
    web_view_link: str


class Button(BaseModel):
    text: str
    callback_data: str
