"""
reports.py
----------
Reports built from what is already in the Sheet. No new data entry: everything here reads the visit,
competency, action and published-performance tabs and turns them into

  1. facility_visit()       one facility, one visit      -> Word report (docx_facility_report)
  2. performance_summary()  all facilities               -> Excel
  3. competency_report()    health workers x topics      -> Excel
  4. action_tracker()       open / overdue actions       -> Excel

The functions take plain DataFrames (see `load`) so they can be tested without Streamlit.
"""
import io
from datetime import date

import pandas as pd

import competency as comp
import mentorship
import visit_tools as vt

DONE = "done"
RATING_COLOURS = {"Green": "C6EFCE", "Amber": "FFEB9C", "Red": "FFC7CE"}
NOT_REVIEWED = "Not yet reviewed"


# ---------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------
def load(gsheets) -> dict:
    """Every table the reports use (each read is cached by gsheets.get_all for 30 s)."""
    g = gsheets.get_table
    d = {k: g(t) for k, t in [("tools", "VisitTools"), ("att", "VisitAttendance"), ("rca", "VisitRCA"),
                              ("actions", "VisitActions"), ("ipi", "VisitIPI"), ("routine", "VisitRoutine"),
                              ("followup", "VisitFollowUp"), ("systems", "VisitSystems"),
                              ("indicators", "VisitIndicators"), ("session", "VisitSession"),
                              ("status", "ActionStatus"), ("ssm", "SSM")]}
    d["ach"] = gsheets.get_published_achievement_data()
    d["comp"] = comp.get_assessments()
    return d


def _df(x) -> pd.DataFrame:
    return x if isinstance(x, pd.DataFrame) else pd.DataFrame()


def _key(s) -> str:
    return str(s).strip().lower()


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _fmt_pct(v) -> str:
    try:
        return "" if v is None or v != v else f"{float(v):.0f}%"
    except (TypeError, ValueError):
        return ""


def _for_visit(df, ref: str) -> pd.DataFrame:
    df = _df(df)
    if df.empty or "visit_ref" not in df.columns:
        return pd.DataFrame(columns=df.columns)
    return df[df["visit_ref"].astype(str) == str(ref)].reset_index(drop=True)


# ---------------------------------------------------------------------
# Actions (shared by the facility report and the tracker)
# ---------------------------------------------------------------------
def plans(d: dict, today: date | None = None) -> pd.DataFrame:
    """Every action of every visit with status, due date, and an overdue flag."""
    p = vt.previous_plans(_df(d.get("actions")), _df(d.get("status")), _df(d.get("followup")))
    if p.empty:
        return p.assign(due=pd.NaT, open=False, overdue=False, days_overdue=0)
    today = pd.Timestamp(today or date.today())
    p = p.copy()
    p["due"] = pd.to_datetime(p["timeline"], errors="coerce")
    p["open"] = p["status"].astype(str).str.strip().str.lower() != DONE
    p["overdue"] = p["open"] & p["due"].notna() & (p["due"] < today)
    p["days_overdue"] = ((today - p["due"]).dt.days).where(p["overdue"], 0).fillna(0).astype(int)
    return p


