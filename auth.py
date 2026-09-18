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
