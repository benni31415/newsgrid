"""US National Weather Service active alerts (https://www.weather.gov/documentation/services-web-api)."""

from __future__ import annotations

from typing import Any

from newsgrid.core.http import HttpClient
from newsgrid.core.models import Alert, Location, Severity

ALERTS_URL = "https://api.weather.gov/alerts/active"

_SEVERITIES: set[str] = {"Extreme", "Severe", "Moderate", "Minor"}


async def active_alerts(http: HttpClient, location: Location) -> list[Alert]:
    """Alerts whose area contains the location's point. NWS does the spatial match."""
    data = await http.get_json(
        ALERTS_URL,
        {"point": f"{location.lat:.4f},{location.lon:.4f}", "status": "actual"},
        headers={"Accept": "application/geo+json"},
        ttl=http.settings.alerts_ttl,
    )
    return [_alert(f["properties"]) for f in data.get("features", [])]


def _alert(p: dict[str, Any]) -> Alert:
    severity: Severity = p["severity"] if p.get("severity") in _SEVERITIES else "Unknown"
    return Alert(
        id=p["id"],
        source="nws",
        severity=severity,
        event=p["event"],
        headline=p.get("headline") or p["event"],
        description=p.get("description"),
        instruction=p.get("instruction"),
        onset=p.get("onset") or p.get("effective"),
        expires=p.get("ends") or p.get("expires"),
        area=p.get("areaDesc", ""),
    )
