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

    def get_org_units(self, level: int = 4) -> pd.DataFrame:
        """Pull facility-level org units (default level 4 = facility in most DHIS2 hierarchies)."""
        params = {"filter": f"level:eq:{level}", "fields": "id,name,parent[name]", "paging": "false"}
        r = requests.get(f"{self.base_url}/api/organisationUnits.json", params=params,
                          auth=self.auth, timeout=30)
        r.raise_for_status()
        data = r.json()["organisationUnits"]
        return pd.DataFrame(data)

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
        r.raise_for_status()
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
