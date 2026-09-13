"""Drive identity resolution and diff/versioning (Section 8, steps 5-6)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from rapidfuzz import fuzz

from cutoff import db
from cutoff.models import Criteria, Drive, Notice

FUZZY_MATCH_THRESHOLD = 90
FUZZY_REVIEW_BAND = (75, 90)

MEANINGFUL_FIELDS = ["criteria", "deadline", "form_url", "events", "status", "salary_lpa", "role_category"]


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "unknown"


def drive_id_for(company: str, role: str, season: str) -> str:
    return f"{slugify(company)}:{slugify(role)}:{season}"


# --- storage -----------------------------------------------------------------

def get_drive(db_path: str, drive_id: str) -> Drive | None:
    conn = db.get_connection(db_path)
    row = conn.execute("SELECT blob FROM drives WHERE drive_id = ?", (drive_id,)).fetchone()
    return Drive.model_validate_json(row["blob"]) if row else None


def save_drive(db_path: str, drive: Drive) -> None:
    conn = db.get_connection(db_path)
    conn.execute(
        "INSERT INTO drives (drive_id, blob, version) VALUES (?, ?, ?) "
        "ON CONFLICT(drive_id) DO UPDATE SET blob = excluded.blob, version = excluded.version",
        (drive.drive_id, drive.model_dump_json(), drive.version),
    )
    conn.commit()


def record_history(db_path: str, drive_id: str, version: int, diff: dict, source_message_id: str) -> None:
    conn = db.get_connection(db_path)
    conn.execute(
        "INSERT INTO drive_history (drive_id, version, diff, source_message_id, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (drive_id, version, json.dumps(diff, default=str), source_message_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_drive_by_thread(db_path: str, thread_id: str) -> Drive | None:
    conn = db.get_connection(db_path)
    row = conn.execute("SELECT drive_id FROM drive_threads WHERE thread_id = ?", (thread_id,)).fetchone()
    return get_drive(db_path, row["drive_id"]) if row else None


def link_thread(db_path: str, thread_id: str, drive_id: str) -> None:
    conn = db.get_connection(db_path)
    conn.execute(
        "INSERT INTO drive_threads (thread_id, drive_id) VALUES (?, ?) "
        "ON CONFLICT(thread_id) DO UPDATE SET drive_id = excluded.drive_id",
        (thread_id, drive_id),
    )
    conn.commit()


def list_open_drives(db_path: str) -> list[Drive]:
    conn = db.get_connection(db_path)
    rows = conn.execute("SELECT blob FROM drives").fetchall()
    return [d for r in rows if (d := Drive.model_validate_json(r["blob"])).status == "OPEN"]


# --- resolution ----------------------------------------------------------------

@dataclass
class ResolveResult:
    drive: Drive | None
    method: str  # "thread" | "slug" | "fuzzy" | "new" | "fuzzy_ambiguous" | "none"
    needs_review_question: str | None = None


def resolve_drive(db_path: str, thread_id: str, notice: Notice, batch_season: str) -> ResolveResult:
    drive = get_drive_by_thread(db_path, thread_id)
    if drive is not None:
        return ResolveResult(drive=drive, method="thread")

    if notice.company and notice.role:
        candidate_id = drive_id_for(notice.company, notice.role, batch_season)
        drive = get_drive(db_path, candidate_id)
        if drive is not None:
            return ResolveResult(drive=drive, method="slug")

    if notice.company:
        scored = []
        for d in list_open_drives(db_path):
            # partial_ratio, not ratio: a short/partial company reference
            # ("the Solstice drive") is a real thing recruiters write, and
            # plain ratio penalizes it purely for being shorter than the
            # full name it's actually naming — found live (dev_034): scored
            # "Solstice" at only ~57% against "Solstice Innovations", well
            # below the review band, so two genuinely open "Solstice ..."
            # drives never triggered the ambiguous "which one?" question at
            # all. partial_ratio finds the best-aligned substring match
            # instead, scoring a true prefix/substring match near 100 while
            # still keeping two full, merely similar-sounding company names
            # (e.g. "Zentrix Robotics" vs "Zentrix Analytics", ~69%) safely
            # below the review band — verified against the existing dev
            # fixtures' company names before switching.
            company_score = fuzz.partial_ratio(notice.company.lower(), d.company.lower())
            if company_score >= FUZZY_REVIEW_BAND[0]:
                scored.append((company_score, d))
        strong = [(s, d) for s, d in scored if s >= FUZZY_MATCH_THRESHOLD]
        if len(strong) == 1:
            return ResolveResult(drive=strong[0][1], method="fuzzy")
        if scored:
            names = ", ".join(f"{d.company} ({d.role})" for _, d in scored[:3])
            return ResolveResult(
                drive=None, method="fuzzy_ambiguous",
                needs_review_question=f"Is this about {names}?",
            )

    if notice.notice_type == "NEW_DRIVE":
        return ResolveResult(drive=None, method="new")
    return ResolveResult(
        drive=None, method="none",
        needs_review_question="Update for a drive I haven't seen.",
    )


# --- diff / versioning -----------------------------------------------------------

def merge_criteria(old: Criteria, new: Criteria) -> Criteria:
    data = old.model_dump()
    new_data = new.model_dump()
    for field in ("min_gpa", "gpa_inclusive", "max_active_backlogs", "min_10th_pct", "min_12th_pct"):
        if new_data[field] is not None:
            data[field] = new_data[field]
    if new_data["branches_allowed"]:
        data["branches_allowed"] = new_data["branches_allowed"]
    if new_data["branches_text"]:
        data["branches_text"] = new_data["branches_text"]
    if new_data["batch_years"]:
        data["batch_years"] = new_data["batch_years"]
    if new_data["other_conditions"]:
        data["other_conditions"] = list(dict.fromkeys(data["other_conditions"] + new_data["other_conditions"]))
    return Criteria.model_validate(data)


def apply_notice(existing: Drive | None, notice: Notice, message_id: str, batch_season: str) -> tuple[Drive, dict, bool]:
    """Applies `notice` to `existing` (or creates a new Drive). Returns
    (drive, diff, changed) where `changed` is True only if a meaningful field
    changed (Section 8 step 6: a REMINDER with no changes bumps nothing)."""
    if notice.notice_type == "CANCELLATION":
        merged_criteria = existing.criteria if existing else notice.criteria
    else:
        merged_criteria = merge_criteria(existing.criteria if existing else Criteria(), notice.criteria)

    if existing is None:
        drive_id = drive_id_for(notice.company or "unknown", notice.role or "unknown", batch_season)
        drive = Drive(
            drive_id=drive_id,
            company=notice.company or "Unknown",
            role=notice.role or "Unknown",
            version=1,
            status="CANCELLED" if notice.notice_type == "CANCELLATION" else "OPEN",
            criteria=merged_criteria,
            deadline=notice.deadline,
            form_url=notice.form_url,
            events=notice.events,
            source_message_ids=[message_id],
            history=[],
            salary_lpa=notice.salary_lpa,
            role_category=notice.role_category,
        )
        return drive, {"created": True}, True

    before = existing.model_dump()
    updated = existing.model_copy(deep=True)
    updated.source_message_ids = list(dict.fromkeys([*existing.source_message_ids, message_id]))
    updated.criteria = merged_criteria

    if notice.notice_type == "CANCELLATION":
        updated.status = "CANCELLED"
    if notice.deadline is not None:
        updated.deadline = notice.deadline
    if notice.form_url is not None:
        updated.form_url = notice.form_url
    if notice.events:
        updated.events = notice.events
    if notice.salary_lpa is not None:
        updated.salary_lpa = notice.salary_lpa
    if notice.role_category is not None:
        updated.role_category = notice.role_category

    after = updated.model_dump()
    changed_fields = [f for f in MEANINGFUL_FIELDS if before.get(f) != after.get(f)]
    changed = bool(changed_fields)
    if changed:
        updated.version = existing.version + 1

    diff = {
        "changed_fields": changed_fields,
        "notice_type": notice.notice_type,
        "change_summary": notice.change_summary,
    }
    return updated, diff, changed
