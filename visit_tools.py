"""
visit_tools.py
---------------
The support supervision / mentorship visit, in ONE Kobo form: the five tools first, then (if you
choose to continue) the competency assessment of each health worker, then the support supervision report.
Kobo can't reliably jump from one form to the next, so the form itself asks "continue?" between the parts.

The five tools:

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
import mentorship

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
YES_NO = [("yes", "Yes"), ("no", "No")]
SSM_PHOTO_FIELDS = ("photo_1", "photo_2", "photo_3")

# Part A (visit information) of the mentorship tool
DIVISIONS = ["Central", "Kawempe", "Lubaga", "Makindye", "Nakawa"]
FACILITY_LEVELS = [("hc2", "HC II"), ("hc3", "HC III"), ("hc4", "HC IV"), ("hospital", "Hospital"), ("clinic", "Clinic/DIC")]
VISIT_TYPES = [("baseline", "Baseline (pre-assessment)"), ("follow_up", "Follow-up")]
VISIT_MODES = [("onsite", "Onsite"), ("virtual", "Virtual"), ("joint", "Joint ROM-KCCA")]
FOLLOWUP_STATUS = [("D", "Done"), ("P", "Partially done"), ("N", "Not done")]
ESCALATE = [("facility", "Facility"), ("division", "Division"), ("kcca", "KCCA")]
MENTOR_METHODS = [("side_by_side", "Side-by-side / bedside"), ("case_review", "Case review"),
                  ("demo", "Demonstration & return demonstration"), ("simulation", "Simulation / drill"),
                  ("cme", "Short CME"), ("data_review", "Data review & run chart"),
                  ("job_aid", "Job aid / SOP orientation"), ("virtual", "Virtual session")]
QI_STATUS = [("registered", "Registered"), ("not_yet", "Not yet registered (agree aim today)")]
RATINGS = [("Green", "Green (85% or more) - sustain"), ("Amber", "Amber (70-84%) - targeted mentorship"),
           ("Red", "Red (below 70%) - intensive mentorship and escalation")]
SYS_SCALE = [("2", "Fully met"), ("1", "Partially met"), ("0", "Not met"), ("NA", "Not applicable")]
SYS_LABEL = dict(SYS_SCALE)
PART_A_COLS = ["kcca_division", "facility_level", "visit_type", "visit_mode", "units_visited", "priority_facility",
               "lead_mentor", "co_mentor", "facility_in_charge", "time_in", "time_out"]
UPLOAD_FIELDS = ("attendance_paper_1", "attendance_paper_2", "attendance_paper_file")

MAIN_TAB = "VisitTools"
CHILD_TABS = ("VisitAttendance", "VisitRCA", "VisitActions", "VisitIPI", "VisitRoutine",
              "VisitFollowUp", "VisitSystems", "VisitIndicators", "VisitRecords", "VisitSession")

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
                "constraint", "constraint_message", "calculation", "read_only"]


def _r(type_, name="", label="", hint="", required="", relevant="", default="", appearance="",
       constraint="", cmsg="", calc="", ro=""):
    return [type_, name, label, hint, required, relevant, default, appearance, constraint, cmsg, calc, ro]



def _topic_blocks(parts: pd.DataFrame, topics: list) -> list:
    """Report-part rows: the topic picker, then (per topic) Part D systems, Part E data verification and
    Part F record review, then the generic Parts G (session record), I (QI link) and J (rating)."""
    ML = "multiline"
    s = []
    if topics:
        s.append(_r("select_one topics", "ssm_topic", "Mentorship topic (technical area)",
                    "Choose the topic - its facility systems, data verification and record review tools are shown.", ""))
    for t in topics:
        tid = competency.slug(t)
        pt = parts[parts["topic"] == t]
        info = pt[pt["part"] == "INFO"]
        d_items, e_items, f_cols = (pt[pt["part"] == p].reset_index(drop=True) for p in "DEF")
        rel = f"${{ssm_topic}} = '{tid}'"
        if not d_items.empty:
            s.append(_r("begin_group", f"grp_sys_{tid}", f"Part D - Facility systems and service readiness ({t})", relevant=rel))
            for _, r in d_items.iterrows():
                s.append(_r("select_one sys_scale", r["item_id"], r["text"], r["group"], "yes", appearance="horizontal-compact"))
                s.append(_r("text", r["item_id"] + "_ev", "Evidence / observation (optional)"))
            ids = list(d_items["item_id"])
            s += [_r("calculate", f"spts_{tid}", calc=" + ".join(f"if(${{{i}}} = '2', 2, if(${{{i}}} = '1', 1, 0))" for i in ids)),
                  _r("calculate", f"sapp_{tid}", calc=" + ".join(f"if(${{{i}}} = 'NA', 0, 1)" for i in ids)),
                  _r("calculate", f"spct_{tid}", calc=f"if(${{sapp_{tid}}} > 0, round(${{spts_{tid}}} * 100 div (2 * ${{sapp_{tid}}}), 1), '')"),
                  _r("note", f"snote_{tid}", "Systems score: ${spct_" + tid + "}%", relevant=f"${{spct_{tid}}} != ''"),
                  _r("end_group")]
        if not e_items.empty:
            s.append(_r("begin_group", f"grp_ind_{tid}", f"Part E - Data verification and performance review ({t})", relevant=rel,
                        hint="Recount from the primary register. Leave a value blank if it is not available (do not enter zero). "
                             "Refer any coverage above 100% to the M&E team."))
            for _, r in e_items.iterrows():
                i = r["item_id"]
                den = mentorship.has_denominator(r["den_def"])
                s.append(_r("begin_group", f"grp_{i}", r["text"], hint=f"Benchmark: {r['benchmark']}"))
                s.append(_r("decimal", f"{i}_num", f"Numerator - {r['num_def']}", constraint=". >= 0", cmsg="Enter 0 or a positive number."))
                if den:
                    s.append(_r("decimal", f"{i}_den", f"Denominator - {r['den_def']}", constraint=". >= 0", cmsg="Enter 0 or a positive number."))
                    s.append(_r("calculate", f"{i}_pct", calc=f"if(${{{i}_den}} > 0 and ${{{i}_num}} != '', round(${{{i}_num}} * 100 div ${{{i}_den}}, 1), '')"))
                    s.append(_r("note", f"{i}_pctnote", "Calculated: ${" + f"{i}_pct" + "}%", relevant=f"${{{i}_pct}} != ''"))
                s.append(_r("decimal", f"{i}_rep", "Reported in DHIS2 (optional)", constraint=". >= 0", cmsg="Enter 0 or a positive number."))
                s.append(_r("calculate", f"{i}_var", calc=f"if(${{{i}_num}} > 0 and ${{{i}_rep}} != '', round((${{{i}_rep}} - ${{{i}_num}}) * 100 div ${{{i}_num}}, 1), '')"))
                s.append(_r("note", f"{i}_varnote", "Variance (reported vs recount): ${" + f"{i}_var" + "}%  - above 5% needs a correction plan within ten working days.",
                            relevant=f"${{{i}_var}} != ''"))
                s.append(_r("select_one yes_no", f"{i}_met", "Benchmark met?"))
                s.append(_r("end_group"))
            s.append(_r("end_group"))
        if not f_cols.empty:
            s.append(_r("begin_group", f"grp_rec_{tid}", f"Part F - Record / client file review ({t})", relevant=rel,
                        hint="Review the 10 most recent records, or select systematically. One row per record (use the + button)."))
            s.append(_r("begin_repeat", f"rep_rec_{tid}", "Records reviewed"))
            for _, r in f_cols.iterrows():
                if r["group"] == "yesno":
                    s.append(_r("select_one yes_no", r["item_id"], r["text"]))
                elif r["group"] == "number":
                    s.append(_r("integer", r["item_id"], r["text"], constraint=". >= 0", cmsg="Enter 0 or a positive whole number."))
                else:
                    s.append(_r("text", r["item_id"], r["text"]))
            s += [_r("end_repeat"), _r("end_group")]
        pr = info[info["group"] == "priority"]["text"].tolist()
        if pr and pr[0]:
            s.append(_r("note", f"priority_{tid}", "Priority issues to probe: " + pr[0], relevant=rel))
    s += [
        _r("begin_group", "grp_record_review", "Record review summary"),
        _r("integer", "records_met", "Records fully meeting the standard", constraint=". >= 0", cmsg="Enter 0 or more."),
        _r("integer", "records_total", "Records reviewed", constraint=". >= 0", cmsg="Enter 0 or more."),
        _r("text", "record_observation", "Key observation", appearance=ML),
        _r("end_group"),
        _r("begin_group", "grp_mentorship_session", "Part G - Mentorship session record"),
        _r("text", "skills_mentored", "Skills / topics mentored", appearance=ML),
        _r("select_multiple mentor_methods", "mentor_methods_used", "Mentorship methods used"),
        _r("integer", "mentored_female", "Number mentored - female", constraint=". >= 0", cmsg="Enter 0 or more."),
        _r("integer", "mentored_male", "Number mentored - male", constraint=". >= 0", cmsg="Enter 0 or more."),
        _r("decimal", "session_hours", "Duration of the session (hours)", constraint=". >= 0", cmsg="Enter 0 or more."),
        _r("text", "job_aids", "Job aids / SOPs left at the facility", appearance=ML),
        _r("text", "good_practices", "Good practices observed", appearance=ML),
        _r("end_group"),
        _r("begin_group", "grp_qi", "Part I - Link to quality improvement"),
        _r("select_one qi_status", "qi_status", "QI project in this area"),
        _r("text", "qi_aim", "Aim statement", appearance=ML),
        _r("text", "qi_baseline", "Baseline (value and date)"),
        _r("decimal", "qi_value", "Current value (%)", constraint=". >= 0", cmsg="Enter 0 or more."),
        _r("integer", "qi_points", "Data points plotted on the run chart", constraint=". >= 0", cmsg="Enter 0 or more."),
        _r("select_one yes_no", "qi_run_chart", "Run chart updated this month?"),
        _r("text", "qi_change_idea", "Change idea being tested (PDSA)", appearance=ML),
        _r("text", "qi_coach", "Coach comment", appearance=ML),
        _r("end_group"),
        _r("begin_group", "grp_rating", "Part J - Overall rating"),
        _r("select_one ratings", "overall_rating", "Overall facility rating for this technical area"),
        _r("end_group"),
        _r("begin_group", "grp_signoff", "Part K - Sign-off"),
        _r("image", "incharge_signature", "Facility in-charge signature (optional)", "Confirms the findings were discussed and the gap closure plan agreed.",
           appearance="signature"),
        _r("end_group"),
    ]
    return s


def build_xlsform(ipi_df: pd.DataFrame, routine_df: pd.DataFrame, items_df: pd.DataFrame | None = None,
                  parts_df: pd.DataFrame | None = None, form_title: str = "Support Supervision Visit", form_id: str = "visit_tools") -> bytes:
    """An .xlsx XLSForm ready to upload to Kobo — the whole visit in one form:
       1-5  the visit tools (attendance, RCA — repeatable, action plan, IPI mapping, routine indicators)
       then "Continue to the competency assessment?"  -> one block per health worker, from the checklist
       then "Continue to the support supervision report?" -> the report, with location and photos."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    ipi = _active(normalize_list(ipi_df, IPI_COLS))
    routine = _active(normalize_list(routine_df, ROUTINE_COLS))
    if ipi.empty:
        raise ValueError("There are no active IPI indicators to build the form from.")
    errs = validate_list(ipi, IPI_COLS, "IPI") + (validate_list(routine, ROUTINE_COLS, "Routine indicator") if not routine.empty else [])
    comp_items = pd.DataFrame(columns=competency.ITEM_COLS)
    if items_df is not None and not items_df.empty:
        comp_items = competency.active_items(competency.normalize_items(items_df))
        errs += competency.validate_items(comp_items) if not comp_items.empty else []
    if errs:
        raise ValueError("; ".join(errs[:5]))
    include_comp = not comp_items.empty
    parts = mentorship.normalize_parts(parts_df) if parts_df is not None else mentorship.normalize_parts(pd.DataFrame())
    parts = parts[(parts["active"] != "no") & parts["part"].isin(["D", "E", "F"])]
    topic_titles = list(dict.fromkeys(parts["topic"]))             # topics that have Parts D/E/F

    ML = "multiline"
    s = [
        _r("text", "facility", "Facility", "Filled in automatically when opened from the dashboard — correct it if needed.", "yes"),
        _r("text", "visit_ref", "Visit reference", "Filled in automatically. It ties together everything recorded in this visit — leave it as it is."),
        _r("date", "visit_date", "Date of the visit", required="yes", default="today()"),
        _r("text", "data_element", "Data element / service area of the visit (optional)", "Filled in automatically when opened from the dashboard."),
        _r("text", "prev_actions", "Previous visit's action plan (for reference)",
           "Filled in automatically. Mark the status of each action in the follow-up table under the action plan (section 3).",
           relevant="${prev_actions} != ''", appearance="multiline", ro="yes"),
        _r("text", "assessor", "Your name", required="yes"),

        # ---- Part A: visit information (mentorship tool)
        _r("begin_group", "grp_visit_info", "Visit information"),
        _r("select_one divisions", "kcca_division", "KCCA division"),
        _r("select_one facility_levels", "facility_level", "Facility level"),
        _r("select_one visit_types", "visit_type", "Visit type"),
        _r("integer", "followup_no", "Follow-up visit number", relevant="${visit_type} = 'follow_up'", constraint=". >= 1", cmsg="Enter 1 or more."),
        _r("select_one visit_modes", "visit_mode", "Mode of the visit"),
        _r("text", "units_visited", "Unit(s) visited"),
        _r("select_one yes_no", "priority_facility", "Priority facility for this area?"),
        _r("text", "lead_mentor", "Lead mentor (ROM)"),
        _r("text", "co_mentor", "Co-mentor (KCCA / division)"),
        _r("text", "facility_in_charge", "Facility in-charge"),
        _r("time", "time_in", "Time in"),
        _r("time", "time_out", "Time out"),
        _r("end_group"),

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

        # ---- 2. root cause analysis — repeatable: each entry is its own analysis with its own method
        _r("begin_group", "grp_rca", "2. Root cause analysis"),
        _r("begin_repeat", "rep_rca", "Root cause analyses",
           "Add one entry per problem. Use the + button to add another 5 Whys, Fishbone or any other analysis."),
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
        _r("text", "tree_direct_causes", "Direct causes", "One per line.", appearance=ML),
        _r("text", "tree_root_causes", "Root causes", "One per line.", appearance=ML),
        _r("text", "tree_effects", "Effects of the problem", "One per line.", appearance=ML),
        _r("text", "rca_tree_root", "Root cause(s) you will act on", required="yes", appearance=ML),
        _r("end_group"),
        _r("begin_group", "grp_rca_other", "Other method", relevant="${rca_method} = 'other'"),
        _r("text", "rca_other_name", "Name of the method used", required="yes"),
        _r("text", "rca_other_notes", "Findings / analysis", required="yes", appearance=ML),
        _r("text", "rca_other_root", "Root cause(s) identified", required="yes", appearance=ML),
        _r("end_group"),
        _r("calculate", "rca_root_cause", calc="if(${rca_method} = 'five_whys', ${rca_5whys_root}, if(${rca_method} = 'fishbone', "
                                               "${rca_fb_root}, if(${rca_method} = 'problem_tree', ${rca_tree_root}, ${rca_other_root})))"),
        _r("end_repeat"),
        _r("end_group"),

        # ---- 3. action plan matrix
        _r("begin_group", "grp_actions", "3. Action plan matrix"),
        _r("begin_repeat", "rep_followup", "Follow-up of the previous visit's action plan",
           "Copy each action agreed at the last visit and mark its status. Skip this on a first visit."),
        _r("text", "fu_action", "Action agreed at the last visit", required="yes", appearance=ML),
        _r("text", "fu_responsible", "Responsible"),
        _r("date", "fu_due", "Due date"),
        _r("select_one followup_status", "fu_status", "Status", required="yes"),
        _r("text", "fu_comment", "Evidence / comment", appearance=ML),
        _r("end_repeat"),
        _r("begin_repeat", "rep_actions", "Action plan (gaps, root causes and gap closure plan)", "One row per challenge (use the + button)."),
        _r("text", "act_challenge", "Identified challenge", required="yes", appearance=ML),
        _r("text", "act_action", "Corrective action", required="yes", appearance=ML),
        _r("text", "act_responsible", "Responsible person", required="yes"),
        _r("date", "act_timeline", "Timeline (deadline)", required="yes"),
        _r("select_one escalate_to", "act_escalate", "Escalate to (if it needs support above the facility)"),
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
    ]

    # ---- ask: continue to the competency assessment?
    if include_comp:
        s += [
            _r("begin_group", "grp_next_comp", "The visit tools are complete"),
            _r("select_one yes_no", "continue_competency", "Do you want to continue to the competency assessment now?",
               "Choose No if you will do the competency assessment in the app, or at another time.", required="yes"),
            _r("end_group"),
            _r("begin_group", "grp_competency", "6. Competency assessment", relevant="${continue_competency} = 'yes'"),
            _r("begin_repeat", "rep_competency", "Health workers assessed",
               "Assess each health worker separately — use the + button to add the next health worker."),
            _r("text", "mentee_name", "Health worker being assessed (full name)", required="yes"),
            _r("select_one cadres", "cadre", "Cadre", required="yes"),
            _r("text", "mentee_unit", "Unit / department"),
            _r("text", "mentee_phone", "Telephone", constraint="regex(., '^[+]?[0-9 ]{9,15}$')", cmsg="Enter a phone number with 9-15 digits."),
            _r("select_one yes_no", "on_cohort", "On the mentorship cohort list?"),
            _r("select_one service_areas", "service_area", "Topic / service area assessed", "Choose the topic - its competency tool is shown below.", required="yes"),
            _r("select_one assessment_types", "assessment_type", "Assessment type", "Baseline = before mentorship starts.", required="yes"),
        ]
        areas = list(dict.fromkeys(comp_items["service_area"]))
        for area in areas:
            aid = competency.slug(area)
            s.append(_r("begin_group", f"grp_comp_{aid}", area, relevant=f"${{service_area}} = '{aid}'"))
            for _, it in comp_items[comp_items["service_area"] == area].iterrows():
                s.append(_r("select_one score_scale", it["item_id"], it["competency"], it["domain"], "yes", appearance="horizontal-compact"))
            ids = list(comp_items.loc[comp_items["service_area"] == area, "item_id"])
            s += [_r("calculate", f"cpts_{aid}", calc=" + ".join(f"if(${{{i}}} = '2', 2, if(${{{i}}} = '1', 1, 0))" for i in ids)),
                  _r("calculate", f"capp_{aid}", calc=" + ".join(f"if(${{{i}}} = 'NA', 0, 1)" for i in ids)),
                  _r("calculate", f"cpct_{aid}", calc=f"if(${{capp_{aid}}} > 0, round(${{cpts_{aid}}} * 100 div (2 * ${{capp_{aid}}}), 1), '')")]
            s.append(_r("end_group"))
        pct_expr = "''"
        for area in reversed(areas):
            pct_expr = f"if(${{service_area}} = '{competency.slug(area)}', ${{cpct_{competency.slug(area)}}}, {pct_expr})"
        s += [
            _r("calculate", "comp_pct", calc=pct_expr),
            _r("calculate", "comp_rating", calc="if(${comp_pct} = '', '', if(${comp_pct} >= 85, 'Green', if(${comp_pct} >= 70, 'Amber', 'Red')))"),
            _r("note", "comp_note", "Competency score for this health worker: ${comp_pct}%  -  ${comp_rating}", relevant="${comp_pct} != ''"),
            _r("text", "comp_comments", "Comments on this health worker (optional)", appearance=ML),
            _r("end_repeat"),
            _r("end_group"),
        ]

    # ---- ask: continue to the support supervision report?
    s += [
        _r("begin_group", "grp_next_ssm", "Next"),
        _r("select_one yes_no", "continue_ssm",
           "Do you want to continue to the support supervision / mentorship report now?" if include_comp else
           "Do you want to continue to the support supervision / mentorship report now?",
           "Choose No if you will fill in the report in the app, or at another time.", required="yes"),
        _r("end_group"),
        _r("begin_group", "grp_ssm", "7. Support supervision / mentorship report", relevant="${continue_ssm} = 'yes'"),
        _r("text", "focus_area", "Specific focus area of the visit"),
    ] + _topic_blocks(parts, topic_titles) + [
        _r("text", "findings", "Key findings / observations", appearance=ML),
        _r("text", "mentorship_given", "Mentorship / on-the-job training provided", appearance=ML),
        _r("integer", "ssm_score", "Overall performance score (0-100)",
           "Leave blank if you assessed competency — the score is filled in automatically from the average.",
           constraint=". >= 0 and . <= 100", cmsg="Enter a whole number from 0 to 100."),
        _r("text", "recommendations", "Recommendations", appearance=ML),
        _r("text", "responsible_person", "Facility focal person"),
        _r("date", "follow_up_date", "Follow-up visit date"),
        _r("geopoint", "location", "Location of this visit", "Record the GPS location while you are at the facility."),
        _r("image", "photo_1", "Photo 1 of the visit"),
        _r("image", "photo_2", "Photo 2 of the visit"),
        _r("image", "photo_3", "Photo 3 of the visit"),
        _r("end_group"),
        _r("text", "comments", "Any other comments", appearance=ML),
    ]

    names = {row[1] for row in s if row[1] and row[0] != "select_one score_scale"}     # every question except the checklist items
    clash = sorted(set(comp_items["item_id"]) & names) if include_comp else []
    if clash:
        raise ValueError("These competency item_ids clash with questions in the visit form — rename them: " + ", ".join(clash))

    ch = [("cadres", competency.slug(c), c) for c in competency.CADRES]
    ch += [("divisions", competency.slug(d), d) for d in DIVISIONS]
    ch += [("facility_levels", c, l) for c, l in FACILITY_LEVELS] + [("visit_types", c, l) for c, l in VISIT_TYPES]
    ch += [("visit_modes", c, l) for c, l in VISIT_MODES] + [("followup_status", c, l) for c, l in FOLLOWUP_STATUS]
    ch += [("escalate_to", c, l) for c, l in ESCALATE] + [("mentor_methods", c, l) for c, l in MENTOR_METHODS]
    ch += [("qi_status", c, l) for c, l in QI_STATUS] + [("ratings", c, l) for c, l in RATINGS]
    ch += [("topics", competency.slug(t), t) for t in topic_titles]
    if topic_titles:
        ch += [("sys_scale", c, l) for c, l in SYS_SCALE]
    ch += [("attendance_methods", c, l) for c, l in ATTENDANCE_METHODS]
    ch += [("rca_methods", c, l) for c, l in RCA_METHODS]
    ch += [("yes_no", c, l) for c, l in YES_NO]
    ch += [("ipi_indicators", r["ipi_id"], r["indicator"]) for _, r in ipi.iterrows()]
    ch += [("routine_indicators", r["indicator_id"], r["indicator"]) for _, r in routine.iterrows()]
    ch += [("routine_indicators", "other", "Other (type the name)")]
    if include_comp:
        ch += [("service_areas", competency.slug(a), a) for a in areas]
        ch += [("assessment_types", c, l) for c, l in competency.ASSESSMENT_TYPES]
        ch += [("score_scale", c, l) for c, l, _ in competency.SCALE]

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
    for ws, widths in ((sv, [26, 24, 52, 40, 10, 34, 12, 20, 34, 34, 40]), (cs, [20, 30, 46]), (st_, [34, 20])):
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


