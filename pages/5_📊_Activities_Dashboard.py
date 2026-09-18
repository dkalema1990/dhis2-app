import streamlit as st
import plotly.express as px
import auth
import gsheets

st.set_page_config(page_title="Activities Dashboard", page_icon="📊", layout="wide")
auth.require_login()
auth.sidebar_user_badge()
is_admin = st.session_state["user"]["role"] == "admin"

st.title("📊 DQA / Support Supervision / Learning Visit Dashboard")

tab1, tab2, tab3 = st.tabs(["🔍 DQA", "🤝 Support Supervision", "📘 Learning Visits"])

with tab1:
    df = gsheets.get_all("DQA")
    if df.empty:
        st.info("No DQA activities logged yet.")
    else:
        c1, c2 = st.columns(2)
        c1.metric("Total DQAs conducted", len(df))
        c2.metric("Facilities covered", df["facility"].nunique())
        fig = px.bar(df.groupby("facility").size().reset_index(name="count"),
                     x="facility", y="count", title="DQAs per facility")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(df.drop(columns=["_row"]), use_container_width=True, hide_index=True)
        if is_admin:
            gsheets.render_admin_management("DQA")

with tab2:
    df = gsheets.get_all("SSM")
    if df.empty:
        st.info("No support supervision / mentorship visits logged yet.")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Total visits", len(df))
        c2.metric("Facilities covered", df["facility"].nunique())
        c3.metric("Avg. score", round(df["score"].astype(float).mean(), 1))
        fig = px.box(df, x="facility", y="score", title="Support supervision scores by facility")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(df.drop(columns=["_row"]), use_container_width=True, hide_index=True)
        if is_admin:
            gsheets.render_admin_management("SSM")

with tab3:
    df = gsheets.get_all("LV")
    if df.empty:
        st.info("No learning visits logged yet.")
    else:
        c1, c2 = st.columns(2)
        c1.metric("Total learning visits", len(df))
        c2.metric("Facilities covered", df["facility"].nunique())
        st.dataframe(df.drop(columns=["_row"]), use_container_width=True, hide_index=True)
        if is_admin:
            gsheets.render_admin_management("LV")

if not is_admin:
    st.caption("Log in as `admin` to edit or delete records.")
