"""
storage.py
-----------
Saves photos and attendance lists captured in the in-app forms and returns a link to keep in the
Sheet. Two places to store them — pick ONE in your Streamlit secrets:

A) GitHub (simplest; needs no Google Workspace)
     1. Create a NEW, PRIVATE repository just for evidence, e.g. "dhis2-evidence".
     2. GitHub -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens ->
        Generate: Repository access = ONLY that repository; Permissions -> Contents = Read and write.
     3. Add to your secrets:

        [storage]
        backend = "github"
        repo = "your-username/dhis2-evidence"
        token = "github_pat_..."
        folder = "evidence"            # optional

B) Google Drive — a SHARED DRIVE only (a Google Workspace feature)
     Google refuses uploads from a service account into an ordinary "My Drive" folder
     ("storageQuotaExceeded"). In a Shared Drive: add the service account (client_email in your secrets)
     as "Content manager", copy the folder ID from drive.google.com/drive/folders/<FOLDER_ID>, then:

        [storage]
        backend = "gdrive"
        folder_id = "FOLDER_ID"

Files are filed as   <root> / <DQA|SSM|LV> / <facility> / <date> / <file>.
Photos are shrunk (longest side 2000 px, JPEG) before saving — much faster on a weak connection.
Attachments captured in Kobo are stored by Kobo itself and don't use this module.
"""

import base64
import io
import json
import re
import uuid
from datetime import datetime
from urllib.parse import quote, unquote

import requests

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


def backend():
    """'github', 'gdrive', or None when file storage isn't (fully) set up."""
    cfg = _cfg()
    if not cfg:
        return None
    kind = (cfg.get("backend") or ("github" if cfg.get("repo") else "gdrive")).lower()
    try:
        if kind == "github" and "/" in str(cfg.get("repo", "")) and cfg.get("token"):
            return "github"
        if kind == "gdrive" and cfg.get("folder_id") and "gcp_service_account" in st.secrets:
            return "gdrive"
    except Exception:
        return None
    return None


def is_configured() -> bool:
    return backend() is not None


def describe() -> str:
    b, cfg = backend(), _cfg()
    if b == "github":
        return f"GitHub repository {cfg.get('repo')}"
    if b == "gdrive":
        return "a Google Shared Drive folder"
    return "no file storage"


def problems() -> list:
    """Plain-language reasons file storage isn't working yet (empty list = looks complete)."""
    cfg = _cfg()
    if not cfg:
        return ["There is no [storage] section in your Streamlit secrets."]
    kind = (cfg.get("backend") or ("github" if cfg.get("repo") else "gdrive")).lower()
    out = []
    if kind == "github":
        if "/" not in str(cfg.get("repo", "")):
            out.append("`repo` must look like your-username/repository-name.")
        if not cfg.get("token"):
            out.append("`token` is missing.")
    elif kind == "gdrive":
        if not cfg.get("folder_id"):
            out.append("`folder_id` is missing.")
        try:
            if "gcp_service_account" not in st.secrets:
                out.append("The Google service account isn't in your secrets.")
        except Exception:
            out.append("The Google service account isn't in your secrets.")
    else:
        out.append(f"`backend` must be \"github\" or \"gdrive\" (it says \"{kind}\").")
    return out


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


