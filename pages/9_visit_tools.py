import io

import pandas as pd
import plotly.express as px
import streamlit as st

import auth
import gsheets
import kobo
import visit_tools as vt

auth.require_login()
auth.sidebar_user_badge()
role = st.session_state["user"]["role"]
display_name = st.session_state["user"]["display_name"]
can_act = role in ("submitter", "admin")
is_admin = role == "admin"

st.title("🧭 Visit tools")
st.caption("Attendance, root cause analysis, action plans, IPI milestone mapping and routine indicator mapping "
           "from support supervision / mentorship visits. They're filled in Kobo and arrive here after **Sync now**.")

_flash = st.session_state.pop("vt_flash", None)
if _flash:
    st.success(_flash)

if not gsheets.is_configured():
    st.warning("Google Sheets isn't configured, so there's nothing to show yet.")
    st.stop()

try:
    ipi_all, routine_all = vt.load_lists(seed=can_act)          # fills the sample lists the first time
except RuntimeError as e:
    st.error(f"Couldn't load the indicator lists: {e}")
    st.stop()


@st.cache_data(ttl=900, show_spinner=False, max_entries=40)
def _fetch(url: str):
    return kobo.fetch_attachment(url)


def _is_previewable_image(blob: bytes) -> bool:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(blob)) as im:
            im.verify()
        return True
    except Exception:
        return False


def _links(cell) -> list:
    return [u.strip() for u in str(cell or "").split("|") if u.strip()]


def _show_files(links: list, title: str, key_prefix: str):
    """Kobo files need the API token, so they're fetched on request."""
    for i, url in enumerate(links, 1):
        if kobo.is_kobo_link(url):
            if st.button(f"Show {title} {i}", key=f"{key_prefix}_{i}"):
                try:
                    blob, ctype = _fetch(url)
                    if ctype.startswith("image/") and _is_previewable_image(blob):
                        st.image(blob, use_container_width=True)
                    else:
                        st.download_button(f"Download {title} {i}", blob, file_name=f"{title.replace(' ', '_')}_{i}",
                                           key=f"{key_prefix}_dl_{i}")
                except RuntimeError as e:
                    st.error(str(e))
        else:
            st.link_button(f"Open {title} {i} ↗", url)


tab_res, tab_setup = st.tabs(["📊 Results", "⚙️ Lists & Kobo form"])

