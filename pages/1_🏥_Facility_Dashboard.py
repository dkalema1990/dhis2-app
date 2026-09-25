import streamlit as st
import pandas as pd
import requests
import plotly.express as px
import plotly.graph_objects as go
import auth
import gsheets
from dhis2_client import get_mock_facility_data, generate_periods

st.set_page_config(page_title="Facility Dashboard", page_icon="🏥", layout="wide")
auth.require_login()
auth.sidebar_user_badge()
role = st.session_state["user"]["role"]
can_act = role in ("submitter", "admin")

st.title("🏥 Facility Achievement Dashboard")

# ---------------------------------------------------------------------
# Load data.
# `viewer` users always see the shared, PUBLISHED analysis (from Google
# Sheets) — not their own session, since they have no DHIS2 connection or
# applied targets of their own. `submitter`/`admin` work in their own
# session (mock or a live DHIS2 pull) and can publish it for viewers to see.
# ---------------------------------------------------------------------
if role == "viewer":
    published = gsheets.get_published_achievement_data() if gsheets.is_configured() else pd.DataFrame()
    if published is None or published.empty:
        st.info("No analysis has been published yet. Showing demo data in the meantime — "
                  "ask an admin/submitter to publish their analysis from the Facility Dashboard.")
        df = get_mock_facility_data(st.session_state.get("period", "2026Q3"))
    else:
        published_by = published["published_by"].iloc[0] if "published_by" in published.columns else "an admin"
        published_at = published["published_at"].iloc[0] if "published_at" in published.columns else ""
        st.success(f"📢 Showing the analysis published by **{published_by}** ({published_at}).")
        df = published.drop(columns=["_row", "published_by", "published_at"], errors="ignore")
elif st.session_state.get("use_mock", True) or "dhis2_client" not in st.session_state:
    period = st.session_state.get("period", "2026Q3")
    df = st.session_state.get("mock_df", get_mock_facility_data(period))
