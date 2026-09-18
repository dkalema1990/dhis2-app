import streamlit as st
from datetime import date
import auth
import gsheets

st.set_page_config(page_title="Support Supervision Form", page_icon="🤝", layout="centered")
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

with st.form("ssm_form"):
    focus_area = st.text_input("Specific focus area of the visit", value=data_element or "")
    findings = st.text_area("Key findings / observations")
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
        st.success(f"Support supervision outcome saved for **{facility}**.")
        st.page_link("pages/5_📊_Activities_Dashboard.py", label="View Activities Dashboard →")
        st.page_link("pages/1_🏥_Facility_Dashboard.py", label="← Back to Facility Dashboard")
