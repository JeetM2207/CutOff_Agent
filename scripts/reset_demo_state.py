"""Clears everything CutOff has accumulated from live testing, so the board
starts empty for a clean demo. Never touches Profile, Policy, or
FormTemplates in the Sheet, and never touches any Calendar event CutOff
didn't create itself (it only deletes event IDs it finds in its own local
action ledger — never a blanket calendar wipe).

What it clears:
  - Local SQLite (DB_PATH): processed_messages, drives, drive_threads,
    drive_history, actions, approvals, traces, llm_cache.
  - Google Sheet (SHEET_ID): all data rows in the Drives and Log tabs
    (row 1 headers kept). Profile/Policy/FormTemplates are left alone.
  - Google Calendar (CALENDAR_ID): every event whose ID is recorded as a
    completed calendar action in the local ledger above.

Stop the running agent first (Ctrl+C on `python -m cutoff.main`) — deleting
rows out from under an open WAL connection is asking for a write conflict.

    python scripts/reset_demo_state.py            # dry run: lists what would be cleared
    python scripts/reset_demo_state.py --yes       # actually clears it
"""
from __future__ import annotations

import argparse
import sqlite3
from json import loads

from cutoff.adapters.google_auth import build_service, load_credentials
from cutoff.config import get_settings

DB_TABLES = [
    "processed_messages", "drives", "drive_threads", "drive_history",
    "actions", "approvals", "traces", "llm_cache",
]


def _collect_calendar_event_ids(db_path: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT result FROM actions WHERE app = 'calendar' AND result IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    event_ids: list[str] = []
    for (result_json,) in rows:
        try:
            event_id = loads(result_json).get("event_id")
        except (ValueError, AttributeError):
            continue
        if event_id:
            event_ids.append(event_id)
    return sorted(set(event_ids))


def _clear_sheet_tab(service, sheet_id: str, tab: str, last_col: str) -> None:
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"{tab}!A2:{last_col}",
    ).execute()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="Actually delete/clear. Omit for a dry run.")
    args = parser.parse_args()

    settings = get_settings()
    event_ids = _collect_calendar_event_ids(settings.db_path)

    print(f"DB ({settings.db_path}): would clear tables {', '.join(DB_TABLES)}")
    print(f"Sheet ({settings.sheet_id}): would clear all data rows in Drives and Log (headers kept)")
    print(f"Calendar ({settings.calendar_id}): would delete {len(event_ids)} CutOff-created event(s)")
    for eid in event_ids:
        print(f"  - {eid}")

    if not args.yes:
        print("\nDry run only - nothing changed. Re-run with --yes to actually clear this.")
        return

    conn = sqlite3.connect(settings.db_path)
    try:
        for table in DB_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()
    print("DB cleared.")

    creds = load_credentials(settings.google_client_secret_file, settings.google_token_file)
    sheets = build_service("sheets", "v4", creds)
    _clear_sheet_tab(sheets, settings.sheet_id, "Drives", "K")
    _clear_sheet_tab(sheets, settings.sheet_id, "Log", "D")
    print("Sheet cleared (Profile/Policy/FormTemplates untouched).")

    if event_ids:
        calendar = build_service("calendar", "v3", creds)
        for eid in event_ids:
            try:
                calendar.events().delete(calendarId=settings.calendar_id, eventId=eid).execute()
            except Exception as e:
                print(f"  could not delete {eid}: {e}")
    print(f"Calendar cleared ({len(event_ids)} event(s) attempted).")

    print("\nDone. The board starts empty on the next run of `python -m cutoff.main`.")


if __name__ == "__main__":
    main()
