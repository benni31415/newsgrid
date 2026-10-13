import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import stamina

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def no_retry_waits():
    with stamina.set_testing(True, attempts=3):
        yield


@pytest.fixture
def weather_api():
    """Recorded responses for every upstream the weather server calls."""

    def geocode(request: httpx.Request) -> httpx.Response:
        name = request.url.params["name"].lower()
        path = FIXTURES / "weather" / f"geocode_{name}.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return httpx.Response(200, json=data)

    def nws(request: httpx.Request) -> httpx.Response:
        # Only the Sarasota recording has active alerts.
        sarasota = request.url.params["point"].startswith("27.")
        name = "nws_alerts_sarasota.json" if sarasota else "nws_alerts_empty.json"
        return httpx.Response(200, json=load_fixture(f"weather/{name}"))

    with respx.mock(assert_all_called=False) as router:
        router.get("https://geocoding-api.open-meteo.com/v1/search").mock(side_effect=geocode)
        router.get("https://geocoding-api.open-meteo.com/v1/get").respond(
            json=load_fixture("weather/lookup_munich_de.json")
        )
        router.get("https://api.open-meteo.com/v1/forecast", name="forecast").respond(
            json=load_fixture("weather/forecast_springfield.json")
        )
        router.get("https://api.weather.gov/alerts/active").mock(side_effect=nws)
        router.get("https://feeds.meteoalarm.org/api/v1/warnings/feeds-germany").respond(
            json=load_fixture("weather/meteoalarm_germany.json")
        )
        yield router