def _rca_entries(sub: dict, flat: dict) -> list:
    """Each root cause analysis of the visit (the RCA section repeats). Older single-analysis
    submissions are handled too."""
    entries = _repeat(sub, "rep_rca")
    if not entries and _txt(flat.get("rca_method")):
        entries = [flat]
    return entries


def submission_to_tables(sub: dict, links_fn=None, ipi_labels: dict | None = None,
                         routine_labels: dict | None = None, parts_df: pd.DataFrame | None = None) -> dict:
    """Turn one visit submission into rows for the visit-tool tabs: {tab_name: [row dict, ...]}.
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

    # ---- root cause analyses: one numbered analysis per entry
    rca, methods, problems, roots, n_rca = [], [], [], [], 0
    for e in _rca_entries(sub, flat):
        method = _txt(e.get("rca_method"))
        label = RCA_LABEL.get(method, method)
        problem = _txt(e.get("rca_problem"))
        if not (method or problem):
            continue
        n_rca += 1

        def add(section, item, text, _n=n_rca, _label=label):
            if _txt(text):
                rca.append({**base, "method": _label, "section": section, "item": item, "text": _txt(text), "analysis_no": _n})

        add("Problem", "Problem statement", problem)
        if method == "five_whys":
            for i in range(1, 6):
                add("5 Whys", f"Why {i}", e.get(f"why_{i}"))
            add("5 Whys", "Root cause", e.get("rca_5whys_root"))
        elif method == "fishbone":
            for name, flabel in FISHBONE:
                add("Fishbone", flabel, e.get(name))
            add("Fishbone", "Root cause(s)", e.get("rca_fb_root"))
        elif method == "problem_tree":
            add("Problem tree", "Direct causes", e.get("tree_direct_causes"))
            add("Problem tree", "Root causes", e.get("tree_root_causes"))
            add("Problem tree", "Effects", e.get("tree_effects"))
            add("Problem tree", "Root cause(s) to act on", e.get("rca_tree_root"))
        elif method == "other":
            add("Other method", _txt(e.get("rca_other_name")) or "Method", e.get("rca_other_notes"))
            add("Other method", "Root cause(s)", e.get("rca_other_root"))
        root = _txt(e.get("rca_root_cause"))
        for key in ("rca_5whys_root", "rca_fb_root", "rca_tree_root", "rca_other_root"):
            root = root or _txt(e.get(key))
        if label:
            methods.append(label)
        if problem:
            problems.append(problem)
        if root:
            roots.append(root)

    actions = []
    for e in _repeat(sub, "rep_actions"):
        if any(_txt(e.get(k)) for k in ("act_challenge", "act_action")):
            actions.append({**base, "challenge": _txt(e.get("act_challenge")), "corrective_action": _txt(e.get("act_action")),
                            "responsible_person": _txt(e.get("act_responsible")), "timeline": _txt(e.get("act_timeline"))[:10],
                            "escalate_to": dict(ESCALATE).get(_txt(e.get("act_escalate")), _txt(e.get("act_escalate")))})

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

    followup = []
    for e in _repeat(sub, "rep_followup"):
        if _txt(e.get("fu_action")):
            followup.append({**base, "action": _txt(e.get("fu_action")), "responsible": _txt(e.get("fu_responsible")),
                             "due_date": _txt(e.get("fu_due"))[:10],
                             "status": dict(FOLLOWUP_STATUS).get(_txt(e.get("fu_status")), _txt(e.get("fu_status"))),
                             "comment": _txt(e.get("fu_comment"))})

    uploads = [_txt(flat.get(f)) for f in UPLOAD_FIELDS if _txt(flat.get(f))]
    main = {**base, "assessor": _txt(flat.get("assessor")) or str(sub.get("_submitted_by") or ""),
            "attendance_method": ATT_LABEL.get(_txt(flat.get("attendance_method")), _txt(flat.get("attendance_method"))),
            "attendee_count": len(attendance), "attendance_upload_links": LINK_SEP.join(links_fn(uploads)),
            "rca_method": ", ".join(dict.fromkeys(methods)), "rca_problem": " | ".join(problems),
            "rca_root_cause": " | ".join(roots), "rca_count": n_rca,
            "action_count": len(actions), "ipi_count": len(ipi), "routine_count": len(routine),
            "comments": _txt(flat.get("comments"))}
    lab = {"kcca_division": {competency.slug(d): d for d in DIVISIONS}, "facility_level": dict(FACILITY_LEVELS),
           "visit_type": dict(VISIT_TYPES), "visit_mode": dict(VISIT_MODES), "priority_facility": dict(YES_NO)}
    for k in PART_A_COLS:
        raw = _txt(flat.get(k))
        main[k] = lab.get(k, {}).get(raw, raw)
    if main["visit_type"] == "Follow-up" and _txt(flat.get("followup_no")):
        main["visit_type"] = f"Follow-up no. {_txt(flat.get('followup_no'))}"
    out = {MAIN_TAB: [main], "VisitAttendance": attendance, "VisitRCA": rca, "VisitActions": actions,
           "VisitIPI": ipi, "VisitRoutine": routine, "VisitFollowUp": followup}
    out.update(topic_tables(sub, base, parts_df, links_fn))
    return out


def _num(x):
    try:
        return float(str(x).strip())
    except ValueError:
        return None


def topic_tables(sub: dict, base: dict, parts_df, links_fn=None) -> dict:
    """Parts D, E, F, G, I, J of the mentorship tool for the topic chosen in the report: rows for
    VisitSystems, VisitIndicators, VisitRecords and one VisitSession row."""
    out = {"VisitSystems": [], "VisitIndicators": [], "VisitRecords": [], "VisitSession": []}
    flat = _flat(sub)
    if _txt(flat.get("continue_ssm")) != "yes":
        return out
    parts = mentorship.normalize_parts(parts_df) if parts_df is not None else mentorship.normalize_parts(pd.DataFrame())
    tslug = _txt(flat.get("ssm_topic"))
    topics = {competency.slug(t): t for t in parts["topic"].unique()}
    topic = topics.get(tslug, "")
    sys_pct = None
    if topic:
        pt = parts[parts["topic"] == topic]
        scores = []
        for _, r in pt[pt["part"] == "D"].iterrows():
            sc = _txt(flat.get(r["item_id"])).upper()
            if sc == "":
                continue
            scores.append(sc)
            out["VisitSystems"].append({**base, "topic": topic, "group": r["group"], "standard": r["text"],
                                        "score": SYS_LABEL.get(sc, sc), "evidence": _txt(flat.get(r["item_id"] + "_ev"))})
        sys_pct = mentorship.pct_from_scores(scores)
        for _, r in pt[pt["part"] == "E"].iterrows():
            i = r["item_id"]
            num, den, rep = _num(flat.get(f"{i}_num")), _num(flat.get(f"{i}_den")), _num(flat.get(f"{i}_rep"))
            met = _txt(flat.get(f"{i}_met"))
            if num is None and den is None and rep is None and not met:
                continue
            pct = round(num * 100 / den, 1) if (num is not None and den) else ""
            var = round((rep - num) * 100 / num, 1) if (rep is not None and num) else ""
            out["VisitIndicators"].append({**base, "topic": topic, "indicator": r["text"],
                                           "numerator": "" if num is None else num, "denominator": "" if den is None else den,
                                           "pct": pct, "benchmark": r["benchmark"], "reported_dhis2": "" if rep is None else rep,
                                           "variance_pct": var, "met": dict(YES_NO).get(met, met)})
        cols = pt[pt["part"] == "F"].reset_index(drop=True)
        for n, e in enumerate(_repeat(sub, f"rep_rec_{tslug}"), 1):
            if not any(_txt(v_) for v_ in e.values()):
                continue
            cid = _txt(e.get(cols.loc[0, "item_id"])) if not cols.empty else ""
            det = "; ".join(f"{c['text']}={dict(YES_NO).get(_txt(e.get(c['item_id'])), _txt(e.get(c['item_id'])))}"
                            for _, c in cols.iloc[1:].iterrows() if _txt(e.get(c["item_id"])))
            out["VisitRecords"].append({**base, "topic": topic, "record_no": n, "client_id": cid, "details": det})
    met_, tot_ = _num(flat.get("records_met")), _num(flat.get("records_total"))
    methods = [dict(MENTOR_METHODS).get(m, m) for m in _txt(flat.get("mentor_methods_used")).split()]
    row = {**base, "topic": topic, "systems_pct": "" if sys_pct is None else sys_pct, "systems_rating": mentorship.rating(sys_pct),
           "records_met": "" if met_ is None else int(met_), "records_total": "" if tot_ is None else int(tot_),
           "records_pct": round(met_ * 100 / tot_, 1) if (met_ is not None and tot_) else "",
           "record_observation": _txt(flat.get("record_observation")), "skills_mentored": _txt(flat.get("skills_mentored")),
           "methods": ", ".join(methods), "female": _txt(flat.get("mentored_female")), "male": _txt(flat.get("mentored_male")),
           "hours": _txt(flat.get("session_hours")), "job_aids": _txt(flat.get("job_aids")),
           "good_practices": _txt(flat.get("good_practices")),
           "qi_status": dict(QI_STATUS).get(_txt(flat.get("qi_status")), _txt(flat.get("qi_status"))),
           "qi_aim": _txt(flat.get("qi_aim")), "qi_baseline": _txt(flat.get("qi_baseline")), "qi_value": _txt(flat.get("qi_value")),
           "qi_points": _txt(flat.get("qi_points")), "qi_run_chart": dict(YES_NO).get(_txt(flat.get("qi_run_chart")), _txt(flat.get("qi_run_chart"))),
           "qi_change_idea": _txt(flat.get("qi_change_idea")), "qi_coach": _txt(flat.get("qi_coach")),
           "overall_rating": _txt(flat.get("overall_rating"))}
    if topic or any(_txt(row[k]) for k in ("skills_mentored", "good_practices", "qi_aim", "overall_rating", "job_aids")):
        out["VisitSession"].append(row)
    return out


def competency_rows_from_visit(sub: dict, items_df: pd.DataFrame):
    """Competency assessments done inside the visit form (one block per health worker) as rows for
    the Competency tab. Each assessment gets the id '<submission>#<n>' so one can be deleted without
    affecting the others. Returns (rows, entries_with_no_recognised_answers)."""
    flat = _flat(sub)
    sid = str(sub.get("_uuid") or sub.get("_id") or "").strip()
    when = str(sub.get("_submission_time", ""))
    rows, empty = [], 0
    for i, e in enumerate(_repeat(sub, "rep_competency"), 1):
        if not any(_txt(v) for v in e.values()):
            continue
        pseudo = {**e, "_uuid": f"{sid}#{i}", "_submission_time": when, "_submitted_by": sub.get("_submitted_by"),
                  "facility": flat.get("facility", ""), "visit_ref": flat.get("visit_ref", ""),
                  "assessment_date": _txt(flat.get("visit_date")) or when[:10],
                  "assessor": flat.get("assessor", ""), "comments": e.get("comp_comments", "")}
        got = competency.submission_to_rows(pseudo, items_df)
        if got:
            rows.extend(got)
        else:
            empty += 1
    return rows, empty


def competency_average(rows: list):
    """Average % competent across the assessments in `rows`, or None when nothing was scored."""
    by = {}
    for r in rows:
        by.setdefault(r["assessment_id"], []).append(competency.SCORE_VALUE.get(str(r["score"]).strip().upper()))
    pcts = []
    for vals in by.values():
        scored = [x for x in vals if x is not None]
        if scored:
            pcts.append(sum(scored) / (2 * len(scored)) * 100)       # points / (2 x applicable items)
    return sum(pcts) / len(pcts) if pcts else None


def ssm_row_from_visit(sub: dict, links_fn, avg_pct, loc=("", "", ""), attendance_links: str = ""):
    """The support supervision report part of the visit as a row for the SSM tab (or None if the
    supervisor chose not to continue to it). A blank score is filled with the competency average."""
    flat = _flat(sub)
    fields = ("focus_area", "findings", "mentorship_given", "recommendations", "ssm_score")
    if _txt(flat.get("continue_ssm")) != "yes" and not any(_txt(flat.get(k)) for k in fields):
        return None
    score = _txt(flat.get("ssm_score"))
    if not score and avg_pct is not None:
        score = str(competency.score_from_pct(avg_pct))
    photos = [_txt(flat.get(f)) for f in SSM_PHOTO_FIELDS if _txt(flat.get(f))]
    return {"facility": _txt(flat.get("facility")), "data_element": _txt(flat.get("data_element")),
            "focus_area": _txt(flat.get("focus_area")), "findings": _txt(flat.get("findings")),
            "mentorship_given": _txt(flat.get("mentorship_given")), "score": score,
            "recommendations": _txt(flat.get("recommendations")), "responsible_person": _txt(flat.get("responsible_person")),
            "follow_up_date": _txt(flat.get("follow_up_date"))[:10],
            "submitted_by": _txt(flat.get("assessor")) or str(sub.get("_submitted_by") or ""),
            "timestamp": str(sub.get("_submission_time", "")),
            "kobo_id": str(sub.get("_uuid") or sub.get("_id") or "").strip(),
            "latitude": loc[0], "longitude": loc[1], "gps_accuracy_m": loc[2],
            "photo_links": LINK_SEP.join(links_fn(photos)), "attendance_links": attendance_links,
            "visit_ref": _txt(flat.get("visit_ref"))}


# ---------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Previous visit's action plan: shown in the Kobo form and tracked in the app
# ---------------------------------------------------------------------
STATUS_LABEL = dict(FOLLOWUP_STATUS)
NO_PREVIOUS = "No previous action plan for this facility."


def action_key(kobo_id, n) -> str:
    return f"{kobo_id}#{n}"


def previous_plans(actions_df, status_df=None, followup_df=None) -> pd.DataFrame:
    """Every action of every visit, numbered per visit, with its latest status: set in the app
    (ActionStatus) if any, else what a later visit recorded in its follow-up table, else 'Not yet reviewed'."""
    cols = ["action_key", "kobo_id", "visit_ref", "facility", "visit_date", "n", "challenge", "corrective_action",
            "responsible_person", "timeline", "status", "comment"]
    if actions_df is None or actions_df.empty:
        return pd.DataFrame(columns=cols)
    a = actions_df.copy().reset_index(drop=True)
    a["n"] = a.groupby("kobo_id").cumcount() + 1
    a["action_key"] = [action_key(k, n) for k, n in zip(a["kobo_id"], a["n"])]
    st_map, cm_map = {}, {}
    if status_df is not None and not status_df.empty:
        for _, r in status_df.iterrows():                      # later rows win
            st_map[str(r["action_key"])] = str(r["status"])
            cm_map[str(r["action_key"])] = str(r.get("comment", ""))
    fu_map = {}
    if followup_df is not None and not followup_df.empty:
        for _, r in followup_df.sort_values("visit_date").iterrows():
            fu_map[(str(r["facility"]).strip().lower(), str(r["action"]).strip().lower())] = (str(r["status"]), str(r.get("comment", "")))
    sts, cms = [], []
    for _, r in a.iterrows():
        k = r["action_key"]
        if k in st_map and st_map[k]:
            sts.append(st_map[k]); cms.append(cm_map.get(k, ""))
            continue
        fk = str(r["facility"]).strip().lower()
        hit = fu_map.get((fk, str(r["corrective_action"]).strip().lower())) or fu_map.get((fk, str(r["challenge"]).strip().lower()))
        sts.append(hit[0] if hit else "Not yet reviewed"); cms.append(hit[1] if hit else "")
    a["status"], a["comment"] = sts, cms
    return a.reindex(columns=cols)


def latest_plan(plans: pd.DataFrame, facility: str, exclude_ref: str = "") -> pd.DataFrame:
    """Actions of the facility's most recent earlier visit that had an action plan (empty if none)."""
    if plans is None or plans.empty:
        return pd.DataFrame(columns=plans.columns if plans is not None else [])
    p = plans[plans["facility"].astype(str).str.strip().str.lower() == str(facility).strip().lower()]
    if exclude_ref:
        p = p[p["visit_ref"].astype(str) != str(exclude_ref)]
    if p.empty:
        return p
    p = p.assign(_d=pd.to_datetime(p["visit_date"], errors="coerce"))
    last = p.sort_values(["_d", "kobo_id"]).iloc[-1]["kobo_id"]
    return p[p["kobo_id"] == last].drop(columns="_d")


