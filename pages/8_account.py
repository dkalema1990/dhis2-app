import pandas as pd
import streamlit as st

import auth
import gsheets

auth.require_login()
auth.sidebar_user_badge()
user = st.session_state["user"]
is_admin = user["role"] == "admin"

st.title("👤 My account")

_flash = st.session_state.pop("acct_flash", None)
if _flash:
    st.success(_flash)

if not gsheets.is_configured():
    st.warning("Google Sheets isn't configured, so accounts can't be managed here.")
    st.stop()

# ---------------------------------------------------------------------
# Everyone: change my own password
# ---------------------------------------------------------------------
st.subheader("Change my password")
with st.form("acct_pw", clear_on_submit=True):
    cur = st.text_input("Current password", type="password")
    new1 = st.text_input(f"New password (at least {auth.MIN_PASSWORD} characters)", type="password")
    new2 = st.text_input("Repeat the new password", type="password")
    go = st.form_submit_button("Change password", type="primary")
if go:
    if new1 != new2:
        st.error("The two new passwords don't match.")
    else:
        try:
            auth.change_password(user["username"], cur, new1)
            st.session_state["acct_flash"] = "Password changed. Use the new one next time you log in."
            st.rerun()
        except ValueError as e:
            st.error(str(e))
        except RuntimeError as e:
            st.error(f"Couldn't save: {e}")

# ---------------------------------------------------------------------
# Admins: manage users
# ---------------------------------------------------------------------
if is_admin:
    st.divider()
    st.header("Manage users")
    st.caption("**viewer** — sees the dashboards only.  **submitter** — also fills in the DQA, support supervision, "
               "learning visit and competency forms, but can't delete anything.  **admin** — everything, "
               "including editing/deleting records and managing users.")
    try:
        users = auth.get_users()
    except RuntimeError as e:
        st.error(f"Couldn't load the users: {e}")
        st.stop()
    if not users.empty:
        st.dataframe(users[["username", "display_name", "role"]], hide_index=True, use_container_width=True)
    others = [] if users.empty else [u for u in users["username"].astype(str) if u.strip().lower() != user["username"].strip().lower()]

    def _do(fn, ok_message):
        try:
            fn()
            st.session_state["acct_flash"] = ok_message
            st.rerun()
        except ValueError as e:
            st.error(str(e))
        except RuntimeError as e:
            st.error(f"Couldn't save: {e}")

    with st.expander("➕ Add a user", expanded=users.empty or len(users) <= 1):
        with st.form("acct_add", clear_on_submit=True):
            c1, c2 = st.columns(2)
            u_name = c1.text_input("Username", help="Letters, numbers, . _ -  (no spaces). They log in with this.")
            u_disp = c2.text_input("Full name (shown in the app)")
            c3, c4 = st.columns(2)
            u_role = c3.selectbox("Role", list(auth.ROLES), index=1)
            u_pw = c4.text_input(f"Starting password (at least {auth.MIN_PASSWORD} characters)", type="password")
            add = st.form_submit_button("Create user", type="primary")
        if add:
            _do(lambda: auth.create_user(u_name, u_pw, u_role, u_disp),
                f"User '{u_name.strip()}' created as {u_role}. Give them the username and starting password, "
                "and ask them to change it under My account.")

    with st.expander("🔑 Reset a password"):
        if not others:
            st.caption("No other users yet.")
        else:
            with st.form("acct_reset", clear_on_submit=True):
                who = st.selectbox("User", others, key="acct_reset_user")
                pw = st.text_input(f"New password (at least {auth.MIN_PASSWORD} characters)", type="password", key="acct_reset_pw")
                rs = st.form_submit_button("Reset password")
            if rs:
                _do(lambda: auth.set_password(who, pw), f"Password reset for '{who}'.")

    with st.expander("🎭 Change a role"):
        if not others:
            st.caption("No other users yet.")
        else:
            with st.form("acct_role"):
                who2 = st.selectbox("User", others, key="acct_role_user")
                role2 = st.selectbox("New role", list(auth.ROLES), key="acct_role_new")
                rl = st.form_submit_button("Change role")
            if rl:
                _do(lambda: auth.set_role(who2, role2),
                    f"'{who2}' is now {role2}. It takes effect the next time they log in.")

    with st.expander("🗑️ Remove a user"):
        if not others:
            st.caption("No other users yet.")
        else:
            who3 = st.selectbox("User", others, key="acct_del_user")
            sure = st.checkbox(f"Yes, remove '{who3}'. They will no longer be able to log in.", key="acct_del_sure")
            if st.button("Remove user", disabled=not sure, key="acct_del_btn"):
                _do(lambda: auth.delete_user(who3), f"User '{who3}' removed.")
else:
    st.caption("Need an account for someone else, or a role changed? Ask an admin.")
