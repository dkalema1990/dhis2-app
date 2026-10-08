import streamlit as st
import pandas as pd
import requests
import plotly.express as px
import plotly.graph_objects as go
import auth
import gsheets
import formulas
from dhis2_client import get_mock_facility_data, generate_periods

auth.require_login()
auth.sidebar_user_badge()
role = st.session_state["user"]["role"]
can_act = role in ("submitter", "admin")

st.title("🏥 Facility Achievement Dashboard")

# Data elements picked in the DHIS2 pull settings (available to the formula builder
# even before you click Pull). Stays empty in demo/viewer mode.
selected_de_names = []
in_live_branch = False         # True once the DHIS2 pull settings are showing
calc_builder_rendered = False  # builder is drawn in the pull settings (live) OR under the filters

# Calculated-field definitions (shared, persisted). Loaded up here so the formula builder
# can sit inside the DHIS2 pull settings, before the reporting period.
calc_fields = gsheets.get_calculated_fields() if gsheets.is_configured() else pd.DataFrame()


def fetch_all_data_elements(client) -> pd.DataFrame:
    """Every aggregate data element in the DHIS2 instance (id, name) — across ALL
    datasets, not just the one selected — so a calculated field can combine elements
    that belong to different datasets."""
    try:
        r = requests.get(f"{client.base_url}/api/dataElements.json",
                          params={"fields": "id,name", "filter": "domainType:eq:AGGREGATE", "paging": "false"},
                          auth=client.auth, timeout=60)
    except requests.RequestException as e:
        raise RuntimeError(f"Couldn't reach DHIS2: {e}")
    if not r.ok:
        try:
            detail = r.json().get("message", r.text)
        except ValueError:
            detail = r.text
        raise RuntimeError(f"DHIS2 request failed ({r.status_code}): {detail}")
    return pd.DataFrame(r.json().get("dataElements", []), columns=["id", "name"]).sort_values("name")


def _calc_insert_element():
    el = st.session_state.get("calc_pick")
    if el:
        cur = st.session_state.get("calc_formula", "").rstrip()
        st.session_state["calc_formula"] = f"{cur} [{el}]".strip()


def _calc_insert_op(op):
    cur = st.session_state.get("calc_formula", "").rstrip()
    st.session_state["calc_formula"] = f"{cur} {op}".strip()


def _calc_clear():
    st.session_state["calc_formula"] = ""
    st.session_state["calc_name"] = ""


def _calc_add(universe):
    name = st.session_state.get("calc_name", "").strip()
    formula = st.session_state.get("calc_formula", "").strip()
    if not name or not formula:
        st.session_state["calc_msg"] = ("error", "Both a name and a formula are required.")
        return
    if name in universe:
        st.session_state["calc_msg"] = ("error", "That name collides with an existing data element — pick a different name.")
        return
    try:
        formulas.validate_formula(formula, set(universe))
        gsheets.save_calculated_field(name, formula, st.session_state["user"]["display_name"])
        st.session_state["calc_msg"] = ("success", f"Added calculated field \"{name}\".")
        st.session_state["calc_formula"] = ""
        st.session_state["calc_name"] = ""
    except formulas.FormulaError as e:
        st.session_state["calc_msg"] = ("error", f"Formula error: {e}")
    except RuntimeError as e:
        st.session_state["calc_msg"] = ("error", f"Couldn't save: {e}")


