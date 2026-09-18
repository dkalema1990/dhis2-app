import streamlit as st
from datetime import date
import auth
import gsheets

st.set_page_config(page_title="DQA Form", page_icon="📋", layout="centered")
auth.require_role("submitter", "admin")
auth.sidebar_user_badge()

st.title("📋 Data Quality Assessment (DQA)")

facility = st.session_state.get("selected_facility")
data_element = st.session_state.get("selected_data_element")

if not facility:
    st.info("No facility pre-selected. Choose one below, or launch this from the Facility Dashboard.")
    facility = st.text_input("Facility")
    data_element = st.text_input("Data element")
else:
    st.markdown(f"**Facility:** {facility}  \n**Data element:** {data_element}")

st.divider()

with st.form("dqa_form"):
    period = st.text_input("Period reviewed", value=st.session_state.get("period", "2026Q3"))
    reported_value = st.number_input("Reported value (as in DHIS2)", min_value=0.0, step=1.0)
    verified_value = st.number_input("Verified value (from source documents)", min_value=0.0, step=1.0)
    discrepancy_reason = st.text_area("Reason for discrepancy (if any)")
    root_cause = st.selectbox("Root cause category", [
        "Data entry error", "Register/source document issue", "Reporting timeliness",
        "Double counting / duplication", "Denominator issue", "Staff not trained",
        "System/technical issue", "No discrepancy found", "Other",
    ])
    corrective_action = st.text_area("Corrective action agreed")
    responsible_person = st.text_input("Responsible person")
    follow_up_date = st.date_input("Follow-up date", value=date.today())
    submitted_by = st.text_input("Submitted by", value=st.session_state["user"]["display_name"])

    st.markdown("**Optional: push this outcome back into DHIS2 as an event**")
    push_to_dhis2 = st.checkbox("Push to DHIS2 (requires an Action-Tracking event program set up in DHIS2)")
    dhis2_program = dhis2_stage = dhis2_ou = dhis2_de = ""
    if push_to_dhis2:
        pc1, pc2 = st.columns(2)
        dhis2_program = pc1.text_input("DHIS2 Program UID")
        dhis2_stage = pc2.text_input("DHIS2 Program Stage UID")
        dhis2_ou = pc1.text_input("DHIS2 Org Unit UID (this facility)")
        dhis2_de = pc2.text_input("DHIS2 Data Element UID (for corrective action text)")

    submitted = st.form_submit_button("Submit DQA Outcome", type="primary")

if submitted:
    if not facility or not submitted_by:
        st.error("Facility and 'Submitted by' are required.")
    else:
        pushed = "no"
        if push_to_dhis2:
            client = st.session_state.get("dhis2_client")
            if not client:
                st.warning("Not connected to DHIS2 (see Home page) — outcome saved, but not pushed.")
            elif not all([dhis2_program, dhis2_stage, dhis2_ou, dhis2_de]):
                st.warning("Missing DHIS2 program/stage/org unit/data element UIDs — outcome saved, but not pushed.")
            else:
                try:
                    client.push_event(dhis2_program, dhis2_stage, dhis2_ou, str(date.today()),
                                       {dhis2_de: corrective_action})
                    pushed = "yes"
                    st.success("Pushed to DHIS2 as an event.")
                except Exception as e:
                    st.error(f"DHIS2 push failed: {e}")

        gsheets.save_dqa({
            "facility": facility, "data_element": data_element, "period": period,
            "reported_value": reported_value, "verified_value": verified_value,
            "discrepancy_reason": discrepancy_reason, "root_cause": root_cause,
            "corrective_action": corrective_action, "responsible_person": responsible_person,
            "follow_up_date": str(follow_up_date), "submitted_by": submitted_by,
            "pushed_to_dhis2": pushed,
        })
        st.success(f"DQA outcome saved for **{facility}**.")
        st.page_link("pages/5_📊_Activities_Dashboard.py", label="View Activities Dashboard →")
        st.page_link("pages/1_🏥_Facility_Dashboard.py", label="← Back to Facility Dashboard")
