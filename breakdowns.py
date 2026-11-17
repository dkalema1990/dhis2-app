"""
breakdowns.py
-------------
Sex, age and any other disaggregation of a DHIS2 data element.

In DHIS2 a data element can have a *category combination*: categories (e.g. Sex, Age, Result) each with
options (Male/Female, <15/15+, Positive/Negative). Every combination of one option per category is a
*category option combo* (COC), e.g. "Female, 15+, Positive". The app has so far pulled only each element's
total. A **breakdown series** is a named slice of one data element: the sum of every COC that matches the
options you chose, e.g.

    HTS_TST | Female, 15+     = all COCs of HTS_TST containing "Female" and "15+"
    HTS_TST | <15             = every sex, children only

A series becomes an ordinary row in the dashboard (it can have targets, be published and be used inside
calculated fields like [HTS_TST | Female, 15+] / [HTS_TST]).  Nothing here is specific to sex or age: it
works on whatever categories the data element has.
"""
import json
import pandas as pd

SERIES_COLS = ["name", "data_element", "filters", "visible"]
SEP = " | "


# ---------------------------------------------------------------------
# DHIS2 metadata -> a simple structure
# ---------------------------------------------------------------------
def parse_info(payload: dict) -> dict:
    """DHIS2 /dataElements response -> {data element name: info}.
    info = {"id", "category_combo", "categories": {category: [options]}, "cocs": [{"name", "options": [..]}]}"""
    out = {}
    for de in payload.get("dataElements", []):
        cc = de.get("categoryCombo") or {}
        cats = {}
        for c in cc.get("categories", []) or []:
            if (c.get("name") or "").lower() == "default":
                continue
            cats[c["name"]] = [o["name"] for o in c.get("categoryOptions", [])]
        cocs = [{"name": co["name"], "options": [o["name"] for o in co.get("categoryOptions", [])]}
                for co in cc.get("categoryOptionCombos", []) or []]
        out[de["name"]] = {"id": de["id"], "category_combo": cc.get("name", ""), "categories": cats, "cocs": cocs}
    return out


def has_breakdowns(info: dict) -> bool:
    return bool(info and info.get("categories") and len(info.get("cocs", [])) > 1)


# ---------------------------------------------------------------------
# Matching a choice of options to COCs
# ---------------------------------------------------------------------
def clean_filters(filters: dict) -> dict:
    return {c: list(o) for c, o in (filters or {}).items() if o}


def matching_cocs(info: dict, filters: dict) -> list:
    """COC names that satisfy the filters: for each category with chosen options the COC must carry one of
    them (OR within a category, AND across categories)."""
    f = clean_filters(filters)
    cat_of = {opt: c for c, opts in info.get("categories", {}).items() for opt in opts}
    out = []
    for coc in info.get("cocs", []):
        ok = True
        for cat, wanted in f.items():
            mine = [o for o in coc["options"] if cat_of.get(o) == cat]
            if not (set(mine) & set(wanted)):
                ok = False
                break
        if ok:
            out.append(coc["name"])
    return out


def series_label(info: dict, filters: dict) -> str:
    """'Female, 15+' - the chosen options in category order. A category with several options chosen is shown
    as 'Female/Male'."""
    f = clean_filters(filters)
    parts = []
    for cat in info.get("categories", {}):
        if cat in f:
            parts.append("/".join(f[cat]))
    return ", ".join(parts)


def series_name(data_element: str, label: str) -> str:
    return f"{data_element}{SEP}{label}"


def every_combination(info: dict) -> list:
    """One series definition per COC: [(label, filters)]."""
    cat_of = {opt: c for c, opts in info.get("categories", {}).items() for opt in opts}
    out = []
    for coc in info.get("cocs", []):
        f = {}
        for o in coc["options"]:
            if o in cat_of:
                f.setdefault(cat_of[o], []).append(o)
        out.append((coc["name"], f))
    return out


