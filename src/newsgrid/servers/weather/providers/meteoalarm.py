"""MeteoAlarm warnings for European countries (https://meteoalarm.org).

The feeds carry no geometry, only area names in the national language, so a
location is matched by name against each warning's `areaDesc`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, NamedTuple

from newsgrid.core.http import HttpClient
from newsgrid.core.models import Alert, Location, Severity
from newsgrid.servers.weather.providers import open_meteo

FEED_URL = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{slug}"


class Country(NamedTuple):
    slug: str
    language: str  # language the national weather service uses in `areaDesc`


COUNTRIES: dict[str, Country] = {
    "AT": Country("austria", "de"),
    "BA": Country("bosnia-herzegovina", "bs"),
    "BE": Country("belgium", "fr"),
    "BG": Country("bulgaria", "bg"),
    "CH": Country("switzerland", "de"),
    "CY": Country("cyprus", "el"),
    "CZ": Country("czechia", "cs"),
    "DE": Country("germany", "de"),
    "DK": Country("denmark", "da"),
    "EE": Country("estonia", "et"),
    "ES": Country("spain", "es"),
    "FI": Country("finland", "fi"),
    "FR": Country("france", "fr"),
    "GB": Country("united-kingdom", "en"),
    "GR": Country("greece", "el"),
    "HR": Country("croatia", "hr"),
    "HU": Country("hungary", "hu"),
    "IE": Country("ireland", "en"),
    "IL": Country("israel", "he"),
    "IS": Country("iceland", "is"),
    "IT": Country("italy", "it"),
    "LT": Country("lithuania", "lt"),
    "LU": Country("luxembourg", "fr"),
    "LV": Country("latvia", "lv"),
    "MD": Country("moldova", "ro"),
    "ME": Country("montenegro", "sr"),
    "MK": Country("republic-of-north-macedonia", "mk"),
    "MT": Country("malta", "en"),
    "NL": Country("netherlands", "nl"),
    "NO": Country("norway", "no"),
    "PL": Country("poland", "pl"),
    "PT": Country("portugal", "pt"),
    "RO": Country("romania", "ro"),
    "RS": Country("serbia", "sr"),
    "SE": Country("sweden", "sv"),
    "SI": Country("slovenia", "sl"),
    "SK": Country("slovakia", "sk"),
    "UA": Country("ukraine", "uk"),
}

_SEVERITIES: set[str] = {"Extreme", "Severe", "Moderate", "Minor"}


def covers(location: Location) -> bool:
    return location.country_code in COUNTRIES


async def active_alerts(
    http: HttpClient, location: Location, *, now: datetime | None = None
) -> list[Alert]:
    country = COUNTRIES[location.country_code or ""]
    names = await _names(http, location, country.language)
    data = await http.get_json(FEED_URL.format(slug=country.slug), ttl=http.settings.alerts_ttl)
    now = now or datetime.now(UTC)

    alerts: list[Alert] = []
    for warning in data.get("warnings", []):
        alert = warning["alert"]
        if alert.get("msgType") == "Cancel" or not alert.get("info"):
            continue
        info = _pick_info(alert["info"])
        expires = _parse(info.get("expires"))
        if expires is not None and expires <= now:
            continue
        areas = [a["areaDesc"] for a in info.get("area", []) if _matches(a["areaDesc"], names)]
        if areas:
            alerts.append(_alert(alert, info, areas, expires))
    return alerts


async def _names(http: HttpClient, location: Location, language: str) -> set[str]:
    names = {n for n in (location.name, location.admin2) if n}
    if location.id is not None and language != "en":
        names |= await open_meteo.local_names(http, location.id, language)
    return names


def _matches(area_desc: str, names: set[str]) -> bool:
    return any(
        re.search(rf"(?<!\w){re.escape(name)}(?!\w)", area_desc, re.IGNORECASE) for name in names
    )


def _pick_info(infos: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer the English block; areas are the same in every language."""
    return next((i for i in infos if i.get("language", "").startswith("en")), infos[0])


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _alert(
    alert: dict[str, Any], info: dict[str, Any], areas: list[str], expires: datetime | None
) -> Alert:
    severity: Severity = info["severity"] if info.get("severity") in _SEVERITIES else "Unknown"
    return Alert(
        id=alert["identifier"],
        source="meteoalarm",
        severity=severity,
        event=info.get("event", "Weather warning"),
        headline=info.get("headline") or info.get("event", "Weather warning"),
        description=info.get("description"),
        instruction=info.get("instruction"),
        onset=_parse(info.get("onset") or info.get("effective")),
        expires=expires,
        area="; ".join(areas),
    )