def action_tracker(d: dict, today: date | None = None) -> dict:
    """-> {'actions': detail, 'by_facility': summary, 'by_owner': summary, 'totals': dict}"""
    p = plans(d, today)
    cols = ["Facility", "Visit date", "Challenge", "Action", "Owner", "Due", "Status", "Overdue by (days)", "Comment"]
    if p.empty:
        empty = pd.DataFrame(columns=cols)
        return {"actions": empty, "by_facility": pd.DataFrame(columns=["Facility", "Actions", "Done", "Open", "Overdue", "% done"]),
                "by_owner": pd.DataFrame(columns=["Owner", "Actions", "Done", "Open", "Overdue"]),
                "totals": {"total": 0, "done": 0, "open": 0, "overdue": 0, "pct_done": None}}
    detail = pd.DataFrame({"Facility": p["facility"], "Visit date": p["visit_date"], "Challenge": p["challenge"],
                           "Action": p["corrective_action"], "Owner": p["responsible_person"],
                           "Due": p["due"].dt.strftime("%Y-%m-%d").fillna(p["timeline"].astype(str)),
                           "Status": p["status"], "Overdue by (days)": p["days_overdue"], "Comment": p["comment"]})
    detail = detail.assign(_o=p["overdue"].values, _op=p["open"].values)
    detail = detail.sort_values(["_o", "Overdue by (days)", "Facility"], ascending=[False, False, True])

    def summarise(col, label):
        g = p.assign(_k=p[col].astype(str).str.strip().replace("", "(not set)"))
        s = g.groupby("_k").agg(Actions=("open", "size"), Open=("open", "sum"), Overdue=("overdue", "sum")).reset_index()
        s["Done"] = s["Actions"] - s["Open"]
        s["% done"] = (s["Done"] / s["Actions"] * 100).round(0)
        s = s.rename(columns={"_k": label})
        return s[[label, "Actions", "Done", "Open", "Overdue", "% done"]].sort_values(["Overdue", "Open"], ascending=False).reset_index(drop=True)

    byf = summarise("facility", "Facility")
    byo = summarise("responsible_person", "Owner").drop(columns="% done")
    total, op, ov = len(p), int(p["open"].sum()), int(p["overdue"].sum())
    return {"actions": detail.drop(columns=["_o", "_op"]).reset_index(drop=True), "by_facility": byf, "by_owner": byo,
            "totals": {"total": total, "done": total - op, "open": op, "overdue": ov,
                       "pct_done": round((total - op) / total * 100) if total else None}}


# ---------------------------------------------------------------------
# Competency report
# ---------------------------------------------------------------------
def competency_report(d: dict, facility: str = "", topic: str = "") -> dict:
    """-> {'by_topic': one row per facility x topic, 'workers': latest assessment per worker x topic,
           'weak_items': weakest competencies per topic, 'progress': baseline vs follow-up}"""
    c = _df(d.get("comp"))
    out = {"by_topic": pd.DataFrame(), "workers": pd.DataFrame(), "weak_items": pd.DataFrame(), "progress": pd.DataFrame()}
    if c.empty:
        return out
    if facility:
        c = c[c["facility_key"] == _key(facility)]
    if topic:
        c = c[c["service_area"] == topic]
    if c.empty:
        return out
    summ = comp.summarize(c)
    latest = comp.latest_per_worker_area(c)
    ls = comp.summarize(latest)
    ls["Rating"] = ls["pct_competent"].map(mentorship.rating)
    out["workers"] = pd.DataFrame({
        "Facility": ls["facility"], "Health worker": ls["mentee_name"], "Cadre": ls["cadre"], "Topic": ls["service_area"],
        "Last assessed": ls["assessment_date"].astype(str).str[:10], "Type": ls["assessment_type"],
        "Items scored": ls["scored"], "Gaps": ls["gaps"], "Score %": ls["pct_competent"], "Rating": ls["Rating"]
    }).sort_values(["Facility", "Topic", "Score %"]).reset_index(drop=True)

    g = ls.groupby(["facility", "service_area"]).agg(workers=("mentee_name", "nunique"), avg=("pct_competent", "mean"),
                                                     red=("pct_competent", lambda s: int((s < 70).sum())),
                                                     last=("assessment_date", "max")).reset_index()
    g["avg"] = g["avg"].round(1)
    out["by_topic"] = pd.DataFrame({"Facility": g["facility"], "Topic": g["service_area"], "Health workers": g["workers"],
                                    "Average score %": g["avg"], "Rating": g["avg"].map(mentorship.rating),
                                    "Workers below 70%": g["red"], "Last assessed": g["last"].astype(str).str[:10]}
                                   ).sort_values(["Facility", "Topic"]).reset_index(drop=True)

    weak = []
    for area in sorted(latest["service_area"].dropna().unique()):
        w = comp.weakest(latest, area, 5)
        for _, r in w.iterrows():
            weak.append({"Topic": area, "Competency": r["competency"], "Assessed": r["assessed"],
                         "Competent": r["competent"], "% competent": r["pct_competent"]})
    out["weak_items"] = pd.DataFrame(weak, columns=["Topic", "Competency", "Assessed", "Competent", "% competent"])

    pr = comp.progress(summ)
    pr = pr[pr["baseline_pct"].notna() & pr["followup_pct"].notna()] if not pr.empty else pr
    out["progress"] = pd.DataFrame({"Facility": pr["facility"], "Health worker": pr["mentee_name"], "Topic": pr["service_area"],
                                    "Baseline %": pr["baseline_pct"], "Follow-up %": pr["followup_pct"], "Change": pr["change"]}
                                   ) if not pr.empty else pd.DataFrame(columns=["Facility", "Health worker", "Topic", "Baseline %", "Follow-up %", "Change"])
    return out