# ---------------------------------------------------------------------
# Saved definitions (Google Sheet tab "Breakdowns")
# ---------------------------------------------------------------------
def normalize_series(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy() if df is not None else pd.DataFrame()
    for c in SERIES_COLS:
        if c not in out.columns:
            out[c] = ""
    out = out[SERIES_COLS].fillna("").astype(str)
    out["visible"] = out["visible"].str.lower().replace({"": "yes"})
    return out[out["name"].str.strip() != ""].reset_index(drop=True)


def filters_of(row) -> dict:
    try:
        v = json.loads(row["filters"]) if isinstance(row["filters"], str) else row["filters"]
        return clean_filters(v if isinstance(v, dict) else {})
    except ValueError:
        return {}


def active_series(series_df: pd.DataFrame, selected_elements, referenced_names) -> pd.DataFrame:
    """The saved series to pull now: those whose data element is selected for this pull (and shown), plus any
    that a calculated field uses (those are pulled but hidden from the dashboard unless also shown)."""
    s = normalize_series(series_df)
    if s.empty:
        return s
    sel, ref = set(selected_elements), set(referenced_names)
    keep = ((s["visible"] == "yes") & s["data_element"].isin(sel)) | s["name"].isin(ref)
    return s[keep].reset_index(drop=True)


def hidden_series(active: pd.DataFrame, selected_elements, referenced_names) -> list:
    """Series pulled only to feed calculated fields (not visible and not otherwise asked for)."""
    if active.empty:
        return []
    sel = set(selected_elements)
    return [r["name"] for _, r in active.iterrows() if not (r["visible"] == "yes" and r["data_element"] in sel)]


# ---------------------------------------------------------------------
# Computing series values from COC-level analytics
# ---------------------------------------------------------------------
def compute_series(coc_df: pd.DataFrame, series_df: pd.DataFrame, infos: dict) -> pd.DataFrame:
    """coc_df: facility, data_element, coc, period, value (long). Returns facility, data_element (= series
    name), period, value - one row per facility and period, summed over the matching COCs."""
    cols = ["facility", "data_element", "period", "value"]
    if coc_df is None or coc_df.empty or series_df is None or series_df.empty:
        return pd.DataFrame(columns=cols)
    frames = []
    for _, s in series_df.iterrows():
        info = infos.get(s["data_element"])
        if not info:
            continue
        cocs = set(matching_cocs(info, filters_of(s)))
        part = coc_df[(coc_df["data_element"] == s["data_element"]) & (coc_df["coc"].isin(cocs))]
        if part.empty:
            continue
        g = part.groupby(["facility", "period"], as_index=False)["value"].sum(min_count=1)
        g["data_element"] = s["name"]
        frames.append(g[cols])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)


# ---------------------------------------------------------------------
# DHIS2 calls (take the app's DHIS2 client, so dhis2_client.py is untouched)
# ---------------------------------------------------------------------
def fetch_info(client, data_element_ids: list) -> dict:
    """Category structure of the given data elements -> {name: info}."""
    import requests
    out = {}
    ids = list(dict.fromkeys(data_element_ids))
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        params = {"filter": f"id:in:[{','.join(chunk)}]", "paging": "false",
                  "fields": "id,name,categoryCombo[id,name,categories[id,name,categoryOptions[id,name]],"
                            "categoryOptionCombos[id,name,categoryOptions[id,name]]]"}
        r = requests.get(f"{client.base_url}/api/dataElements.json", params=params, auth=client.auth, timeout=60)
        client._raise_detailed(r)
        out.update(parse_info(r.json()))
    return out


def fetch_coc_analytics(client, data_element_ids: list, org_units: str, periods: str) -> pd.DataFrame:
    """Analytics split by category option combo -> facility, data_element, coc, period, value."""
    import requests
    params = {"dimension": [f"dx:{';'.join(data_element_ids)}", f"ou:{org_units}", f"pe:{periods}", "co"],
              "displayProperty": "NAME", "outputIdScheme": "NAME"}
    r = requests.get(f"{client.base_url}/api/analytics.json", params=params, auth=client.auth, timeout=120)
    client._raise_detailed(r)
    payload = r.json()
    names = [h.get("name") for h in payload["headers"]]
    df = pd.DataFrame(payload["rows"], columns=names)
    df = df.rename(columns={"dx": "data_element", "co": "coc", "ou": "facility", "pe": "period"})
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df[["facility", "data_element", "coc", "period", "value"]]
