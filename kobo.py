"""
kobo.py
--------
Pulls DQA / Support Supervision / Learning Visit submissions from KoboToolbox
into the matching Google Sheet tabs, so the Activities Dashboard (and its admin
edit/delete panel) keeps working from ONE place: the Sheet.

How it works
  * Each submission is stored once, tagged with its Kobo ID in a `kobo_id` column.
  * Syncing is safe to repeat: submissions already in the Sheet are skipped.
  * Deleting a row in the app does NOT delete it from Kobo (Kobo stays the raw
    record); the app remembers the deleted ID so the next sync doesn't re-import it.
  * Edits made in the app stay in the Sheet. A sync never overwrites an imported row.

Configuration (Streamlit secrets) — see KOBO_SETUP section of the README:

    [kobo]
    server = "https://kf.kobotoolbox.org"   # or https://eu.kobotoolbox.org, or your own server
    token  = "your-api-token"
    auto_sync = false                        # true = sync once per session when the dashboard opens

    [kobo.forms.DQA]                         # also: SSM, LV
    asset_uid = "aBcDeFgHiJkLmNoPqRsTuV"     # the project's ID, from its URL in Kobo
    form_url  = "https://ee.kobotoolbox.org/x/AbCdEfGh"   # optional: web-form link for the dashboard buttons
    [kobo.forms.DQA.map]                     # optional: sheet column = Kobo question name
    facility = "facility_name"
"""

import json
from urllib.parse import quote

import requests
import streamlit as st

import gsheets

FORM_SHEETS = ("DQA", "SSM", "LV")

# The forms we generate store choice *names* (machine values) in Kobo; the Sheet has always
# held the readable text (that's what the in-app forms write). Translate on the way in.
# Anything not listed here passes through unchanged, so custom forms keep working.
CHOICE_LABELS = {
    ("DQA", "root_cause"): {
        "data_entry_error": "Data entry error",
        "register_source_issue": "Register/source document issue",
        "reporting_timeliness": "Reporting timeliness",
        "double_counting": "Double counting / duplication",
        "denominator_issue": "Denominator issue",
        "staff_not_trained": "Staff not trained",
        "system_technical_issue": "System/technical issue",
        "no_discrepancy": "No discrepancy found",
        "other": "Other",
    },
}


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
def _plain(x):
    """Streamlit secrets come back as read-only mapping objects; make plain dicts."""
    if hasattr(x, "items"):
        return {k: _plain(v) for k, v in x.items()}
    return x


def config() -> dict:
    """The [kobo] secrets section as a plain dict ({} if absent or secrets missing)."""
    try:
        if "kobo" not in st.secrets:
            return {}
        return _plain(st.secrets["kobo"])
    except Exception:
        return {}


def is_configured() -> bool:
    cfg = config()
    return bool(cfg.get("token")) and any(
        isinstance(v, dict) and v.get("asset_uid") for v in (cfg.get("forms") or {}).values())


def configured_sheets() -> list:
    forms = config().get("forms") or {}
    return [s for s in FORM_SHEETS if isinstance(forms.get(s), dict) and forms[s].get("asset_uid")]


def _server(cfg: dict) -> str:
    return (cfg.get("server") or "https://kf.kobotoolbox.org").rstrip("/")


# ---------------------------------------------------------------------
# Kobo API
# ---------------------------------------------------------------------
def fetch_submissions(server: str, token: str, asset_uid: str, page_size: int = 1000,
                      max_pages: int = 500) -> list:
    """All submissions of one Kobo project, following the API's pagination."""
    url = f"{server}/api/v2/assets/{asset_uid}/data/"
    params = {"format": "json", "limit": page_size}
    headers = {"Authorization": f"Token {token}"}
    out = []
    for _ in range(max_pages):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=60)
        except requests.RequestException as e:
            raise RuntimeError(f"Couldn't reach Kobo at {server}: {e}")
        if r.status_code in (401, 403):
            raise RuntimeError("Kobo rejected the API token (or it has no access to this project). "
                               "Check the token and the project's sharing settings.")
        if r.status_code == 404:
            raise RuntimeError(f"Kobo project '{asset_uid}' wasn't found on {server}. "
                               "Check the asset_uid and which Kobo server you use.")
        if not r.ok:
            raise RuntimeError(f"Kobo returned an error ({r.status_code}): {r.text[:200]}")
        payload = r.json()
        if isinstance(payload, list):                      # some servers return a bare list
            return out + payload
        out.extend(payload.get("results", []))
        url, params = payload.get("next"), None            # `next` already carries its own parameters
        if not url:
            break
    return out


# ---------------------------------------------------------------------
# Submission -> Sheet row
# ---------------------------------------------------------------------
def _lookup(sub: dict, name: str):
    """Value for a Kobo question: exact key first, then by last path segment so questions
    inside groups ('group/facility') match without any configuration."""
    if name in sub:
        return sub[name]
    for k, v in sub.items():
        if isinstance(k, str) and k.split("/")[-1] == name:
            return v
    return None


def submission_id(sub: dict) -> str:
    return str(sub.get("_uuid") or sub.get("_id") or "").strip()


