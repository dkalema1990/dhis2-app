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
import functools
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
    "AchievementData": ["facility", "data_element", "period", "actual", "target",
                          "achievement_pct", "published_by", "published_at"],
}


def is_configured() -> bool:
    try:
        return "gcp_service_account" in st.secrets and "gsheet_id" in st.secrets
    except Exception:
        # No secrets.toml present at all (e.g. running locally without one yet)
        return False


def _friendly_gspread_error(e) -> str:
    """Turn a gspread.exceptions.APIError into a readable message with Google's
    own status + reason, instead of a bare traceback."""
    try:
        body = e.response.json()
        err = body.get("error", {})
        status = err.get("status", e.response.status_code)
        message = err.get("message", e.response.text)
        return f"Google Sheets API error ({status}): {message}"
    except Exception:
        return f"Google Sheets API error: {e}"


def _wrap_gspread_errors(fn):
    """Decorator: catch gspread.exceptions.APIError and re-raise as a plain
    RuntimeError with a readable message, so the app can show it instead of
    crashing with a redacted traceback."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        import gspread
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            raise RuntimeError(_friendly_gspread_error(e)) from e
    return wrapper


@st.cache_resource(show_spinner=False)
def _client():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=scopes)
    return gspread.authorize(creds)


@_wrap_gspread_errors
def _spreadsheet():
    return _client().open_by_key(st.secrets["gsheet_id"])


@st.cache_resource(show_spinner=False)
@_wrap_gspread_errors
def _get_or_create_ws(name: str):
    """Cached as a resource: the worksheet lookup + header check only happens
    once per running app process, not on every Streamlit rerun. This is the
    single biggest fix for hitting Google Sheets' read-quota — without this,
    every click anywhere in the app re-reads every worksheet's header row."""
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
    """Create all worksheets if missing and seed a default admin user.
    Guarded to run at most once per Streamlit session — this used to run on
    every single rerun of the login page (every failed login, every widget
    interaction), which alone was enough to exhaust the read quota."""
    if st.session_state.get("_sheets_initialized"):
        return
    for name in SHEET_COLUMNS:
        _get_or_create_ws(name)
    _seed_default_admin()
    st.session_state["_sheets_initialized"] = True


def _seed_default_admin():
    ws = _get_or_create_ws("Users")
    if not _wrap_gspread_errors(ws.get_all_records)():
        _wrap_gspread_errors(ws.append_row)(["admin", hash_password("admin123"), "admin", "Default Admin"])


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


# ---------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------
def append_record(sheet: str, record: dict):
    record = {**record, "timestamp": datetime.now().isoformat(timespec="seconds")}
    ws = _get_or_create_ws(sheet)
    cols = SHEET_COLUMNS[sheet]
    _wrap_gspread_errors(ws.append_row)([str(record.get(c, "")) for c in cols])
    get_all.clear()  # this sheet changed — next read should be fresh, not the cached one


@st.cache_data(ttl=30, show_spinner=False)
def get_all(sheet: str) -> pd.DataFrame:
    """Returns all rows plus a `_row` column = the actual Google Sheet row number,
    needed for editing/deleting a specific record. Cached for 30s: this is the
    other big read-quota fix — every dashboard render, tab switch, or filter
    change used to re-read the sheet from scratch."""
    ws = _get_or_create_ws(sheet)
    records = _wrap_gspread_errors(ws.get_all_records)()
    df = pd.DataFrame(records)
    if not df.empty:
        df.insert(0, "_row", range(2, len(df) + 2))
    return df


def update_record(sheet: str, row: int, record: dict):
    ws = _get_or_create_ws(sheet)
    cols = SHEET_COLUMNS[sheet]
    last_col_letter = chr(ord("A") + len(cols) - 1)  # fine while columns <= 26
    _wrap_gspread_errors(ws.update)(f"A{row}:{last_col_letter}{row}",
                                      [[str(record.get(c, "")) for c in cols]])
    get_all.clear()


def delete_record(sheet: str, row: int):
    ws = _get_or_create_ws(sheet)
    _wrap_gspread_errors(ws.delete_rows)(row)
    get_all.clear()


# ---------------------------------------------------------------------
# "Published" achievement analysis — lets an admin/submitter's pulled
# DHIS2 data + applied targets be shared with everyone (including
# `viewer` users), instead of only existing in their own browser session.
# ---------------------------------------------------------------------
def publish_achievement_data(df: pd.DataFrame, published_by: str):
    """Overwrite the shared AchievementData sheet with the given dataset —
    this becomes what every viewer sees by default until republished."""
    cols = SHEET_COLUMNS["AchievementData"]
    out = df.copy()
    for c in ["facility", "data_element", "period", "actual", "target", "achievement_pct"]:
        if c not in out.columns:
            out[c] = ""
    out["published_by"] = published_by
    out["published_at"] = datetime.now().isoformat(timespec="seconds")
    out = out[cols].fillna("")

    ws = _get_or_create_ws("AchievementData")
    rows = [cols] + out.astype(str).values.tolist()
    _wrap_gspread_errors(ws.clear)()
    _wrap_gspread_errors(ws.update)(rows)
    get_all.clear()


def get_published_achievement_data() -> pd.DataFrame:
    """The shared analysis, with actual/target/achievement_pct coerced back
    to numeric (Sheets stores everything as text)."""
    df = get_all("AchievementData")
    if df.empty:
        return df
    for c in ["actual", "target", "achievement_pct"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


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