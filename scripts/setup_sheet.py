"""Creates the Profile/Policy/Drives/Log tabs in the configured Google Sheet
and seeds Riya's profile + the college policy (Section 6.2, Section 17).
Reuses eval/profiles/riya.json and policy.json as the seed values, so the
fake-mode eval fixtures and the real Sheet start out consistent.

    python scripts/setup_sheet.py
"""
from __future__ import annotations

import json
from pathlib import Path

from cutoff.adapters.google_auth import build_service, load_credentials
from cutoff.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_SEED = PROJECT_ROOT / "eval" / "profiles" / "riya.json"
POLICY_SEED = PROJECT_ROOT / "eval" / "profiles" / "policy.json"

DRIVES_HEADER = [
    "drive_id", "company", "role", "version", "verdict", "reasons",
    "deadline", "status", "resume", "last_updated", "evidence_link",
]
LOG_HEADER = ["timestamp", "drive_id", "action", "result"]
FORM_TEMPLATES_HEADER = ["form_url", "template_url"]

TAB_NAMES = ["Profile", "Policy", "Drives", "Log", "FormTemplates"]


def _existing_tabs(service, sheet_id: str) -> set[str]:
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return {s["properties"]["title"] for s in meta.get("sheets", [])}


def _ensure_tabs(service, sheet_id: str) -> None:
    existing = _existing_tabs(service, sheet_id)
    missing = [name for name in TAB_NAMES if name not in existing]
    if not missing:
        return
    requests = [{"addSheet": {"properties": {"title": name}}} for name in missing]
    service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()
    print(f"Created tabs: {', '.join(missing)}")


def _write_kv_tab(service, sheet_id: str, tab: str, data: dict) -> None:
    rows = [[str(k), "" if v is None else str(v)] for k, v in data.items()]
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{tab}!A1", valueInputOption="RAW", body={"values": rows},
    ).execute()


def _write_header(service, sheet_id: str, tab: str, header: list[str]) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{tab}!A1", valueInputOption="RAW", body={"values": [header]},
    ).execute()


def _tab_has_data(service, sheet_id: str, tab: str) -> bool:
    resp = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"{tab}!A1:B1").execute()
    return bool(resp.get("values"))


def _profile_kv(profile_data: dict) -> dict:
    # career_office_senders/allowed_form_domains are lists in the JSON seed but
    # single comma-joined cells in the sheet (matches sheets_real.py's reader).
    return profile_data


def _policy_kv(policy_data: dict) -> dict:
    kv = dict(policy_data)
    for list_field in ("career_office_senders", "allowed_form_domains"):
        if isinstance(kv.get(list_field), list):
            kv[list_field] = ",".join(kv[list_field])
    return kv


def main() -> None:
    settings = get_settings()
    if not settings.sheet_id:
        raise SystemExit("SHEET_ID is not set in .env")

    creds = load_credentials(settings.google_client_secret_file, settings.google_token_file)
    service = build_service("sheets", "v4", creds)

    _ensure_tabs(service, settings.sheet_id)

    profile_data = json.loads(PROFILE_SEED.read_text())
    policy_data = json.loads(POLICY_SEED.read_text())
    # Found live: re-running this script (e.g. to add a later tab like
    # FormTemplates) silently clobbered a real Policy tab's hand-edited
    # career_office_senders back to the seed's placeholder value, and every
    # poll after that saw zero mail from the student's real inbox — with no
    # error anywhere, since "no new mail" and "wrong sender filter" look
    # identical from the worker loop. Profile/Policy now only get seeded the
    # first time a tab is genuinely empty; already-populated tabs (real
    # customization, or a previous run of this same script) are left alone.
    if _tab_has_data(service, settings.sheet_id, "Profile"):
        print("Profile tab already has data - leaving it as-is.")
    else:
        _write_kv_tab(service, settings.sheet_id, "Profile", _profile_kv(profile_data))
    if _tab_has_data(service, settings.sheet_id, "Policy"):
        print("Policy tab already has data - leaving it as-is.")
    else:
        _write_kv_tab(service, settings.sheet_id, "Policy", _policy_kv(policy_data))
    _write_header(service, settings.sheet_id, "Drives", DRIVES_HEADER)
    _write_header(service, settings.sheet_id, "Log", LOG_HEADER)
    _write_header(service, settings.sheet_id, "FormTemplates", FORM_TEMPLATES_HEADER)

    print(f"OK: Sheet {settings.sheet_id} set up with Profile, Policy, Drives, Log, FormTemplates tabs.")
    print("Edit the Policy tab's career_office_senders / college_domain to match your real test accounts.")
    print(
        "FormTemplates (Section 6.5 extension): to get a pre-filled registration link, open a form, "
        "fill its fields with the literal tokens student_name / student_roll_no / student_branch / "
        "student_email / student_gpa (see cutoff/pipeline/form_prefill.py), use the form's own "
        "'Get pre-filled link' (the three-dot menu near Send), and paste that URL into FormTemplates column B "
        "next to that drive's form_url in column A."
    )


if __name__ == "__main__":
    main()