def plan_text(plan: pd.DataFrame, max_items: int = 12, max_chars: int = 1500) -> str:
    """The note shown at the top of the Kobo form."""
    if plan is None or plan.empty:
        return NO_PREVIOUS
    first = plan.iloc[0]
    lines = [f"Previous visit: {str(first['visit_date'])[:10]}" + (f" ({first['visit_ref']})" if str(first["visit_ref"]) else "")]
    rows = list(plan.itertuples())
    for r in rows[:max_items]:
        due = str(r.timeline)[:10]
        lines.append(f"{r.n}. {r.challenge} -> {r.corrective_action} | {r.responsible_person or 'no owner'} | due {due or 'no date'} | {r.status}")
    if len(rows) > max_items:
        lines.append(f"... and {len(rows) - max_items} more (see the app).")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars - 30].rstrip() + "\n... (cut short, see the app)"


def previous_actions_note(facility: str, exclude_ref: str = "") -> str:
    """Text for the form's read-only 'previous actions' box, or '' if the Sheet can't be read."""
    try:
        plans = previous_plans(gsheets.get_table("VisitActions"), gsheets.get_table("ActionStatus"), gsheets.get_table("VisitFollowUp"))
        return plan_text(latest_plan(plans, facility, exclude_ref))
    except Exception:
        return ""


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