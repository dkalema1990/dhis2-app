import streamlit as st
from datetime import date
import auth
import gsheets

st.set_page_config(page_title="Learning Visit Form", page_icon="📘", layout="centered")
auth.require_role("submitter", "admin")
auth.sidebar_user_badge()

st.title("📘 Learning Visit")

facility = st.session_state.get("selected_facility")
data_element = st.session_state.get("selected_data_element")

if not facility:
    st.info("No facility pre-selected. Choose one below, or launch this from the Facility Dashboard.")
    facility = st.text_input("Facility")
    data_element = st.text_input("Data element / area of strength")
else:
    st.markdown(f"**Facility:** {facility}  \n**Data element / area of strength:** {data_element}")

st.divider()

with st.form("lv_form"):
    best_practice = st.text_area("Best practice observed")
    enabling_factors = st.text_area("Enabling factors (what made this success possible)")
    lessons_learned = st.text_area("Lessons learned")
    dissemination_plan = st.text_area("Plan to disseminate this practice to other facilities")
    responsible_person = st.text_input("Facility contact person")
    follow_up_date = st.date_input("Planned dissemination / follow-up date", value=date.today())
    submitted_by = st.text_input("Submitted by", value=st.session_state["user"]["display_name"])

    submitted = st.form_submit_button("Submit Learning Visit Outcome", type="primary")

if submitted:
    if not facility or not submitted_by:
        st.error("Facility and 'Submitted by' are required.")
    else:
        gsheets.save_lv({
            "facility": facility, "data_element": data_element, "best_practice": best_practice,
            "enabling_factors": enabling_factors, "lessons_learned": lessons_learned,
            "dissemination_plan": dissemination_plan, "responsible_person": responsible_person,
            "follow_up_date": str(follow_up_date), "submitted_by": submitted_by,
        })
        st.success(f"Learning visit outcome saved for **{facility}**.")
        st.page_link("pages/5_📊_Activities_Dashboard.py", label="View Activities Dashboard →")
        st.page_link("pages/1_🏥_Facility_Dashboard.py", label="← Back to Facility Dashboard")
