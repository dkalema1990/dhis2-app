"""
auth.py
--------
Lightweight username/password + role authentication, backed by the
"Users" worksheet in the same Google Sheet as the activity data.

Roles:
- viewer     : dashboards only, read-only
- submitter  : dashboards + can submit DQA/SSM/LV forms
- admin      : everything, plus edit/delete records and manage users
"""

import streamlit as st
import gsheets


def get_users_df():
    return gsheets.get_all("Users")


def verify_login(username: str, password: str):
    df = get_users_df()
    if df.empty:
        return None
    match = df[df["username"] == username]
    if match.empty:
        return None
    row = match.iloc[0]
    if str(row["password_hash"]) == gsheets.hash_password(password):
        return {"username": username, "role": row["role"],
                "display_name": row.get("display_name") or username}
    return None


def add_user(username: str, password: str, role: str, display_name: str):
    gsheets.append_record("Users", {
        "username": username, "password_hash": gsheets.hash_password(password),
        "role": role, "display_name": display_name,
    })


def require_login():
    """Call at the top of every page. Halts the page if nobody is logged in."""
    if "user" not in st.session_state:
        st.warning("Please log in from the Home page first.")
        st.page_link("app.py", label="← Go to Home / Login")
        st.stop()


def require_role(*roles):
    require_login()
    if st.session_state["user"]["role"] not in roles:
        st.error(f"This page requires one of these roles: {', '.join(roles)}. "
                  f"You're logged in as '{st.session_state['user']['role']}'.")
        st.stop()


def sidebar_user_badge():
    user = st.session_state.get("user")
    if not user:
        return
    with st.sidebar:
        st.markdown(f"👤 **{user['display_name']}** · role: `{user['role']}`")
        if st.button("Log out", use_container_width=True):
            del st.session_state["user"]
            st.rerun()


# ---------------------------------------------------------------------
# Accounts: change your own password; admins add / manage users.
# Everything is stored in the "Users" tab of the Google Sheet (passwords as hashes, never as text).
# ---------------------------------------------------------------------
import re

ROLES = ("viewer", "submitter", "admin")
MIN_PASSWORD = 8


def get_users():
    return gsheets.get_all("Users")


def _find(username: str):
    """(row dict incl. _row, or None) — usernames are matched ignoring case and spaces."""
    df = get_users()
    if df.empty:
        return None
    m = df[df["username"].astype(str).str.strip().str.lower() == str(username).strip().lower()]
    return None if m.empty else m.iloc[0].to_dict()


def _write(row: dict, **changes):
    rec = {"username": row["username"], "password_hash": row["password_hash"],
           "role": row["role"], "display_name": row.get("display_name", "")}
    rec.update(changes)
    gsheets.update_record("Users", int(row["_row"]), rec)


def _check_password(pw: str):
    if len(pw) < MIN_PASSWORD:
        raise ValueError(f"The password must be at least {MIN_PASSWORD} characters.")


def change_password(username: str, current: str, new: str):
    row = _find(username)
    if row is None:
        raise ValueError("This account isn't stored in the Users sheet, so it has no password to change.")
    if str(row["password_hash"]) != gsheets.hash_password(current):
        raise ValueError("The current password is wrong.")
    _check_password(new)
    if new == current:
        raise ValueError("Choose a new password that is different from the current one.")
    _write(row, password_hash=gsheets.hash_password(new))


def create_user(username: str, password: str, role: str, display_name: str = ""):
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,30}", username) or not re.search(r"[A-Za-z]", username):
        raise ValueError("The username must be 3–30 characters (letters, numbers, . _ -) and include a letter.")
    if role not in ROLES:
        raise ValueError("Pick a role.")
    _check_password(password)
    if _find(username) is not None:
        raise ValueError(f"The username '{username}' is already taken.")
    gsheets.append_record("Users", {"username": username, "password_hash": gsheets.hash_password(password),
                                    "role": role, "display_name": display_name.strip() or username})


def set_password(username: str, new: str):
    row = _find(username)
    if row is None:
        raise ValueError("That user doesn't exist.")
    _check_password(new)
    _write(row, password_hash=gsheets.hash_password(new))


def _admin_count() -> int:
    df = get_users()
    return 0 if df.empty else int((df["role"].astype(str).str.strip() == "admin").sum())


def set_role(username: str, role: str):
    row = _find(username)
    if row is None:
        raise ValueError("That user doesn't exist.")
    if role not in ROLES:
        raise ValueError("Pick a role.")
    if str(row["role"]).strip() == "admin" and role != "admin" and _admin_count() <= 1:
        raise ValueError("There must always be at least one admin.")
    _write(row, role=role)


def delete_user(username: str):
    row = _find(username)
    if row is None:
        raise ValueError("That user doesn't exist.")
    if str(row["role"]).strip() == "admin" and _admin_count() <= 1:
        raise ValueError("There must always be at least one admin.")
    gsheets.delete_record("Users", int(row["_row"]))