import time

import streamlit as st
import pandas as pd
import requests
from streamlit.errors import StreamlitAPIException
import plotly.express as px
import plotly.graph_objects as go
import auth
import gsheets
import formulas
import kobo
import breakdowns as bd
import json as _json
try:
    import visit_tools as _vt
except Exception:          # the visit tools module is optional for the dashboard
    _vt = None
from dhis2_client import get_mock_facility_data, generate_periods


def _kobo_visit_dialog(facility, data_element, period, visit_ref):
    """The visit steps done in Kobo, in order: visit tools -> competency -> support supervision form.
    Kobo opens in a new tab and the app can't tell when you've submitted, so each next step unlocks once
    you confirm the previous one is finished. One visit reference goes into every form."""
    steps = kobo.visit_steps(facility, data_element, period, visit_ref,
                             _vt.previous_actions_note(facility, visit_ref) if _vt else "")
    st.markdown(f"**{facility}** — do these in order  ·  visit reference `{visit_ref}`")
    unlocked = True
    for i, s in enumerate(steps, 1):
        st.markdown(f"**Step {i} · {s['title']}**")
        st.link_button("📲 Open in Kobo ↗", s["url"], use_container_width=True,
                        type="primary" if i == len(steps) else "secondary", disabled=not unlocked)
        st.caption(s["note"])
        if i < len(steps):
            ticked = st.checkbox("I've finished this step (or it wasn't needed this time)",
                                  key=f"kv_done_{facility}_{s['key']}", disabled=not unlocked)
            unlocked = unlocked and ticked


def _new_visit_ref(facility: str) -> str:
    """e.g. Kawempe-20261014-0930 — ties together everything recorded during one visit."""
    return "".join(ch for ch in str(facility) if ch.isalnum())[:14] + time.strftime("-%Y%m%d-%H%M")


# st.dialog needs Streamlit >= 1.37; without it the Kobo button falls back to opening the form directly.
kobo_visit_dialog = (st.dialog("Support supervision visit — Kobo")(_kobo_visit_dialog)
                     if hasattr(st, "dialog") else None)

auth.require_login()
auth.sidebar_user_badge()
role = st.session_state["user"]["role"]
can_act = role in ("submitter", "admin")
is_admin = role == "admin"          # only admins can delete shared data (targets, calculated fields)

st.title("🏥 Facility Achievement Dashboard")

# Data elements picked in the DHIS2 pull settings (available to the formula builder
# even before you click Pull). Stays empty in demo/viewer mode.
selected_de_names = []
# DHIS2 period types, finest -> coarsest (used to pick a sensible default when datasets differ)
PERIOD_TYPE_ORDER = ["Daily", "Weekly", "WeeklyWednesday", "WeeklyThursday", "WeeklySaturday", "WeeklySunday",
                     "BiWeekly", "Monthly", "BiMonthly", "Quarterly", "SixMonthly", "SixMonthlyApril",
                     "SixMonthlyNov", "Yearly", "FinancialApril", "FinancialJuly", "FinancialOct", "FinancialNov"]
in_live_branch = False         # True once the DHIS2 pull settings are showing
calc_builder_rendered = False  # builder is drawn in the pull settings (live) OR under the filters

# Calculated-field definitions (shared, persisted). Loaded up here so the formula builder
# can sit inside the DHIS2 pull settings, before the reporting period.
calc_fields = gsheets.get_calculated_fields() if gsheets.is_configured() else pd.DataFrame()
# Breakdown series (sex / age / other categories of a data element) - shared and persisted like calculated fields
bd_saved = bd.normalize_series(gsheets.get_breakdowns() if gsheets.is_configured() else pd.DataFrame())


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