def submission_to_record(sheet: str, sub: dict, column_map: dict | None = None) -> dict:
    """Turn one Kobo submission into a dict keyed by the Sheet's column names."""
    column_map = column_map or {}
    record = {}
    for col in gsheets.SHEET_COLUMNS[sheet]:
        if col == "kobo_id":
            continue
        val = _lookup(sub, column_map.get(col, col))
        if val is None:
            val = ""
        elif isinstance(val, (list, dict)):                # repeat groups etc. kept as text
            val = json.dumps(val, ensure_ascii=False)
        val = str(val)
        record[col] = CHOICE_LABELS.get((sheet, col), {}).get(val, val)

    # Fall back to Kobo's own metadata where the form has no matching question
    if not record.get("timestamp"):
        record["timestamp"] = str(sub.get("_submission_time", ""))
    if "submitted_by" in record and not record["submitted_by"]:
        record["submitted_by"] = str(sub.get("_submitted_by") or "")
    if sheet == "DQA" and not record.get("pushed_to_dhis2"):
        record["pushed_to_dhis2"] = "no"
    record["kobo_id"] = submission_id(sub)
    return record


# ---------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------
def sync_form(sheet: str) -> dict:
    """Import new submissions for one form. Returns counts: fetched / new / already_in_sheet / deleted_skipped."""
    cfg = config()
    form = (cfg.get("forms") or {}).get(sheet) or {}
    if not cfg.get("token") or not form.get("asset_uid"):
        raise RuntimeError(f"Kobo isn't configured for {sheet}.")

    subs = fetch_submissions(_server(cfg), cfg["token"], form["asset_uid"])

    gsheets.get_all.clear()                                # compare against the live Sheet, not a cached copy
    known = gsheets.get_kobo_ids(sheet)
    deleted = gsheets.get_kobo_deleted_ids(sheet)

    new_records, already, skipped_deleted, seen = [], 0, 0, set()
    for sub in subs:
        sid = submission_id(sub)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        if sid in deleted:
            skipped_deleted += 1
        elif sid in known:
            already += 1
        else:
            new_records.append(submission_to_record(sheet, sub, form.get("map") or {}))

    added = gsheets.append_kobo_rows(sheet, new_records)
    return {"fetched": len(subs), "new": added, "already_in_sheet": already, "deleted_skipped": skipped_deleted}


def sync_all() -> dict:
    """Sync every configured form. One form failing doesn't stop the others."""
    gsheets.ensure_kobo_columns()
    results = {}
    for sheet in configured_sheets():
        try:
            results[sheet] = sync_form(sheet)
        except RuntimeError as e:
            results[sheet] = {"error": str(e)}
    return results


# ---------------------------------------------------------------------
# Prefilled link to the Kobo web form (used by the dashboard's action buttons)
# ---------------------------------------------------------------------
def prefilled_link(sheet: str, facility: str, data_element: str, period: str | None = None):
    """Kobo web-form URL with facility / data element (/ period) filled in, or None if this
    form has no `form_url` configured. Uses Enketo's ?d[question]=value convention."""
    form = (config().get("forms") or {}).get(sheet) or {}
    base = form.get("form_url")
    if not base:
        return None
    cmap = form.get("map") or {}
    values = {"facility": facility, "data_element": data_element}
    if period:
        values["period"] = period
    parts = [f"d[{cmap.get(col, col)}]={quote(str(v), safe='')}" for col, v in values.items() if v]
    return base + ("&" if "?" in base else "?") + "&".join(parts)


# ---------------------------------------------------------------------
# Sync panel for the Activities Dashboard
# ---------------------------------------------------------------------
def _show_results(results: dict):
    for sheet, res in results.items():
        if "error" in res:
            st.error(f"**{sheet}:** {res['error']}")
        else:
            extra = f", {res['deleted_skipped']} skipped (deleted in the app)" if res["deleted_skipped"] else ""
            st.success(f"**{sheet}:** {res['new']} new, {res['already_in_sheet']} already imported{extra} "
                       f"— {res['fetched']} in Kobo.")


def render_sync_panel():
    """Call near the top of the Activities Dashboard. Shows nothing unless Kobo is configured
    (admins get a short hint). Syncing is limited to submitter/admin roles."""
    user = st.session_state.get("user") or {}
    role = user.get("role")
    if not is_configured():
        if role == "admin":
            st.caption("🔌 Kobo isn't connected yet — add a `[kobo]` section to your secrets to import "
                       "Kobo submissions here.")
        return
    if role not in ("submitter", "admin"):
        return

    with st.container(border=True):
        c1, c2 = st.columns([4, 1], vertical_alignment="center")
        c1.markdown("**🔄 Kobo sync** — import new submissions from Kobo into this dashboard "
                    f"({', '.join(configured_sheets())}). Safe to run as often as you like.")
        clicked = c2.button("Sync now", type="primary", use_container_width=True, key="kobo_sync_btn")

        auto = bool(config().get("auto_sync")) and not st.session_state.get("_kobo_auto_done")
        if clicked or auto:
            st.session_state["_kobo_auto_done"] = True
            try:
                with st.spinner("Syncing from Kobo..."):
                    st.session_state["_kobo_last_result"] = sync_all()
            except RuntimeError as e:
                st.session_state["_kobo_last_result"] = {"Kobo": {"error": str(e)}}
            st.rerun()

        last = st.session_state.get("_kobo_last_result")
        if last:
            _show_results(last)