def prepare_image(data: bytes, filename: str, max_px: int = 2000, quality: int = 85):
    """Shrink a phone photo before saving (a 5 MB photo becomes a few hundred KB). Returns
    (bytes, filename). Anything that isn't a readable JPEG/PNG is returned unchanged."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("jpg", "jpeg", "png"):
        return data, filename
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, "white")
            flat.paste(im, mask=im.split()[-1])
            im = flat
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((max_px, max_px))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
        new = out.getvalue()
        if ext == "png" or len(new) < len(data):
            return new, filename.rsplit(".", 1)[0] + ".jpg"
    except Exception:
        pass
    return data, filename


def _unique_name(filename: str) -> str:
    return f"{datetime.now():%H%M%S}_{uuid.uuid4().hex[:4]}_{_clean(filename, 'file')}"


GH_API = "https://api.github.com"


def _gh_headers(token: str, raw: bool = False) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _gh_check(r, what: str):
    if r.status_code < 400:
        return r
    try:
        msg = r.json().get("message", r.text)
    except Exception:
        msg = r.text
    if r.status_code == 401:
        hint = "The token is wrong or has expired — create a new one."
    elif r.status_code == 404:
        hint = ("Repository not found, or the token can't see it. Check `repo`, and that the token was given "
                "access to THAT repository with Contents = Read and write.")
    elif r.status_code == 403:
        hint = "The token has no permission to write (needs Contents = Read and write), or GitHub is rate-limiting."
    elif r.status_code == 422:
        hint = "GitHub rejected the file (it may already exist) — try again."
    else:
        hint = ""
    raise RuntimeError(f"GitHub couldn't {what} ({r.status_code}): {msg}. {hint}".strip())


def _github_save(data: bytes, filename: str, path: list, session=None) -> str:
    cfg = _cfg()
    root = _clean(cfg.get("folder") or "evidence")
    rel = "/".join([root] + [_clean(p) for p in path] + [_unique_name(filename)])
    body = {"message": f"Add {rel.rsplit('/', 1)[-1]}", "content": base64.b64encode(data).decode()}
    if cfg.get("branch"):
        body["branch"] = cfg["branch"]
    http = session or requests
    try:
        r = http.put(f"{GH_API}/repos/{cfg['repo']}/contents/{quote(rel, safe='/')}",
                     headers=_gh_headers(cfg["token"]), json=body, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"Couldn't reach GitHub: {e}")
    _gh_check(r, "save a file")
    content = r.json().get("content", {})
    return content.get("html_url") or f"https://github.com/{cfg['repo']}/blob/{cfg.get('branch') or 'main'}/{rel}"


def is_github_link(url: str) -> bool:
    return backend() == "github" and str(url).startswith(f"https://github.com/{_cfg()['repo']}/blob/")


def fetch_file(url: str, session=None):
    """Download a file saved in the GitHub repository. Only links inside the configured repository
    are fetched, so the token can't be sent anywhere else. Returns (bytes, content_type)."""
    if not is_github_link(url):
        raise RuntimeError("That link isn't in your evidence repository.")
    cfg = _cfg()
    m = re.match(rf"https://github\.com/{re.escape(cfg['repo'])}/blob/([^/]+)/(.+)$", url)
    if not m:
        raise RuntimeError("Couldn't read that GitHub link.")
    branch, rel = m.group(1), unquote(m.group(2))
    http = session or requests
    try:
        r = http.get(f"{GH_API}/repos/{cfg['repo']}/contents/{quote(rel, safe='/')}", params={"ref": branch},
                     headers=_gh_headers(cfg["token"], raw=True), timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"Couldn't reach GitHub: {e}")
    _gh_check(r, "download a file")
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    return r.content, MIME.get(ext, "application/octet-stream")


def save_file(data: bytes, filename: str, path: list, session=None) -> str:
    """Save one file and return a link to it. Uses GitHub or Google Drive, whichever is configured."""
    if backend() == "github":
        return _github_save(data, filename, path, session)
    if backend() != "gdrive":
        raise RuntimeError("File storage isn't set up yet (see the [storage] notes in storage.py).")
    return _drive_save(data, filename, path, session)


def test_upload(session=None) -> str:
    """Save a tiny test file the same way real attachments are saved. Returns its link, or raises
    RuntimeError saying exactly what's wrong."""
    return save_file(f"Storage test {datetime.now():%Y-%m-%d %H:%M:%S}".encode(), "storage_test.txt",
                     ["_storage_test"], session)


def _drive_save(data: bytes, filename: str, path: list, session=None) -> str:
    """Upload one file to the Google Shared Drive folder and return a link to it."""
    session = session or _session()
    parent = _cfg()["folder_id"]
    for part in path:
        parent = ensure_folder(session, parent, _clean(part))
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = MIME.get(ext, "application/octet-stream")
    name = _unique_name(filename)
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