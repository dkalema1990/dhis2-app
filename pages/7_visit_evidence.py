import io

import pandas as pd
import streamlit as st

import auth
import gsheets
import kobo

auth.require_login()
auth.sidebar_user_badge()

st.title("📍 Visit evidence")
st.caption("Where DQAs, support supervision visits and learning visits took place, with the photos and "
           "attendance lists attached to each.")

if not gsheets.is_configured():
    st.warning("Google Sheets isn't configured, so there's nothing to show yet.")
    st.stop()

LABELS = {"DQA": "DQA", "SSM": "Support Supervision", "LV": "Learning Visit"}


@st.cache_data(ttl=900, show_spinner=False, max_entries=60)
def _fetch_kobo_file(url: str):
    return kobo.fetch_attachment(url)


def _is_previewable_image(blob: bytes) -> bool:
    """True only if the bytes really are an image the browser can show (iPhone HEIC photos and
    damaged files are not, and would appear as a broken picture)."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(blob)) as im:
            im.verify()
        return True
    except Exception:
        return False


def _links(cell) -> list:
    return [u.strip() for u in str(cell or "").split("|") if u.strip()]


def _load() -> pd.DataFrame:
    frames = []
    for key, label in LABELS.items():
        df = gsheets.get_all(key)
        if df.empty:
            continue
        df = df.copy()
        for c in gsheets.EVIDENCE_COLUMNS + ["facility", "timestamp", "submitted_by"]:
            if c not in df.columns:
                df[c] = ""
        df["activity"] = label
        df["date"] = df["timestamp"].astype(str).str[:10]
        df["lat"] = pd.to_numeric(df["latitude"], errors="coerce")
        df["lon"] = pd.to_numeric(df["longitude"], errors="coerce")
        df["n_photos"] = df["photo_links"].map(lambda v: len(_links(v)))
        df["n_attendance"] = df["attendance_links"].map(lambda v: len(_links(v)))
        frames.append(df[["activity", "facility", "date", "submitted_by", "lat", "lon", "gps_accuracy_m",
                          "photo_links", "attendance_links", "n_photos", "n_attendance"]])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


try:
    data = _load()
except RuntimeError as e:
    st.error(f"Couldn't load the activities: {e}")
    st.stop()

if st.button("🔄 Refresh", key="ev_refresh"):
    gsheets.get_all.clear()
    st.rerun()

if data.empty:
    st.info("No activities recorded yet.")
    st.stop()

f1, f2 = st.columns(2)
act_sel = f1.multiselect("Activity type", sorted(data["activity"].unique()))
fac_sel = f2.multiselect("Facility", sorted(data["facility"].astype(str).unique()))
d = data
if act_sel:
    d = d[d["activity"].isin(act_sel)]
if fac_sel:
    d = d[d["facility"].isin(fac_sel)]

total = len(d)
k1, k2, k3, k4 = st.columns(4)
k1.metric("Activities", total)
pct = lambda n: f"{n} ({n / total * 100:.0f}%)" if total else "0"
k2.metric("With a location", pct(int(d["lat"].notna().sum())))
k3.metric("With photos", pct(int((d["n_photos"] > 0).sum())))
k4.metric("With attendance list", pct(int((d["n_attendance"] > 0).sum())))

st.subheader("Map")
located = d.dropna(subset=["lat", "lon"])
if located.empty:
    st.info("No activity in this selection has a location yet.")
else:
    st.map(located.rename(columns={"lat": "latitude", "lon": "longitude"})[["latitude", "longitude"]], zoom=None)
    st.caption(f"{len(located)} activity location(s) shown.")

st.subheader("Evidence coverage by facility")
cov = (d.assign(has_location=d["lat"].notna(), has_photos=d["n_photos"] > 0, has_attendance=d["n_attendance"] > 0)
         .groupby("facility").agg(activities=("activity", "size"), with_location=("has_location", "sum"),
                                  with_photos=("has_photos", "sum"), with_attendance=("has_attendance", "sum"))
         .reset_index().sort_values("activities", ascending=False))
st.dataframe(cov, hide_index=True, use_container_width=True)

st.subheader("Look at one activity")
d = d.sort_values("date", ascending=False).reset_index(drop=True)
labels = [f"{r.date} · {r.activity} · {r.facility} · {r.submitted_by}" for r in d.itertuples()]
pick = st.selectbox("Activity", range(len(d)), format_func=lambda i: labels[i], key="ev_pick")
row = d.iloc[pick]

c1, c2 = st.columns([1, 2])
with c1:
    st.markdown(f"**{row['activity']}** at **{row['facility']}**  \n{row['date']} · {row['submitted_by']}")
    if pd.notna(row["lat"]):
        acc = row["gps_accuracy_m"]
        st.markdown(f"📍 {row['lat']:.5f}, {row['lon']:.5f}" + (f" (±{acc} m)" if str(acc).strip() else ""))
    else:
        st.caption("No location recorded.")
with c2:
    if pd.notna(row["lat"]):
        st.map(pd.DataFrame({"latitude": [row["lat"]], "longitude": [row["lon"]]}), zoom=15, height=200)

for title, cell in (("Attendance lists", row["attendance_links"]), ("Photos", row["photo_links"])):
    links = _links(cell)
    st.markdown(f"**{title}** ({len(links)})")
    if not links:
        st.caption("None attached.")
        continue
    for i, url in enumerate(links, 1):
        if kobo.is_kobo_link(url):
            # Kobo files need the API token, so the app fetches them on your behalf
            if st.button(f"Show {title.lower().rstrip('s')} {i} (from Kobo)", key=f"ev_{title}_{i}_{pick}"):
                try:
                    blob, ctype = _fetch_kobo_file(url)
                    shown = False
                    if ctype.startswith("image/"):
                        if _is_previewable_image(blob):
                            st.image(blob, use_container_width=True)
                            shown = True
                        else:                   # e.g. iPhone HEIC photos or a damaged file
                            st.caption("This image can't be previewed here — download it instead.")
                    if not shown:
                        st.download_button(f"Download file {i}", blob, file_name=f"{title.lower()}_{i}",
                                           key=f"evd_{title}_{i}_{pick}")
                except RuntimeError as e:
                    st.error(str(e))
        else:
            st.link_button(f"Open {title.lower().rstrip('s')} {i} ↗", url)

st.download_button("⬇️ Download this list (CSV)",
                   d.drop(columns=["lat", "lon"]).to_csv(index=False).encode("utf-8"),
                   "visit_evidence.csv", "text/csv")
