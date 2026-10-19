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

# Location + attachments recorded with an activity. Appended at the END of each tab so existing tabs
# upgrade in place (ensure_kobo_columns adds the headers; existing rows are untouched).
EVIDENCE_COLUMNS = ["latitude", "longitude", "gps_accuracy_m", "photo_links", "attendance_links"]

SHEET_COLUMNS = {
    "DQA": ["facility", "data_element", "period", "reported_value", "verified_value",
            "discrepancy_reason", "root_cause", "corrective_action", "responsible_person",
            "follow_up_date", "submitted_by", "timestamp", "pushed_to_dhis2", "kobo_id"] + EVIDENCE_COLUMNS,
    "SSM": ["facility", "data_element", "focus_area", "findings", "mentorship_given",
            "score", "recommendations", "responsible_person", "follow_up_date",
            "submitted_by", "timestamp", "kobo_id"] + EVIDENCE_COLUMNS + ["visit_ref"],
    "LV": ["facility", "data_element", "best_practice", "enabling_factors",
           "lessons_learned", "dissemination_plan", "responsible_person",
           "follow_up_date", "submitted_by", "timestamp", "kobo_id"] + EVIDENCE_COLUMNS,
    "Users": ["username", "password_hash", "role", "display_name"],
    "AchievementData": ["facility", "data_element", "period", "actual", "target",
                          "achievement_pct", "published_by", "published_at"],
    "Targets": ["facility", "data_element", "target", "updated_by", "updated_at"],
    "CalculatedFields": ["name", "formula", "created_by", "created_at"],
    "KoboDeleted": ["sheet", "kobo_id", "deleted_by", "deleted_at"],
    # Competency assessments: one row per item scored, per assessment (see competency.py)
    "Competency": ["assessment_id", "visit_ref", "facility", "mentee_name", "cadre", "service_area",
                   "assessment_type", "assessment_date", "item_id", "competency", "score", "assessor",
                   "comments", "timestamp", "kobo_id"],
    "CompetencyItems": ["item_id", "service_area", "domain", "competency", "active"],
}

# Tabs that can receive Kobo submissions (each has a trailing `kobo_id` column)
KOBO_SHEETS = ("DQA", "SSM", "LV")


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
        if sheet == "SSM" and "score" in df.columns:
            # blank scores (filled later from the competency average) must not break averages/charts
            df["score"] = pd.to_numeric(df["score"], errors="coerce")
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
    if sheet in KOBO_SHEETS:
        # If this row was imported from Kobo, remember its ID so a later sync doesn't re-import it.
        kobo_id = ""
        try:
            idx = SHEET_COLUMNS[sheet].index("kobo_id")
            vals = _wrap_gspread_errors(ws.row_values)(row)
            kobo_id = str(vals[idx]).strip() if len(vals) > idx else ""
        except RuntimeError:
            pass
        if kobo_id:
            who = (st.session_state.get("user") or {}).get("display_name", "")
            _wrap_gspread_errors(_get_or_create_ws("KoboDeleted").append_row)(
                [sheet, kobo_id, who, datetime.now().isoformat(timespec="seconds")])
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


# ---------------------------------------------------------------------
# Persistent targets — these used to live only in st.session_state (lost
# on logout/reboot). Now stored in their own "Targets" sheet, shared and
# permanent: upsert by (facility, data_element), so entering a target once
# is never lost, and newly-downloaded templates always come pre-filled
# with whatever's already been set, even across logins/devices.
# ---------------------------------------------------------------------
def save_targets(new_targets: pd.DataFrame, updated_by: str) -> pd.DataFrame:
    """Merge new_targets (facility, data_element, target) into whatever's
    already persisted — new values win for matching rows, everything else
    is kept as-is — then write the combined result back. Returns the
    combined DataFrame."""
    cols = SHEET_COLUMNS["Targets"]
    existing = get_all("Targets")
    if not existing.empty:
        existing = existing.drop(columns=["_row"], errors="ignore")

    stamped = new_targets[["facility", "data_element", "target"]].copy()
    stamped["updated_by"] = updated_by
    stamped["updated_at"] = datetime.now().isoformat(timespec="seconds")

    if existing.empty:
        combined = stamped
    else:
        combined = (pd.concat([existing, stamped], ignore_index=True)
                      .drop_duplicates(subset=["facility", "data_element"], keep="last"))
    combined = combined[cols].fillna("")

    ws = _get_or_create_ws("Targets")
    rows = [cols] + combined.astype(str).values.tolist()
    _wrap_gspread_errors(ws.clear)()
    _wrap_gspread_errors(ws.update)(rows)
    get_all.clear()
    return combined


