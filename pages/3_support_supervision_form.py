import streamlit as st
from datetime import date
import auth
import gsheets

auth.require_role("submitter", "admin")
auth.sidebar_user_badge()

st.title("🤝 Support Supervision / Mentorship Visit")

facility = st.session_state.get("selected_facility")
data_element = st.session_state.get("selected_data_element")

if not facility:
    st.info("No facility pre-selected. Choose one below, or launch this from the Facility Dashboard.")
    facility = st.text_input("Facility")
    data_element = st.text_input("Data element / service area")
else:
    st.markdown(f"**Facility:** {facility}  \n**Data element / focus:** {data_element}")

st.divider()

prefill_findings = st.session_state.get("ssm_prefill_findings", "")
if prefill_findings:
    st.info("📋 The competency assessment results from this visit have been added to **Key findings** below — "
            "edit them as needed.")

with st.form("ssm_form"):
    focus_area = st.text_input("Specific focus area of the visit", value=data_element or "")
    findings = st.text_area("Key findings / observations", value=prefill_findings, height=200 if prefill_findings else None)
    mentorship_given = st.text_area("Mentorship / on-the-job training provided")
    score = st.slider("Overall performance score (self-assessed, 0–100)", 0, 100, 50)
    recommendations = st.text_area("Recommendations")
    responsible_person = st.text_input("Facility focal person")
    follow_up_date = st.date_input("Follow-up visit date", value=date.today())
    submitted_by = st.text_input("Submitted by", value=st.session_state["user"]["display_name"])

    submitted = st.form_submit_button("Submit Support Supervision Outcome", type="primary")

if submitted:
    if not facility or not submitted_by:
        st.error("Facility and 'Submitted by' are required.")
    else:
        gsheets.save_ssm({
            "facility": facility, "data_element": data_element, "focus_area": focus_area,
            "findings": findings, "mentorship_given": mentorship_given, "score": score,
            "recommendations": recommendations, "responsible_person": responsible_person,
            "follow_up_date": str(follow_up_date), "submitted_by": submitted_by,
        })
        st.session_state.pop("ssm_prefill_findings", None)     # visit finished: clear the competency hand-off
        st.session_state.pop("visit_flow", None)
        st.success(f"Support supervision outcome saved for **{facility}**.")
        st.page_link("pages/5_activities_dashboard.py", label="View Activities Dashboard →")
        st.page_link("pages/1_facility_dashboard.py", label="← Back to Facility Dashboard")