import io

import pandas as pd
import plotly.express as px
import streamlit as st

import auth
import competency as comp
import mentorship
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
st.caption("Attendance, root cause analyses, action plans, IPI milestone mapping and routine indicator mapping "
           "from support supervision / mentorship visits. They're filled in the Kobo visit form and arrive here "
           "after **Sync now**. (Competency assessments and support supervision reports from the same form go to "
           "their own pages.)")

_flash = st.session_state.pop("vt_flash", None)
if _flash:
    st.success(_flash)

if not gsheets.is_configured():
    st.warning("Google Sheets isn't configured, so there's nothing to show yet.")
    st.stop()

try:
    ipi_all, routine_all = vt.load_lists(seed=can_act)          # fills the sample lists the first time
    items_all = comp.load_items(seed=can_act)                   # the competency checklist (part of the visit form)
    parts_all = gsheets.get_topic_parts()                       # Parts D/E/F of the topic tools
    fac_all = gsheets.get_facilities()                          # facility dropdown of the Kobo form (from DHIS2)
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


tab_res, tab_ment, tab_follow, tab_topics, tab_setup = st.tabs(["📊 Results", "🧾 Mentorship results", "🔁 Action follow-up", "📚 Mentorship topics", "⚙️ Lists & Kobo form"])

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
        if "visit_source" not in tools.columns:
            tools["visit_source"] = ""
        tools["visit_source"] = tools["visit_source"].replace("", "Standalone")      # visits from before this column existed
        f1, f2, f3 = st.columns(3)
        fac_sel = f1.multiselect("Facility", sorted(tools["facility"].astype(str).unique()))
        src_sel = f3.multiselect("Visit type", [l for _, l in vt.VISIT_SOURCES], key="vt_src",
                                 help="Standalone = Kobo link used on its own. Linked = opened from the DHIS2 performance dashboard.")
        view = tools[tools["facility"].isin(fac_sel)] if fac_sel else tools
        if src_sel:
            view = view[view["visit_source"].isin(src_sel)]
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
        k1.metric("Visits", len(v_tools),
                  help=f"{int((v_tools['visit_source'] == 'Standalone').sum())} standalone, "
                       f"{int((v_tools['visit_source'] != 'Standalone').sum())} linked to DHIS2 performance")
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
                show = v_tools.copy()
                if "rca_count" not in show.columns:
                    show["rca_count"] = ""
                st.dataframe(show[["visit_date", "facility", "rca_count", "rca_method", "rca_problem", "rca_root_cause", "visit_ref"]],
                             hide_index=True, use_container_width=True,
                             column_config={"rca_count": st.column_config.TextColumn("analyses"),
                                            "rca_method": "methods", "rca_problem": "problems", "rca_root_cause": "root causes"})
                n_an = int(pd.to_numeric(show["rca_count"], errors="coerce").fillna(0).sum())
                st.caption(f"{n_an} root cause analysis(es) across {len(show)} visit(s). A visit can have several — "
                           "each with its own problem and method.")
                if pick != "All visits" and not v_rca.empty:
                    r = v_rca.copy()
                    if "analysis_no" not in r.columns:
                        r["analysis_no"] = 1
                    r["analysis_no"] = pd.to_numeric(r["analysis_no"], errors="coerce").fillna(1).astype(int)
                    for n, grp in r.groupby("analysis_no"):
                        prob = grp.loc[grp["section"] == "Problem", "text"]
                        head = f"Analysis {n} — {grp['method'].iloc[0]}" + (f": {str(prob.iloc[0])[:70]}" if len(prob) else "")
                        with st.expander(head, expanded=True):
                            st.dataframe(grp[["section", "item", "text"]], hide_index=True, use_container_width=True)
                elif pick == "All visits":
                    st.caption("Pick one visit above to see each of its analyses in full.")

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
with tab_ment:
    st.caption("What was recorded in the topic tools: facility systems (Part D), data verification (Part E), record review "
               "(Part F), mentorship session and QI link (Parts G, I, J) and the follow-up of last visit's actions (Part B).")
    _m = {"VisitSession": "Session summary (G, I, J)", "VisitSystems": "Systems (D)", "VisitIndicators": "Data verification (E)",
          "VisitRecords": "Record review (F)", "VisitFollowUp": "Previous actions (B)"}
    _sub = st.tabs(list(_m.values()))
    for _t, (_name, _label) in zip(_sub, _m.items()):
        with _t:
            _df = gsheets.get_table(_name)
            if _df.empty:
                st.info("Nothing here yet. It arrives after **Sync now** once a visit has been submitted in Kobo.")
                continue
            _df = _df.drop(columns=[c for c in ("_row", "kobo_id") if c in _df.columns])
            _facs = sorted(_df["facility"].astype(str).unique()) if "facility" in _df.columns else []
            _pick = st.multiselect("Facility", _facs, key=f"vtm_fac_{_name}")
            if _pick:
                _df = _df[_df["facility"].astype(str).isin(_pick)]
            st.dataframe(_df, use_container_width=True, hide_index=True)
            st.download_button("⬇️ Download (CSV)", _df.to_csv(index=False).encode("utf-8"), f"{_name}.csv", "text/csv", key=f"vtm_dl_{_name}")
            if _name == "VisitSession" and "systems_pct" in _df.columns:
                _s = _df.assign(systems_pct=pd.to_numeric(_df["systems_pct"], errors="coerce")).dropna(subset=["systems_pct"])
                if not _s.empty:
                    _fig = px.bar(_s, x="facility", y="systems_pct", color="topic", barmode="group",
                                  labels={"systems_pct": "Systems score (%)", "facility": ""})
                    _fig.add_hline(y=85, line_dash="dot"); _fig.add_hline(y=70, line_dash="dot")
                    st.plotly_chart(_fig, use_container_width=True)
                    st.caption("Dotted lines: Green at 85% and above, Amber 70-84%, Red below 70%.")