def get_targets() -> pd.DataFrame:
    """All persisted targets, with `target` coerced to numeric."""
    df = get_all("Targets")
    if df.empty:
        return df
    df = df.drop(columns=["_row"], errors="ignore")
    df["target"] = pd.to_numeric(df["target"], errors="coerce")
    return df


def clear_targets():
    """Wipe all persisted targets (header row kept) — affects everyone."""
    ws = _get_or_create_ws("Targets")
    _wrap_gspread_errors(ws.clear)()
    _wrap_gspread_errors(ws.append_row)(SHEET_COLUMNS["Targets"])
    get_all.clear()


# ---------------------------------------------------------------------
# Calculated fields — definitions (name + formula) persisted and shared.
# ---------------------------------------------------------------------
def get_calculated_fields() -> pd.DataFrame:
    df = get_all("CalculatedFields")
    if not df.empty:
        df = df.drop(columns=["_row"], errors="ignore")
    return df


def save_calculated_field(name: str, formula: str, created_by: str):
    """Upsert by name — redefining a calculated field replaces its formula."""
    cols = SHEET_COLUMNS["CalculatedFields"]
    existing = get_calculated_fields()
    new_row = pd.DataFrame([{"name": name, "formula": formula, "created_by": created_by,
                             "created_at": datetime.now().isoformat(timespec="seconds")}])
    combined = new_row if existing.empty else (
        pd.concat([existing, new_row], ignore_index=True).drop_duplicates(subset=["name"], keep="last"))
    combined = combined[cols].fillna("")
    ws = _get_or_create_ws("CalculatedFields")
    _wrap_gspread_errors(ws.clear)()
    _wrap_gspread_errors(ws.update)([cols] + combined.astype(str).values.tolist())
    get_all.clear()


def delete_calculated_field(name: str):
    existing = get_calculated_fields()
    if existing.empty:
        return
    cols = SHEET_COLUMNS["CalculatedFields"]
    remaining = existing[existing["name"] != name]
    ws = _get_or_create_ws("CalculatedFields")
    _wrap_gspread_errors(ws.clear)()
    _wrap_gspread_errors(ws.update)([cols] + remaining[cols].astype(str).values.tolist())
    get_all.clear()


# ---------------------------------------------------------------------
# Kobo support: bulk import, duplicate protection, header upgrade
# ---------------------------------------------------------------------
def ensure_kobo_columns():
    """Tabs created before Kobo support lack the trailing `kobo_id` column. Add the header
    in place (existing rows are untouched). Refuses if the header was customised, rather
    than guessing."""
    for sheet in KOBO_SHEETS:
        ws = _get_or_create_ws(sheet)
        expected = SHEET_COLUMNS[sheet]
        header = _wrap_gspread_errors(ws.row_values)(1)
        if header == expected:
            continue
        if header == expected[:len(header)]:
            last = chr(ord("A") + len(expected) - 1)
            _wrap_gspread_errors(ws.update)(range_name=f"A1:{last}1", values=[expected])
        else:
            raise RuntimeError(
                f"The header row of the '{sheet}' tab doesn't match what the app expects, so it can't be "
                f"upgraded automatically. Expected: {', '.join(expected)}")
    get_all.clear()