# ---------------------------------------------------------------------
# Performance and visits summary
# ---------------------------------------------------------------------
def performance_summary(d: dict, period: str = "", today: date | None = None) -> dict:
    """One row per facility: DHIS2 achievement (published analysis) next to visits, competency and actions.
    `period` '' = every published period. Achievement uses the same bands as the tools (85 / 70)."""
    ach = _df(d.get("ach"))
    rows = {}

    def row(fac):
        return rows.setdefault(_key(fac), {"Facility": str(fac).strip()})

    if not ach.empty:
        a = ach if not period else ach[ach["period"].astype(str) == str(period)]
        a = a[a["achievement_pct"].notna()]
        for k, g in a.groupby(a["facility"].map(_key)):
            r = row(g["facility"].iloc[0])
            r["Indicators"] = int(len(g))
            r["Avg achievement %"] = round(float(g["achievement_pct"].mean()), 1)
            r["Indicators at 85%+"] = int((g["achievement_pct"] >= 85).sum())
            r["Indicators below 70%"] = int((g["achievement_pct"] < 70).sum())
            r["Weakest indicator"] = g.sort_values("achievement_pct").iloc[0]["data_element"]
    tools = _df(d.get("tools"))
    if not tools.empty:
        t = tools.assign(_d=pd.to_datetime(tools["visit_date"], errors="coerce"))
        for k, g in t.groupby(t["facility"].map(_key)):
            r = row(g["facility"].iloc[0])
            r["Visits"] = int(len(g))
            last = g.sort_values("_d").iloc[-1]
            r["Last visit"] = str(last["visit_date"])[:10]
            if "visit_source" in g.columns:
                r["Linked to DHIS2"] = int((g["visit_source"].astype(str).str.contains("DHIS2|dashboard", case=False)).sum())
    c = _df(d.get("comp"))
    if not c.empty:
        ls = comp.summarize(comp.latest_per_worker_area(c))
        for k, g in ls.groupby(ls["facility"].map(_key)):
            r = row(g["facility"].iloc[0])
            avg = g["pct_competent"].mean()
            r["Health workers assessed"] = int(g["mentee_name"].nunique())
            r["Competency %"] = round(float(avg), 1) if avg == avg else None
            r["Competency rating"] = mentorship.rating(avg)
    ses = _df(d.get("session"))
    if not ses.empty:
        s = ses.assign(_p=_num(ses["systems_pct"]))
        for k, g in s.groupby(s["facility"].map(_key)):
            r = row(g["facility"].iloc[0])
            m = g["_p"].mean()
            r["Systems readiness %"] = round(float(m), 1) if m == m else None
    p = plans(d, today)
    if not p.empty:
        for k, g in p.groupby(p["facility"].map(_key)):
            r = row(g["facility"].iloc[0])
            r["Open actions"] = int(g["open"].sum())
            r["Overdue actions"] = int(g["overdue"].sum())

    cols = ["Facility", "Indicators", "Avg achievement %", "Indicators at 85%+", "Indicators below 70%", "Weakest indicator",
            "Visits", "Linked to DHIS2", "Last visit", "Health workers assessed", "Competency %", "Competency rating",
            "Systems readiness %", "Open actions", "Overdue actions"]
    df = pd.DataFrame(list(rows.values())).reindex(columns=cols)
    if not df.empty:
        for c_ in ["Visits", "Linked to DHIS2", "Open actions", "Overdue actions", "Health workers assessed"]:
            df[c_] = df[c_].fillna(0).astype(int)
        df["Achievement rating"] = df["Avg achievement %"].map(lambda v: mentorship.rating(v) if v == v and v is not None else "")
        df.insert(3, "Achievement rating", df.pop("Achievement rating"))
        df = df.sort_values(["Avg achievement %", "Facility"], na_position="last").reset_index(drop=True)
    else:
        df.insert(3, "Achievement rating", [])
    # facilities with weak performance and no visit: where to go next
    attention = df[(df.get("Avg achievement %").fillna(100) < 70) & (df.get("Visits", 0) == 0)] if not df.empty else df
    totals = {"facilities": int(len(df)), "visited": int((df["Visits"] > 0).sum()) if not df.empty else 0,
              "red": int((df["Achievement rating"] == "Red").sum()) if not df.empty else 0,
              "overdue": int(df["Overdue actions"].sum()) if not df.empty else 0,
              "needs_visit": int(len(attention))}
    return {"table": df, "attention": attention.reset_index(drop=True), "totals": totals}