def render_calc_builder(extra_names, preview_df, inline=False):
    """Searchable formula builder.
    extra_names: element names that can be referenced even if they aren't in the loaded
                 data (e.g. every element in DHIS2, or ones ticked but not yet pulled).
    preview_df:  data currently loaded (or None) — drives the live preview.
    inline:      True inside the pull settings (an expander can't nest inside an expander)."""
    existing = set(calc_fields["name"]) if calc_fields is not None and not calc_fields.empty else set()
    have_data = preview_df is not None and not preview_df.empty
    raw_elements = (set(preview_df["data_element"].unique()) - existing) if have_data else set()
    universe = (raw_elements | set(extra_names)) - existing

    box = (st.container(border=True) if inline else
           st.expander("🧮 Calculated fields — build a new field from your data elements", expanded=True))
    with box:
        if inline:
            st.markdown("**🧮 Calculated fields** — build them from data elements of *any* dataset")
        _msg = st.session_state.pop("calc_msg", None)
        if _msg:
            (st.success if _msg[0] == "success" else st.error)(_msg[1])

        if calc_fields is not None and not calc_fields.empty:
            st.markdown("**Existing calculated fields** (already included in the filters, targets and charts):")
            for _, cf in calc_fields.iterrows():
                fc1, fc2 = st.columns([6, 1])
                fc1.markdown(f"**{cf['name']}** = `{cf['formula']}`")
                if can_act and fc2.button("🗑️", key=f"del_calc_{cf['name']}", help="Delete this calculated field"):
                    try:
                        gsheets.delete_calculated_field(cf["name"])
                        st.rerun()
                    except RuntimeError as e:
                        st.error(f"Couldn't delete: {e}")

        if not can_act:
            st.info("Your role (`viewer`) can see calculated fields but not add or remove them.")
        elif not universe:
            st.info("No data elements available yet — load data first, then build a calculated field here.")
        else:
            st.markdown("**Add a calculated field**")
            st.text_input("Field name", key="calc_name", placeholder="e.g. DPT3 Coverage Ratio")

            pc1, pc2 = st.columns([5, 1], vertical_alignment="bottom")
            pc1.selectbox("🔍 Search a data element (type to filter), then click Insert",
                           sorted(universe), index=None, key="calc_pick",
                           placeholder="Start typing a data element name…")
            pc2.button("➕ Insert", on_click=_calc_insert_element, use_container_width=True)

            oc = st.columns(7)
            for col, (label, op) in zip(oc[:6], [("＋", "+"), ("−", "-"), ("×", "*"), ("÷", "/"), ("(", "("), (")", ")")]):
                col.button(label, key=f"calc_op_{op}", on_click=_calc_insert_op, args=(op,), use_container_width=True)
            oc[6].button("Clear", key="calc_clear", on_click=_calc_clear, use_container_width=True)

            st.text_input("Formula (built from the buttons above, or type it)", key="calc_formula",
                           placeholder="[DPT3 Immunization] / [ANC 4th visit] * 100")

            _f = st.session_state.get("calc_formula", "").strip()
            _valid = False
            if _f:
                try:
                    formulas.validate_formula(_f, universe)
                    _valid = True
                    _refs = formulas.extract_referenced_elements(_f)
                    _missing = [r for r in _refs if r not in raw_elements]
                    if _missing:
                        st.info("✅ Formula is valid. Values will appear once you pull data that includes: "
                                  + ", ".join(f"`{m}`" for m in _missing))
                    else:
                        _piv = preview_df[preview_df["data_element"].isin(raw_elements)].pivot_table(
                            index="facility", columns="data_element", values="actual", aggfunc="mean")
                        _rows = []
                        for _fac in _piv.index[:8]:
                            try:
                                _v = formulas.evaluate_formula(_f, raw_elements, _piv.loc[_fac].to_dict())
                            except formulas.FormulaError:
                                _v = float("nan")
                            _rows.append({"facility": _fac, "value": round(_v, 2) if pd.notna(_v) else None})
                        st.caption("✅ Formula is valid — live preview (first facilities):")
                        st.dataframe(pd.DataFrame(_rows), hide_index=True, use_container_width=True)
                except formulas.FormulaError as e:
                    st.error(f"Formula problem: {e}")

            st.button("➕ Add calculated field", type="primary", key="calc_add",
                       on_click=_calc_add, args=(frozenset(universe),),
                       disabled=not (_valid and st.session_state.get("calc_name", "").strip()))