else:
    with st.expander("⚙️ Real DHIS2 pull settings", expanded=True):
        client = st.session_state["dhis2_client"]

        # --- 1) Dataset dropdown ---------------------------------------
        if "dhis2_datasets" not in st.session_state:
            if st.button("🔄 Load datasets from DHIS2"):
                try:
                    with st.spinner("Fetching datasets..."):
                        st.session_state["dhis2_datasets"] = client.get_datasets()
                    st.rerun()
                except RuntimeError as e:
                    st.error(f"Couldn't load datasets from DHIS2: {e}")
        if "dhis2_datasets" in st.session_state:
            ds_df = st.session_state["dhis2_datasets"]
            if ds_df.empty:
                st.warning("No datasets returned — check your DHIS2 permissions.")
            else:
                ds_names = ds_df["name"].tolist()
                ds_name = st.selectbox("Dataset", ds_names, key="ds_name")
                ds_row = ds_df[ds_df["name"] == ds_name].iloc[0]
                dataset_id = ds_row["id"]
                period_type = ds_row.get("periodType", "Monthly")

                # --- 2) Data element dropdown, scoped to the chosen dataset ---
                de_cache_key = f"dhis2_elements_{dataset_id}"
                if de_cache_key not in st.session_state:
                    try:
                        with st.spinner("Fetching data elements for this dataset..."):
                            st.session_state[de_cache_key] = client.get_dataset_elements(dataset_id)
                    except RuntimeError as e:
                        st.error(f"Couldn't load data elements for this dataset: {e}")
                        st.stop()
                de_df = st.session_state[de_cache_key]
                de_names = st.multiselect("Data elements", de_df["name"].tolist(),
                                            default=de_df["name"].tolist()[:5])
                de_ids = de_df[de_df["name"].isin(de_names)]["id"].tolist()

                # --- 3) Reporting period dropdown, generated from the dataset's periodType ---
                period_options = generate_periods(period_type, count=12)
                period_label = st.selectbox(
                    "Reporting period", [p[0] for p in period_options],
                    help=f"Periods generated for this dataset's period type: {period_type}"
                )
                period_code = dict(period_options)[period_label]

                # --- 4) Org unit dropdown, multi-select ------------------------
                ou_level = st.number_input("Org unit level (1=country, 4=typical facility level)",
                                             min_value=1, max_value=8, value=4, step=1)
                ou_cache_key = f"dhis2_orgunits_{ou_level}"
                if ou_cache_key not in st.session_state:
                    if st.button("🔄 Load org units at this level"):
                        try:
                            with st.spinner("Fetching org units..."):
                                st.session_state[ou_cache_key] = client.get_org_units(level=int(ou_level))
                            st.rerun()
                        except RuntimeError as e:
                            st.error(f"Couldn't load org units: {e}")
                if ou_cache_key in st.session_state:
                    ou_df = st.session_state[ou_cache_key]
                    if ou_df.empty:
                        st.warning("No org units returned at this level — check the level number and your "
                                    "DHIS2 permissions.")
                        ou_ids = []
                    else:
                        ou_df = ou_df.copy()
                        ou_df["display"] = ou_df.apply(
                            lambda r: f"{r['name']} ({r['parent']['name']})" if isinstance(r.get("parent"), dict)
                            else r["name"], axis=1)
                        ou_display_sel = st.multiselect(
                            "Org units (select one or more)", ou_df["display"].tolist(),
                            default=ou_df["display"].tolist()[:10]
                        )
                        ou_ids = ou_df[ou_df["display"].isin(ou_display_sel)]["id"].tolist()
                else:
                    ou_ids = []
                    st.caption("Click \"Load org units at this level\" above to pick facilities.")

                if st.button("Pull from DHIS2", type="primary") and de_ids and ou_ids:
                    try:
                        with st.spinner("Pulling analytics from DHIS2..."):
                            raw = client.get_analytics(de_ids, ";".join(ou_ids), period_code)
                    except RuntimeError as e:
                        st.error(str(e))
                        st.info("Common fixes: double-check the org unit UID is correct and that your "
                                  "DHIS2 login has access to it, and confirm analytics tables have been "
                                  "generated for this period on your DHIS2 instance.")
                        st.stop()
                    df_pulled = raw.rename(columns={"value": "actual"})
                    df_pulled["target"] = pd.NA
                    df_pulled["achievement_pct"] = pd.NA
                    st.session_state["live_df"] = df_pulled
                    st.session_state["period"] = period_code
                    st.info("Data pulled. Scroll down to \"🎯 Apply your own targets\" to add targets and see "
                              "achievement %.")

                if st.button("↻ Refresh dataset/data element/org unit lists"):
                    for k in list(st.session_state.keys()):
                        if (k == "dhis2_datasets" or k.startswith("dhis2_elements_")
                                or k.startswith("dhis2_orgunits_")):
                            del st.session_state[k]
                    st.rerun()
    df = st.session_state.get("live_df", get_mock_facility_data(st.session_state.get("period", "2026Q3")))

# ---------------------------------------------------------------------
# Apply manually-entered targets — independent of mock vs. live DHIS2,
# and independent of re-pulling. Once applied, they stick (via
# session_state) until you clear them or apply a new file.
# ---------------------------------------------------------------------
manual_targets = st.session_state.get("manual_targets")
if role != "viewer" and manual_targets is not None and not manual_targets.empty:
    df = df.drop(columns=["target", "achievement_pct"], errors="ignore").merge(
        manual_targets, on=["facility", "data_element"], how="left"
    )
    df["target"] = pd.to_numeric(df["target"], errors="coerce")
    df["achievement_pct"] = (df["actual"] / df["target"] * 100).round(1)

# ---------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------
col1, col2 = st.columns(2)
facilities = sorted(df["facility"].unique())
elements = sorted(df["data_element"].unique())
f_sel = col1.multiselect("Filter facilities", facilities, default=facilities)
e_sel = col2.multiselect("Filter data elements", elements, default=elements)
view = df[df["facility"].isin(f_sel) & df["data_element"].isin(e_sel)]

