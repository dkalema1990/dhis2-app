"""
gsheets.py
-----------
Google Sheets backend for activity submissions (DQA / SSM / LV) and the
"Users" worksheet used for simple role-based login. Replaces the earlier
SQLite database.py.

SETUP
1. Create a Google Cloud project → enable "Google Sheets API" and
   "Google Drive API".
2. Create a Service Account → generate a JSON key.
3. Create a Google Sheet (any name) and share it with the service
   account's client_email as Editor.
4. In Streamlit secrets (.streamlit/secrets.toml locally, or the
   Streamlit Cloud "Secrets" panel), add:

   gsheet_id = "the-id-from-the-sheet-url"

   [gcp_service_account]
   type = "service_account"
   project_id = "..."
   private_key_id = "..."
   private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
   client_email = "xxx@xxx.iam.gserviceaccount.com"
   client_id = "..."
   token_uri = "https://oauth2.googleapis.com/token"

   (copy these fields straight out of the downloaded service-account JSON)

Worksheets "DQA", "SSM", "LV" and "Users" are created automatically with
the right header row the first time the app runs.
"""

import hashlib
from datetime import datetime

import pandas as pd
import streamlit as st

SHEET_COLUMNS = {
    "DQA": ["facility", "data_element", "period", "reported_value", "verified_value",
            "discrepancy_reason", "root_cause", "corrective_action", "responsible_person",
            "follow_up_date", "submitted_by", "timestamp", "pushed_to_dhis2"],
    "SSM": ["facility", "data_element", "focus_area", "findings", "mentorship_given",
            "score", "recommendations", "responsible_person", "follow_up_date",
            "submitted_by", "timestamp"],
    "LV": ["facility", "data_element", "best_practice", "enabling_factors",
           "lessons_learned", "dissemination_plan", "responsible_person",
           "follow_up_date", "submitted_by", "timestamp"],
    "Users": ["username", "password_hash", "role", "display_name"],
}


def is_configured() -> bool:
    try:
        return "gcp_service_account" in st.secrets and "gsheet_id" in st.secrets
    except Exception:
        # No secrets.toml present at all (e.g. running locally without one yet)
        return False


@st.cache_resource(show_spinner=False)
def _client():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=scopes)
    return gspread.authorize(creds)


def _spreadsheet():
    return _client().open_by_key(st.secrets["gsheet_id"])


def _get_or_create_ws(name: str):
    ss = _spreadsheet()
    try:
        ws = ss.worksheet(name)
    except Exception:
        ws = ss.add_worksheet(title=name, rows=2000, cols=len(SHEET_COLUMNS[name]) + 2)
        ws.append_row(SHEET_COLUMNS[name])
        return ws
    if not ws.row_values(1):
        ws.append_row(SHEET_COLUMNS[name])
    return ws


def init_sheets():
    """Create all worksheets if missing and seed a default admin user."""
    for name in SHEET_COLUMNS:
        _get_or_create_ws(name)
    _seed_default_admin()


def _seed_default_admin():
    ws = _get_or_create_ws("Users")
    if not ws.get_all_records():
        ws.append_row(["admin", hash_password("admin123"), "admin", "Default Admin"])


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


# ---------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------
def append_record(sheet: str, record: dict):
    record = {**record, "timestamp": datetime.now().isoformat(timespec="seconds")}
    ws = _get_or_create_ws(sheet)
    cols = SHEET_COLUMNS[sheet]
    ws.append_row([str(record.get(c, "")) for c in cols])


def get_all(sheet: str) -> pd.DataFrame:
    """Returns all rows plus a `_row` column = the actual Google Sheet row number,
    needed for editing/deleting a specific record."""
    ws = _get_or_create_ws(sheet)
    records = ws.get_all_records()
    df = pd.DataFrame(records)
    if not df.empty:
        df.insert(0, "_row", range(2, len(df) + 2))
    return df


def update_record(sheet: str, row: int, record: dict):
    ws = _get_or_create_ws(sheet)
    cols = SHEET_COLUMNS[sheet]
    last_col_letter = chr(ord("A") + len(cols) - 1)  # fine while columns <= 26
    ws.update(f"A{row}:{last_col_letter}{row}", [[str(record.get(c, "")) for c in cols]])


def delete_record(sheet: str, row: int):
    _get_or_create_ws(sheet).delete_rows(row)


# Thin convenience wrappers so the form pages read naturally
def save_dqa(record: dict):
    append_record("DQA", record)


def save_ssm(record: dict):
    append_record("SSM", record)


def save_lv(record: dict):
    append_record("LV", record)


# ---------------------------------------------------------------------
# Admin edit/delete UI — shared across the three activity tables
# ---------------------------------------------------------------------
def render_admin_management(sheet: str):
    df = get_all(sheet)
    if df.empty:
        return
    with st.expander(f"🛠️ Manage {sheet} records (admin)"):
        options = df["_row"].tolist()
        labels = [f"Row {r} — {df.loc[df['_row'] == r, 'facility'].values[0]}" for r in options]
        idx = st.selectbox("Select record", range(len(options)),
                            format_func=lambda i: labels[i], key=f"manage_sel_{sheet}")
        row = options[idx]
        record = df[df["_row"] == row].iloc[0].to_dict()
        cols = SHEET_COLUMNS[sheet]

        with st.form(f"edit_form_{sheet}_{row}"):
            new_values = {}
            for c in cols:
                new_values[c] = st.text_input(c, value=str(record.get(c, "")))
            c1, c2 = st.columns(2)
            save = c1.form_submit_button("💾 Save changes")
            delete = c2.form_submit_button("🗑️ Delete record", type="secondary")

        if save:
            update_record(sheet, row, new_values)
            st.success("Record updated.")
            st.rerun()
        if delete:
            delete_record(sheet, row)
            st.warning("Record deleted.")
            st.rerun()
