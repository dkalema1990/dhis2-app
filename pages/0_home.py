import streamlit as st
from dhis2_client import DHIS2Client, get_mock_facility_data, DATA_ELEMENTS
import auth

auth.require_login()
auth.sidebar_user_badge()

st.title("📊 DHIS2 Facility Performance & Action Tracker")

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
    connect = st.form_submit_button("Connect & Pull Data", type="primary")

if connect:
    if base_url and username and password:
        client = DHIS2Client(base_url, username, password)
        if client.test_connection():
            st.session_state["dhis2_client"] = client
            st.session_state["use_mock"] = False
            st.success("Connected to DHIS2 — taking you to the Facility Dashboard...")
            st.switch_page("pages/1_facility_dashboard.py")
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
    st.switch_page("pages/1_facility_dashboard.py")

st.caption(f"Data elements tracked in this demo: {', '.join(DATA_ELEMENTS)}")
