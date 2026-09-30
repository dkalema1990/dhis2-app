import streamlit as st
import gsheets
import auth

st.set_page_config(page_title="DHIS2 Performance & Action Tracker", page_icon="📊", layout="wide")

# ---------------------------------------------------------------------
# Login (backed by the "Users" worksheet in Google Sheets)
# ---------------------------------------------------------------------
if "user" not in st.session_state:
    st.title("📊 DHIS2 Facility Performance & Action Tracker")
    if gsheets.is_configured():
        try:
            gsheets.init_sheets()  # creates worksheets + seeds default admin if first run
        except RuntimeError as e:
            st.error(f"Couldn't connect to your Google Sheet: {e}")
            st.info("Double-check: the sheet is shared with your service account's `client_email` as "
                      "**Editor**, `gsheet_id` in your secrets matches the sheet's URL exactly, and the "
                      "Sheets + Drive APIs are enabled on the same Google Cloud project as the service account.")
            st.stop()
        st.subheader("🔐 Log in")
        with st.form("login"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            go = st.form_submit_button("Log in", type="primary")
        if go:
            try:
                user = auth.verify_login(u, p)
            except RuntimeError as e:
                st.error(f"Couldn't reach Google Sheets to check your login: {e}")
                st.stop()
            if user:
                st.session_state["user"] = user
                st.rerun()
            else:
                st.error("Invalid username or password.")
        st.caption("First time? Default admin login is **admin / admin123** — change it on the "
                    "Users sheet as soon as you're in.")
        st.stop()
    else:
        st.warning(
            "**Google Sheets backend isn't configured yet.** Add `gsheet_id` and "
            "`[gcp_service_account]` to your Streamlit secrets to enable login, forms, "
            "and data storage (see README). Until then you can preview the dashboard "
            "with demo data, read-only."
        )
        if st.button("Continue in read-only preview mode"):
            st.session_state["user"] = {"username": "preview", "role": "viewer",
                                          "display_name": "Preview (read-only)"}
            st.session_state["use_mock"] = True
            st.rerun()
        st.stop()

# ---------------------------------------------------------------------
# Role-based navigation — this is what actually removes pages from the
# sidebar for a `viewer` (not just blocking them once opened). Each page
# still keeps its own auth.require_login()/require_role() call too, as a
# second line of defense in case someone guesses a page's URL directly.
# ---------------------------------------------------------------------
role = st.session_state["user"]["role"]

facility_page = st.Page("pages/1_facility_dashboard.py", title="Facility Dashboard", icon="🏥")
activities_page = st.Page("pages/5_activities_dashboard.py", title="Activities Dashboard", icon="📊")

if role == "viewer":
    pages = [facility_page, activities_page]
else:
    home_page = st.Page("pages/0_home.py", title="Home", icon="🏠")
    dqa_page = st.Page("pages/2_dqa_form.py", title="DQA Form", icon="📋")
    ssm_page = st.Page("pages/3_support_supervision_form.py", title="Support Supervision", icon="🤝")
    lv_page = st.Page("pages/4_learning_visit_form.py", title="Learning Visit", icon="📘")
    pages = [home_page, facility_page, dqa_page, ssm_page, lv_page, activities_page]

nav = st.navigation(pages)
nav.run()