with tab_follow:
    st.caption("Track the action plans agreed at past visits. The most recent plan of a facility is also sent into the Kobo "
               "visit form as a read-only note when you open it from the dashboard; a facility with no plan shows "
               "\"No previous action plan for this facility.\"")
    _plans = vt.previous_plans(gsheets.get_table("VisitActions"), gsheets.get_table("ActionStatus"), gsheets.get_table("VisitFollowUp"))
    if _plans.empty:
        st.info(vt.NO_PREVIOUS + " Plans appear here after a visit is submitted in Kobo and synced.")
    else:
        _open = _plans[~_plans["status"].isin(["Done"])].copy()
        _open["due"] = _open["timeline"].map(vt.action_status)
        k1, k2, k3 = st.columns(3)
        k1.metric("Actions recorded", len(_plans))
        k2.metric("Not yet done", len(_open))
        k3.metric("Overdue", int((_open["due"] == "Overdue").sum()))
        _facs = sorted(_plans["facility"].astype(str).unique())
        _fac = st.selectbox("Facility", _facs, key="vt_fu_fac")
        _fp = _plans[_plans["facility"].astype(str) == _fac].copy()
        _visits = (_fp.drop_duplicates("kobo_id").sort_values("visit_date", ascending=False))
        _labels = {r.kobo_id: f"{str(r.visit_date)[:10]} · {r.visit_ref or r.kobo_id[:8]}" for r in _visits.itertuples()}
        _kid = st.selectbox("Visit", list(_labels), format_func=lambda k: _labels[k], key="vt_fu_visit")
        _sel = _fp[_fp["kobo_id"] == _kid].reset_index(drop=True)
        _opts = ["Not yet reviewed"] + [l for _, l in vt.FOLLOWUP_STATUS]
        _sel["status"] = _sel["status"].where(_sel["status"].isin(_opts), "Not yet reviewed")
        _show = _sel[["n", "challenge", "corrective_action", "responsible_person", "timeline", "status", "comment"]]
        _ed = st.data_editor(_show, hide_index=True, use_container_width=True, key=f"vt_fu_ed_{_kid}",
                             disabled=[c for c in _show.columns if c not in ("status", "comment")] if can_act else list(_show.columns),
                             column_config={"n": "#", "challenge": "Challenge", "corrective_action": "Corrective action",
                                            "responsible_person": "Responsible", "timeline": "Due",
                                            "status": st.column_config.SelectboxColumn("Status", options=_opts, required=True),
                                            "comment": "Comment"})
        if can_act:
            if st.button("💾 Save status", type="primary", key=f"vt_fu_save_{_kid}"):
                _rows = []
                for (_, old), (_, new) in zip(_show.iterrows(), _ed.iterrows()):
                    if (old["status"], old["comment"]) != (new["status"], new["comment"]):
                        _r = _sel.iloc[int(old["n"]) - 1]
                        _rows.append({"action_key": _r["action_key"], "visit_ref": _r["visit_ref"], "facility": _fac,
                                      "action": _r["corrective_action"], "status": "" if new["status"] == "Not yet reviewed" else new["status"],
                                      "comment": new["comment"], "updated_by": display_name,
                                      "updated_at": pd.Timestamp.now().isoformat(timespec="seconds")})
                if not _rows:
                    st.info("Nothing changed.")
                else:
                    try:
                        gsheets.append_kobo_rows("ActionStatus", _rows)
                        st.session_state["vt_flash"] = f"Saved {len(_rows)} action status update(s)."
                        st.rerun()
                    except RuntimeError as e:
                        st.error(f"Couldn't save: {e}")
        st.markdown("**What the Kobo form will show for this facility next visit**")
        st.code(vt.plan_text(vt.latest_plan(_plans, _fac)), language=None)
        with st.expander("All actions not yet done, across facilities"):
            st.dataframe(_open[["facility", "visit_date", "challenge", "corrective_action", "responsible_person", "timeline", "due", "status"]]
                         .sort_values("timeline"), use_container_width=True, hide_index=True)