# ---------------------------------------------------------------------
# Facility visit report
# ---------------------------------------------------------------------
def visits_for(d: dict, facility: str = "") -> pd.DataFrame:
    t = _df(d.get("tools"))
    if t.empty:
        return t
    if facility:
        t = t[t["facility"].map(_key) == _key(facility)]
    return t.assign(_d=pd.to_datetime(t["visit_date"], errors="coerce")).sort_values("_d", ascending=False).drop(columns="_d").reset_index(drop=True)


def visit_label(r) -> str:
    return f"{str(r.get('visit_date', ''))[:10]} - {r.get('visit_ref', '')}"


def facility_visit(d: dict, visit_ref: str, today: date | None = None) -> dict:
    """Everything recorded for one visit, ready to show or write to Word."""
    tools = _df(d.get("tools"))
    m = tools[tools["visit_ref"].astype(str) == str(visit_ref)] if not tools.empty else tools
    if m.empty:
        raise ValueError(f"No visit with reference {visit_ref}.")
    h = m.iloc[0].to_dict()
    fac, vdate = str(h.get("facility", "")), str(h.get("visit_date", ""))[:10]
    rep = {"ref": str(visit_ref), "facility": fac, "date": vdate, "header": h}

    att = _for_visit(d.get("att"), visit_ref)
    rep["attendance"] = pd.DataFrame({"Name": att.get("fullname"), "Cadre": att.get("cadre"), "Phone": att.get("phone")}) if not att.empty else pd.DataFrame(columns=["Name", "Cadre", "Phone"])

    # competency of this visit
    c = _df(d.get("comp"))
    cv = c[c["visit_ref"].astype(str) == str(visit_ref)] if not c.empty else c
    if not cv.empty:
        s = comp.summarize(cv)
        s["Rating"] = s["pct_competent"].map(mentorship.rating)
        rep["competency"] = pd.DataFrame({"Health worker": s["mentee_name"], "Cadre": s["cadre"], "Topic": s["service_area"],
                                          "Items scored": s["scored"], "Gaps": s["gaps"], "Score %": s["pct_competent"], "Rating": s["Rating"]}
                                         ).sort_values(["Topic", "Health worker"]).reset_index(drop=True)
        gaps = cv[cv["score_num"].notna() & (cv["score_num"] < comp.COMPETENT_MIN)]
        if not gaps.empty:
            g = gaps.groupby(["service_area", "competency"]).agg(n=("mentee_name", "nunique")).reset_index().sort_values("n", ascending=False)
            rep["gaps"] = pd.DataFrame({"Topic": g["service_area"], "Competency not yet met": g["competency"], "Health workers": g["n"]}).head(15).reset_index(drop=True)
        avg = s["pct_competent"].mean()
        rep["competency_avg"] = None if avg != avg else round(float(avg), 1)
    else:
        rep["competency"] = pd.DataFrame(columns=["Health worker", "Cadre", "Topic", "Items scored", "Gaps", "Score %", "Rating"])
        rep["competency_avg"] = None
    rep.setdefault("gaps", pd.DataFrame(columns=["Topic", "Competency not yet met", "Health workers"]))

    # per-topic session summary (systems %, records %, rating, who/how)
    ses = _for_visit(d.get("session"), visit_ref)
    rep["session"] = (pd.DataFrame({"Topic": ses["topic"], "Systems %": _num(ses["systems_pct"]), "Systems rating": ses["systems_rating"],
                                    "Records reviewed": ses["records_total"], "Records met %": _num(ses["records_pct"]),
                                    "Overall rating": ses["overall_rating"], "Methods": ses["methods"],
                                    "Female": ses["female"], "Male": ses["male"], "Hours": ses["hours"]})
                      if not ses.empty else pd.DataFrame(columns=["Topic", "Systems %", "Systems rating", "Records reviewed", "Records met %", "Overall rating", "Methods", "Female", "Male", "Hours"]))
    sy = _for_visit(d.get("systems"), visit_ref)
    sy_gaps = sy[sy["score"].astype(str).str.strip().isin(["0", "1"])] if not sy.empty else sy
    rep["systems_gaps"] = (pd.DataFrame({"Topic": sy_gaps["topic"], "Standard": sy_gaps["standard"],
                                         "Finding": sy_gaps["score"].astype(str).map({"0": "Not met", "1": "Partially met"}),
                                         "Evidence": sy_gaps["evidence"]}).reset_index(drop=True)
                           if not sy_gaps.empty else pd.DataFrame(columns=["Topic", "Standard", "Finding", "Evidence"]))
    ind = _for_visit(d.get("indicators"), visit_ref)
    rep["indicators"] = (pd.DataFrame({"Topic": ind["topic"], "Indicator": ind["indicator"], "Numerator": ind["numerator"],
                                       "Denominator": ind["denominator"], "% (verified)": _num(ind["pct"]), "Benchmark": ind["benchmark"],
                                       "Reported in DHIS2": ind["reported_dhis2"], "Variance %": _num(ind["variance_pct"]), "Met": ind["met"]})
                         if not ind.empty else pd.DataFrame(columns=["Topic", "Indicator", "Numerator", "Denominator", "% (verified)", "Benchmark", "Reported in DHIS2", "Variance %", "Met"]))

    rca = _for_visit(d.get("rca"), visit_ref)
    rep["rca"] = (pd.DataFrame({"Method": rca["method"], "Section": rca["section"], "Item": rca["item"], "Text": rca["text"]})
                  if not rca.empty else pd.DataFrame(columns=["Method", "Section", "Item", "Text"]))
    act = _for_visit(d.get("actions"), visit_ref)
    rep["actions"] = (pd.DataFrame({"Challenge": act["challenge"], "Action": act["corrective_action"], "Owner": act["responsible_person"],
                                    "Due": act["timeline"], "Escalate to": act.get("escalate_to", "")})
                      if not act.empty else pd.DataFrame(columns=["Challenge", "Action", "Owner", "Due", "Escalate to"]))
    ipi = _for_visit(d.get("ipi"), visit_ref)
    rep["ipi"] = (pd.DataFrame({"IPI indicator": ipi["ipi_indicator"], "Action": ipi["action"], "Evidence": ipi["evidence"]})
                  if not ipi.empty else pd.DataFrame(columns=["IPI indicator", "Action", "Evidence"]))
    rt = _for_visit(d.get("routine"), visit_ref)
    rep["routine"] = (pd.DataFrame({"Indicator": rt["indicator"], "Benchmark": rt["benchmark"], "Achieved": rt["achieved"]})
                      if not rt.empty else pd.DataFrame(columns=["Indicator", "Benchmark", "Achieved"]))

    # follow-up of the previous visit's actions, recorded during this visit
    fu = _for_visit(d.get("followup"), visit_ref)
    rep["followup"] = (pd.DataFrame({"Previous action": fu["action"], "Responsible": fu["responsible"], "Status": fu["status"], "Comment": fu["comment"]})
                       if not fu.empty else pd.DataFrame(columns=["Previous action", "Responsible", "Status", "Comment"]))
    # the plan that stood before this visit (what the supervisor started from)
    p = plans(d, today)
    prev = p[(p["facility"].map(_key) == _key(fac)) & (p["visit_ref"].astype(str) != str(visit_ref))] if not p.empty else p
    if not prev.empty:
        prev = prev[pd.to_datetime(prev["visit_date"], errors="coerce") < pd.to_datetime(vdate, errors="coerce")] if vdate else prev
    pl = vt.latest_plan(prev, fac) if not prev.empty else prev
    rep["previous_plan"] = (pd.DataFrame({"Action": pl["corrective_action"], "Owner": pl["responsible_person"], "Due": pl["timeline"], "Status now": pl["status"]}).reset_index(drop=True)
                            if not pl.empty else pd.DataFrame(columns=["Action", "Owner", "Due", "Status now"]))
    rep["previous_date"] = str(pl["visit_date"].iloc[0])[:10] if not pl.empty else ""

    # support supervision score
    ssm = _df(d.get("ssm"))
    sc = None
    if not ssm.empty and "visit_ref" in ssm.columns:
        mm = ssm[ssm["visit_ref"].astype(str) == str(visit_ref)]
        if not mm.empty:
            sc = _num(mm["score"]).mean()
    rep["ssm_score"] = None if sc is None or sc != sc else round(float(sc), 1)
    rep["ssm_rating"] = mentorship.rating(rep["ssm_score"]) if rep["ssm_score"] is not None else ""

    # headline
    parts = []
    n_att = len(rep["attendance"]) or h.get("attendee_count", "")
    who = f"{h.get('assessor', '').strip() or 'The team'}"
    parts.append(f"{who} visited {fac} on {vdate}" + (f" ({h.get('visit_type')})" if h.get("visit_type") else "") + (f"; {n_att} attended." if str(n_att) not in ("", "0") else "."))
    if rep["competency_avg"] is not None:
        nw = rep["competency"]["Health worker"].nunique()
        parts.append(f"{nw} health worker(s) were assessed across {rep['competency']['Topic'].nunique()} topic(s); the average competency score was "
                     f"{rep['competency_avg']:.0f}% ({mentorship.rating(rep['competency_avg'])}).")
    if not rep["session"].empty and rep["session"]["Systems %"].notna().any():
        parts.append(f"Facility systems readiness averaged {rep['session']['Systems %'].mean():.0f}%.")
    if not rep["actions"].empty:
        parts.append(f"{len(rep['actions'])} action(s) were agreed.")
    if not rep["previous_plan"].empty:
        op = int((rep["previous_plan"]["Status now"].astype(str).str.lower() != DONE).sum())
        parts.append(f"Of {len(rep['previous_plan'])} actions from the previous visit ({rep['previous_date']}), {op} are still open.")
    rep["summary"] = " ".join(parts)
    return rep


