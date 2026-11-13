"""
visit_tools.py
---------------
The five tools used at the START of a support supervision / mentorship visit, before the
competency assessment:

  1. Attendance            full name, cadre, email, phone  (or upload the paper list)
  2. Root cause analysis   the supervisor picks the method (5 Whys, Fishbone, Problem tree, other)
  3. Action plan matrix    identified challenge | corrective action | responsible person | timeline
  4. IPI milestone mapping IPI indicator (dropdown) | action | validation / evidence
  5. Routine indicator     routine indicator | benchmark | achieved
     mapping

They are captured in ONE Kobo form (generated here, so it always matches your indicator lists) and
synced into Sheet tabs, so they can be viewed and tracked in the app.

Things you will probably want to change later: the SAMPLE indicator lists (edit them in the app —
Visit tools > Lists & Kobo form), RCA_METHODS and FISHBONE below.
"""

import io
import re
from datetime import date

import pandas as pd

import competency
import gsheets

LINK_SEP = " | "

# EDIT ME: root cause analysis methods offered to the supervisor (code, label).
RCA_METHODS = [("five_whys", "5 Whys"), ("fishbone", "Fishbone (Ishikawa) diagram"),
               ("problem_tree", "Problem tree"), ("other", "Other method")]
RCA_LABEL = dict(RCA_METHODS)

# EDIT ME: the "bones" of the fishbone diagram (question name, label).
FISHBONE = [("fb_people", "People / staff"), ("fb_process", "Processes / methods"),
            ("fb_supplies", "Equipment, supplies and commodities"), ("fb_environment", "Environment / infrastructure"),
            ("fb_data", "Data and information systems"), ("fb_leadership", "Leadership, management and policy")]

ATTENDANCE_METHODS = [("digital", "Enter the attendance list here"),
                      ("paper", "It was written on paper — upload it"),
                      ("both", "Both — enter it here and upload the paper list")]
ATT_LABEL = dict(ATTENDANCE_METHODS)
UPLOAD_FIELDS = ("attendance_paper_1", "attendance_paper_2", "attendance_paper_file")

MAIN_TAB = "VisitTools"
CHILD_TABS = ("VisitAttendance", "VisitRCA", "VisitActions", "VisitIPI", "VisitRoutine")

IPI_COLS = ["ipi_id", "indicator", "active"]
ROUTINE_COLS = ["indicator_id", "indicator", "active"]

# ---------------------------------------------------------------------
# EDIT ME: SAMPLE indicator lists — placeholders so the tools work on day one. Replace with your
# real Incentivised Programme Indicators and routine indicators in the app (they live in the Sheet).
# ---------------------------------------------------------------------
_SAMPLE_IPI = [("ipi_01", "ANC 4th visit coverage"), ("ipi_02", "Skilled birth attendance"),
               ("ipi_03", "Penta3 / DPT3 immunization coverage"), ("ipi_04", "HIV-positive clients linked to ART"),
               ("ipi_05", "Viral load suppression"), ("ipi_06", "TB treatment success rate"),
               ("ipi_07", "Timely and complete HMIS reporting"), ("ipi_08", "Tracer commodities stock-out free days")]
_SAMPLE_ROUTINE = [("ri_01", "ANC 1st visit coverage"), ("ri_02", "ANC 4th visit coverage"),
                   ("ri_03", "Facility deliveries"), ("ri_04", "DPT3 coverage"), ("ri_05", "HIV testing coverage"),
                   ("ri_06", "Malaria cases tested and treated"), ("ri_07", "TB case notification"),
                   ("ri_08", "Reporting rate (HMIS)")]


def default_ipi() -> pd.DataFrame:
    return pd.DataFrame([(i, t, "yes") for i, t in _SAMPLE_IPI], columns=IPI_COLS)


def default_routine() -> pd.DataFrame:
    return pd.DataFrame([(i, t, "yes") for i, t in _SAMPLE_ROUTINE], columns=ROUTINE_COLS)