def render_breakdown_builder(client, element_names, name_to_id):
    """Pick a data element, then slice it by its own categories (sex, age, result ... whatever DHIS2 defines)."""
    box = st.container(border=True)
    with box:
        st.markdown("**🧩 Breakdowns** — sex, age and other disaggregations of a data element")
        _m = st.session_state.pop("bd_msg", None)
        if _m:
            (st.success if _m[0] == "success" else st.error)(_m[1])
        if not bd_saved.empty:
            st.caption("Saved breakdowns (pulled with the data element and usable in calculated fields):")
            for _, s in bd_saved.iterrows():
                c1, c2 = st.columns([6, 1])
                c1.markdown(f"**{s['name']}**" + ("" if s["visible"] == "yes" else "  _(hidden — only feeds calculated fields)_"))
                if can_act and c2.button("🗑️", key=f"bd_del_{s['name']}", help="Remove this breakdown"):
                    try:
                        gsheets.delete_breakdowns([s["name"]])
                        st.rerun()
                    except RuntimeError as e:
                        st.error(f"Couldn't remove: {e}")
        if not can_act:
            st.info("Your role (`viewer`) can see breakdowns but not add or remove them.")
            return
        if not element_names:
            st.info("Pick data elements first.")
            return
        de = st.selectbox("Data element to break down", element_names, key="bd_de")
        cache = st.session_state.setdefault("bd_info", {})
        if de not in cache:
            if st.button("🔍 Load this element's breakdowns from DHIS2", key="bd_load"):
                try:
                    with st.spinner("Reading the categories of this data element..."):
                        cache.update(bd.fetch_info(client, [name_to_id[de]]))
                        cache.setdefault(de, {"categories": {}, "cocs": []})
                    st.rerun()
                except (RuntimeError, KeyError) as e:
                    st.error(f"Couldn't read the breakdowns: {e}")
            return
        info = cache[de]
        if not bd.has_breakdowns(info):
            st.info("This data element has no sex, age or other breakdown in DHIS2 — only its total exists.")
            return
        st.caption(f"Category combination: **{info['category_combo']}** · {len(info['cocs'])} combinations")
        chosen = {}
        for cat, opts in info["categories"].items():
            chosen[cat] = st.multiselect(f"{cat}", opts, key=f"bd_{de}_{cat}",
                                         help="Pick one or several options - several are added together into one row. "
                                              "Leave empty to include every option of this category.")
        flt = bd.clean_filters(chosen)
        cats = list(info["categories"])
        split_cat = st.selectbox("Rows to make", ["One row - add the chosen options together"] + [f"A separate row for each {c} option" for c in cats],
                                 key=f"bd_split_{de}",
                                 help="For example choose 15+ under Age and 'a separate row for each Sex option' to get "
                                      "one row for males and one for females.")
        split = None if split_cat.startswith("One row") else cats[[f"A separate row for each {c} option" for c in cats].index(split_cat)]
        groups = bd.split_groups(info, flt, split)
        show = st.checkbox("Show on the dashboard (untick to use only inside calculated fields)", value=True, key="bd_show")
        if not groups:
            st.caption("Choose at least one option to make a slice, e.g. Female and 15+. "
                       "Or add one row for every combination below.")
        else:
            st.caption("Rows to be added: " + "; ".join(f"**{bd.series_name(de, lab)}**" for lab, _ in groups[:6])
                       + (f" … ({len(groups)} rows)" if len(groups) > 6 else ""))
        b1, b2 = st.columns(2)
        if b1.button("➕ Add this breakdown", type="primary", key="bd_add", disabled=not groups):
            try:
                gsheets.save_breakdowns([{"name": bd.series_name(de, lab), "data_element": de, "filters": _json.dumps(g),
                                          "visible": "yes" if show else "no"} for lab, g in groups],
                                        st.session_state["user"]["display_name"])
                st.session_state["bd_msg"] = ("success", f"Added {len(groups)} breakdown row(s). Pull again to see them.")
                st.rerun()
            except RuntimeError as e:
                st.error(f"Couldn't save: {e}")
        combos = bd.every_combination(info)
        if b2.button(f"➕ One row for every combination ({len(combos)})", key="bd_all"):
            if len(combos) > 60:
                st.error("More than 60 combinations - pick options above instead.")
            else:
                try:
                    gsheets.save_breakdowns(
                        [{"name": bd.series_name(de, lab), "data_element": de, "filters": _json.dumps(f),
                          "visible": "yes" if show else "no"} for lab, f in combos],
                        st.session_state["user"]["display_name"])
                    st.session_state["bd_msg"] = ("success", f"Added {len(combos)} breakdown rows for {de}. Pull again to see them.")
                    st.rerun()
                except RuntimeError as e:
                    st.error(f"Couldn't save: {e}")

        # --- apply the same choice to a calculated field ---------------------------------------------
        if calc_fields is not None and not calc_fields.empty:
            st.markdown("**Apply this breakdown to a calculated field**")
            st.caption("Uses the options chosen above. Every data element in the formula is switched to the same slice, "
                       "so a coverage or positivity formula is calculated for each age group, sex and so on. "
                       "Elements without that breakdown stay as totals.")
            cf_name = st.selectbox("Calculated field", calc_fields["name"].tolist(),
                                   index=None, key="bd_calc_pick", placeholder="Choose a calculated field…")
            if cf_name:
                cf_formula = calc_fields.loc[calc_fields["name"] == cf_name, "formula"].iloc[0]
                st.caption(f"Formula: `{cf_formula}`")
                if st.button("🧮 Create the calculated field(s) for this breakdown", key="bd_calc_go",
                             disabled=not groups):
                    try:
                        _els = [e for e in formulas.extract_referenced_elements(cf_formula) if bd.SEP not in e]
                        _need = [name_to_id[e] for e in _els if e in name_to_id and e not in cache]
                        if _need:
                            with st.spinner("Reading the breakdowns of the formula's data elements..."):
                                cache.update(bd.fetch_info(client, _need))
                        _res = bd.apply_to_formula(cf_formula, groups, {e: cache.get(e) for e in _els})
                        _series = {s["name"]: s for r in _res for s in r["series"]}
                        if _series:
                            gsheets.save_breakdowns(list(_series.values()), st.session_state["user"]["display_name"])
                        for r in _res:
                            gsheets.save_calculated_field(f"{cf_name}{bd.SEP}{r['label']}", r["formula"],
                                                          st.session_state["user"]["display_name"])
                        _notes = sorted({n for r in _res for n in r["notes"]})
                        st.session_state["bd_msg"] = ("success", f"Created {len(_res)} calculated field(s): "
                                                      + ", ".join(f"{cf_name}{bd.SEP}{r['label']}" for r in _res[:4])
                                                      + (" …" if len(_res) > 4 else "") + ". Pull again to calculate them."
                                                      + ("  Note: " + "; ".join(_notes) if _notes else ""))
                        st.rerun()
                    except (RuntimeError, KeyError) as e:
                        st.error(f"Couldn't create them: {e}")


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
                if is_admin and fc2.button("🗑️", key=f"del_calc_{cf['name']}", help="Delete this calculated field"):
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
                sel_ds_names = st.multiselect(
                    "Dataset(s)", ds_names, default=ds_names[:1],
                    help="Pick one or more datasets — you can combine data elements from several of them.")
                sel_ds = ds_df[ds_df["name"].isin(sel_ds_names)]
                if sel_ds.empty:
                    st.info("Select at least one dataset to list its data elements.")

                # --- 2) Data elements from ALL the selected datasets ----------------------
                _frames, sources = [], {}
                for _, _ds in sel_ds.iterrows():
                    _key = f"dhis2_elements_{_ds['id']}"
                    if _key not in st.session_state:
                        try:
                            with st.spinner(f"Fetching data elements for {_ds['name']}..."):
                                st.session_state[_key] = client.get_dataset_elements(_ds["id"])
                        except RuntimeError as e:
                            st.error(f"Couldn't load data elements for {_ds['name']}: {e}")
                            st.stop()
                    _els = st.session_state[_key]
                    _frames.append(_els)
                    for _n in _els["name"]:
                        sources.setdefault(_n, []).append(_ds["name"])
                de_df = (pd.concat(_frames, ignore_index=True).drop_duplicates(subset="id")
                         if _frames else pd.DataFrame(columns=["id", "name"]))
                multi_ds = len(sel_ds) > 1
                de_names = st.multiselect(
                    "Data elements", de_df["name"].tolist(), default=de_df["name"].tolist()[:5],
                    format_func=(lambda n: f"{n}  ·  {', '.join(sources.get(n, []))}") if multi_ds else str,
                    help=("Showing the data elements of every selected dataset — the dataset each one "
                          "belongs to is shown after the name." if multi_ds else None))
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
                _bd_name_to_id = dict(zip(all_el["name"], all_el["id"]))
                _bd_name_to_id.update(dict(zip(de_df["name"], de_df["id"])))
                render_breakdown_builder(client, list(dict.fromkeys(selected_de_names)), _bd_name_to_id)
                render_calc_builder(extra_names=set(all_el["name"]) | set(selected_de_names) | set(bd_saved["name"]),
                                     preview_df=st.session_state.get("live_df"), inline=True)
                calc_builder_rendered = True

                # Inputs of saved calculated fields get pulled too, even from other datasets
                name_to_id = dict(zip(all_el["name"], all_el["id"]))
                name_to_id.update(dict(zip(de_df["name"], de_df["id"])))
                id_to_name = {v: k for k, v in name_to_id.items()}
                calc_input_ids, calc_unresolved, calc_series_refs = [], set(), set()
                _series_names = set(bd_saved["name"])
                _series_de = dict(zip(bd_saved["name"], bd_saved["data_element"]))
                if calc_fields is not None and not calc_fields.empty:
                    for _, _cf in calc_fields.iterrows():
                        for _nm in formulas.extract_referenced_elements(_cf["formula"]):
                            if _nm in _series_names:
                                calc_series_refs.add(_nm)          # pulled by the breakdown step below
                            elif _nm in name_to_id:
                                calc_input_ids.append(name_to_id[_nm])
                            else:
                                calc_unresolved.add(_nm)
                pull_ids = list(dict.fromkeys(list(de_ids) + calc_input_ids))
                helper_ids = [i for i in pull_ids if i not in set(de_ids)]

                # --- 3) Reporting period(s), generated from the dataset's periodType ---
                types_present = (list(dict.fromkeys(sel_ds["periodType"].dropna().tolist()))
                                  if "periodType" in sel_ds.columns else [])
                if not types_present:
                    types_present = ["Monthly"]
                _rank = lambda t: PERIOD_TYPE_ORDER.index(t) if t in PERIOD_TYPE_ORDER else PERIOD_TYPE_ORDER.index("Monthly")
                types_sorted = sorted(types_present, key=_rank)
                period_type = types_sorted[-1]  # coarsest by default
                if len(types_sorted) > 1:
                    period_type = st.selectbox(
                        "Period type for the reporting periods", types_sorted, index=len(types_sorted) - 1,
                        help="Your datasets report at different frequencies. DHIS2 can roll monthly data up "
                             "into quarterly or yearly periods, but not the other way round, so the coarsest "
                             "type is selected by default.")
                period_options = generate_periods(period_type, count=24)
                period_labels = [p[0] for p in period_options]
                sel_labels = st.multiselect(
                    "Reporting period(s)", period_labels, default=period_labels[:1],
                    help=f"Periods generated for the {period_type} period type. "
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
                ou_levels = st.multiselect("Org unit levels (1=country; facilities are often level 4, 5, 6 or 7 - pick all that apply)",
                                           list(range(1, 9)), default=[4], key="ou_levels")
                ou_levels = sorted(ou_levels)
                ou_cache_keys = {lv: f"dhis2_orgunits_{lv}" for lv in ou_levels}
                try:
                    _saved_fac = gsheets.get_my_facilities() if gsheets.is_configured() else pd.DataFrame()
                except RuntimeError:
                    _saved_fac = pd.DataFrame()
                _missing_lv = [lv for lv, k in ou_cache_keys.items() if k not in st.session_state]
                _from_dhis2 = bool(ou_levels) and not _missing_lv
                _use_saved = (not _from_dhis2) and not _saved_fac.empty
                if _use_saved:
                    ou_df = pd.DataFrame({"id": _saved_fac["org_unit_id"].astype(str), "name": _saved_fac["name"].astype(str),
                                          "parent": [({"name": p} if str(p).strip() else None) for p in _saved_fac["parent"]],
                                          "level": _saved_fac["level"].astype(str)})
                    st.caption(f"📌 Using your saved list of {len(ou_df)} facilities - no DHIS2 call needed. "
                               "Load from DHIS2 below only to add or change facilities.")
                elif _from_dhis2:
                    ou_df = pd.concat([st.session_state[ou_cache_keys[lv]].assign(level=str(lv)) for lv in ou_levels], ignore_index=True).drop_duplicates("id")
                else:
                    ou_df = None
                if not ou_levels:
                    st.caption("Pick at least one org unit level.")
                elif not _from_dhis2:
                    if st.button("🔄 Load org units at these levels from DHIS2" if _use_saved else "🔄 Load org units at these levels"):
                        try:
                            with st.spinner("Fetching org units..."):
                                for lv in _missing_lv:
                                    st.session_state[ou_cache_keys[lv]] = client.get_org_units(level=int(lv))
                            st.rerun()
                        except RuntimeError as e:
                            st.error(f"Couldn't load org units: {e}")
                if ou_df is not None:
                    if ou_df.empty:
                        st.warning("No org units returned at these levels — check the level numbers and your "
                                    "DHIS2 permissions.")
                        ou_ids = []
                    else:
                        ou_df = ou_df.copy()
                        ou_df["display"] = ou_df.apply(
                            lambda r: f"{r['name']} ({r['parent']['name']})" if isinstance(r.get("parent"), dict)
                            else r["name"], axis=1)
                        ou_display_sel = st.multiselect(
                            "Org units (select one or more)", ou_df["display"].tolist(),
                            default=ou_df["display"].tolist() if _use_saved else ou_df["display"].tolist()[:10],
                            key=f"ou_sel_{'saved' if _use_saved else '-'.join(map(str, ou_levels))}"
                        )
                        ou_ids = ou_df[ou_df["display"].isin(ou_display_sel)]["id"].tolist()
                        sv1, sv2 = st.columns(2)
                        if sv1.button("💾 Save the selected facilities as my list", disabled=not (can_act and ou_ids),
                                      key="save_my_fac", help="Next time the list loads from here instead of DHIS2."):
                            _pick = ou_df[ou_df["id"].isin(ou_ids)]
                            try:
                                gsheets.save_my_facilities(pd.DataFrame({
                                    "org_unit_id": _pick["id"], "name": _pick["name"],
                                    "parent": [p["name"] if isinstance(p, dict) else "" for p in _pick["parent"]],
                                    "level": _pick["level"]}), st.session_state["user"]["display_name"])
                                st.success(f"Saved {len(_pick)} facilities. They will load from your list from now on.")
                            except RuntimeError as e:
                                st.error(f"Couldn't save the list: {e}")
                        if _use_saved and sv2.button("🗑️ Forget my saved list", disabled=not can_act, key="forget_my_fac"):
                            try:
                                gsheets.save_my_facilities(pd.DataFrame(columns=["org_unit_id", "name", "parent", "level"]),
                                                           st.session_state["user"]["display_name"])
                                st.rerun()
                            except RuntimeError as e:
                                st.error(f"Couldn't clear the list: {e}")
                else:
                    ou_ids = []
                    st.caption("Pick the levels above, then click \"Load org units at these levels\" to choose facilities.")

                if helper_ids:
                    st.caption(f"➕ {len(helper_ids)} extra data element(s) will also be pulled because your "
                                "calculated fields use them (hidden from the dashboard by default).")
                if calc_unresolved:
                    st.warning("These calculated-field inputs weren't found in DHIS2, so those fields can't be "
                                "calculated: " + ", ".join(f"`{n}`" for n in sorted(calc_unresolved)))
                include_bd = True
                if not bd_saved.empty:
                    include_bd = st.checkbox("Include my saved breakdowns (sex, age, other) in this pull", value=True,
                                             key="bd_include",
                                             help="Untick to pull only the totals of the selected data elements. "
                                                  "Calculated fields built on breakdowns can't be calculated without them.")
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
                    # --- breakdown series (sex / age / other categories) ---------------------------------
                    _active = (bd.active_series(bd_saved, selected_de_names, calc_series_refs) if include_bd
                               else bd.normalize_series(pd.DataFrame()))
                    _hidden_series = bd.hidden_series(_active, selected_de_names, calc_series_refs)
                    if not _active.empty:
                        try:
                            with st.spinner("Pulling the breakdowns..."):
                                _infos = bd.fetch_info(client, [name_to_id[d] for d in _active["data_element"].unique()
                                                                if d in name_to_id])
                                _coc = bd.fetch_coc_analytics(client, [_infos[d]["id"] for d in _active["data_element"].unique()
                                                                       if d in _infos], ";".join(ou_ids), ";".join(period_codes))
                            _sr = bd.compute_series(_coc, _active, _infos)
                            if not _sr.empty:
                                raw = pd.concat([raw, _sr], ignore_index=True)
                            _missing_s = sorted(set(_active["name"]) - set(_sr["data_element"]))
                            if _missing_s:
                                st.warning("No data came back for these breakdowns: " + ", ".join(f"`{m}`" for m in _missing_s))
                        except (RuntimeError, KeyError) as e:
                            st.error(f"Couldn't pull the breakdowns: {e}")
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
                    st.session_state["helper_elements"] = ([id_to_name[i] for i in helper_ids if i in id_to_name]
                                                           + list(_hidden_series))
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
ALL_FAC = "*"          # a Targets row with facility "*" is the default for every facility without its own target
manual_targets = gsheets.get_targets() if (role != "viewer" and gsheets.is_configured()) else None
if manual_targets is not None and not manual_targets.empty:
    _specific = manual_targets[manual_targets["facility"].astype(str) != ALL_FAC]
    _default = (manual_targets[manual_targets["facility"].astype(str) == ALL_FAC]
                .drop_duplicates("data_element", keep="last").set_index("data_element")["target"])
    df = df.drop(columns=["target", "achievement_pct"], errors="ignore").merge(
        _specific[["facility", "data_element", "target"]], on=["facility", "data_element"], how="left"
    )
    df["target"] = pd.to_numeric(df["target"], errors="coerce")
    df["target_is_default"] = df["target"].isna() & df["data_element"].isin(_default.index)
    df["target"] = df["target"].fillna(df["data_element"].map(_default))
    df["achievement_pct"] = (df["actual"] / df["target"] * 100).round(1)
else:
    _default = pd.Series(dtype=float)
    _specific = pd.DataFrame(columns=["facility", "data_element", "target"])

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
        "Excel/Sheets, at your own pace (leave a row blank to use the all-facilities target). **Step 3.** Upload it below and click Apply — "
        "achievement % updates immediately."
    )
    st.markdown("##### Same target for every facility")
    st.caption("Set a target once per indicator (including calculated ones). It applies to every facility - "
               "including any you add later - unless a facility has its own target in the template below, "
               "which always wins.")
    _els = sorted(df["data_element"].unique())
    _hid = set(st.session_state.get("helper_elements", []))
    _els = [e for e in _els if e not in _hid]
    _def_df = pd.DataFrame({"data_element": _els,
                            "target": [float(_default.get(e)) if e in _default.index and pd.notna(_default.get(e)) else None for e in _els]})
    _edited = st.data_editor(_def_df, hide_index=True, use_container_width=True, key="default_targets_editor",
                             disabled=["data_element"] if can_act else True,
                             column_config={"data_element": "Indicator", "target": st.column_config.NumberColumn("Target (all facilities)")})
    if st.button("💾 Save targets for all facilities", disabled=not can_act, key="save_default_targets"):
        _rows = _edited.dropna(subset=["target"])
        if _rows.empty:
            st.warning("Enter at least one target first.")
        else:
            try:
                gsheets.save_targets(pd.DataFrame({"facility": ALL_FAC, "data_element": _rows["data_element"], "target": _rows["target"]}),
                                     st.session_state["user"]["display_name"])
                st.success(f"Saved {len(_rows)} target(s) for all facilities.")
                st.rerun()
            except RuntimeError as e:
                st.error(f"Couldn't save targets: {e}")
    st.markdown("##### Different target for some facilities")
    st.caption("Pick **by indicator** (see every facility for one indicator) or **by facility** (see every indicator for one "
               "facility). *Own target* is saved only when it differs from the all-facilities target; tick "
               "**Use common target** (or clear the cell) to go back to it.")
    _fp_now = gsheets.targets_fingerprint(manual_targets) if manual_targets is not None else ""
    _ver = st.session_state.get("tg_ver", 0)
    _mode = st.radio("View", ["By indicator", "By facility"], horizontal=True, key="tg_mode")
    _facs_all = sorted(view["facility"].unique())
    _els_all = [e for e in sorted(view["data_element"].unique()) if e not in _hid]
    _own = {(str(f), str(e)): t for f, e, t in zip(_specific["facility"], _specific["data_element"], _specific["target"]) if pd.notna(t)}
    _com = {e: float(_default[e]) for e in _default.index if pd.notna(_default[e])}
    if _mode == "By indicator" and _els_all:
        _pick = st.selectbox("Indicator", _els_all, key="tg_pick_el")
        _grid = pd.DataFrame({"Facility": _facs_all}); _grid["_f"] = _grid["Facility"]; _grid["_e"] = _pick
    elif _mode == "By facility" and _facs_all:
        _pick = st.selectbox("Facility", _facs_all, key="tg_pick_fac")
        _grid = pd.DataFrame({"Indicator": _els_all}); _grid["_e"] = _grid["Indicator"]; _grid["_f"] = _pick
    else:
        _grid = None
    if _grid is not None and not _grid.empty:
        _fpk = f"tg_fp_{_mode}_{_pick}_{_ver}"
        if _fpk not in st.session_state:                      # remembered from when this table was first shown
            st.session_state[_fpk] = _fp_now
        _grid["Common target"] = [_com.get(e) for e in _grid["_e"]]
        _grid["Own target"] = [_own.get((f, e)) for f, e in zip(_grid["_f"], _grid["_e"])]
        _grid["Use common target"] = _grid["Own target"].isna()
        _lab = "Facility" if _mode == "By indicator" else "Indicator"
        _show_cols = [_lab, "Common target", "Own target", "Use common target"]
        _ed = st.data_editor(_grid[_show_cols], hide_index=True, use_container_width=True,
                             key=f"tg_grid_{_mode}_{_pick}_{_ver}",
                             disabled=[_lab, "Common target"] if can_act else True,
                             column_config={"Common target": st.column_config.NumberColumn(format="%g"),
                                            "Own target": st.column_config.NumberColumn(format="%g"),
                                            "Use common target": st.column_config.CheckboxColumn()})
        if st.button("💾 Save these targets", disabled=not can_act, key="tg_grid_save"):
            if gsheets.targets_fingerprint() != st.session_state[_fpk]:
                st.warning("Someone saved targets while this table was open. Reload the page (press R) and redo your changes "
                           "so nothing is overwritten.")
            else:
                _ups, _dels = [], []
                for i, r in _ed.reset_index(drop=True).iterrows():
                    f, e = _grid.at[i, "_f"], _grid.at[i, "_e"]
                    own_new = r["Own target"]
                    use_common = bool(r["Use common target"]) or pd.isna(own_new)
                    c = _com.get(e)
                    if use_common or (c is not None and float(own_new) == c):
                        if (f, e) in _own:
                            _dels.append((f, e))
                    elif (f, e) not in _own or float(_own[(f, e)]) != float(own_new):
                        _ups.append({"facility": f, "data_element": e, "target": float(own_new)})
                if not _ups and not _dels:
                    st.info("No changes to save.")
                else:
                    try:
                        gsheets.apply_target_changes(pd.DataFrame(_ups, columns=["facility", "data_element", "target"]), _dels,
                                                     st.session_state["user"]["display_name"])
                        st.session_state["tg_ver"] = _ver + 1
                        st.success(f"Saved {len(_ups)} own target(s); {len(_dels)} went back to the common target.")
                        st.rerun()
                    except RuntimeError as ex:
                        st.error(f"Couldn't save targets: {ex}")
    st.markdown("##### Or fill many at once with a template")
    # Pre-fill from whatever target values are already part of the analysis
    # currently on screen — this works the same whether that's your own
    # applied targets (admin/submitter) or a published analysis (viewer),
    # since `view` already carries the merged target column either way.
    _tv = view.copy()
    if "target_is_default" in _tv.columns:                 # defaults aren't copied into the template
        _tv.loc[_tv["target_is_default"].fillna(False).astype(bool), "target"] = pd.NA
    template_df = (_tv[["facility", "data_element", "target"]]
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
                  disabled=(manual_targets is None or manual_targets.empty or not is_admin),
                  help=None if is_admin else "Only an admin can clear targets."):
        try:
            gsheets.clear_targets()
            st.warning("All persisted targets cleared.")
            st.rerun()
        except RuntimeError as e:
            st.error(f"Couldn't clear targets: {e}")
    if manual_targets is not None and not manual_targets.empty:
        st.caption(f"Currently saved: {len(manual_targets)} target row(s) (a facility of * means all facilities) "
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
            # Every activity always has its in-app button. If a Kobo web-form link is configured for
            # it, a second button below opens that Kobo form (facility / data element / period pre-filled).
            _kobo = {a: kobo.prefilled_link(a, facility, de_for_action, st.session_state.get("period"))
                     for a in ("DQA", "SSM", "LV")}
            _step_keys = kobo.visit_step_keys()                         # Kobo visit steps that are linked
            for _col, _act, _page in ((a1, "DQA", "pages/2_dqa_form.py"),
                                       (a2, "SSM", "pages/3_support_supervision_form.py"),
                                       (a3, "LV", "pages/4_learning_visit_form.py")):
                if _col.button(ACTION_LABELS[_act], key=f"{_act.lower()}_{facility}", use_container_width=True,
                                type=btn_type(_act), disabled=not can_act,
                                help=("Takes you through the visit steps in order, ending with the support supervision form."
                                      if _act == "SSM" else None)):
                    if _act == "SSM":
                        # Competency assessment first (step 1), then the support supervision form (step 2).
                        st.session_state["visit_flow"] = {"facility": facility, "data_element": de_for_action,
                                                          "next": "SSM", "assessed": 0, "findings": "",
                                                          "pcts": [], "started": time.time(),
                                                          "visit_ref": _new_visit_ref(facility),
                                                          # the visit tools (Kobo) come first when that form is linked
                                                          "stage": "tools" if "TOOLS" in _step_keys else "competency"}
                        for _k in ("ssm_prefill_findings", "ssm_prefill_score", "ssm_prefill_score_n",
                                   "ssm_prefill_visit_ref"):
                            st.session_state.pop(_k, None)
                        try:
                            route("SSM", "pages/6_competency.py")
                        except StreamlitAPIException:        # competency page isn't in the sidebar yet
                            st.session_state.pop("visit_flow", None)
                            route("SSM", _page)
                    else:
                        route(_act, _page)
                if _act == "SSM" and "TOOLS" in _step_keys:
                    # the all-in-one visit form: tools -> (ask) competency -> (ask) support supervision report
                    _vref = _new_visit_ref(facility)
                    _visit_url = kobo.visit_steps(facility, de_for_action, st.session_state.get("period"), _vref,
                                                  _vt.previous_actions_note(facility, _vref) if _vt else "")[0]["url"]
                    _col.link_button("📲 Open Kobo visit form ↗", _visit_url, use_container_width=True,
                                      type=btn_type(_act), disabled=not can_act,
                                      help="One Kobo form for the whole visit: the visit tools, then it asks whether to "
                                           "continue to the competency assessment and the support supervision report.")
                elif _kobo[_act]:
                    if _act == "SSM" and len(_step_keys) >= 2 and kobo_visit_dialog is not None:
                        if _col.button("📲 Open in Kobo (visit steps)", key=f"kobo_ssm_{facility}",
                                        use_container_width=True, type=btn_type(_act), disabled=not can_act,
                                        help="Opens a step-by-step guide through the Kobo forms of the visit, "
                                             "in order, ending with the support supervision form."):
                            kobo_visit_dialog(facility, de_for_action, st.session_state.get("period"),
                                              _new_visit_ref(facility))
                    else:
                        _col.link_button("📲 Open Kobo form ↗", _kobo[_act], use_container_width=True,
                                          type=btn_type(_act), disabled=not can_act,
                                          help="Opens the Kobo form with this facility and data element filled in.")

st.caption("🔴 <50%   🟡 50–89%   🟢 ≥90% achievement  ·  highlighted button = system-recommended action "
            "(you can still pick any of the three)")