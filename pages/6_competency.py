from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

import auth
import gsheets
import kobo
import competency as comp

auth.require_login()
auth.sidebar_user_badge()
role = st.session_state["user"]["role"]
display_name = st.session_state["user"]["display_name"]
can_act = role in ("submitter", "admin")
is_admin = role == "admin"

st.title("🎯 Competency assessment")
st.caption("Assess each health worker separately before mentorship starts (baseline), then reassess at a later "
           "visit (follow-up) to show progress. The checklist is editable in the **Checklist & Kobo form** tab.")

_flash = st.session_state.pop("comp_flash", None)
if _flash:
    st.success(_flash)

if not gsheets.is_configured():
    st.warning("Google Sheets isn't configured, so assessments can't be saved or shown.")
    st.stop()

try:
    items_all = comp.load_items(seed=can_act)          # seeds the sample checklist the first time
except RuntimeError as e:
    st.error(f"Couldn't load the checklist: {e}")
    st.stop()
items_active = comp.active_items(items_all) if not items_all.empty else items_all
SCALE_LABELS = [label for _, label, _ in comp.SCALE]

tab_assess, tab_results, tab_setup = st.tabs(["📝 Assess a health worker", "📊 Results", "⚙️ Checklist & Kobo form"])

# =====================================================================
# 1) Assess a health worker (in-app form)
# =====================================================================
with tab_assess:
    n = st.session_state.get("comp_form_n", 0)          # bumping this resets the form after a save
    last = st.session_state.pop("comp_last", None)
    if last:
        st.success(f"Saved: **{last['mentee']}** — {last['area']} ({last['type']}): "
                   f"**{last['pct']}% competent** ({last['competent']} of {last['scored']} items).")
        if last["gaps"]:
            st.markdown("**Focus areas for mentorship** (scored below competent) — copy these into the "
                        "Support Supervision form's mentorship / recommendations:")
            st.text_area("Gaps", last["gaps"], height=min(300, 40 + 28 * last["gaps"].count("\n")),
                         label_visibility="collapsed", key=f"gaps_view_{n}")
        else:
            st.info("No gaps — this health worker scored competent on every item assessed. 🎉")

    if not can_act:
        st.info("Your role (`viewer`) can see results but can't record assessments.")
    elif items_active.empty:
        st.info("The checklist has no active items yet. An admin can add them in the **Checklist & Kobo form** tab.")
    else:
        areas = list(dict.fromkeys(items_active["service_area"]))
        c1, c2 = st.columns(2)
        facility = c1.text_input("Facility", value=st.session_state.get("selected_facility") or "", key="comp_facility")
        mentee = c2.text_input("Health worker being assessed (full name)", key=f"comp_mentee_{n}")
        c3, c4, c5 = st.columns(3)
        cadre = c3.selectbox("Cadre", comp.CADRES, key="comp_cadre")
        area = c4.selectbox("Service area", areas, key="comp_area")
        atype = c5.radio("Assessment type", [lbl for _, lbl in comp.ASSESSMENT_TYPES], horizontal=True, key="comp_type")
        c6, c7, c8 = st.columns(3)
        adate = c6.date_input("Date of assessment", value=date.today(), key="comp_date")
        assessor = c7.text_input("Assessor", value=display_name, key="comp_assessor")
        visit_ref = c8.text_input("Visit reference (optional)", key="comp_visit",
                                  help="Use the same reference for every health worker assessed in one visit.")

        # Offline option: the same checklist as a Kobo form
        _link = kobo.prefilled_link("COMP", facility.strip(), "")
        if _link:
            st.link_button("📲 Prefer your phone or no internet? Open the Kobo form", _link,
                           help="Same checklist, works offline. Submissions arrive here after Kobo sync.")

        # Baseline context for this health worker + area
        if facility.strip() and mentee.strip():
            hist = comp.get_assessments()
            if not hist.empty:
                mine = hist[(hist["facility_key"] == facility.strip().lower())
                            & (hist["mentee_key"] == mentee.strip().lower()) & (hist["service_area"] == area)]
                s = comp.summarize(mine)
                base = s[s["assessment_type"] == comp.TYPE_LABEL["baseline"]]
                if not base.empty:
                    b = base.sort_values("date_dt").iloc[0]
                    st.info(f"Baseline for {b['mentee_name']} in {area} ({b['assessment_date']}): "
                            f"**{b['pct_competent']}% competent** ({b['competent']} of {b['scored']}).")
                elif atype == comp.TYPE_LABEL["follow_up"]:
                    st.warning("No baseline found for this health worker in this service area. You can still save "
                               "this, but there's nothing to compare it with yet.")

        area_items = items_active[items_active["service_area"] == area]
        with st.form(f"comp_form_{n}"):
            st.markdown(f"##### {area}")
            st.caption("Score every item. Use **Not applicable** for anything that wasn't relevant or observable.")
            scores = {}
            for domain, grp in area_items.groupby("domain", sort=False):
                if domain:
                    st.markdown(f"**{domain}**")
                for _, it in grp.iterrows():
                    choice = st.radio(it["competency"], SCALE_LABELS, index=None, horizontal=True,
                                      key=f"sc_{n}_{it['item_id']}")
                    scores[it["item_id"]] = comp.SCORE_CODE.get(choice)
            comments = st.text_area("Comments / observations", key=f"comp_comments_{n}")
            submitted = st.form_submit_button("Submit assessment", type="primary")

        if submitted:
            problems = []
            if not facility.strip():
                problems.append("Facility is required.")
            if not mentee.strip():
                problems.append("The health worker's name is required.")
            if not assessor.strip():
                problems.append("Assessor is required.")
            unscored = [k for k, v in scores.items() if v is None]
            if unscored:
                problems.append(f"{len(unscored)} item(s) still have no score — pick one for every item "
                                "(use Not applicable where it doesn't apply).")
            if problems:
                for p in problems:
                    st.error(p)
            else:
                header = {"facility": facility.strip(), "mentee_name": mentee.strip(), "cadre": cadre,
                          "service_area": area, "assessment_type": atype, "assessment_date": str(adate),
                          "assessor": assessor.strip(), "visit_ref": visit_ref.strip(), "comments": comments.strip()}
                try:
                    comp.save_assessment(header, scores, area_items)
                except RuntimeError as e:
                    st.error(f"Couldn't save: {e}")
                else:
                    vals = [comp.SCORE_VALUE[c] for c in scores.values() if comp.SCORE_VALUE[c] is not None]
                    competent = sum(1 for v in vals if v >= comp.COMPETENT_MIN)
                    st.session_state["comp_last"] = {
                        "mentee": mentee.strip(), "area": area, "type": atype, "scored": len(vals),
                        "competent": competent, "pct": round(competent / len(vals) * 100, 1) if vals else 0,
                        "gaps": comp.gaps_text(scores, area_items)}
                    st.session_state["comp_form_n"] = n + 1
                    st.rerun()