# ---------------------------------------------------------------------
# Indicator lists
# ---------------------------------------------------------------------
def normalize_list(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = ""
    out = out[cols].fillna("").astype(str)
    for c in cols:
        out[c] = out[c].str.strip()
    out = out[~(out[cols] == "").all(axis=1)].reset_index(drop=True)
    out["active"] = out["active"].str.lower().replace({"": "yes", "true": "yes", "false": "no", "1": "yes", "0": "no"})
    return out


def validate_list(df: pd.DataFrame, cols: list, what: str) -> list:
    errs = []
    d = normalize_list(df, cols)
    if d.empty:
        return [f"The {what} list is empty."]
    idc = cols[0]
    for i, r in d.iterrows():
        line = f"{what}, row {i + 1}"
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", r[idc]):
            errs.append(f"{line}: {idc} '{r[idc]}' must start with a letter and use only letters, numbers and underscores.")
        elif r[idc].lower() == "other":
            errs.append(f"{line}: '{r[idc]}' is reserved — choose another code.")
        if not r["indicator"]:
            errs.append(f"{line}: the indicator name is empty.")
        if r["active"] not in ("yes", "no"):
            errs.append(f"{line}: active must be yes or no.")
    dup = d[idc][d[idc].duplicated()].unique()
    if len(dup):
        errs.append(f"{what}: duplicate {idc}: " + ", ".join(dup))
    return errs


def load_lists(seed: bool = False):
    """(ipi_df, routine_df) from the Sheet. With seed=True, empty lists are filled with the samples."""
    ipi = gsheets.get_table("IPIIndicators")
    rout = gsheets.get_table("RoutineIndicators")
    if ipi.empty and seed:
        gsheets.save_table("IPIIndicators", default_ipi())
        ipi = gsheets.get_table("IPIIndicators")
    if rout.empty and seed:
        gsheets.save_table("RoutineIndicators", default_routine())
        rout = gsheets.get_table("RoutineIndicators")
    ipi = normalize_list(ipi, IPI_COLS) if not ipi.empty else pd.DataFrame(columns=IPI_COLS)
    rout = normalize_list(rout, ROUTINE_COLS) if not rout.empty else pd.DataFrame(columns=ROUTINE_COLS)
    return ipi, rout


def _active(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["active"] != "no"].reset_index(drop=True)


# ---------------------------------------------------------------------
# The Kobo form (XLSForm), generated from the lists above
# ---------------------------------------------------------------------
_SURVEY_COLS = ["type", "name", "label", "hint", "required", "relevant", "default", "appearance",
                "constraint", "constraint_message", "calculation"]


def _r(type_, name="", label="", hint="", required="", relevant="", default="", appearance="",
       constraint="", cmsg="", calc=""):
    return [type_, name, label, hint, required, relevant, default, appearance, constraint, cmsg, calc]


def build_xlsform(ipi_df: pd.DataFrame, routine_df: pd.DataFrame, form_title: str = "Support Supervision Visit Tools",
                  form_id: str = "visit_tools") -> bytes:
    """An .xlsx XLSForm ready to upload to Kobo. Repeating rows hold attendees, actions, IPI
    milestones and routine indicators; the RCA section shows only the chosen method's questions."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    ipi = _active(normalize_list(ipi_df, IPI_COLS))
    routine = _active(normalize_list(routine_df, ROUTINE_COLS))
    if ipi.empty:
        raise ValueError("There are no active IPI indicators to build the form from.")
    errs = validate_list(ipi, IPI_COLS, "IPI") + (validate_list(routine, ROUTINE_COLS, "Routine indicator") if not routine.empty else [])
    if errs:
        raise ValueError("; ".join(errs[:5]))

    ML = "multiline"
    s = [
        _r("text", "facility", "Facility", "Filled in automatically when opened from the dashboard — correct it if needed.", "yes"),
        _r("text", "visit_ref", "Visit reference", "Filled in automatically. It ties together every form of this visit — leave it as it is."),
        _r("date", "visit_date", "Date of the visit", required="yes", default="today()"),
        _r("text", "assessor", "Your name", required="yes"),

        # ---- 1. attendance
        _r("begin_group", "grp_attendance", "1. Attendance"),
        _r("select_one attendance_methods", "attendance_method", "How is the attendance list being recorded?", required="yes"),
        _r("begin_repeat", "rep_attendance", "Attendees", "Add one row per person (use the + button).", relevant="${attendance_method} != 'paper'"),
        _r("text", "att_fullname", "Full name", required="yes"),
        _r("select_one cadres", "att_cadre", "Cadre", required="yes"),
        _r("text", "att_email", "Email", constraint="regex(., '^[^@ ]+@[^@ ]+[.][^@ ]+$')", cmsg="Enter a valid email address, or leave it blank."),
        _r("text", "att_phone", "Phone contact", constraint="regex(., '^[+]?[0-9 ]{9,15}$')", cmsg="Enter a phone number with 9–15 digits, e.g. 0772 123456."),
        _r("end_repeat"),
        _r("image", "attendance_paper_1", "Photo of the written attendance list — page 1", "Take a clear, flat photo of the whole page.", relevant="${attendance_method} != 'digital'"),
        _r("image", "attendance_paper_2", "Photo of the written attendance list — page 2 (if any)", relevant="${attendance_method} != 'digital'"),
        _r("file", "attendance_paper_file", "Or upload the list as a file (PDF or Excel)", relevant="${attendance_method} != 'digital'"),
        _r("end_group"),

        # ---- 2. root cause analysis
        _r("begin_group", "grp_rca", "2. Root cause analysis"),
        _r("text", "rca_problem", "Problem / performance gap being analysed", required="yes", appearance=ML),
        _r("select_one rca_methods", "rca_method", "Which root cause analysis method will you use?", required="yes"),
        _r("begin_group", "grp_rca_5whys", "5 Whys", relevant="${rca_method} = 'five_whys'"),
        _r("text", "why_1", "Why 1 — Why is this problem happening?", required="yes", appearance=ML),
    ]
    for i in range(2, 6):
        s.append(_r("text", f"why_{i}", f"Why {i} — Why did that happen?", appearance=ML))
    s += [
        _r("text", "rca_5whys_root", "Root cause — the deepest cause you can act on", required="yes", appearance=ML),
        _r("end_group"),
        _r("begin_group", "grp_rca_fishbone", "Fishbone (Ishikawa) diagram", relevant="${rca_method} = 'fishbone'"),
    ]
    for name, label in FISHBONE:
        s.append(_r("text", name, f"Causes — {label}", "One cause per line. Leave blank if none.", appearance=ML))
    s += [
        _r("text", "rca_fb_root", "Most likely root cause(s)", required="yes", appearance=ML),
        _r("end_group"),
        _r("begin_group", "grp_rca_tree", "Problem tree", relevant="${rca_method} = 'problem_tree'"),
        _r("begin_repeat", "rep_tree_causes", "Causes", "Add every direct and root cause (use the + button)."),
        _r("select_one cause_levels", "tree_cause_level", "Level", required="yes"),
        _r("text", "tree_cause", "Cause", required="yes", appearance=ML),
        _r("end_repeat"),
        _r("begin_repeat", "rep_tree_effects", "Effects", "Add the effects the problem has (use the + button)."),
        _r("text", "tree_effect", "Effect", required="yes", appearance=ML),
        _r("end_repeat"),
        _r("text", "rca_tree_root", "Root cause(s) you will act on", required="yes", appearance=ML),
        _r("end_group"),
        _r("begin_group", "grp_rca_other", "Other method", relevant="${rca_method} = 'other'"),
        _r("text", "rca_other_name", "Name of the method used", required="yes"),
        _r("text", "rca_other_notes", "Findings / analysis", required="yes", appearance=ML),
        _r("text", "rca_other_root", "Root cause(s) identified", required="yes", appearance=ML),
        _r("end_group"),
        _r("calculate", "rca_root_cause", calc="if(${rca_method} = 'five_whys', ${rca_5whys_root}, if(${rca_method} = 'fishbone', "
                                               "${rca_fb_root}, if(${rca_method} = 'problem_tree', ${rca_tree_root}, ${rca_other_root})))"),
        _r("end_group"),

        # ---- 3. action plan matrix
        _r("begin_group", "grp_actions", "3. Action plan matrix"),
        _r("begin_repeat", "rep_actions", "Action plan", "One row per challenge (use the + button)."),
        _r("text", "act_challenge", "Identified challenge", required="yes", appearance=ML),
        _r("text", "act_action", "Corrective action", required="yes", appearance=ML),
        _r("text", "act_responsible", "Responsible person", required="yes"),
        _r("date", "act_timeline", "Timeline (deadline)", required="yes"),
        _r("end_repeat"),
        _r("end_group"),

        # ---- 4. IPI milestone mapping
        _r("begin_group", "grp_ipi", "4. IPI Milestone Mapping"),
        _r("begin_repeat", "rep_ipi", "IPI milestones", "One row per IPI indicator (use the + button)."),
        _r("select_one ipi_indicators", "ipi_indicator", "IPI indicator", required="yes"),
        _r("text", "ipi_action", "Action", required="yes", appearance=ML),
        _r("text", "ipi_evidence", "Validation / evidence", required="yes", appearance=ML),
        _r("image", "ipi_evidence_file", "Photo of the evidence (optional)"),
        _r("end_repeat"),
        _r("end_group"),

        # ---- 5. routine indicator mapping
        _r("begin_group", "grp_routine", "5. Routine Indicator Mapping Tool"),
        _r("begin_repeat", "rep_routine", "Routine indicators", "One row per routine indicator (use the + button)."),
        _r("select_one routine_indicators", "ri_indicator", "Routine indicator", required="yes"),
        _r("text", "ri_indicator_other", "Indicator name", relevant="${ri_indicator} = 'other'", required="yes"),
        _r("decimal", "ri_benchmark", "Benchmark", required="yes", constraint=". >= 0", cmsg="Enter 0 or a positive number."),
        _r("decimal", "ri_achieved", "Achieved", required="yes", constraint=". >= 0", cmsg="Enter 0 or a positive number."),
        _r("end_repeat"),
        _r("end_group"),
        _r("text", "comments", "Comments", appearance=ML),
    ]

    ch = [("cadres", competency.slug(c), c) for c in competency.CADRES]
    ch += [("attendance_methods", c, l) for c, l in ATTENDANCE_METHODS]
    ch += [("rca_methods", c, l) for c, l in RCA_METHODS]
    ch += [("cause_levels", "direct", "Direct cause"), ("cause_levels", "root", "Root cause")]
    ch += [("ipi_indicators", r["ipi_id"], r["indicator"]) for _, r in ipi.iterrows()]
    ch += [("routine_indicators", r["indicator_id"], r["indicator"]) for _, r in routine.iterrows()]
    ch += [("routine_indicators", "other", "Other (type the name)")]

    wb = Workbook()
    sv = wb.active
    sv.title = "survey"
    sv.append(_SURVEY_COLS)
    for row in s:
        sv.append(row)
    cs = wb.create_sheet("choices")
    cs.append(["list_name", "name", "label"])
    for row in ch:
        cs.append(list(row))
    st_ = wb.create_sheet("settings")
    st_.append(["form_title", "form_id"])
    st_.append([form_title, form_id])
    for ws, widths in ((sv, [26, 24, 52, 40, 10, 34, 12, 12, 34, 34, 40]), (cs, [20, 30, 46]), (st_, [34, 20])):
        for cell in ws[1]:
            cell.font = Font(name="Arial", bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.font = Font(name="Arial")
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------
# One Kobo submission -> rows for the Sheet tabs
# ---------------------------------------------------------------------
def _flat(sub: dict) -> dict:
    """Plain answers keyed by their last path segment (groups are ignored); repeats are skipped."""
    out = {}
    for k, v in sub.items():
        if isinstance(k, str) and not k.startswith("_") and not isinstance(v, (list, dict)):
            out[k.split("/")[-1]] = v
    return out


def _repeat(sub: dict, name: str) -> list:
    """Entries of a repeating group as plain dicts keyed by question name."""
    for k, v in sub.items():
        if isinstance(k, str) and k.split("/")[-1] == name and isinstance(v, list):
            return [{kk.split("/")[-1]: vv for kk, vv in e.items()} if isinstance(e, dict) else {} for e in v]
    return []


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


def submission_to_tables(sub: dict, links_fn=None, ipi_labels: dict | None = None,
                         routine_labels: dict | None = None) -> dict:
    """Turn one visit-tools submission into rows for each tab: {tab_name: [row dict, ...]}.
    links_fn(filenames) -> download links for those attachments (supplied by kobo.py)."""
    links_fn = links_fn or (lambda names: [])
    ipi_labels, routine_labels = ipi_labels or {}, routine_labels or {}
    flat = _flat(sub)
    kid = str(sub.get("_uuid") or sub.get("_id") or "").strip()
    cadre_by_slug = {competency.slug(c): c for c in competency.CADRES}
    base = {"visit_ref": _txt(flat.get("visit_ref")), "facility": _txt(flat.get("facility")),
            "visit_date": _txt(flat.get("visit_date")) or str(sub.get("_submission_time", ""))[:10],
            "timestamp": str(sub.get("_submission_time", "")), "kobo_id": kid}

    attendance = []
    for e in _repeat(sub, "rep_attendance"):
        name = _txt(e.get("att_fullname"))
        if name:
            attendance.append({**base, "fullname": name, "cadre": cadre_by_slug.get(_txt(e.get("att_cadre")), _txt(e.get("att_cadre"))),
                               "email": _txt(e.get("att_email")), "phone": _txt(e.get("att_phone"))})

    method = _txt(flat.get("rca_method"))
    method_label = RCA_LABEL.get(method, method)
    rca = []

    def add(section, item, text):
        if _txt(text):
            rca.append({**base, "method": method_label, "section": section, "item": item, "text": _txt(text)})

    add("Problem", "Problem statement", flat.get("rca_problem"))
    if method == "five_whys":
        for i in range(1, 6):
            add("5 Whys", f"Why {i}", flat.get(f"why_{i}"))
        add("5 Whys", "Root cause", flat.get("rca_5whys_root"))
    elif method == "fishbone":
        for name, label in FISHBONE:
            add("Fishbone", label, flat.get(name))
        add("Fishbone", "Root cause(s)", flat.get("rca_fb_root"))
    elif method == "problem_tree":
        for e in _repeat(sub, "rep_tree_causes"):
            add("Problem tree", "Root cause" if _txt(e.get("tree_cause_level")) == "root" else "Direct cause", e.get("tree_cause"))
        for e in _repeat(sub, "rep_tree_effects"):
            add("Problem tree", "Effect", e.get("tree_effect"))
        add("Problem tree", "Root cause(s) to act on", flat.get("rca_tree_root"))
    elif method == "other":
        add("Other method", _txt(flat.get("rca_other_name")) or "Method", flat.get("rca_other_notes"))
        add("Other method", "Root cause(s)", flat.get("rca_other_root"))

    actions = []
    for e in _repeat(sub, "rep_actions"):
        if any(_txt(e.get(k)) for k in ("act_challenge", "act_action")):
            actions.append({**base, "challenge": _txt(e.get("act_challenge")), "corrective_action": _txt(e.get("act_action")),
                            "responsible_person": _txt(e.get("act_responsible")), "timeline": _txt(e.get("act_timeline"))[:10]})

    ipi = []
    for e in _repeat(sub, "rep_ipi"):
        code = _txt(e.get("ipi_indicator"))
        if code or _txt(e.get("ipi_action")):
            files = [_txt(e.get("ipi_evidence_file"))] if _txt(e.get("ipi_evidence_file")) else []
            ipi.append({**base, "ipi_indicator": ipi_labels.get(code, code), "action": _txt(e.get("ipi_action")),
                        "evidence": _txt(e.get("ipi_evidence")), "evidence_links": LINK_SEP.join(links_fn(files))})

    routine = []
    for e in _repeat(sub, "rep_routine"):
        code = _txt(e.get("ri_indicator"))
        name = _txt(e.get("ri_indicator_other")) if code == "other" else routine_labels.get(code, code)
        if name:
            routine.append({**base, "indicator": name, "benchmark": _txt(e.get("ri_benchmark")), "achieved": _txt(e.get("ri_achieved"))})

    uploads = [_txt(flat.get(f)) for f in UPLOAD_FIELDS if _txt(flat.get(f))]
    root = _txt(flat.get("rca_root_cause"))
    if not root:
        for key in ("rca_5whys_root", "rca_fb_root", "rca_tree_root", "rca_other_root"):
            root = root or _txt(flat.get(key))
    main = {**base, "assessor": _txt(flat.get("assessor")) or str(sub.get("_submitted_by") or ""),
            "attendance_method": ATT_LABEL.get(_txt(flat.get("attendance_method")), _txt(flat.get("attendance_method"))),
            "attendee_count": len(attendance), "attendance_upload_links": LINK_SEP.join(links_fn(uploads)),
            "rca_method": method_label, "rca_problem": _txt(flat.get("rca_problem")), "rca_root_cause": root,
            "action_count": len(actions), "ipi_count": len(ipi), "routine_count": len(routine),
            "comments": _txt(flat.get("comments"))}
    return {MAIN_TAB: [main], "VisitAttendance": attendance, "VisitRCA": rca, "VisitActions": actions,
            "VisitIPI": ipi, "VisitRoutine": routine}


# ---------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------
def action_status(timeline, today=None) -> str:
    d = pd.to_datetime(str(timeline)[:10], errors="coerce")
    if pd.isna(d):
        return "No date"
    days = (d - pd.Timestamp(today or date.today())).days
    if days < 0:
        return "Overdue"
    return "Due within 2 weeks" if days <= 14 else "Upcoming"


def routine_with_pct(df: pd.DataFrame) -> pd.DataFrame:
    """Routine indicator rows with numeric benchmark/achieved and % of benchmark achieved."""
    d = df.copy()
    d["benchmark_n"] = pd.to_numeric(d.get("benchmark"), errors="coerce")
    d["achieved_n"] = pd.to_numeric(d.get("achieved"), errors="coerce")
    d["pct_of_benchmark"] = (d["achieved_n"] / d["benchmark_n"] * 100).where(d["benchmark_n"] > 0).round(1)
    return d