with tab_topics:
    st.markdown("Each **topic** is one mentorship and competency assessment tool (for example *MT-01 HIV Testing Services and "
                "Prevention*). Upload the Word (.docx) tools here — the app reads Part C (competency items), Part D (systems "
                "standards), Part E (data verification indicators) and Part F (record review columns) and builds them into the "
                "Kobo form. In Kobo, choosing the topic shows that topic's tool.")
    _summ = mentorship.topic_summary(items_all, parts_all)
    if _summ.empty:
        st.info("No topics yet.")
    else:
        st.dataframe(_summ, use_container_width=True, hide_index=True)
        st.caption("Rows with 0 in D/E/F are topics that only have a competency checklist (for example the built-in samples).")
    if not is_admin:
        st.caption("Only an admin can import or replace topics.")
    else:
        _ups = st.file_uploader("Upload one or more topic tools (.docx)", type=["docx"], accept_multiple_files=True, key="vt_topic_up")
        _parsed = []
        for _f in _ups or []:
            try:
                _tp = mentorship.parse_docx(_f.getvalue(), _f.name)
                _parsed.append(_tp)
                st.success(f"**{_tp['title']}** (code `{_tp['code']}`): {len(_tp['C'])} competency items · {len(_tp['D'])} systems standards · "
                           f"{len(_tp['E'])} indicators · {len(_tp['F'])} record-review columns")
            except Exception as e:
                st.error(f"{_f.name}: couldn't read this tool — {e}")
        if _parsed and st.button("📥 Import / replace these topics", type="primary", key="vt_topic_import"):
            _it, _pa = items_all.copy(), mentorship.normalize_parts(parts_all)
            for _tp in _parsed:
                _it, _pa = mentorship.import_topic(_tp, _it, _pa)
            try:
                gsheets.save_competency_items(_it)
                gsheets.save_topic_parts(_pa)
                st.session_state["vt_flash"] = (f"Imported {len(_parsed)} topic(s). Now download the Kobo form again (Lists & Kobo form tab) "
                                                "and replace it in Kobo.")
                st.rerun()
            except RuntimeError as e:
                st.error(f"Couldn't save: {e}")
        st.caption("Importing a topic with the same title replaces its items. Topics you no longer need (such as the samples) can be "
                   "set to inactive on the Competency assessment page.")

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
        st.markdown("**2. Facility list for the Kobo form (from DHIS2).** The form's *Facility* question becomes a search-as-you-type "
                    "dropdown, so supervisors who open the link themselves pick the same spelling as the dashboard. "
                    "Without a list the question is a plain text box.")
        n_fac = len(vt.facility_choices(fac_all))
        _fc = st.session_state.get("dhis2_client")
        if is_admin:
            _lvl = st.number_input("Org unit level of facilities (4 is typical)", min_value=1, max_value=8, value=4, step=1, key="vt_fac_level")
            if _fc is None:
                st.info("Connect to DHIS2 first (Home page), then come back and refresh the list.")
            elif st.button("🔄 Refresh facility list from DHIS2", key="vt_fac_refresh"):
                try:
                    with st.spinner("Fetching org units..."):
                        _ou = _fc.get_org_units(level=int(_lvl))
                    _df = pd.DataFrame({"org_unit_id": _ou.get("id", ""), "name": _ou.get("name", ""),
                                        "parent": _ou["parent"].map(lambda p: p.get("name", "") if isinstance(p, dict) else "") if "parent" in _ou else "",
                                        "level": int(_lvl), "updated_at": pd.Timestamp.now().isoformat(timespec="seconds")})
                    if _df.empty:
                        st.warning("DHIS2 returned no org units at that level.")
                    else:
                        gsheets.save_facilities(_df)
                        st.session_state["vt_flash"] = (f"Saved {len(_df)} facilities. Download the Kobo form again and replace it in Kobo.")
                        st.rerun()
                except RuntimeError as e:
                    st.error(f"Couldn't load org units: {e}")
        st.caption(f"{n_fac} facilities saved." if n_fac else "No facility list saved yet.")

        st.divider()
        st.markdown("**3. Get the Kobo form.** One form for the whole visit: the visit tools (with a repeatable root "
                    "cause analysis), then it asks *\"continue to the competency assessment?\"* (one block per health "
                    "worker, from your competency checklist), then *\"continue to the support supervision report?\"*. "
                    "Built from the *saved* lists. Upload it to Kobo as a new project and deploy it. "
                    "**Whenever you change these lists or the competency checklist, download the form again and "
                    "replace it in Kobo** — Kobo keeps the options it was deployed with.")
        try:
            xls = vt.build_xlsform(ipi_all, routine_all, items_all, parts_all, fac_all)
            st.download_button("⬇️ Download Kobo form (XLSForm)", xls, "visit_tools_form.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="vt_dl_form")
            n_items = int((items_all["active"] != "no").sum()) if not items_all.empty else 0
            st.caption(f"{int((ipi_all['active'] != 'no').sum())} active IPI indicators, "
                       f"{int((routine_all['active'] != 'no').sum())} active routine indicators"
                       + (f" and {n_items} competency items." if n_items else
                          ". The competency checklist is empty, so the form has no competency part yet."))
        except ValueError as e:
            st.error(f"Can't build the Kobo form yet: {e}")
        except ImportError:
            st.error("The `openpyxl` package isn't installed yet, so the Kobo form can't be built. Add "
                     "`openpyxl>=3.1` to requirements.txt, install it, and restart the app.")

        st.divider()
        st.markdown("**4. Connect the Kobo project** by adding this to your Streamlit secrets "
                    "(same file as the other Kobo forms):")
        st.code('[kobo.forms.TOOLS]\nasset_uid = "your-visit-tools-project-id"\n'
                'form_url = "paste the Collect data link here"   # the dashboard buttons open this all-in-one form',
                language="toml")
        st.caption("The root cause analysis methods and fishbone categories are set in `visit_tools.py`. The "
                   "separate competency and support supervision Kobo forms still work on their own, outside a full visit.")

        with st.expander("Reset the lists to the samples"):
            st.warning("This replaces both lists with the built-in samples. Existing submissions are kept.")
            ok = st.checkbox("Yes, replace both lists with the samples", key="vt_reset_ok")
            if st.button("Reset lists", disabled=not ok, key="vt_reset_btn"):
                gsheets.save_table("IPIIndicators", vt.default_ipi())
                gsheets.save_table("RoutineIndicators", vt.default_routine())
                st.session_state["vt_flash"] = "Lists reset to the samples."
                st.rerun()