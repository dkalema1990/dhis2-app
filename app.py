import streamlit as st
from dhis2_client import DHIS2Client, get_mock_facility_data, DATA_ELEMENTS
import gsheets
import auth

st.set_page_config(page_title="DHIS2 Performance & Action Tracker", page_icon="📊", layout="wide")

st.title("📊 DHIS2 Facility Performance & Action Tracker")

# ---------------------------------------------------------------------
# Login (backed by the "Users" worksheet in Google Sheets)
# ---------------------------------------------------------------------
if "user" not in st.session_state:
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

auth.sidebar_user_badge()

st.markdown("""
This app pulls data-element performance from **DHIS2**, shows achievement by facility,
and lets you launch the right follow-up activity — **DQA**, **Support Supervision /
Mentorship**, or a **Learning Visit** — directly from a facility's result, then tracks
the outcomes of those activities in **Google Sheets**.

**Use the sidebar to navigate:**
- **Facility Dashboard** — achievement by facility, with a recommended action highlighted
- **DQA / SSM / Learning Visit Forms** — opened automatically when you pick an action
  (requires the `submitter` or `admin` role)
- **Activities Dashboard** — history of all outcomes, with edit/delete for `admin`
""")

st.divider()
st.subheader("🔌 DHIS2 Connection")

with st.form("dhis2_conn"):
    col1, col2, col3 = st.columns(3)
    base_url = col1.text_input("DHIS2 Base URL", placeholder="https://play.dhis2.org/demo")
    username = col2.text_input("Username")
    password = col3.text_input("Password", type="password")
    period = st.text_input("Period (DHIS2 format)", value="2026Q3",
                            help="e.g. 2026Q3, THIS_YEAR, 202608")
    connect = st.form_submit_button("Connect & Pull Data", type="primary")

if connect:
    if base_url and username and password:
        client = DHIS2Client(base_url, username, password)
        if client.test_connection():
            st.session_state["dhis2_client"] = client
            st.session_state["period"] = period
            st.session_state["use_mock"] = False
            st.success("Connected to DHIS2. Go to the Facility Dashboard page to view data.")
        else:
            st.error("Could not authenticate against that DHIS2 instance. Check URL/credentials.")
    else:
        st.warning("Fill in URL, username and password — or just continue with demo data below.")

st.divider()
st.subheader("🧪 Or explore with demo data (no DHIS2 needed)")
if st.button("Load demo dataset"):
    st.session_state["use_mock"] = True
    st.session_state["period"] = st.session_state.get("period", "2026Q3")
    st.session_state["mock_df"] = get_mock_facility_data(st.session_state["period"])
    st.success("Demo data loaded. Open **Facility Dashboard** in the sidebar.")

st.caption(f"Data elements tracked in this demo: {', '.join(DATA_ELEMENTS)}")