# ---------------------------------------------------------------------
# Load data.
# `viewer` users always see the shared, PUBLISHED analysis (from Google
# Sheets) — not their own session, since they have no DHIS2 connection or
# applied targets of their own. `submitter`/`admin` work in their own
# session (mock or a live DHIS2 pull) and can publish it for viewers to see.
# ---------------------------------------------------------------------
if role == "viewer":
    vc1, vc2 = st.columns([5, 1])
    if vc2.button("🔄 Refresh", help="Force-reload the published analysis now instead of waiting "
                                        "up to 30s for the cache to refresh on its own."):
        gsheets.get_all.clear()
        st.rerun()
    published = gsheets.get_published_achievement_data() if gsheets.is_configured() else pd.DataFrame()
    if published is None or published.empty:
        vc1.info("No analysis has been published yet. Showing demo data in the meantime — "
                  "ask an admin/submitter to publish their analysis from the Facility Dashboard.")
        df = get_mock_facility_data(st.session_state.get("period", "2026Q3"))
    else:
        published_by = published["published_by"].iloc[0] if "published_by" in published.columns else "an admin"
        published_at = published["published_at"].iloc[0] if "published_at" in published.columns else ""
        vc1.success(f"📢 Showing the analysis published by **{published_by}** ({published_at}).")
        df = published.drop(columns=["_row", "published_by", "published_at"], errors="ignore")
elif st.session_state.get("use_mock", True) or "dhis2_client" not in st.session_state:
    period = st.session_state.get("period", "2026Q3")
    if "mock_df" in st.session_state:
        df = st.session_state["mock_df"]
    else:
        # Nothing pulled yet this session (e.g. you just logged back in). Rather
        # than jumping straight to unrelated demo data, recover your own last
        # PUBLISHED analysis if you have one — this is the only thing from a
        # previous session that's actually saved anywhere.
        last_published = gsheets.get_published_achievement_data() if gsheets.is_configured() else pd.DataFrame()
        if last_published is not None and not last_published.empty:
            df = last_published.drop(columns=["_row", "published_by", "published_at"], errors="ignore")
            pub_by = last_published["published_by"].iloc[0] if "published_by" in last_published.columns else "someone"
            pub_at = last_published["published_at"].iloc[0] if "published_at" in last_published.columns else ""
            st.info(f"Showing the last analysis published by **{pub_by}** ({pub_at}) — connect to DHIS2 "
                      f"or load demo data below to start a fresh pull instead.")
        else:
            df = get_mock_facility_data(period)