# =====================================================================
# 2) Results
# =====================================================================
with tab_results:
    try:
        hist = comp.get_assessments()
    except RuntimeError as e:
        st.error(f"Couldn't load assessments: {e}")
        hist = pd.DataFrame()
    if st.button("🔄 Refresh", key="comp_refresh"):
        gsheets.get_all.clear()
        st.rerun()

    if hist.empty:
        st.info("No assessments recorded yet.")
    else:
        f1, f2, f3 = st.columns(3)
        fac_sel = f1.multiselect("Facility", sorted(hist["facility"].astype(str).unique()))
        area_sel = f2.multiselect("Service area", sorted(hist["service_area"].astype(str).unique()))
        cadre_sel = f3.multiselect("Cadre", sorted(hist["cadre"].astype(str).unique()))
        d = hist
        if fac_sel:
            d = d[d["facility"].isin(fac_sel)]
        if area_sel:
            d = d[d["service_area"].isin(area_sel)]
        if cadre_sel:
            d = d[d["cadre"].isin(cadre_sel)]

        if d.empty:
            st.info("Nothing matches those filters.")
        else:
            summ = comp.summarize(d)
            latest_sum = comp.summarize(comp.latest_per_worker_area(d))
            workers = d[["facility_key", "mentee_key"]].drop_duplicates().shape[0]
            overall = (latest_sum["competent"].sum() / latest_sum["scored"].sum() * 100) if latest_sum["scored"].sum() else float("nan")
            base_avg = summ.loc[summ["assessment_type"] == comp.TYPE_LABEL["baseline"], "pct_competent"].mean()
            foll_avg = summ.loc[summ["assessment_type"] == comp.TYPE_LABEL["follow_up"], "pct_competent"].mean()
            fmt = lambda v: "—" if pd.isna(v) else f"{v:.1f}%"
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Assessments", len(summ))
            k2.metric("Health workers", workers)
            k3.metric("Avg. baseline", fmt(base_avg))
            k4.metric("Avg. follow-up", fmt(foll_avg), None if pd.isna(base_avg) or pd.isna(foll_avg)
                      else f"{foll_avg - base_avg:+.1f} pts")
            st.caption(f"Latest assessment per health worker and service area: {fmt(overall)} of items scored competent.")

            st.subheader("Assessments")
            show = summ[["assessment_date", "facility", "mentee_name", "cadre", "service_area", "assessment_type",
                         "pct_competent", "competent", "scored", "gaps", "assessor", "visit_ref"]]
            st.dataframe(show, hide_index=True, use_container_width=True,
                         column_config={"pct_competent": st.column_config.NumberColumn("% competent", format="%.1f%%")})

            st.subheader("Progress: baseline vs follow-up")
            prog = comp.progress(summ)
            both = prog.dropna(subset=["baseline_pct", "followup_pct"])
            if both.empty:
                st.info("No health worker has both a baseline and a follow-up yet.")
            else:
                st.dataframe(prog, hide_index=True, use_container_width=True, column_config={
                    "baseline_pct": st.column_config.NumberColumn("baseline %", format="%.1f"),
                    "followup_pct": st.column_config.NumberColumn("follow-up %", format="%.1f"),
                    "change": st.column_config.NumberColumn("change (pts)", format="%+.1f")})
                long = both.assign(label=both["mentee_name"] + " — " + both["service_area"]).melt(
                    id_vars="label", value_vars=["baseline_pct", "followup_pct"], var_name="when", value_name="pct")
                long["when"] = long["when"].map({"baseline_pct": "Baseline", "followup_pct": "Follow-up"})
                fig = px.bar(long, x="label", y="pct", color="when", barmode="group",
                             color_discrete_map={"Baseline": "#b0b0b0", "Follow-up": "#3fa54a"},
                             labels={"pct": "% competent", "label": "", "when": ""})
                fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10), yaxis_range=[0, 100])
                st.plotly_chart(fig, use_container_width=True)

            st.subheader("Weakest competencies")
            latest_rows = comp.latest_per_worker_area(d)
            areas_here = sorted(latest_rows["service_area"].astype(str).unique())
            wk_area = st.selectbox("Service area", areas_here, key="comp_weak_area")
            wk = comp.weakest(latest_rows, wk_area, n=10)
            if wk.empty:
                st.info("Nothing scored in this service area yet.")
            else:
                fig2 = px.bar(wk.iloc[::-1], x="pct_competent", y="competency", orientation="h",
                              hover_data=["assessed", "competent"], labels={"pct_competent": "% of health workers competent", "competency": ""})
                fig2.update_traces(marker_color="#e05252")
                fig2.update_layout(height=max(300, 42 * len(wk)), margin=dict(l=10, r=10, t=10, b=10), xaxis_range=[0, 100])
                st.plotly_chart(fig2, use_container_width=True)
                st.caption("Based on each health worker's most recent assessment — a good guide to what to mentor on.")

            st.download_button("⬇️ Download all scored items (CSV)",
                               d[comp.COMP_COLS].to_csv(index=False).encode("utf-8"),
                               "competency_assessments.csv", "text/csv")

            if is_admin:
                with st.expander("🗑️ Delete an assessment (admin)"):
                    opts = {f"{r.assessment_date} · {r.facility} · {r.mentee_name} · {r.service_area} · "
                            f"{r.assessment_type} ({r.pct_competent}%)": r.assessment_id for r in summ.itertuples()}
                    pick = st.selectbox("Assessment", list(opts), key="comp_del_pick")
                    sure = st.checkbox("Yes, delete all rows of this assessment", key="comp_del_sure")
                    if st.button("Delete assessment", disabled=not sure, key="comp_del_btn"):
                        try:
                            removed = gsheets.delete_competency_assessment(opts[pick], display_name)
                            st.session_state["comp_flash"] = f"Deleted {removed} row(s). If it came from Kobo, it won't be re-imported."
                            st.rerun()
                        except RuntimeError as e:
                            st.error(f"Couldn't delete: {e}")