# --- Targets: download a template, then apply it whenever you're ready --
st.divider()
with st.expander("🎯 Targets — download a template, fill it in, then apply it here", expanded=True):
    st.markdown(
        "**Step 1.** Download a template listing every facility × data element combo below "
        "(respects the filters above) — already-applied targets are pre-filled, so you only "
        "need to fill in rows for anything new. **Step 2.** Fill in the `target` column in "
        "Excel/Sheets, at your own pace. **Step 3.** Upload it below and click Apply — "
        "achievement % updates immediately."
    )
    template_df = (view[["facility", "data_element"]]
                    .drop_duplicates()
                    .sort_values(["facility", "data_element"])
                    .reset_index(drop=True))
    if manual_targets is not None and not manual_targets.empty:
        # Pre-fill with whatever's already been applied, so re-downloading after
        # adding new data elements only leaves the genuinely new rows blank.
        template_df = template_df.merge(manual_targets, on=["facility", "data_element"], how="left")
        template_df["target"] = template_df["target"].fillna("")
        new_rows = int((template_df["target"] == "").sum())
        st.caption(f"Pre-filled with your {len(manual_targets)} previously-applied target(s) — "
                    f"{new_rows} row(s) still need a target.")
    else:
        template_df["target"] = ""
    st.download_button(
        "📥 Download targets template (CSV)",
        data=template_df.to_csv(index=False).encode("utf-8"),
        file_name="targets_template.csv",
        mime="text/csv",
        help=f"{len(template_df)} facility × data element rows, ready to fill in and re-upload.",
    )

    uploaded = st.file_uploader("Upload your filled-in targets CSV", type=["csv"], key="targets_upload",
                                  disabled=not can_act)
    if not can_act:
        st.info("Your role (`viewer`) can view and download this template, but only `submitter`/`admin` "
                  "roles can apply or clear targets.")
    c1, c2 = st.columns(2)
    if c1.button("✅ Apply targets", type="primary", disabled=(uploaded is None or not can_act)):
        try:
            new_targets = pd.read_csv(uploaded)
        except Exception as e:
            st.error(f"Couldn't read that CSV: {e}")
            st.stop()
        missing_cols = {"facility", "data_element", "target"} - set(new_targets.columns)
        if missing_cols:
            st.error(f"CSV is missing required column(s): {', '.join(missing_cols)}")
            st.stop()
        new_targets = new_targets.dropna(subset=["target"])
        new_targets = new_targets[new_targets["target"].astype(str).str.strip() != ""]
        if new_targets.empty:
            st.warning("No rows with a filled-in target were found — nothing to apply yet.")
        else:
            # Merge with any previously-applied targets so you can top it up
            # incrementally instead of needing every row filled in at once.
            existing = st.session_state.get("manual_targets")
            combined = new_targets[["facility", "data_element", "target"]]
            if existing is not None and not existing.empty:
                combined = (pd.concat([existing, combined])
                              .drop_duplicates(subset=["facility", "data_element"], keep="last"))
            st.session_state["manual_targets"] = combined
            st.success(f"Applied targets for {len(new_targets)} row(s).")
            st.rerun()
    if c2.button("🗑️ Clear all applied targets", disabled=("manual_targets" not in st.session_state or not can_act)):
        st.session_state.pop("manual_targets", None)
        st.rerun()
    if manual_targets is not None and not manual_targets.empty:
        st.caption(f"Currently applied: targets set for {len(manual_targets)} facility × data element row(s).")

# --- Publish: share this analysis with everyone, including viewers -----
if can_act:
    with st.expander("📢 Publish this analysis for everyone to see", expanded=False):
        st.markdown(
            "`viewer` users don't have their own DHIS2 connection or applied targets — "
            "they see whatever was last **published** here. Publishing saves the dataset "
            "currently shown above (including any targets you've applied) to a shared "
            "Google Sheet, replacing whatever was published before."
        )
        if st.button("📢 Publish current analysis", type="primary"):
            publish_df = df[["facility", "data_element", "period", "actual", "target", "achievement_pct"]].copy() \
                if "period" in df.columns else df.assign(period=st.session_state.get("period", "2026Q3"))
            try:
                gsheets.publish_achievement_data(publish_df, st.session_state["user"]["display_name"])
                st.success("Published! Viewers will now see this analysis by default.")
            except RuntimeError as e:
                st.error(f"Couldn't publish: {e}")

RED, YELLOW = 50, 90  # <RED = red, RED-<YELLOW = yellow, >=YELLOW = green


def badge(pct):
    if pd.isna(pct):
        return "⚪ N/A"
    if pct < RED:
        return f"🔴 {pct}%"
    if pct < YELLOW:
        return f"🟡 {pct}%"
    return f"🟢 {pct}%"


