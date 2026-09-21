"""
dhis2_client.py
----------------
Thin wrapper around the DHIS2 Web API (analytics endpoint) to pull
data-element values by org unit (facility) and period, plus a
mock-data generator so the app runs without live DHIS2 credentials.

DHIS2 analytics reference:
GET /api/analytics.json?dimension=dx:<dataElement.categoryOptionCombo>
    &dimension=ou:<orgUnit>&dimension=pe:<period>&displayProperty=NAME
"""

import random
import requests
import pandas as pd


class DHIS2Client:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = (username, password)

    def test_connection(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/api/me.json", auth=self.auth, timeout=10)
            return r.status_code == 200
        except requests.RequestException:
            return False

    @staticmethod
    def _raise_detailed(r: requests.Response):
        """Raise with DHIS2's own error message included, not just a bare status code."""
        if not r.ok:
            try:
                detail = r.json().get("message", r.text)
            except ValueError:
                detail = r.text
            raise RuntimeError(f"DHIS2 request failed ({r.status_code}): {detail}")

    def get_org_units(self, level: int = 4) -> pd.DataFrame:
        """Pull facility-level org units (default level 4 = facility in most DHIS2 hierarchies)."""
        params = {"filter": f"level:eq:{level}", "fields": "id,name,parent[name]", "paging": "false"}
        r = requests.get(f"{self.base_url}/api/organisationUnits.json", params=params,
                          auth=self.auth, timeout=30)
        self._raise_detailed(r)
        data = r.json()["organisationUnits"]
        return pd.DataFrame(data)

    def get_datasets(self) -> pd.DataFrame:
        """List all datasets (id, name, periodType) for the dataset dropdown."""
        params = {"fields": "id,name,periodType", "paging": "false"}
        r = requests.get(f"{self.base_url}/api/dataSets.json", params=params,
                          auth=self.auth, timeout=30)
        self._raise_detailed(r)
        return pd.DataFrame(r.json().get("dataSets", []))

    def get_dataset_elements(self, dataset_id: str) -> pd.DataFrame:
        """List the data elements that belong to a given dataset, for the data element dropdown."""
        params = {"fields": "dataSetElements[dataElement[id,name]]"}
        r = requests.get(f"{self.base_url}/api/dataSets/{dataset_id}.json", params=params,
                          auth=self.auth, timeout=30)
        self._raise_detailed(r)
        rows = [{"id": d["dataElement"]["id"], "name": d["dataElement"]["name"]}
                for d in r.json().get("dataSetElements", [])]
        return pd.DataFrame(rows)


    def get_analytics(self, data_elements: list[str], org_unit: str, period: str) -> pd.DataFrame:
        """
        data_elements: list of DHIS2 data element (or indicator) UIDs
        org_unit: org unit UID or group UID (e.g. LEVEL-4 or a specific OU)
        period: DHIS2 period string, e.g. '2026Q2' or 'THIS_YEAR'
        Returns long-format DataFrame: facility, data_element, period, value
        """
        dx = ";".join(data_elements)
        params = {
            "dimension": [f"dx:{dx}", f"ou:{org_unit}", f"pe:{period}"],
            "displayProperty": "NAME",
            "outputIdScheme": "NAME",
        }
        r = requests.get(f"{self.base_url}/api/analytics.json", params=params,
                          auth=self.auth, timeout=60)
        self._raise_detailed(r)
        payload = r.json()
        headers = [h["column"] for h in payload["headers"]]
        df = pd.DataFrame(payload["rows"], columns=headers)
        df = df.rename(columns={"Data": "data_element", "Organisation unit": "facility",
                                 "Period": "period", "Value": "value"})
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        return df

    def push_event(self, program: str, program_stage: str, org_unit: str,
                    event_date: str, data_values: dict) -> dict:
        """
        Push an activity outcome (e.g. a DQA corrective action) back into DHIS2
        as an Event, so follow-up actions live inside DHIS2 too, not just this app.

        program / program_stage / org_unit: DHIS2 UIDs (set these up once in
        DHIS2 as an "Action Tracking" event program with the fields you want).
        data_values: {dataElementUID: value, ...}
        """
        payload = {
            "events": [{
                "program": program,
                "programStage": program_stage,
                "orgUnit": org_unit,
                "eventDate": event_date,
                "status": "COMPLETED",
                "dataValues": [{"dataElement": de, "value": str(v)} for de, v in data_values.items()],
            }]
        }
        r = requests.post(f"{self.base_url}/api/events", json=payload, auth=self.auth, timeout=30)
        r.raise_for_status()
        return r.json()


def generate_periods(period_type: str, count: int = 12) -> list[tuple[str, str]]:
    """
    DHIS2 has no 'list periods' API — periods are generated from the
    dataset's periodType. Returns the most recent `count` periods as
    (display_label, dhis2_period_code) tuples, newest first, so the UI
    can offer a clean dropdown instead of asking users to type period codes.
    """
    from datetime import date
    today = date.today()
    pt = (period_type or "").lower()
    periods = []

    if pt == "monthly":
        y, m = today.year, today.month
        for i in range(count):
            mm, yy = m - i, y
            while mm <= 0:
                mm += 12
                yy -= 1
            periods.append((f"{yy}-{mm:02d}", f"{yy}{mm:02d}"))
    elif pt == "quarterly":
        y, q = today.year, (today.month - 1) // 3 + 1
        for i in range(count):
            qq, yy = q - i, y
            while qq <= 0:
                qq += 4
                yy -= 1
            periods.append((f"{yy} Q{qq}", f"{yy}Q{qq}"))
    elif pt in ("weekly",):
        iso_year, iso_week, _ = today.isocalendar()
        for i in range(count):
            ww, yy = iso_week - i, iso_year
            while ww <= 0:
                ww += 52
                yy -= 1
            periods.append((f"{yy} Week {ww}", f"{yy}W{ww}"))
    elif pt.startswith("financialjuly") or pt.startswith("financial"):
        fy = today.year if today.month >= 7 else today.year - 1
        for i in range(count):
            yy = fy - i
            periods.append((f"FY {yy}-{yy + 1}", f"{yy}July"))
    else:  # "yearly" and any unrecognized type fall back to calendar years
        for i in range(count):
            yy = today.year - i
            periods.append((str(yy), str(yy)))

    return periods


# ---------------------------------------------------------------------
# Mock data — lets the app run fully offline for demos / development
# ---------------------------------------------------------------------
FACILITIES = [
    "Kawempe HC IV", "Nansana HC III", "Kira Health Centre", "Mukono Gen. Hospital",
    "Entebbe HC III", "Wakiso HC IV", "Bweyogerere HC II", "Mpigi HC III",
]
DATA_ELEMENTS = [
    "ANC 4th visit", "Skilled Birth Deliveries", "DPT3 Immunization",
    "HIV Testing Coverage", "Malaria Cases Treated", "TB Case Detection",
]


def get_mock_facility_data(period: str = "2026Q3") -> pd.DataFrame:
    """Generate a realistic mock dataset: facility x data element, with target & actual."""
    random.seed(hash(period) % 1000)
    rows = []
    for facility in FACILITIES:
        for de in DATA_ELEMENTS:
            target = random.randint(80, 300)
            # bias some facilities to be strong, some weak, for a realistic spread
            performance_bias = random.choice([0.35, 0.55, 0.7, 0.9, 1.05, 1.15])
            actual = int(target * performance_bias * random.uniform(0.85, 1.1))
            rows.append({
                "facility": facility,
                "data_element": de,
                "period": period,
                "target": target,
                "actual": actual,
            })
    df = pd.DataFrame(rows)
    df["achievement_pct"] = (df["actual"] / df["target"] * 100).round(1)
    return df