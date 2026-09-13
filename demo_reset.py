"""demo_reset.py: Helper script to reset the board and Google Sheet Drives tab
before starting your demo video recording.
"""
from cutoff import db
from cutoff.config import get_settings
from cutoff.main import _build_adapters

def main():
    settings = get_settings()
    conn = db.get_connection(settings.db_path)

    conn.execute("DELETE FROM drives")
    conn.execute("DELETE FROM drive_history")
    conn.execute("DELETE FROM actions")
    conn.execute("DELETE FROM approvals")
    conn.execute("DELETE FROM processed_messages")
    conn.commit()
    print("Local database cleared: 0 drives on Web Dashboard.")

    try:
        adapters = _build_adapters(settings)
        service = adapters.sheets._service
        sheet_id = settings.sheet_id

        header = [
            ["drive_id", "company", "role", "version", "verdict", "reasons", "deadline", "status", "resume", "last_updated", "evidence_link"]
        ]
        service.spreadsheets().values().clear(spreadsheetId=sheet_id, range="Drives!A1:K50").execute()
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range="Drives!A1:K1",
            valueInputOption="RAW",
            body={"values": header}
        ).execute()
        print("Google Sheet Drives tab reset (clean header only).")
    except Exception as e:
        print("Google Sheet clear notice:", e)

    print("\nReset complete! Ready to start demo recording.")

if __name__ == "__main__":
    main()