# =====================================================================
# Results
# =====================================================================
with tab_res:
    try:
        tools = gsheets.get_table("VisitTools")
        att = gsheets.get_table("VisitAttendance")
        rca = gsheets.get_table("VisitRCA")
        act = gsheets.get_table("VisitActions")
        ipi = gsheets.get_table("VisitIPI")
        rout = gsheets.get_table("VisitRoutine")
    except RuntimeError as e:
        st.error(f"Couldn't load the visit tools: {e}")
        st.stop()
    if st.button("🔄 Refresh", key="vt_refresh"):
        gsheets.get_all.clear()
        st.rerun()

    if tools.empty:
        st.info("No visit tools have been submitted yet. Fill in the Kobo visit tools form, then click "
                "**Sync now** on the Activities Dashboard.")
    else:
        tools = tools.copy()
        tools["kobo_id"] = tools["kobo_id"].astype(str)
        f1, f2 = st.columns(2)
        fac_sel = f1.multiselect("Facility", sorted(tools["facility"].astype(str).unique()))
        view = tools[tools["facility"].isin(fac_sel)] if fac_sel else tools
        view = view.sort_values("visit_date", ascending=False)
        labels = {f"{r.visit_date} · {r.facility} · {r.visit_ref or r.kobo_id[:8]}": r.kobo_id for r in view.itertuples()}
        pick = f2.selectbox("Visit", ["All visits"] + list(labels), key="vt_visit")
        kids = {labels[pick]} if pick != "All visits" else set(view["kobo_id"])

        def sel(df):
            if df is None or df.empty or "kobo_id" not in df.columns:
                return pd.DataFrame(columns=list(df.columns) if df is not None else [])
            return df[df["kobo_id"].astype(str).isin(kids)].copy()

        v_tools, v_att, v_rca, v_act, v_ipi, v_rout = (sel(x) for x in (tools, att, rca, act, ipi, rout))
        if not v_act.empty:
            v_act["status"] = v_act["timeline"].map(vt.action_status)
        v_rout = vt.routine_with_pct(v_rout) if not v_rout.empty else v_rout

        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Visits", len(v_tools))
        k2.metric("Attendees recorded", len(v_att))
        k3.metric("Actions planned", len(v_act))
        k4.metric("Overdue actions", int((v_act["status"] == "Overdue").sum()) if not v_act.empty else 0)
        avg_pct = v_rout["pct_of_benchmark"].mean() if not v_rout.empty else float("nan")
        k5.metric("Routine indicators vs benchmark", "—" if pd.isna(avg_pct) else f"{avg_pct:.0f}%")

        t_att, t_rca, t_act, t_ipi, t_rout = st.tabs(["👥 Attendance", "🔎 Root cause analysis", "✅ Action plans",
                                                       "🎯 IPI milestones", "📈 Routine indicators"])

        with t_att:
            if v_att.empty:
                st.info("No typed-in attendance lists for this selection.")
            else:
                st.dataframe(v_att[["visit_date", "facility", "fullname", "cadre", "email", "phone", "visit_ref"]],
                             hide_index=True, use_container_width=True)
                cad = v_att.groupby("cadre").size().reset_index(name="attendees").sort_values("attendees", ascending=False)
                st.caption("Attendees by cadre: " + ", ".join(f"{r.cadre} ({r.attendees})" for r in cad.itertuples()))
                st.download_button("⬇️ Download attendance (CSV)", v_att.to_csv(index=False).encode("utf-8"),
                                   "visit_attendance.csv", "text/csv", key="dl_att")
            uploaded = v_tools[v_tools["attendance_upload_links"].astype(str).str.strip() != ""]
            if pick == "All visits":
                if len(uploaded):
                    st.caption(f"📎 {len(uploaded)} visit(s) have an uploaded paper attendance list — pick one visit above to open them.")
            elif len(uploaded):
                st.markdown("**Uploaded paper attendance list**")
                _show_files(_links(uploaded.iloc[0]["attendance_upload_links"]), "attendance list", "vt_att_file")

        with t_rca:
            if v_tools.empty:
                st.info("Nothing to show.")
            else:
                st.dataframe(v_tools[["visit_date", "facility", "rca_method", "rca_problem", "rca_root_cause", "visit_ref"]],
                             hide_index=True, use_container_width=True)
                meth = v_tools.groupby("rca_method").size().reset_index(name="visits")
                if len(meth) > 1:
                    st.caption("Methods used: " + ", ".join(f"{r.rca_method or '—'} ({r.visits})" for r in meth.itertuples()))
                if pick != "All visits" and not v_rca.empty:
                    st.markdown("**The full analysis for this visit**")
                    st.dataframe(v_rca[["method", "section", "item", "text"]], hide_index=True, use_container_width=True)
                elif pick == "All visits":
                    st.caption("Pick one visit above to see its full analysis.")

        with t_act:
            if v_act.empty:
                st.info("No action plan rows for this selection.")
            else:
                order = ["Overdue", "Due within 2 weeks", "Upcoming", "No date"]
                st_sel = st.multiselect("Status", order, default=order, key="vt_status")
                shown = v_act[v_act["status"].isin(st_sel)].copy()
                shown["_o"] = shown["status"].map({s: i for i, s in enumerate(order)})
                shown = shown.sort_values(["_o", "timeline"]).drop(columns="_o")
                st.dataframe(shown[["status", "timeline", "facility", "challenge", "corrective_action",
                                    "responsible_person", "visit_date", "visit_ref"]], hide_index=True, use_container_width=True)
                st.download_button("⬇️ Download action plans (CSV)", shown.to_csv(index=False).encode("utf-8"),
                                   "action_plans.csv", "text/csv", key="dl_act")

        with t_ipi:
            if v_ipi.empty:
                st.info("No IPI milestone rows for this selection.")
            else:
                cnt = v_ipi.groupby("ipi_indicator").size().reset_index(name="times_mapped").sort_values("times_mapped")
                fig = px.bar(cnt, x="times_mapped", y="ipi_indicator", orientation="h",
                             labels={"times_mapped": "Times mapped", "ipi_indicator": ""})
                fig.update_layout(height=max(220, 36 * len(cnt)), margin=dict(l=10, r=10, t=10, b=10))
                st.plotly_chart(fig, use_container_width=True)
                shown = v_ipi.assign(evidence_files=v_ipi["evidence_links"].map(lambda c: len(_links(c))))
                st.dataframe(shown[["visit_date", "facility", "ipi_indicator", "action", "evidence", "evidence_files"]],
                             hide_index=True, use_container_width=True)
                if pick != "All visits":
                    for r_i, r in enumerate(v_ipi.itertuples()):
                        if _links(r.evidence_links):
                            st.markdown(f"**Evidence — {r.ipi_indicator}**")
                            _show_files(_links(r.evidence_links), "evidence", f"vt_ipi_{r_i}")
                st.download_button("⬇️ Download IPI mapping (CSV)", v_ipi.to_csv(index=False).encode("utf-8"),
                                   "ipi_mapping.csv", "text/csv", key="dl_ipi")

        with t_rout:
            if v_rout.empty:
                st.info("No routine indicator rows for this selection.")
            else:
                st.dataframe(v_rout[["visit_date", "facility", "indicator", "benchmark_n", "achieved_n", "pct_of_benchmark"]],
                             hide_index=True, use_container_width=True, column_config={
                                 "benchmark_n": st.column_config.NumberColumn("benchmark"),
                                 "achieved_n": st.column_config.NumberColumn("achieved"),
                                 "pct_of_benchmark": st.column_config.NumberColumn("% of benchmark", format="%.1f%%")})
                by = (v_rout.dropna(subset=["pct_of_benchmark"]).groupby("indicator")["pct_of_benchmark"].mean()
                      .reset_index().sort_values("pct_of_benchmark"))
                if not by.empty:
                    fig = px.bar(by, x="pct_of_benchmark", y="indicator", orientation="h",
                                 labels={"pct_of_benchmark": "Average % of benchmark achieved", "indicator": ""})
                    fig.add_vline(x=100, line_dash="dot", line_color="#3fa54a")
                    fig.update_traces(marker_color="#3b82c4")
                    fig.update_layout(height=max(240, 38 * len(by)), margin=dict(l=10, r=10, t=10, b=10))
                    st.plotly_chart(fig, use_container_width=True)
                    st.caption("The dotted line is the benchmark (100%).")
                st.download_button("⬇️ Download routine indicators (CSV)", v_rout.to_csv(index=False).encode("utf-8"),
                                   "routine_indicators.csv", "text/csv", key="dl_rout")

        if is_admin:
            with st.expander("🗑️ Delete a visit's tool data (admin)"):
                allv = {f"{r.visit_date} · {r.facility} · {r.visit_ref or r.kobo_id[:8]}": r.kobo_id for r in tools.itertuples()}
                del_pick = st.selectbox("Visit", list(allv), key="vt_del_pick")
                sure = st.checkbox("Yes, delete everything recorded for this visit's tools", key="vt_del_sure")
                if st.button("Delete", disabled=not sure, key="vt_del_btn"):
                    try:
                        n = gsheets.delete_visit_tools(allv[del_pick], display_name)
                        st.session_state["vt_flash"] = f"Deleted {n} row(s). It won't be re-imported from Kobo."
                        st.rerun()
                    except RuntimeError as e:
                        st.error(f"Couldn't delete: {e}")

