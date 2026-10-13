"""Open-Meteo geocoding and forecast APIs (https://open-meteo.com)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from newsgrid.core.http import HttpClient
from newsgrid.core.models import CurrentConditions, DailyForecast, Forecast, Location

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
LOOKUP_URL = "https://geocoding-api.open-meteo.com/v1/get"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

MAX_FORECAST_DAYS = 16

CURRENT_VARS = (
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
    "wind_direction_10m",
    "is_day",
)
DAILY_VARS = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "precipitation_probability_max",
    "wind_speed_10m_max",
    "sunrise",
    "sunset",
)

# WMO weather interpretation codes, as documented by Open-Meteo.
WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def describe(code: int) -> str:
    return WMO_CODES.get(code, f"Unknown (WMO {code})")


async def geocode(http: HttpClient, name: str, *, count: int = 10) -> list[Location]:
    """Search places by name. Results come back in Open-Meteo's relevance order."""
    data = await http.get_json(
        GEOCODING_URL,
        {"name": name, "count": count, "language": "en", "format": "json"},
        ttl=http.settings.geocode_ttl,
    )
    return [_location(r) for r in data.get("results", [])]


async def local_names(http: HttpClient, place_id: int, language: str) -> set[str]:
    """The place's own name and its municipality/district names in another language.

    MeteoAlarm describes warning areas in the country's language ("Kreis und Stadt
    München"), while search results come back in English ("Munich").
    """
    r = await http.get_json(
        LOOKUP_URL, {"id": place_id, "language": language}, ttl=http.settings.geocode_ttl
    )
    return {n for n in (r.get("name"), r.get("admin3"), r.get("admin4")) if n}


def _location(r: dict[str, Any]) -> Location:
    return Location(
        id=r.get("id"),
        name=r["name"],
        country=r.get("country"),
        country_code=r.get("country_code"),
        admin1=r.get("admin1"),
        admin2=r.get("admin2"),
        lat=r["latitude"],
        lon=r["longitude"],
        timezone=r.get("timezone"),
        population=r.get("population"),
    )


async def forecast(http: HttpClient, location: Location, *, days: int = 3) -> Forecast:
    data = await http.get_json(
        FORECAST_URL,
        {
            "latitude": round(location.lat, 4),
            "longitude": round(location.lon, 4),
            "current": ",".join(CURRENT_VARS),
            "daily": ",".join(DAILY_VARS),
            "timezone": location.timezone or "auto",
            "forecast_days": days,
        },
        ttl=http.settings.forecast_ttl,
    )
    if location.timezone is None:
        location = location.model_copy(update={"timezone": data.get("timezone")})
    tz = _zone(data)
    return Forecast(
        location=location,
        current=_current(data["current"], tz),
        daily=_daily(data["daily"], tz),
    )


def _zone(data: dict[str, Any]) -> tzinfo:
    """The forecast's time zone. Open-Meteo returns local times without an offset."""
    try:
        return ZoneInfo(data["timezone"])
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        return timezone(timedelta(seconds=data.get("utc_offset_seconds", 0)))


def _local(value: str | None, tz: tzinfo) -> datetime | None:
    return datetime.fromisoformat(value).replace(tzinfo=tz) if value else None


def _current(c: dict[str, Any], tz: tzinfo) -> CurrentConditions:
    return CurrentConditions(
        time=datetime.fromisoformat(c["time"]).replace(tzinfo=tz),
        temperature_c=c["temperature_2m"],
        apparent_temperature_c=c["apparent_temperature"],
        relative_humidity_pct=c["relative_humidity_2m"],
        precipitation_mm=c["precipitation"],
        wind_speed_kmh=c["wind_speed_10m"],
        wind_direction_deg=c["wind_direction_10m"],
        weather_code=c["weather_code"],
        description=describe(c["weather_code"]),
        is_day=bool(c["is_day"]),
    )


def _daily(d: dict[str, list[Any]], tz: tzinfo) -> list[DailyForecast]:
    return [
        DailyForecast(
            date=d["time"][i],
            weather_code=d["weather_code"][i],
            description=describe(d["weather_code"][i]),
            temperature_max_c=d["temperature_2m_max"][i],
            temperature_min_c=d["temperature_2m_min"][i],
            precipitation_mm=d["precipitation_sum"][i],
            precipitation_probability_pct=d["precipitation_probability_max"][i],
            wind_speed_max_kmh=d["wind_speed_10m_max"][i],
            sunrise=_local(d["sunrise"][i], tz),
            sunset=_local(d["sunset"][i], tz),
        )
        for i in range(len(d["time"]))
    ]
