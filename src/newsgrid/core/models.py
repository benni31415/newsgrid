"""Domain models shared by the newsgrid servers. Also used as tool output schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["Extreme", "Severe", "Moderate", "Minor", "Unknown"]

SEVERITY_RANK: dict[Severity, int] = {
    "Extreme": 4,
    "Severe": 3,
    "Moderate": 2,
    "Minor": 1,
    "Unknown": 0,
}


class Location(BaseModel):
    id: int | None = Field(
        default=None, description="GeoNames id, when the place came from the geocoder"
    )
    name: str
    country: str | None = None
    country_code: str | None = Field(default=None, description="ISO 3166-1 alpha-2")
    admin1: str | None = Field(default=None, description="State, province or region")
    admin2: str | None = Field(default=None, description="County or district")
    lat: float
    lon: float
    timezone: str | None = None
    population: int | None = None

    @property
    def label(self) -> str:
        """Human-readable, comma-separated name that also works as a location query."""
        parts = [self.name, self.admin1, self.country]
        return ", ".join(dict.fromkeys(p for p in parts if p))


class CurrentConditions(BaseModel):
    time: datetime = Field(description="Local time at the location")
    temperature_c: float
    apparent_temperature_c: float
    relative_humidity_pct: float
    precipitation_mm: float
    wind_speed_kmh: float
    wind_direction_deg: float
    weather_code: int = Field(description="WMO weather interpretation code")
    description: str
    is_day: bool


class DailyForecast(BaseModel):
    date: date
    weather_code: int
    description: str
    temperature_max_c: float
    temperature_min_c: float
    precipitation_mm: float
    precipitation_probability_pct: float | None = None
    wind_speed_max_kmh: float
    sunrise: datetime | None = None
    sunset: datetime | None = None


class Forecast(BaseModel):
    location: Location
    current: CurrentConditions
    daily: list[DailyForecast]


class Alert(BaseModel):
    id: str
    source: Literal["nws", "meteoalarm"]
    severity: Severity
    event: str
    headline: str
    description: str | None = None
    instruction: str | None = None
    onset: datetime | None = None
    expires: datetime | None = None
    area: str