# =====================================================================
# Lists & Kobo form (admin)
# =====================================================================
with tab_setup:
    if not is_admin:
        st.info("Only admins can edit the indicator lists or download the Kobo form.")
    else:
        st.markdown("**1. Edit the indicator lists.** They feed the dropdowns in the Kobo form. These are **samples** "
                    "— replace them with your real IPI and routine indicators. Don't delete an indicator already "
                    "used in submissions; set `active` to `no` instead so old records keep their names.")
        ipi_edit = st.data_editor(ipi_all if not ipi_all.empty else vt.default_ipi(), num_rows="dynamic", hide_index=True,
                                  use_container_width=True, key="vt_ipi_editor", column_config={
                                      "ipi_id": st.column_config.TextColumn("ipi_id", help="Short unique code, e.g. ipi_01. Don't change once in use.", required=True),
                                      "indicator": st.column_config.TextColumn("IPI indicator", width="large", required=True),
                                      "active": st.column_config.SelectboxColumn("active", options=["yes", "no"], default="yes", required=True)})
        rt_edit = st.data_editor(routine_all if not routine_all.empty else vt.default_routine(), num_rows="dynamic", hide_index=True,
                                 use_container_width=True, key="vt_routine_editor", column_config={
                                     "indicator_id": st.column_config.TextColumn("indicator_id", help="Short unique code, e.g. ri_01. Don't change once in use.", required=True),
                                     "indicator": st.column_config.TextColumn("Routine indicator", width="large", required=True),
                                     "active": st.column_config.SelectboxColumn("active", options=["yes", "no"], default="yes", required=True)})
        if st.button("💾 Save both lists", type="primary", key="vt_save_lists"):
            errs = vt.validate_list(ipi_edit, vt.IPI_COLS, "IPI") + vt.validate_list(rt_edit, vt.ROUTINE_COLS, "Routine indicator")
            if errs:
                for e in errs[:8]:
                    st.error(e)
            else:
                try:
                    gsheets.save_table("IPIIndicators", vt.normalize_list(ipi_edit, vt.IPI_COLS))
                    gsheets.save_table("RoutineIndicators", vt.normalize_list(rt_edit, vt.ROUTINE_COLS))
                    st.session_state["vt_flash"] = "Lists saved. Download the Kobo form again below and replace it in Kobo."
                    st.rerun()
                except RuntimeError as e:
                    st.error(f"Couldn't save: {e}")

        st.divider()
        st.markdown("**2. Get the Kobo form.** Built from the *saved* lists. Upload it to Kobo as a new project and "
                    "deploy it. **Whenever you change the lists, download the form again and replace it in Kobo** — "
                    "Kobo keeps the dropdown options it was deployed with.")
        try:
            xls = vt.build_xlsform(ipi_all, routine_all)
            st.download_button("⬇️ Download Kobo form (XLSForm)", xls, "visit_tools_form.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="vt_dl_form")
            st.caption(f"{int((ipi_all['active'] != 'no').sum())} active IPI indicators and "
                       f"{int((routine_all['active'] != 'no').sum())} active routine indicators.")
        except ValueError as e:
            st.error(f"Can't build the Kobo form yet: {e}")
        except ImportError:
            st.error("The `openpyxl` package isn't installed yet, so the Kobo form can't be built. Add "
                     "`openpyxl>=3.1` to requirements.txt, install it, and restart the app.")

        st.divider()
        st.markdown("**3. Connect the Kobo project** by adding this to your Streamlit secrets "
                    "(same file as the other Kobo forms):")
        st.code('[kobo.forms.TOOLS]\nasset_uid = "your-visit-tools-project-id"\n'
                'form_url = "paste the Collect data link here"   # adds the visit-tools step to the dashboard buttons',
                language="toml")
        st.caption("The root cause analysis methods and fishbone categories are set in `visit_tools.py`.")

        with st.expander("Reset the lists to the samples"):
            st.warning("This replaces both lists with the built-in samples. Existing submissions are kept.")
            ok = st.checkbox("Yes, replace both lists with the samples", key="vt_reset_ok")
            if st.button("Reset lists", disabled=not ok, key="vt_reset_btn"):
                gsheets.save_table("IPIIndicators", vt.default_ipi())
                gsheets.save_table("RoutineIndicators", vt.default_routine())
                st.session_state["vt_flash"] = "Lists reset to the samples."
                st.rerun()
