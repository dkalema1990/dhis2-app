import streamlit as st
import pandas as pd
import auth
from dhis2_client import get_mock_facility_data

st.set_page_config(page_title="Facility Dashboard", page_icon="🏥", layout="wide")
auth.require_login()
auth.sidebar_user_badge()
can_act = st.session_state["user"]["role"] in ("submitter", "admin")

st.title("🏥 Facility Achievement Dashboard")

# ---------------------------------------------------------------------
# Load data: real DHIS2 pull (if connected + configured) or mock data
# ---------------------------------------------------------------------
if st.session_state.get("use_mock", True) or "dhis2_client" not in st.session_state:
    period = st.session_state.get("period", "2026Q3")
    df = st.session_state.get("mock_df", get_mock_facility_data(period))
else:
    with st.expander("⚙️ Real DHIS2 pull settings", expanded=True):
        client = st.session_state["dhis2_client"]
        c1, c2 = st.columns(2)
        de_ids = c1.text_input("Data element UIDs (comma-separated)")
        ou_id = c2.text_input("Org unit / group UID (e.g. a district group)")
        target_csv = st.file_uploader("Upload targets CSV (columns: facility, data_element, target)")
        if st.button("Pull from DHIS2") and de_ids and ou_id:
            raw = client.get_analytics(de_ids.split(","), ou_id, st.session_state["period"])
            if target_csv is not None:
                targets = pd.read_csv(target_csv)
                df = raw.merge(targets, on=["facility", "data_element"], how="left")
                df = df.rename(columns={"value": "actual"})
                df["achievement_pct"] = (df["actual"] / df["target"] * 100).round(1)
            else:
                st.warning("Upload a targets CSV to compute achievement %.")
                df = raw.rename(columns={"value": "actual"})
                df["target"] = None
                df["achievement_pct"] = None
            st.session_state["live_df"] = df
    df = st.session_state.get("live_df", get_mock_facility_data(st.session_state.get("period", "2026Q3")))

# ---------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------
col1, col2 = st.columns(2)
facilities = sorted(df["facility"].unique())
elements = sorted(df["data_element"].unique())
f_sel = col1.multiselect("Filter facilities", facilities, default=facilities)
e_sel = col2.multiselect("Filter data elements", elements, default=elements)
view = df[df["facility"].isin(f_sel) & df["data_element"].isin(e_sel)]

RED, YELLOW = 50, 90  # <RED = red, RED-<YELLOW = yellow, >=YELLOW = green


def badge(pct):
    if pd.isna(pct):
        return "⚪ N/A"
    if pct < RED:
        return f"🔴 {pct}%"
    if pct < YELLOW:
        return f"🟡 {pct}%"
    return f"🟢 {pct}%"


def recommended_action(pct):
    """Auto-suggest a default follow-up based on achievement band.
    Still lets the user pick any of the three — this just pre-highlights one."""
    if pd.isna(pct):
        return None
    if pct < RED:
        return "DQA"          # verify the data before concluding it's a real performance gap
    if pct < YELLOW:
        return "SSM"           # likely a genuine performance gap -> mentor/support
    return "LV"                 # strong performer -> capture and spread the practice


ACTION_LABELS = {"DQA": "🔍 Do DQA", "SSM": "🤝 Do Support Supervision / Mentorship",
                  "LV": "📘 Do Learning Visit"}

st.divider()
st.subheader("Achievement by Facility")

summary = view.groupby("facility", as_index=False)["achievement_pct"].mean().round(1)
summary = summary.sort_values("achievement_pct")

for _, row in summary.iterrows():
    facility = row["facility"]
    avg_pct = row["achievement_pct"]
    rec = recommended_action(avg_pct)
    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 2, 2])
        c1.markdown(f"### {facility}")
        c2.metric("Avg. Achievement", badge(avg_pct))
        detail = view[view["facility"] == facility][["data_element", "target", "actual", "achievement_pct"]]
        with c3:
            with st.expander("View data elements"):
                st.dataframe(detail.style.format({"achievement_pct": "{:.1f}%"}), hide_index=True,
                              use_container_width=True)

        with st.expander(f"⚡ Actions to be taken" + (f" — recommended: {ACTION_LABELS[rec]}" if rec else "")):
            if not can_act:
                st.info("Your role (`viewer`) can't launch activities — ask an admin for `submitter` access.")
            de_for_action = st.selectbox(
                "Which data element is this action for?",
                detail["data_element"].tolist(), key=f"de_{facility}"
            )
            a1, a2, a3 = st.columns(3)

            def route(action, page):
                st.session_state["selected_facility"] = facility
                st.session_state["selected_data_element"] = de_for_action
                st.session_state["selected_action"] = action
                st.switch_page(page)

            btn_type = lambda action: "primary" if action == rec else "secondary"
            if a1.button(ACTION_LABELS["DQA"], key=f"dqa_{facility}", use_container_width=True,
                          type=btn_type("DQA"), disabled=not can_act):
                route("DQA", "pages/2_📋_DQA_Form.py")
            if a2.button(ACTION_LABELS["SSM"], key=f"ssm_{facility}", use_container_width=True,
                          type=btn_type("SSM"), disabled=not can_act):
                route("SSM", "pages/3_🤝_Support_Supervision_Form.py")
            if a3.button(ACTION_LABELS["LV"], key=f"lv_{facility}", use_container_width=True,
                          type=btn_type("LV"), disabled=not can_act):
                route("LV", "pages/4_📘_Learning_Visit_Form.py")

st.caption("🔴 <50%   🟡 50–89%   🟢 ≥90% achievement  ·  highlighted button = system-recommended action "
            "(you can still pick any of the three)")
