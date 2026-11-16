"""
mentorship.py
-------------
Topic-based mentorship and competency assessment tools (e.g. "MT-01 HIV Testing Services and Prevention").

Each topic is a Word (.docx) tool laid out in Parts A-K. `parse_docx` reads one and returns a plain
dict; `to_library_rows` turns it into rows for two Sheet tabs:

  * CompetencyItems  - Part C (the health worker competency checklist), so the existing competency
                       page / Kobo flow keeps working. The topic is the item's `service_area`.
  * TopicParts       - Parts D (facility systems), E (data verification indicators) and F (record
                       review columns), plus the topic's scope / priority issues.

The Kobo form then shows the right tool after the supervisor picks the topic.
"""
import io
import re

import pandas as pd

import competency

PART_COLS = ["topic", "part", "item_id", "group", "text", "method", "num_def", "den_def", "benchmark", "active"]
PARTS_LABEL = {"C": "Health worker competency", "D": "Facility systems and service readiness",
               "E": "Data verification and performance review", "F": "Record / client file review"}
EMPTY_DASH = {"", "-", "—", "–", "n/a", "na"}


# ---------------------------------------------------------------------
# Scoring helpers shared with the Kobo form and the sync
# ---------------------------------------------------------------------
def rating(pct) -> str:
    """Performance rating from the tool: Green >=85, Amber 70-84, Red <70."""
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return ""
    if p != p:
        return ""
    return "Green" if p >= 85 else ("Amber" if p >= 70 else "Red")


def pct_from_scores(scores) -> float | None:
    """Tool formula: points / (2 x applicable items) x 100. `scores` are codes '2','1','0','NA' (or blanks)."""
    vals = [competency.SCORE_VALUE.get(str(s).strip().upper()) for s in scores if str(s).strip() != ""]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(sum(vals) / (2 * len(vals)) * 100, 1)


# ---------------------------------------------------------------------
# Reading a .docx tool
# ---------------------------------------------------------------------
def _clean(t) -> str:
    return re.sub(r"\s+", " ", str(t or "").replace("\n", " ")).strip()


def _row_cells(row) -> list:
    """Cell texts of a row with merged (repeated) cells collapsed."""
    out, prev = [], None
    for c in row.cells:
        if c._tc is prev:
            continue
        prev = c._tc
        out.append(_clean(c.text))
    return out


def _code_from_name(filename: str, title: str) -> str:
    m = re.match(r"(?:.*?[-_])?(MT[-_ ]?\d+)", filename or "", re.I)
    if m:
        return re.sub(r"[^a-z0-9]", "", m.group(1).lower())
    return competency.slug(title)[:12].strip("_") or "topic"


_SMALL = {"and", "of", "to", "in", "the", "for", "on", "a"}


def _title_from(text: str, filename: str = "") -> str:
    """The tool title. Titles are in capitals, so acronyms are recovered from the file name
    (MT-05_Immunisation_and_Polio_EPI.docx -> 'EPI')."""
    t = re.split(r"\s+[—–-]+\s+MENTORSHIP", text, flags=re.I)[0].strip()
    if not t.isupper():
        return t
    caps = {w.lower(): w for w in re.split(r"[^A-Za-z0-9]+", filename or "") if len(w) >= 2 and w.isupper() and not re.fullmatch(r"MT\d*", w)}
    caps.update({"hiv": "HIV", "anc": "ANC", "tb": "TB", "ahd": "AHD", "epi": "EPI", "ncd": "NCD", "mnch": "MNCH", "pmtct": "PMTCT",
                 "cqi": "CQI", "ipc": "IPC", "hts": "HTS", "art": "ART", "m&e": "M&E"})
    def fix(tok, first):
        m = re.fullmatch(r"([^A-Za-z0-9&]*)([A-Za-z0-9&]+)([^A-Za-z0-9&]*)", tok)
        if not m:
            return tok
        pre, w, post = m.groups()
        lw = w.lower()
        w2 = caps.get(lw) or (lw if (lw in _SMALL and not first) else lw.capitalize())
        return pre + w2 + post
    out = []
    for i, tok in enumerate(re.split(r"(\s+|/|-)", t)):
        out.append(tok if (not tok.strip() or tok in ("/", "-")) else fix(tok, i == 0))
    return "".join(out)


def _col_type(header: str) -> str:
    h = header.lower()
    if "(y/n)" in h or "yes/no" in h:
        return "text" if re.search(r"\(y/n\)\s*&|&\s*\w", h) else "yesno"     # "(Y/N) & value" needs a typed answer
    if "(n)" in h or "number" in h or re.search(r"\bn\b", h):
        return "number"
    return "text"


