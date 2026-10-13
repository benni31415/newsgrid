"""Weather logic behind the MCP server: place lookup, forecasts and alert routing."""

from __future__ import annotations

import re

from newsgrid.core.http import HttpClient
from newsgrid.core.models import SEVERITY_RANK, Alert, Forecast, Location
from newsgrid.servers.weather.providers import meteoalarm, nws, open_meteo

_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

# A place counts as the obvious match when it is this many times more populous
# than the next candidate ("Paris" -> Paris, France rather than Paris, Texas).
DOMINANCE_RATIO = 10

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "Washington, D.C.",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming",
}  # fmt: skip


class LocationNotFound(ValueError):
    pass


class AlertsUnavailable(ValueError):
    pass


class WeatherService:
    def __init__(self, http: HttpClient):
        self.http = http

    async def search(
        self, query: str, *, count: int = 10, prefer_exact: bool = True
    ) -> list[Location]:
        """Resolve a location query to candidate places, best match first.

        Accepts "lat,lon" or a place name optionally narrowed by region and/or
        country: "Springfield", "Springfield, IL", "Springfield, Illinois, United States".
        `prefer_exact=False` keeps fuzzy matches, for completing partial input.
        """
        if m := _COORDS.match(query):
            lat, lon = float(m[1]), float(m[2])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise LocationNotFound(f"Coordinates out of range: {query!r}")
            return [Location(name=f"{lat:.4f},{lon:.4f}", lat=lat, lon=lon)]

        parts = [part.strip() for part in query.split(",") if part.strip()]
        if not parts:
            raise LocationNotFound("Empty location query")
        name, *qualifiers = parts
        # Fetch extra results when filtering, so the wanted place survives the filter.
        results = await open_meteo.geocode(
            self.http, name, count=max(count, 20) if qualifiers else count
        )
        results = [r for r in results if all(_qualifies(r, q) for q in qualifiers)]
        # The geocoder matches fuzzily ("Springfield" also finds Palmyra, Missouri);
        # when some results carry exactly the requested name, keep only those.
        exact = [r for r in results if r.name.casefold() == name.casefold()] if prefer_exact else []
        return (exact or results)[:count]

    async def locate(self, query: str) -> Location:
        """The single best match for a query."""
        candidates = await self.search(query)
        if not candidates:
            raise LocationNotFound(f"No place found for {query!r}")
        return candidates[0]

    async def forecast(self, location: Location, days: int = 3) -> Forecast:
        return await open_meteo.forecast(self.http, location, days=days)

    async def alerts(self, location: Location) -> list[Alert]:
        """Active alerts, most severe first."""
        if location.country_code == "US":
            alerts = await nws.active_alerts(self.http, location)
        elif meteoalarm.covers(location):
            alerts = await meteoalarm.active_alerts(self.http, location)
        elif location.country_code is None:
            raise AlertsUnavailable(
                "Alerts need a place name rather than coordinates, so the country is known"
            )
        else:
            raise AlertsUnavailable(
                f"No alert source covers {location.country or location.country_code}; "
                "alerts are available for the US (NWS) and MeteoAlarm member countries"
            )
        return sorted(alerts, key=lambda a: SEVERITY_RANK[a.severity], reverse=True)


def is_ambiguous(candidates: list[Location]) -> bool:
    """True when several places fit and none clearly dominates by population."""
    if len(candidates) < 2:
        return False
    first, second = candidates[0].population or 0, candidates[1].population or 0
    return first < DOMINANCE_RATIO * max(second, 1)


def _qualifies(location: Location, qualifier: str) -> bool:
    q = qualifier.casefold()
    if location.country_code == "US" and qualifier.upper() in US_STATES:
        q = US_STATES[qualifier.upper()].casefold()
    fields = (location.admin1, location.admin2, location.country, location.country_code)
    return any(f and f.casefold().startswith(q) for f in fields)
