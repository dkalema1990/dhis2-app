import pandas as pd
import plotly.express as px
import streamlit as st

import auth
import gsheets
import mentorship
import reports as rp

auth.require_login()
auth.sidebar_user_badge()

st.title("📄 Reports")
st.caption("Reports built from what is already in the Sheet - nothing new to enter. The facility visit report downloads as "
           "**Word** (open it and use *Save as PDF* if you need a PDF); the summaries download as **Excel**.")

if not gsheets.is_configured():
    st.warning("Google Sheets isn't configured, so there's nothing to report on yet.")
    st.stop()

try:
    D = rp.load(gsheets)
except RuntimeError as e:
    st.error(f"Couldn't load the data: {e}")
    st.stop()


def _style(df):
    if df is None or df.empty:
        return df
    colours = {"Green": "background-color:#C6EFCE", "Amber": "background-color:#FFEB9C", "Red": "background-color:#FFC7CE"}
    return df.style.map(lambda v: colours.get(v, ""))


def _show(df, **kw):
    st.dataframe(_style(df), use_container_width=True, hide_index=True, **kw)


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

t1, t2, t3, t4 = st.tabs(["Facility visit report", "Performance & visits", "Competency", "Action plan tracker"])

# ---- 1. one facility, one visit ---------------------------------------------------------
with t1:
    visits = rp.visits_for(D)
    if visits.empty:
        st.info("No visits yet. Visits appear here after the Kobo visit form is synced.")
    else:
        facs = sorted(visits["facility"].astype(str).unique())
        fac = st.selectbox("Facility", facs, key="rp_fac")
        vs = rp.visits_for(D, fac)
        labels = {rp.visit_label(r): r["visit_ref"] for _, r in vs.iterrows()}
        pick = st.selectbox("Visit", list(labels), key="rp_visit")
        rep = rp.facility_visit(D, labels[pick])
        st.subheader(f"{rep['facility']} - {rep['date']}")
        st.write(rep["summary"])
        m = st.columns(4)
        m[0].metric("Competency", f"{rep['competency_avg']:.0f}%" if rep["competency_avg"] is not None else "-",
                    mentorship.rating(rep["competency_avg"]) if rep["competency_avg"] is not None else None, delta_color="off")
        sy = rep["session"]["Systems %"].mean() if not rep["session"].empty else None
        m[1].metric("Systems readiness", f"{sy:.0f}%" if sy == sy and sy is not None else "-")
        m[2].metric("Actions agreed", len(rep["actions"]))
        m[3].metric("Support supervision score", f"{rep['ssm_score']:.0f}%" if rep["ssm_score"] is not None else "-")
        st.download_button("⬇️ Download Word report", rp.docx_facility_report(rep),
                           file_name=f"visit_report_{rep['facility']}_{rep['date']}.docx".replace(" ", "_"), mime=DOCX, type="primary")
        for title, key in [("Scores by topic", "session"), ("Health worker competency", "competency"),
                           ("Competencies most often not yet met", "gaps"), ("Facility systems gaps", "systems_gaps"),
                           ("Data verification", "indicators"), ("Root cause analysis", "rca"), ("Action plan agreed", "actions"),
                           ("Follow-up of previous actions", "followup"), ("Previous action plan - status now", "previous_plan"),
                           ("IPI milestone mapping", "ipi"), ("Routine indicators", "routine"), ("Attendance", "attendance")]:
            df = rep[key]
            with st.expander(f"{title} ({len(df)})", expanded=key in ("competency", "actions")):
                if key == "previous_plan" and df.empty:
                    st.caption("No previous action plan for this facility.")
                else:
                    _show(df)

