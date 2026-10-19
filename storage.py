"""
storage.py
-----------
Saves photos and attendance lists captured in the in-app forms to Google Drive and returns a link
to store in the Sheet.

IMPORTANT — Google only lets a service account upload into a SHARED DRIVE (a Google Workspace
feature). Uploading into an ordinary "My Drive" folder fails with "storageQuotaExceeded".

One-time setup
  1. In Google Drive create (or use) a Shared Drive, and make a folder in it, e.g. "Activity evidence".
  2. Add the service account (the `client_email` in your secrets) to the Shared Drive as
     "Content manager".
  3. Copy the folder's ID from its address:  drive.google.com/drive/folders/<FOLDER_ID>
  4. Add to your Streamlit secrets:

        [storage]
        folder_id = "FOLDER_ID"

Files are filed as   <folder> / <DQA|SSM|LV> / <facility> / <date> / <file>.
Attachments captured in Kobo are stored by Kobo itself and don't use this module.
"""

import json
import re
import uuid
from datetime import datetime

import streamlit as st

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"

MAX_MB = 10
PHOTO_TYPES = ["jpg", "jpeg", "png"]
ATTENDANCE_TYPES = ["jpg", "jpeg", "png", "pdf", "xlsx", "xls", "csv", "doc", "docx"]
MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "pdf": "application/pdf",
        "csv": "text/csv", "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

_FOLDERS: dict = {}     # (parent_id, name) -> folder id, remembered for the life of the app process


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
def _cfg() -> dict:
    try:
        return dict(st.secrets["storage"]) if "storage" in st.secrets else {}
    except Exception:
        return {}


def is_configured() -> bool:
    try:
        return bool(_cfg().get("folder_id")) and "gcp_service_account" in st.secrets
    except Exception:
        return False


@st.cache_resource(show_spinner=False)
def _session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=["https://www.googleapis.com/auth/drive"])
    return AuthorizedSession(creds)


# ---------------------------------------------------------------------
# Drive calls
# ---------------------------------------------------------------------
def _check(r, what: str):
    if r.status_code < 400:
        return r
    try:
        err = r.json().get("error", {})
        msg = err.get("message", r.text)
        reason = (err.get("errors") or [{}])[0].get("reason", "")
    except Exception:
        msg, reason = r.text, ""
    if reason == "storageQuotaExceeded" or "storage quota" in str(msg).lower():
        hint = ("Service accounts can only upload into a Shared Drive — move the folder into a Shared Drive "
                "and add the service account to it as Content manager.")
    elif r.status_code == 404:
        hint = "Folder not found — check folder_id, and that the service account is a member of that Shared Drive."
    elif r.status_code in (401, 403):
        hint = "No permission — add the service account (client_email) to the Shared Drive as Content manager."
    else:
        hint = ""
    raise RuntimeError(f"Google Drive couldn't {what} ({r.status_code}): {msg}. {hint}".strip())


def _clean(name: str, default: str = "unknown") -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n]+", "-", str(name or "")).strip(" .-")
    return s[:80] or default


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def ensure_folder(session, parent_id: str, name: str) -> str:
    key = (parent_id, name)
    if key in _FOLDERS:
        return _FOLDERS[key]
    q = (f"name = '{_esc(name)}' and mimeType = '{FOLDER_MIME}' and '{parent_id}' in parents "
         "and trashed = false")
    r = _check(session.get(f"{API}/files", params={
        "q": q, "fields": "files(id,name)", "pageSize": 1, "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true", "corpora": "allDrives"}), "look up a folder")
    found = r.json().get("files", [])
    if found:
        fid = found[0]["id"]
    else:
        r = _check(session.post(f"{API}/files", params={"supportsAllDrives": "true", "fields": "id"},
                                json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}),
                   "create a folder")
        fid = r.json()["id"]
    _FOLDERS[key] = fid
    return fid


def check_file(filename: str, size: int, allowed) -> None:
    """Raise ValueError if a file is the wrong type or too large."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not ext:
        raise ValueError(f"'{filename}' has no file extension, so its type can't be checked (use {', '.join(allowed)}).")
    if ext not in allowed:
        raise ValueError(f"'{filename}': .{ext} files aren't accepted (use {', '.join(allowed)}).")
    if size > MAX_MB * 1024 * 1024:
        raise ValueError(f"'{filename}' is {size / 1048576:.1f} MB — the limit is {MAX_MB} MB per file.")


def save_file(data: bytes, filename: str, path: list, session=None) -> str:
    """Upload one file under the configured folder / path... and return a link to it."""
    session = session or _session()
    parent = _cfg()["folder_id"]
    for part in path:
        parent = ensure_folder(session, parent, _clean(part))
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = MIME.get(ext, "application/octet-stream")
    name = f"{datetime.now():%H%M%S}_{_clean(filename, 'file')}"
    boundary = "b" + uuid.uuid4().hex
    meta = json.dumps({"name": name, "parents": [parent]})
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--".encode()
    r = _check(session.post(UPLOAD, params={"uploadType": "multipart", "supportsAllDrives": "true",
                                            "fields": "id,webViewLink"},
                            data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"}),
               "upload a file")
    info = r.json()
    return info.get("webViewLink") or f"https://drive.google.com/file/d/{info['id']}/view"
