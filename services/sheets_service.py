import logging
from datetime import datetime

import gspread
from google.auth import default

logger = logging.getLogger(__name__)

SHEET_NAME = "Jobs"
HEADERS    = [
    "Job ID", "Title", "Company", "Location", "Type",
    "ATS Score", "Why You Match", "Missing Skills",
    "Apply URL", "Notified At", "Status"
]

# Column indices (1-based for gspread)
COL_JOB_ID = 1


def _get_worksheet(sheet_id: str) -> gspread.Worksheet:
    """Get or create the Jobs worksheet."""
    creds, _ = default(scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ])
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(sheet_id)

    try:
        ws = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        logger.info(f"Creating '{SHEET_NAME}' worksheet...")
        ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows=2000, cols=len(HEADERS))
        ws.append_row(HEADERS)
        # Style header row
        ws.format(f"A1:{_col_letter(len(HEADERS))}1", {
            "backgroundColor": {"red": 0.13, "green": 0.37, "blue": 0.73},
            "textFormat": {
                "bold": True,
                "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
            },
            "horizontalAlignment": "CENTER"
        })
        # Freeze header
        spreadsheet.batch_update({"requests": [{
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws.id,
                    "gridProperties": {"frozenRowCount": 1}
                },
                "fields": "gridProperties.frozenRowCount"
            }
        }]})
        logger.info("Worksheet created with header formatting.")

    return ws


def get_seen_job_ids(sheet_id: str) -> set:
    """Read all job IDs already in the sheet (column A, skip header)."""
    ws = _get_worksheet(sheet_id)
    all_ids = ws.col_values(COL_JOB_ID)   # returns list of strings
    seen = set(all_ids[1:])               # skip header row
    logger.info(f"Loaded {len(seen)} seen job IDs from sheet.")
    return seen


def append_jobs(sheet_id: str, jobs: list):
    """
    Append scored jobs to the sheet.
    Writes ALL scored jobs (even below threshold) for a full audit trail.
    """
    if not jobs:
        return

    ws = _get_worksheet(sheet_id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    rows = []
    for job in jobs:
        rows.append([
            job.get("id", ""),
            job.get("title", ""),
            job.get("company", ""),
            job.get("location", ""),
            "Remote" if job.get("remote") else job.get("employment_type", ""),
            f"{job.get('ats_score', 0)}%",
            job.get("why_match", ""),
            job.get("missing", ""),
            job.get("url", ""),
            now,
            "Pending",   # You update this manually: Applied / Ignored / Saved
        ])

    ws.append_rows(rows, value_input_option="USER_ENTERED")
    logger.info(f"Appended {len(rows)} jobs to Google Sheet.")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _col_letter(n: int) -> str:
    """Convert column number to letter (1 → A, 11 → K)."""
    result = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result