# ---- 2. performance and visits ----------------------------------------------------------
with t2:
    ach = D["ach"]
    periods = sorted(ach["period"].astype(str).unique()) if ach is not None and not ach.empty else []
    per = st.selectbox("Period", ["All published periods"] + periods, key="rp_period")
    ps = rp.performance_summary(D, "" if per.startswith("All") else per)
    tot = ps["totals"]
    if ps["table"].empty:
        st.info("Nothing to show yet. Publish an analysis on the Facility dashboard and record visits.")
    else:
        c = st.columns(4)
        c[0].metric("Facilities", tot["facilities"]); c[1].metric("Visited", tot["visited"])
        c[2].metric("Red on achievement", tot["red"]); c[3].metric("Overdue actions", tot["overdue"])
        if tot["needs_visit"]:
            st.warning(f"{tot['needs_visit']} facility(ies) are below 70% achievement and have no recorded visit: "
                       + ", ".join(ps["attention"]["Facility"].tolist()))
        _show(ps["table"])
        fig_df = ps["table"].dropna(subset=["Avg achievement %"])
        if not fig_df.empty:
            fig = px.bar(fig_df, x="Avg achievement %", y="Facility", orientation="h", color="Visits",
                         color_continuous_scale="Blues", title="Average achievement by facility (colour = visits)")
            fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=max(300, 28 * len(fig_df) + 120))
            st.plotly_chart(fig, use_container_width=True)
        st.download_button("⬇️ Download Excel", rp.to_excel({"Performance and visits": ps["table"], "Needs a visit": ps["attention"]}),
                           file_name="performance_and_visits.xlsx", mime=XLSX, key="dl_perf")

# ---- 3. competency ----------------------------------------------------------------------
with t3:
    cdf = D["comp"]
    if cdf is None or cdf.empty:
        st.info("No competency assessments yet.")
    else:
        a, b = st.columns(2)
        f_opt = a.selectbox("Facility", ["All"] + sorted(cdf["facility"].astype(str).unique()), key="rc_fac")
        t_opt = b.selectbox("Topic", ["All"] + sorted(cdf["service_area"].astype(str).unique()), key="rc_topic")
        cr = rp.competency_report(D, "" if f_opt == "All" else f_opt, "" if t_opt == "All" else t_opt)
        st.markdown("**By facility and topic** (each health worker's latest assessment)")
        _show(cr["by_topic"])
        st.markdown("**Health workers**")
        _show(cr["workers"])
        if not cr["weak_items"].empty:
            st.markdown("**Weakest competencies**")
            _show(cr["weak_items"])
        if not cr["progress"].empty:
            st.markdown("**Baseline to follow-up**")
            _show(cr["progress"])
        st.download_button("⬇️ Download Excel", rp.to_excel({"By topic": cr["by_topic"], "Health workers": cr["workers"],
                                                          "Weakest items": cr["weak_items"], "Progress": cr["progress"]}),
                           file_name="competency_report.xlsx", mime=XLSX, key="dl_comp")

# ---- 4. action plan tracker -------------------------------------------------------------
with t4:
    at_ = rp.action_tracker(D)
    tt = at_["totals"]
    if not tt["total"]:
        st.info("No action plans recorded yet.")
    else:
        c = st.columns(4)
        c[0].metric("Actions", tt["total"]); c[1].metric("Done", tt["done"], f"{tt['pct_done']}%", delta_color="off")
        c[2].metric("Open", tt["open"]); c[3].metric("Overdue", tt["overdue"])
        view = st.radio("Show", ["Overdue", "Open", "All"], horizontal=True, key="ra_view")
        det = at_["actions"]
        if view == "Overdue":
            det = det[det["Overdue by (days)"] > 0]
        elif view == "Open":
            det = det[det["Status"].astype(str).str.lower() != "done"]
        _show(det)
        x, y = st.columns(2)
        x.markdown("**By facility**"); x.dataframe(at_["by_facility"], hide_index=True, use_container_width=True)
        y.markdown("**By owner**"); y.dataframe(at_["by_owner"], hide_index=True, use_container_width=True)
        st.download_button("⬇️ Download Excel", rp.to_excel({"Actions": at_["actions"], "By facility": at_["by_facility"], "By owner": at_["by_owner"]}),
                           file_name="action_plan_tracker.xlsx", mime=XLSX, key="dl_act")