def get_kobo_ids(sheet: str) -> set:
    """Kobo submission IDs already imported into this tab."""
    df = get_all(sheet)
    if df.empty or "kobo_id" not in df.columns:
        return set()
    return {str(v).strip() for v in df["kobo_id"] if str(v).strip()}


def get_kobo_deleted_ids(sheet: str) -> set:
    """Kobo IDs deleted in the app for this tab (these are skipped on every sync)."""
    df = get_all("KoboDeleted")
    if df.empty or "sheet" not in df.columns:
        return set()
    return {str(v).strip() for v in df.loc[df["sheet"] == sheet, "kobo_id"] if str(v).strip()}


def append_kobo_rows(sheet: str, records: list, chunk: int = 500) -> int:
    """Append many records in a few API calls (not one call per row)."""
    if not records:
        return 0
    cols = SHEET_COLUMNS[sheet]
    ws = _get_or_create_ws(sheet)
    rows = [[str(r.get(c, "")) for c in cols] for r in records]
    for i in range(0, len(rows), chunk):
        _wrap_gspread_errors(ws.append_rows)(rows[i:i + chunk])
    get_all.clear()
    return len(rows)


# ---------------------------------------------------------------------
# Competency assessment: editable checklist + deleting a whole assessment
# ---------------------------------------------------------------------
def get_competency_items() -> pd.DataFrame:
    df = get_all("CompetencyItems")
    return df.drop(columns=["_row"], errors="ignore") if not df.empty else df


def save_competency_items(df: pd.DataFrame):
    """Replace the whole checklist with `df` (columns per SHEET_COLUMNS['CompetencyItems'])."""
    cols = SHEET_COLUMNS["CompetencyItems"]
    out = df.reindex(columns=cols).fillna("").astype(str)
    ws = _get_or_create_ws("CompetencyItems")
    _wrap_gspread_errors(ws.clear)()
    _wrap_gspread_errors(ws.update)(range_name="A1", values=[cols] + out.values.tolist())
    get_all.clear()


def delete_competency_assessment(assessment_id: str, deleted_by: str = "") -> int:
    """Delete every row of one assessment. Rows imported from Kobo have their Kobo ID remembered
    so the next sync doesn't bring them back. Returns the number of rows removed."""
    cols = SHEET_COLUMNS["Competency"]
    get_all.clear()
    df = get_all("Competency")
    if df.empty:
        return 0
    mask = df["assessment_id"].astype(str) == str(assessment_id)
    removed = int(mask.sum())
    if not removed:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    kobo_ids = {str(v).strip() for v in df.loc[mask, "kobo_id"] if str(v).strip()}
    if kobo_ids:
        _wrap_gspread_errors(_get_or_create_ws("KoboDeleted").append_rows)(
            [["Competency", k, deleted_by, now] for k in sorted(kobo_ids)])
    remaining = df.loc[~mask].reindex(columns=cols).fillna("").astype(str)
    ws = _get_or_create_ws("Competency")
    _wrap_gspread_errors(ws.clear)()
    _wrap_gspread_errors(ws.update)(range_name="A1", values=[cols] + remaining.values.tolist())
    get_all.clear()
    return removed


def set_cells(sheet: str, updates: list):
    """updates: [(sheet_row_number, column_name, value), ...] written in ONE API call."""
    if not updates:
        return
    cols = SHEET_COLUMNS[sheet]
    data = []
    for row, col, value in updates:
        letter = chr(ord("A") + cols.index(col))                # tabs here stay under 26 columns
        data.append({"range": f"{letter}{row}", "values": [[str(value)]]})
    ws = _get_or_create_ws(sheet)
    _wrap_gspread_errors(ws.batch_update)(data)
    get_all.clear()


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
                _v = record.get(c, "")
                new_values[c] = st.text_input(c, value="" if pd.isna(_v) else str(_v))
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