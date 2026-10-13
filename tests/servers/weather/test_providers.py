"""Providers and service logic against recorded upstream responses."""

from datetime import datetime

import httpx
import pytest
import respx

from newsgrid.core.http import HttpClient, UpstreamError
from newsgrid.core.models import Location
from newsgrid.servers.weather.providers import meteoalarm, nws, open_meteo
from newsgrid.servers.weather.service import AlertsUnavailable, WeatherService, is_ambiguous

pytestmark = pytest.mark.anyio


@pytest.fixture
async def http():
    client = HttpClient()
    yield client
    await client.aclose()


@pytest.mark.usefixtures("weather_api")
async def test_forecast_parsing(http: HttpClient):
    place = Location(name="Springfield", lat=39.8, lon=-89.64, timezone="America/Chicago")
    forecast = await open_meteo.forecast(http, place, days=3)

    assert len(forecast.daily) == 3
    assert forecast.current.description == open_meteo.describe(forecast.current.weather_code)
    assert forecast.daily[0].temperature_max_c >= forecast.daily[0].temperature_min_c
    assert forecast.daily[0].sunrise is not None


@pytest.mark.usefixtures("weather_api")
async def test_forecast_times_carry_utc_offset(http: HttpClient):
    # MCP clients validate output against `format: date-time`, which requires an offset.
    place = Location(name="Springfield", lat=39.8, lon=-89.64, timezone="America/Chicago")
    data = (await open_meteo.forecast(http, place, days=3)).model_dump(mode="json")

    assert data["current"]["time"] == "2026-10-13T14:15:00-05:00"
    assert data["daily"][0]["sunrise"] == "2026-10-13T07:06:00-05:00"
    assert all(day["sunset"].endswith("-05:00") for day in data["daily"])


def test_unknown_weather_code_is_described():
    assert open_meteo.describe(42) == "Unknown (WMO 42)"


@pytest.mark.usefixtures("weather_api")
async def test_nws_alert_parsing(http: HttpClient):
    place = Location(name="Sarasota", country_code="US", lat=27.33, lon=-82.53)
    [alert] = await nws.active_alerts(http, place)

    assert alert.severity == "Severe"
    assert alert.area == "Sarasota, FL"
    # "ends" (when the hazard ends) wins over "expires" (when the message expires).
    assert alert.expires == datetime.fromisoformat("2026-10-15T14:00:00-04:00")


@pytest.mark.usefixtures("weather_api")
async def test_meteoalarm_matches_local_area_names(http: HttpClient):
    [munich] = await WeatherService(http).search("Munich, Germany", count=1)
    before_expiry = datetime.fromisoformat("2026-10-13T06:00:00+02:00")

    [alert] = await meteoalarm.active_alerts(http, munich, now=before_expiry)

    # The feed says "Kreis und Stadt München"; the geocoder says "Munich".
    assert alert.area == "Kreis und Stadt München"
    assert alert.event == "frost"
    assert alert.source == "meteoalarm"


@pytest.mark.usefixtures("weather_api")
async def test_meteoalarm_drops_expired_warnings(http: HttpClient):
    [munich] = await WeatherService(http).search("Munich, Germany", count=1)
    after_expiry = datetime.fromisoformat("2026-10-13T09:00:00+02:00")

    assert await meteoalarm.active_alerts(http, munich, now=after_expiry) == []


def test_meteoalarm_name_matching_uses_word_boundaries():
    assert meteoalarm._matches("Kreis und Stadt München", {"München"})
    assert not meteoalarm._matches("Stadt Mönchengladbach", {"München"})
    assert not meteoalarm._matches("Kreis Stader Land", {"Stade"})


async def test_unsupported_country_has_no_alerts(http: HttpClient):
    tokyo = Location(name="Tokyo", country="Japan", country_code="JP", lat=35.69, lon=139.69)
    with pytest.raises(AlertsUnavailable, match="Japan"):
        await WeatherService(http).alerts(tokyo)


@pytest.mark.usefixtures("weather_api")
async def test_ambiguity_uses_population(http: HttpClient):
    service = WeatherService(http)
    assert is_ambiguous(await service.search("Springfield"))
    assert not is_ambiguous(await service.search("Paris"))
    assert not is_ambiguous(await service.search("Springfield, IL"))


@pytest.mark.usefixtures("weather_api")
async def test_us_state_abbreviations(http: HttpClient):
    places = await WeatherService(http).search("Springfield, MA")
    assert {p.admin1 for p in places} == {"Massachusetts"}


async def test_search_rejects_bad_coordinates(http: HttpClient):
    with pytest.raises(ValueError, match="out of range"):
        await WeatherService(http).search("95, 10")


@respx.mock
async def test_http_retries_transient_errors_and_caches(http: HttpClient):
    route = respx.get("https://example.test/data").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )

    assert await http.get_json("https://example.test/data", ttl=60) == {"ok": True}
    assert await http.get_json("https://example.test/data", ttl=60) == {"ok": True}
    assert route.call_count == 2  # one retry, then served from cache


@respx.mock
async def test_http_client_errors_are_not_retried(http: HttpClient):
    route = respx.get("https://example.test/missing").respond(404)

    with pytest.raises(UpstreamError, match="HTTP 404"):
        await http.get_json("https://example.test/missing")
    assert route.call_count == 1