# =====================================================================
# 3) Checklist & Kobo form (admin)
# =====================================================================
with tab_setup:
    if not is_admin:
        st.info("Only admins can edit the checklist or download the Kobo form.")
        if not items_active.empty:
            with st.expander("View the current checklist"):
                st.dataframe(items_active, hide_index=True, use_container_width=True)
    else:
        st.markdown(
            "**1. Edit the checklist.** One row per competency. The checklist below is a **sample** — replace it "
            "with your own. Don't delete an item that already has assessments; set `active` to `no` instead, so "
            "old results keep their wording.")
        base_df = items_all if not items_all.empty else comp.default_items()
        edited = st.data_editor(
            base_df, num_rows="dynamic", hide_index=True, use_container_width=True, key="comp_items_editor",
            column_config={
                "item_id": st.column_config.TextColumn("item_id", help="Short unique code, e.g. anc_01. Letters, numbers, underscores. Don't change once in use.", required=True),
                "service_area": st.column_config.TextColumn("service_area", required=True),
                "domain": st.column_config.TextColumn("domain", help="Optional heading to group items."),
                "competency": st.column_config.TextColumn("competency", width="large", required=True),
                "active": st.column_config.SelectboxColumn("active", options=["yes", "no"], required=True, default="yes"),
            })
        b1, b2 = st.columns([1, 3])
        if b1.button("💾 Save checklist", type="primary", key="comp_save_items"):
            errs = comp.validate_items(edited)
            if errs:
                for e in errs[:8]:
                    st.error(e)
            else:
                try:
                    gsheets.save_competency_items(comp.normalize_items(edited))
                    st.session_state["comp_flash"] = "Checklist saved. Download the Kobo form again below if you use Kobo."
                    st.rerun()
                except RuntimeError as e:
                    st.error(f"Couldn't save: {e}")

        st.divider()
        st.markdown(
            "**2. Get the Kobo form.** Built from the *saved* checklist (save above first). Upload it to Kobo as "
            "a new project, deploy it, and use the same Collect-data link for the dashboard (see below). "
            "**Whenever you change the checklist, download and replace the form in Kobo** — Kobo keeps the "
            "questions it was deployed with.")
        try:
            xls = comp.build_xlsform(items_all)
            st.download_button("⬇️ Download Kobo form (XLSForm)", xls, "competency_assessment.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="comp_dl_form")
            st.caption(f"{len(items_active)} active items across {items_active['service_area'].nunique()} service area(s).")
        except ValueError as e:
            st.error(f"Can't build the Kobo form yet: {e}")

        st.divider()
        st.markdown("**3. Connect the Kobo project** by adding this to your Streamlit secrets "
                    "(same file as the other Kobo forms):")
        st.code('[kobo.forms.COMP]\nasset_uid = "your-competency-project-id"\n'
                'form_url = "paste the Collect data link here"   # optional: adds the Kobo button on this page',
                language="toml")
        st.caption("Kobo submissions arrive in this app when you click **Sync now** on the Activities Dashboard.")

        with st.expander("Reset to the sample checklist"):
            st.warning("This replaces the whole checklist with the built-in sample. Existing assessments are kept.")
            ok = st.checkbox("Yes, replace the checklist with the sample", key="comp_reset_ok")
            if st.button("Reset checklist", disabled=not ok, key="comp_reset_btn"):
                gsheets.save_competency_items(comp.default_items())
                st.session_state["comp_flash"] = "Checklist reset to the sample."
                st.rerun()