else:
    in_live_branch = True
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
                selected_de_names = list(de_names)

                # --- 2b) Calculated fields — inputs can come from ANY dataset ---------------
                all_el = st.session_state.get("dhis2_all_elements")
                if all_el is None:
                    try:
                        with st.spinner("Loading data elements from all datasets..."):
                            all_el = fetch_all_data_elements(client)
                    except RuntimeError as e:
                        all_el = pd.DataFrame(columns=["id", "name"])
                        st.session_state["dhis2_all_elements_err"] = str(e)
                    st.session_state["dhis2_all_elements"] = all_el
                if st.session_state.get("dhis2_all_elements_err"):
                    st.warning("Couldn't load data elements from all datasets, so the formula picker only "
                                f"offers this dataset's elements for now: {st.session_state['dhis2_all_elements_err']}")
                    if st.button("↻ Retry loading all data elements"):
                        st.session_state.pop("dhis2_all_elements", None)
                        st.session_state.pop("dhis2_all_elements_err", None)
                        st.rerun()
                render_calc_builder(extra_names=set(all_el["name"]) | set(selected_de_names),
                                     preview_df=st.session_state.get("live_df"), inline=True)
                calc_builder_rendered = True

                # Inputs of saved calculated fields get pulled too, even from other datasets
                name_to_id = dict(zip(all_el["name"], all_el["id"]))
                name_to_id.update(dict(zip(de_df["name"], de_df["id"])))
                id_to_name = {v: k for k, v in name_to_id.items()}
                calc_input_ids, calc_unresolved = [], set()
                if calc_fields is not None and not calc_fields.empty:
                    for _, _cf in calc_fields.iterrows():
                        for _nm in formulas.extract_referenced_elements(_cf["formula"]):
                            if _nm in name_to_id:
                                calc_input_ids.append(name_to_id[_nm])
                            else:
                                calc_unresolved.add(_nm)
                pull_ids = list(dict.fromkeys(list(de_ids) + calc_input_ids))
                helper_ids = [i for i in pull_ids if i not in set(de_ids)]

                # --- 3) Reporting period(s), generated from the dataset's periodType ---
                period_options = generate_periods(period_type, count=24)
                period_labels = [p[0] for p in period_options]
                sel_labels = st.multiselect(
                    "Reporting period(s)", period_labels, default=period_labels[:1],
                    help=f"Periods generated for this dataset's period type: {period_type}. "
                         "Select several to combine them (e.g. three months for a quarter)."
                )
                code_by_label = dict(period_options)
                ordered_labels = [l for l in reversed(period_labels) if l in sel_labels]  # oldest first
                period_codes = [code_by_label[l] for l in ordered_labels]
                combine_mode = "Sum"
                if len(period_codes) > 1:
                    combine_mode = st.radio("Combine the selected periods by", ["Sum", "Average"], horizontal=True,
                                              help="Sum suits counts (cases, visits). Average suits rates or "
                                                   "percentages. Calculated fields are computed AFTER combining.")
                if not period_codes:
                    period_text = ""
                elif len(period_codes) == 1:
                    period_text = period_codes[0]
                elif len(period_codes) <= 3:
                    period_text = " + ".join(period_codes) + f" ({combine_mode.lower()})"
                else:
                    period_text = f"{period_codes[0]} … {period_codes[-1]} ({len(period_codes)} periods, {combine_mode.lower()})"

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

                if helper_ids:
                    st.caption(f"➕ {len(helper_ids)} extra data element(s) will also be pulled because your "
                                "calculated fields use them (hidden from the dashboard by default).")
                if calc_unresolved:
                    st.warning("These calculated-field inputs weren't found in DHIS2, so those fields can't be "
                                "calculated: " + ", ".join(f"`{n}`" for n in sorted(calc_unresolved)))
                pull_clicked = st.button("Pull from DHIS2", type="primary")
                if pull_clicked and not (pull_ids and ou_ids and period_codes):
                    st.warning("Pick at least one data element, one org unit and one reporting period first.")
                if pull_clicked and pull_ids and ou_ids and period_codes:
                    try:
                        with st.spinner("Pulling analytics from DHIS2..."):
                            raw = client.get_analytics(pull_ids, ";".join(ou_ids), ";".join(period_codes))
                    except RuntimeError as e:
                        st.error(str(e))
                        st.info("Common fixes: double-check the org unit UID is correct and that your "
                                  "DHIS2 login has access to it, and confirm analytics tables have been "
                                  "generated for this period on your DHIS2 instance.")
                        st.stop()
                    df_pulled = raw.rename(columns={"value": "actual"})
                    if len(period_codes) > 1:
                        # One row per facility × data element across the selected periods.
                        grp = df_pulled.groupby(["facility", "data_element"], as_index=False)["actual"]
                        df_pulled = grp.sum(min_count=1) if combine_mode == "Sum" else grp.mean()
                        df_pulled["period"] = period_text
                    df_pulled["target"] = pd.NA
                    df_pulled["achievement_pct"] = pd.NA
                    st.session_state["live_df"] = df_pulled
                    st.session_state["period"] = period_text
                    st.session_state["helper_elements"] = [id_to_name[i] for i in helper_ids if i in id_to_name]
                    st.info("Data pulled. Scroll down to \"🎯 Apply your own targets\" to add targets and see "
                              "achievement %.")

                if st.button("↻ Refresh dataset/data element/org unit lists"):
                    for k in list(st.session_state.keys()):
                        if (k == "dhis2_datasets" or k.startswith("dhis2_elements_")
                                or k.startswith("dhis2_orgunits_")
                                or k in ("dhis2_all_elements", "dhis2_all_elements_err")):
                            del st.session_state[k]
                    st.rerun()
    df = st.session_state.get("live_df", get_mock_facility_data(st.session_state.get("period", "2026Q3")))