# ---------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------
def to_excel(sheets: dict, title: str = "") -> bytes:
    """{sheet name: DataFrame} -> .xlsx with a styled header, frozen top row, widths, and Green/Amber/Red shading."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
            df = df.copy()
            for c in df.columns:
                if str(df[c].dtype).startswith("datetime"):
                    df[c] = df[c].dt.strftime("%Y-%m-%d")
            df.to_excel(xw, sheet_name=name[:31], index=False)
            ws = xw.sheets[name[:31]]
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F4E79")
                cell.alignment = Alignment(wrap_text=True, vertical="center")
            ws.freeze_panes = "A2"
            for i, col in enumerate(df.columns, 1):
                vals = [str(col)] + [str(v) for v in df[col].head(200).tolist()]
                ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, max(len(v) for v in vals) + 2))
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if cell.value in RATING_COLOURS:
                        cell.fill = PatternFill("solid", fgColor=RATING_COLOURS[cell.value])
                    if isinstance(cell.value, str) and len(cell.value) > 60:
                        cell.alignment = Alignment(wrap_text=True, vertical="top")
    return buf.getvalue()


# ---------------------------------------------------------------------
# Word (facility visit report)
# ---------------------------------------------------------------------
def _shade(cell, hex_):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    tcPr = cell._tc.get_or_add_tcPr()
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear"); sh.set(qn("w:color"), "auto"); sh.set(qn("w:fill"), hex_)
    tcPr.append(sh)


def _add_table(doc, df: pd.DataFrame, max_rows: int = 60):
    from docx.shared import Pt
    if df is None or df.empty:
        p = doc.add_paragraph("Nothing recorded.")
        p.runs[0].italic = True
        return
    df = df.head(max_rows)
    t = doc.add_table(rows=1, cols=len(df.columns))
    t.style = "Table Grid"
    for i, c in enumerate(df.columns):
        cell = t.rows[0].cells[i]
        cell.text = ""
        r = cell.paragraphs[0].add_run(str(c)); r.bold = True; r.font.size = Pt(9)
        _shade(cell, "D9E2F3")
    for _, row in df.iterrows():
        cells = t.add_row().cells
        for i, v in enumerate(row):
            txt = "" if v is None or (isinstance(v, float) and v != v) else (f"{v:.0f}" if isinstance(v, float) and float(v).is_integer() else (f"{v:.1f}" if isinstance(v, float) else str(v)))
            cells[i].text = ""
            cells[i].paragraphs[0].add_run(txt).font.size = Pt(9)
            if txt in RATING_COLOURS:
                _shade(cells[i], RATING_COLOURS[txt])
    doc.add_paragraph()


def _chart_png(rep: dict):
    """Competency vs systems % by topic, as a PNG for the Word report (None if there's nothing to plot)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:                      # chart is optional: the report still works without it
        return None
    comp_df, ses = rep["competency"], rep["session"]
    topics = list(dict.fromkeys(list(comp_df["Topic"]) + list(ses["Topic"])))
    if not topics:
        return None
    cv = comp_df.groupby("Topic")["Score %"].mean() if not comp_df.empty else pd.Series(dtype=float)
    sv = ses.groupby("Topic")["Systems %"].mean() if not ses.empty else pd.Series(dtype=float)
    fig, ax = plt.subplots(figsize=(7, max(2.2, 0.55 * len(topics) + 1)))
    y = range(len(topics))
    ax.barh([i + 0.2 for i in y], [cv.get(t, 0) or 0 for t in topics], height=0.38, color="#1F4E79", label="Health worker competency")
    ax.barh([i - 0.2 for i in y], [sv.get(t, 0) or 0 for t in topics], height=0.38, color="#7FA8D1", label="Facility systems")
    ax.axvline(85, color="#2E7D32", ls="--", lw=1); ax.axvline(70, color="#C62828", ls="--", lw=1)
    ax.set_yticks(list(y)); ax.set_yticklabels(topics, fontsize=8); ax.set_xlim(0, 100); ax.invert_yaxis()
    ax.set_xlabel("Score %  (green line 85, red line 70)", fontsize=8); ax.legend(fontsize=7, loc="lower right")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    buf = io.BytesIO(); fig.tight_layout(); fig.savefig(buf, format="png", dpi=160); plt.close(fig)
    buf.seek(0)
    return buf


def docx_facility_report(rep: dict) -> bytes:
    from docx import Document
    from docx.shared import Inches, Pt
    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Inches(0.8)
    doc.styles["Normal"].font.name = "Calibri"; doc.styles["Normal"].font.size = Pt(10.5)
    h = rep["header"]
    doc.add_heading("Facility Mentorship / Support Supervision Visit Report", 0)
    doc.add_heading(rep["facility"], 1)

    info = [("Visit date", rep["date"]), ("Visit reference", rep["ref"]), ("Visit type", h.get("visit_type", "")),
            ("Mode", h.get("visit_mode", "")), ("Facility level", h.get("facility_level", "")), ("Division", h.get("kcca_division", "")),
            ("Lead mentor / assessor", h.get("lead_mentor") or h.get("assessor", "")), ("Co-mentor", h.get("co_mentor", "")),
            ("Facility in-charge", h.get("facility_in_charge", "")), ("Units visited", h.get("units_visited", "")),
            ("Visit source", h.get("visit_source", ""))]
    info = [(k, v) for k, v in info if str(v).strip()]
    t = doc.add_table(rows=0, cols=4); t.style = "Table Grid"
    for i in range(0, len(info), 2):
        cells = t.add_row().cells
        for j, (k, v) in enumerate(info[i:i + 2]):
            cells[j * 2].text = ""; r = cells[j * 2].paragraphs[0].add_run(k); r.bold = True; r.font.size = Pt(9)
            _shade(cells[j * 2], "F2F2F2")
            cells[j * 2 + 1].text = str(v)
    doc.add_paragraph()

    doc.add_heading("Summary", 1)
    doc.add_paragraph(rep["summary"])
    if rep["ssm_score"] is not None:
        doc.add_paragraph(f"Support supervision score: {rep['ssm_score']:.0f}% ({rep['ssm_rating']}).")

    doc.add_heading("Scores by topic", 1)
    png = _chart_png(rep)
    if png:
        doc.add_picture(png, width=Inches(6.2))
    _add_table(doc, rep["session"][["Topic", "Systems %", "Systems rating", "Records met %", "Overall rating"]] if not rep["session"].empty else rep["session"])

    doc.add_heading("Health worker competency", 1)
    _add_table(doc, rep["competency"])
    if not rep["gaps"].empty:
        doc.add_heading("Competencies most often not yet met", 2)
        _add_table(doc, rep["gaps"])

    doc.add_heading("Facility systems and service readiness - gaps", 1)
    _add_table(doc, rep["systems_gaps"])
    if not rep["indicators"].empty:
        doc.add_heading("Data verification", 1)
        _add_table(doc, rep["indicators"])

    doc.add_heading("Root cause analysis", 1)
    if not rep["rca"].empty:
        doc.add_paragraph(f"Method: {rep['rca']['Method'].iloc[0]}")
    _add_table(doc, rep["rca"][["Section", "Item", "Text"]] if not rep["rca"].empty else rep["rca"])

    doc.add_heading("Action plan agreed", 1)
    _add_table(doc, rep["actions"])

    doc.add_heading("Follow-up of the previous action plan", 1)
    if not rep["followup"].empty:
        _add_table(doc, rep["followup"])
    elif not rep["previous_plan"].empty:
        doc.add_paragraph(f"Actions agreed at the visit of {rep['previous_date']} and their status now:")
        _add_table(doc, rep["previous_plan"])
    else:
        doc.add_paragraph("No previous action plan for this facility.")

    if not rep["ipi"].empty:
        doc.add_heading("IPI milestone mapping", 1); _add_table(doc, rep["ipi"])
    if not rep["routine"].empty:
        doc.add_heading("Routine indicators", 1); _add_table(doc, rep["routine"])
    if not rep["attendance"].empty:
        doc.add_heading("Attendance", 1); _add_table(doc, rep["attendance"])

    p = doc.add_paragraph(f"Generated {date.today():%d %B %Y} from the visit records.")
    p.runs[0].italic = True
    buf = io.BytesIO(); doc.save(buf)
    return buf.getvalue()