def parse_docx(source, filename: str = "") -> dict:
    """Read one mentorship tool. `source` is a path or bytes. Raises ValueError with a readable
    message if the layout isn't recognised."""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table

    doc = Document(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
    part, title, paras = "", "", []
    tables = []                      # (part, header cells, rows of cells)
    info, priority = {}, ""
    for el in doc.element.body.iterchildren():
        if el.tag == qn("w:p"):
            text = _clean("".join(t.text or "" for t in el.iter(qn("w:t"))))
            if not text:
                continue
            if not title:
                title = _title_from(text, filename)
                continue
            m = re.match(r"PART ([A-K])\b", text, re.I)
            if m:
                part = m.group(1).upper()
            elif text.lower().startswith("priority issues to probe"):
                priority = text.split(":", 1)[-1].strip()
        elif el.tag == qn("w:tbl"):
            tb = Table(el, doc)
            rows = [_row_cells(r) for r in tb.rows]
            if not rows:
                continue
            if not part and len(rows[0]) == 2 and rows[0][0] in ("Scope", "Mentees (cadres)"):
                info = {r[0]: r[1] for r in rows if len(r) >= 2}
            tables.append((part, rows))
    if not title:
        raise ValueError("The document has no title paragraph.")

    topic = {"title": title, "code": _code_from_name(filename, title),
             "scope": info.get("Scope", ""), "mentees": info.get("Mentees (cadres)", ""),
             "priority": priority, "C": [], "D": [], "E": [], "F": []}

    for p, rows in tables:
        head = [h.lower() for h in rows[0]]
        if p == "C" and len(head) >= 3 and head[0] == "#" and head[1].startswith("competency"):
            group = ""
            for r in rows[1:]:
                if len(r) == 1:
                    group = r[0]
                elif r and re.fullmatch(r"\d+", r[0]) and len(r) >= 2:
                    topic["C"].append({"n": int(r[0]), "group": group, "text": r[1], "method": r[2] if len(r) > 2 else ""})
        elif p == "D" and len(head) >= 2 and head[0] == "#" and head[1].startswith("standard"):
            group = ""
            for r in rows[1:]:
                if len(r) == 1:
                    group = r[0]
                elif r and re.fullmatch(r"\d+", r[0]) and len(r) >= 2:
                    topic["D"].append({"n": int(r[0]), "group": group, "text": r[1]})
        elif p == "E" and head and head[0].startswith("indicator"):
            for r in rows[1:]:
                if len(r) >= 4 and r[0]:
                    topic["E"].append({"indicator": r[0], "num_def": r[1], "den_def": r[2], "benchmark": r[3]})
        elif p == "F" and len(head) >= 3 and head[0] == "#":
            topic["F"] = [{"header": h, "type": _col_type(h)} for h in rows[0][1:]]
            topic["F_rows"] = sum(1 for r in rows[1:] if r and re.fullmatch(r"\d+", r[0]))

    if not topic["C"]:
        raise ValueError("Part C (health worker competency checklist) was not found. Is this the standard tool layout?")
    return topic


# ---------------------------------------------------------------------
# Sheet rows
# ---------------------------------------------------------------------
def to_library_rows(topic: dict):
    """-> (competency_items_rows, topic_parts_rows) for one parsed topic."""
    code, area = topic["code"], topic["title"]
    items = []
    for it in topic["C"]:
        dom = it["group"] + (f" - method: {it['method']}" if it.get("method") else "")
        items.append({"item_id": f"{code}_c{it['n']:02d}", "service_area": area, "domain": dom,
                      "competency": it["text"], "active": "yes"})
    parts = [{"topic": area, "part": "INFO", "item_id": code, "group": "scope", "text": topic.get("scope", ""),
              "method": "", "num_def": "", "den_def": "", "benchmark": "", "active": "yes"},
             {"topic": area, "part": "INFO", "item_id": code, "group": "priority", "text": topic.get("priority", ""),
              "method": "", "num_def": "", "den_def": "", "benchmark": "", "active": "yes"}]
    for it in topic["D"]:
        parts.append({"topic": area, "part": "D", "item_id": f"{code}_d{it['n']:02d}", "group": it["group"],
                      "text": it["text"], "method": "", "num_def": "", "den_def": "", "benchmark": "", "active": "yes"})
    for i, it in enumerate(topic["E"], 1):
        parts.append({"topic": area, "part": "E", "item_id": f"{code}_e{i:02d}", "group": "", "text": it["indicator"],
                      "method": "", "num_def": it["num_def"], "den_def": it["den_def"], "benchmark": it["benchmark"], "active": "yes"})
    for i, c in enumerate(topic["F"], 1):
        parts.append({"topic": area, "part": "F", "item_id": f"{code}_f{i:02d}", "group": c["type"], "text": c["header"],
                      "method": "", "num_def": "", "den_def": "", "benchmark": "", "active": "yes"})
    return items, parts


def has_denominator(den_def: str) -> bool:
    return _clean(den_def).lower() not in EMPTY_DASH


def import_topic(topic: dict, items_df: pd.DataFrame, parts_df: pd.DataFrame):
    """Merge a parsed topic into the existing library (replacing a topic of the same title).
    Returns (items_df, parts_df) ready to save."""
    items, parts = to_library_rows(topic)
    area = topic["title"]
    keep_items = items_df[items_df["service_area"].map(competency.slug) != competency.slug(area)] if not items_df.empty else items_df
    keep_parts = parts_df[parts_df["topic"].map(competency.slug) != competency.slug(area)] if not parts_df.empty else parts_df
    return (pd.concat([keep_items, pd.DataFrame(items)], ignore_index=True),
            pd.concat([keep_parts, pd.DataFrame(parts)], ignore_index=True))


def normalize_parts(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy() if df is not None else pd.DataFrame()
    for c in PART_COLS:
        if c not in out.columns:
            out[c] = ""
    out = out[PART_COLS].fillna("").astype(str)
    for c in PART_COLS:
        out[c] = out[c].str.strip()
    out["active"] = out["active"].str.lower().replace({"": "yes"})
    return out[out["topic"] != ""].reset_index(drop=True)


def topic_summary(items_df: pd.DataFrame, parts_df: pd.DataFrame) -> pd.DataFrame:
    """One row per topic: how many items in each part."""
    rows = []
    areas = list(dict.fromkeys(items_df["service_area"])) if items_df is not None and not items_df.empty else []
    p = normalize_parts(parts_df)
    for a in areas:
        pa = p[p["topic"].map(competency.slug) == competency.slug(a)]
        rows.append({"Topic": a,
                     "C competency items": int((items_df["service_area"] == a).sum()),
                     "D systems standards": int((pa["part"] == "D").sum()),
                     "E indicators": int((pa["part"] == "E").sum()),
                     "F record columns": int((pa["part"] == "F").sum())})
    return pd.DataFrame(rows)