# ---------------------------------------------------------------------
# Calculated fields — persisted formula definitions, e.g.
# "[DPT3 Immunization] / [ANC 4th visit] * 100", recomputed per facility
# from the raw data loaded and added as extra rows, so filters, targets and
# publishing treat them like any other data element. Skipped for `viewer`
# (their published analysis already contains the calculated rows, targets
# included) and for any field whose raw inputs aren't in the data loaded.
# ---------------------------------------------------------------------
if role != "viewer" and calc_fields is not None and not calc_fields.empty and not df.empty:
    calc_names = set(calc_fields["name"])
    raw_available = set(df["data_element"].unique()) - calc_names
    pivot_actual = df[~df["data_element"].isin(calc_names)].pivot_table(
        index="facility", columns="data_element", values="actual", aggfunc="mean")
    period_val = df["period"].iloc[0] if "period" in df.columns else ""
    new_calc_rows, recomputed = [], set()
    for _, cf in calc_fields.iterrows():
        if not set(formulas.extract_referenced_elements(cf["formula"])) <= raw_available:
            continue  # inputs not present in this dataset — leave any existing rows alone
        recomputed.add(cf["name"])
        for facility_name in pivot_actual.index:
            try:
                value = formulas.evaluate_formula(cf["formula"], raw_available,
                                                  pivot_actual.loc[facility_name].to_dict())
            except formulas.FormulaError:
                value = float("nan")
            new_calc_rows.append({"facility": facility_name, "data_element": cf["name"],
                                  "period": period_val, "actual": value,
                                  "target": pd.NA, "achievement_pct": pd.NA})
    if new_calc_rows:
        df = df[~df["data_element"].isin(recomputed)]
        df = pd.concat([df, pd.DataFrame(new_calc_rows)], ignore_index=True)