def _pct_cell_style(val):
    """Background/text color for a single achievement % cell, same red/yellow/green
    bands as the facility-level badge, so the detail table reads at a glance."""
    if pd.isna(val):
        return "background-color: #f0f0f0; color: #888888;"
    if val < RED:
        return "background-color: #fdecea; color: #b3261e; font-weight: 600;"
    if val < YELLOW:
        return "background-color: #fff8e1; color: #8a6d00; font-weight: 600;"
    return "background-color: #e6f4ea; color: #1e7e34; font-weight: 600;"


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


def _band(pct):
    if pd.isna(pct):
        return "No target"
    if pct < RED:
        return "Red (<50%)"
    if pct < YELLOW:
        return "Yellow (50–89%)"
    return "Green (≥90%)"


BAND_COLORS = {"Red (<50%)": "#e05252", "Yellow (50–89%)": "#e0b23c",
               "Green (≥90%)": "#3fa54a", "No target": "#b0b0b0"}

# ---------------------------------------------------------------------
# Visual overview: KPI counts, a color-coded bar chart per facility, and
# a facility × data element heatmap — sits above the individual cards.
# ---------------------------------------------------------------------
if not summary.empty:
    summary["band"] = summary["achievement_pct"].apply(_band)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Facilities shown", len(summary))
    k2.metric("🔴 Red (<50%)", int((summary["band"] == "Red (<50%)").sum()))
    k3.metric("🟡 Yellow (50–89%)", int((summary["band"] == "Yellow (50–89%)").sum()))
    k4.metric("🟢 Green (≥90%)", int((summary["band"] == "Green (≥90%)").sum()))

    tab_bar, tab_heat = st.tabs(["📊 Avg. achievement by facility", "🗺️ Heatmap (facility × data element)"])

    with tab_bar:
        bar_df = summary.sort_values("achievement_pct")
        fig = go.Figure(go.Bar(
            x=bar_df["achievement_pct"], y=bar_df["facility"], orientation="h",
            marker_color=[BAND_COLORS[b] for b in bar_df["band"]],
            text=bar_df["achievement_pct"].map(lambda v: f"{v:.0f}%" if pd.notna(v) else "N/A"),
            textposition="outside",
        ))
        fig.add_vline(x=RED, line_dash="dot", line_color="#e05252", opacity=0.5)
        fig.add_vline(x=YELLOW, line_dash="dot", line_color="#3fa54a", opacity=0.5)
        fig.update_layout(
            height=max(300, 32 * len(bar_df)), margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title="Avg. achievement %", yaxis_title="", showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)

    with tab_heat:
        pivot = view.pivot_table(index="facility", columns="data_element",
                                   values="achievement_pct", aggfunc="mean")
        if pivot.empty:
            st.info("Nothing to show for the current filters.")
        else:
            fig2 = px.imshow(
                pivot, color_continuous_scale="RdYlGn", zmin=0, zmax=120,
                aspect="auto", text_auto=".0f",
                labels=dict(color="Achievement %"),
            )
            fig2.update_layout(height=max(300, 40 * len(pivot)), margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig2, use_container_width=True)
            st.caption("Gray/blank cells mean no target has been set for that facility × data element yet.")

for _, row in summary.iterrows():
    facility = row["facility"]
    avg_pct = row["achievement_pct"]
    rec = recommended_action(avg_pct)
    with st.container(border=True):
        c1, c2 = st.columns([3, 2])
        c1.markdown(f"### {facility}")
        c2.metric("Avg. Achievement", badge(avg_pct))

        detail = view[view["facility"] == facility][["data_element", "target", "actual", "achievement_pct"]]
        with st.expander("View data elements", expanded=False):
            # na_rep handles missing achievement % gracefully (e.g. no targets
            # uploaded yet for a live DHIS2 pull) instead of crashing on format()
            styler = detail.style.format({"achievement_pct": "{:.1f}%"}, na_rep="N/A")
            # pandas >=2.1 renamed Styler.applymap to .map; support either so this
            # doesn't break depending on exactly which pandas version gets installed
            color_fn = styler.map if hasattr(styler, "map") else styler.applymap
            styled = color_fn(_pct_cell_style, subset=["achievement_pct"])
            st.dataframe(styled, hide_index=True, use_container_width=True)

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