"""
activity_extras.py
-------------------
Location + attachments for the in-app DQA / Support Supervision / Learning Visit forms.

  location = extras.location_block("dqa")            # ABOVE the form: a button that reads the device GPS
  with st.form(...):
      ...
      att = extras.attachment_inputs("dqa")            # INSIDE the form, just before Submit
  ...
  evidence = extras.save_attachments(att, "DQA", facility, date)   # uploads, returns links for the Sheet
  record.update(evidence); record.update(extras.location_fields(location))

Why the location button sits above the form: the browser only shares its location after the user
taps a button and allows it, and that widget can't live inside a Streamlit form.
"""

import pandas as pd
import streamlit as st

import storage

LINK_SEP = " | "          # same separator the Kobo sync uses for links stored in one Sheet cell
POOR_ACCURACY_M = 100


def location_block(prefix: str):
    """GPS capture. Returns {'latitude', 'longitude', 'accuracy'} or None."""
    loc = None
    with st.container(border=True):
        st.markdown("**📍 Location of this activity**")
        try:
            from streamlit_geolocation import streamlit_geolocation
        except ImportError:
            streamlit_geolocation = None

        if streamlit_geolocation is not None:
            st.caption("Tap the button, then **Allow** when your browser asks for your location. "
                       "Do this at the facility. (Needs a secure https connection.)")
            raw = streamlit_geolocation() or {}
            lat, lon, acc = raw.get("latitude"), raw.get("longitude"), raw.get("accuracy")
            if lat is not None and lon is not None:
                loc = {"latitude": float(lat), "longitude": float(lon),
                       "accuracy": float(acc) if acc is not None else None}
        else:
            st.caption("Automatic location needs the `streamlit-geolocation` package (add it to "
                       "requirements.txt). Until then, you can type the coordinates.")
            c1, c2 = st.columns(2)
            lat_s = c1.text_input("Latitude", key=f"{prefix}_lat", placeholder="e.g. 0.3476")
            lon_s = c2.text_input("Longitude", key=f"{prefix}_lon", placeholder="e.g. 32.5825")
            try:
                if lat_s.strip() and lon_s.strip():
                    lat, lon = float(lat_s), float(lon_s)
                    if -90 <= lat <= 90 and -180 <= lon <= 180:
                        loc = {"latitude": lat, "longitude": lon, "accuracy": None}
                    else:
                        st.error("Latitude must be between -90 and 90, and longitude between -180 and 180.")
            except ValueError:
                st.error("Coordinates must be numbers, e.g. 0.3476 and 32.5825.")

        if loc:
            acc = loc["accuracy"]
            st.success(f"📍 {loc['latitude']:.5f}, {loc['longitude']:.5f}"
                       + (f"  (accurate to about {acc:.0f} m)" if acc is not None else ""))
            if acc is not None and acc > POOR_ACCURACY_M:
                st.warning("The location is only accurate to a wide area. Move to an open spot or wait a few "
                           "seconds and tap the button again for a better fix.")
            st.map(pd.DataFrame({"lat": [loc["latitude"]], "lon": [loc["longitude"]]}), zoom=15, height=180)
        else:
            st.caption("No location captured yet — the record can still be saved without one.")
    return loc


def location_fields(loc) -> dict:
    """Sheet columns for a captured location (blank when there isn't one)."""
    if not loc:
        return {"latitude": "", "longitude": "", "gps_accuracy_m": ""}
    return {"latitude": f"{loc['latitude']:.6f}", "longitude": f"{loc['longitude']:.6f}",
            "gps_accuracy_m": "" if loc.get("accuracy") is None else f"{loc['accuracy']:.1f}"}


def attachment_inputs(prefix: str) -> dict:
    """Attendance list + photo uploaders (call INSIDE the form, before the submit button)."""
    if not storage.is_configured():
        st.info("📎 **Attendance lists and photos can't be attached here yet** — file storage hasn't been set up. "
                "An admin can set it up in a few minutes (see `storage.py`, or ask your admin). Until then, "
                "capture them in the Kobo form.")
        return {"attendance": [], "photos": []}
    st.markdown("**📎 Attachments**")
    st.caption(f"Saved to {storage.describe()}. Photos are shrunk automatically so they upload quickly.")
    attendance = st.file_uploader(
        "Attendance list (photo, PDF or Excel)", type=storage.ATTENDANCE_TYPES, accept_multiple_files=True,
        key=f"{prefix}_attendance", help=f"Up to {storage.MAX_MB} MB per file.")
    photos = st.file_uploader(
        "Photos of this activity", type=storage.PHOTO_TYPES, accept_multiple_files=True,
        key=f"{prefix}_photos", help="On a phone you can take the photos with the camera. "
                                     f"Up to {storage.MAX_MB} MB per photo.")
    return {"attendance": list(attendance or []), "photos": list(photos or [])}


def save_attachments(att: dict, activity: str, facility: str, date_str: str) -> dict:
    """Validate and upload everything chosen. Returns {'photo_links', 'attendance_links'}.
    Raises ValueError (bad file) or RuntimeError (storage problem) — call BEFORE saving the record so
    nothing is half-saved."""
    out = {"photo_links": "", "attendance_links": ""}
    groups = (("attendance_links", att.get("attendance") or [], storage.ATTENDANCE_TYPES),
              ("photo_links", att.get("photos") or [], storage.PHOTO_TYPES))
    for _, files, allowed in groups:
        for f in files:
            storage.check_file(f.name, f.size, allowed)
    for key, files, _allowed in groups:
        links = []
        for f in files:
            data, name = storage.prepare_image(f.getvalue(), f.name)      # shrink phone photos
            links.append(storage.save_file(data, name, [activity, facility, date_str]))
        out[key] = LINK_SEP.join(links)
    return out