# ---------------------------------------------------------------------
# Apply persisted targets — stored in Google Sheets (not just this
# session), so they survive logout/reboot and are shared across whoever's
# working on the analysis. Independent of mock vs. live DHIS2 and of
# re-pulling.
# ---------------------------------------------------------------------
manual_targets = gsheets.get_targets() if (role != "viewer" and gsheets.is_configured()) else None
if manual_targets is not None and not manual_targets.empty:
    df = df.drop(columns=["target", "achievement_pct"], errors="ignore").merge(
        manual_targets[["facility", "data_element", "target"]], on=["facility", "data_element"], how="left"
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
_hidden = set(st.session_state.get("helper_elements", [])) if in_live_branch else set()
e_sel = col2.multiselect(
    "Filter data elements", elements, default=[e for e in elements if e not in _hidden],
    help=("Elements pulled only to feed calculated fields are hidden by default — add them here to see them."
          if _hidden & set(elements) else None))
view = df[df["facility"].isin(f_sel) & df["data_element"].isin(e_sel)]

# --- Calculated fields (demo mode, or before DHIS2 datasets are loaded) -----
# In live DHIS2 mode the builder lives inside the pull settings above, ahead of the
# reporting period. Everywhere else it sits here, right under the data element filters.
if not calc_builder_rendered:
    render_calc_builder(extra_names=set(), preview_df=df)

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
    # Pre-fill from whatever target values are already part of the analysis
    # currently on screen — this works the same whether that's your own
    # applied targets (admin/submitter) or a published analysis (viewer),
    # since `view` already carries the merged target column either way.
    template_df = (view[["facility", "data_element", "target"]]
                    .drop_duplicates()
                    .sort_values(["facility", "data_element"])
                    .reset_index(drop=True))
    template_df["target"] = pd.to_numeric(template_df["target"], errors="coerce")
    filled = int(template_df["target"].notna().sum())
    blank = len(template_df) - filled
    template_df["target"] = template_df["target"].apply(lambda v: "" if pd.isna(v) else v)
    if filled:
        st.caption(f"Pre-filled with the {filled} target(s) already set for this analysis — "
                    f"{blank} row(s) still need one.")
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
            # Saved permanently to Google Sheets, merged with whatever's already
            # there — new values win for matching rows, nothing else is touched.
            # This is what makes targets survive logout/reboot and reappear
            # pre-filled in future template downloads, including for other users.
            try:
                gsheets.save_targets(new_targets, st.session_state["user"]["display_name"])
                st.success(f"Saved targets for {len(new_targets)} row(s) — these are now permanent.")
                st.rerun()
            except RuntimeError as e:
                st.error(f"Couldn't save targets: {e}")
    if c2.button("🗑️ Clear all targets (for everyone)",
                  disabled=(manual_targets is None or manual_targets.empty or not can_act)):
        try:
            gsheets.clear_targets()
            st.warning("All persisted targets cleared.")
            st.rerun()
        except RuntimeError as e:
            st.error(f"Couldn't clear targets: {e}")
    if manual_targets is not None and not manual_targets.empty:
        st.caption(f"Currently saved: targets set for {len(manual_targets)} facility × data element "
                    f"row(s) — persisted in Google Sheets, not just this session.")

# --- Publish: share this analysis with everyone, including viewers -----
if can_act:
    with st.expander("📢 Publish this analysis for everyone to see", expanded=True):
        st.markdown(
            "`viewer` users don't have their own DHIS2 connection or applied targets — "
            "they see whatever was last **published** here. Publishing saves the dataset "
            "currently shown above (including any targets you've applied) to a shared "
            "Google Sheet, replacing whatever was published before."
        )

        # Show what's LIVE right now — the exact thing a viewer sees — so you
        # can confirm a publish worked without needing a separate viewer login,
        # and catch it immediately if this environment's Google Sheet doesn't
        # match what you expect (e.g. local vs. Streamlit Cloud secrets pointing
        # at different sheets).
        st.markdown("**👁️ Currently published (what viewers see right now):**")
        try:
            currently_published = gsheets.get_published_achievement_data()
        except RuntimeError as e:
            currently_published = None
            st.error(f"Couldn't check the published data: {e}")
        if currently_published is None:
            pass
        elif currently_published.empty:
            st.caption("Nothing published yet — viewers currently see demo data.")
        else:
            pub_by = currently_published["published_by"].iloc[0] if "published_by" in currently_published.columns else "?"
            pub_at = currently_published["published_at"].iloc[0] if "published_at" in currently_published.columns else "?"
            st.caption(f"Published by **{pub_by}** at {pub_at} — {len(currently_published)} row(s).")
            st.dataframe(
                currently_published.drop(columns=["_row", "published_by", "published_at"], errors="ignore"),
                hide_index=True, use_container_width=True, height=180,
            )

        st.divider()
        # Preview of exactly what WILL be published if you click the button below —
        # uses `view` (the filtered set you're currently looking at above), not the
        # full unfiltered dataset, so what you see here is exactly what viewers get.
        publish_df = view[["facility", "data_element", "period", "actual", "target", "achievement_pct"]].copy() \
            if "period" in view.columns else view.assign(period=st.session_state.get("period", "2026Q3"))
        st.markdown(f"**📝 About to publish ({len(publish_df)} row(s), matching your current filters above):**")
        st.dataframe(publish_df, hide_index=True, use_container_width=True, height=180)

        if st.button("📢 Publish current analysis", type="primary"):
            try:
                gsheets.publish_achievement_data(publish_df, st.session_state["user"]["display_name"])
                st.success(f"Published {len(publish_df)} row(s)! Viewers will now see this analysis by default.")
                st.rerun()  # refresh the "currently published" preview above immediately
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
                route("DQA", "pages/2_dqa_form.py")
            if a2.button(ACTION_LABELS["SSM"], key=f"ssm_{facility}", use_container_width=True,
                          type=btn_type("SSM"), disabled=not can_act):
                route("SSM", "pages/3_support_supervision_form.py")
            if a3.button(ACTION_LABELS["LV"], key=f"lv_{facility}", use_container_width=True,
                          type=btn_type("LV"), disabled=not can_act):
                route("LV", "pages/4_learning_visit_form.py")

st.caption("🔴 <50%   🟡 50–89%   🟢 ≥90% achievement  ·  highlighted button = system-recommended action "
            "(you can still pick any of the three)")