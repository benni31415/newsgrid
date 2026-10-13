"""Protocol tests: a real MCP client talking to the weather server in-process."""

import json
from typing import Any

import mcp_types as t
import pytest
from mcp import Client

from newsgrid.servers.weather.server import create_server

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("weather_api")]

TEMPLATE = t.ResourceTemplateReference(type="ref/resource", uri="weather://{location}/current")


class Elicitor:
    """Elicitation callback that records each question and answers it scripted."""

    def __init__(self, action: str = "accept", pick: str = "Illinois"):
        self.action = action
        self.pick = pick
        self.questions: list[t.ElicitRequestParams] = []

    async def __call__(self, context: Any, params: t.ElicitRequestParams) -> t.ElicitResult:
        self.questions.append(params)
        if self.action != "accept":
            return t.ElicitResult(action=self.action)  # type: ignore[arg-type]
        options = self.options(params)
        return t.ElicitResult(
            action="accept", content={"place": next(o for o in options if self.pick in o)}
        )

    @staticmethod
    def options(params: t.ElicitRequestParams) -> list[str]:
        return params.requested_schema["properties"]["place"]["enum"]  # type: ignore[union-attr]


def result(r: t.CallToolResult) -> Any:
    assert not r.is_error, r.content
    return r.structured_content


def error_text(r: t.CallToolResult) -> str:
    assert r.is_error
    return r.content[0].text  # type: ignore[union-attr]


async def test_tools_are_read_only_and_hide_resolver_params():
    async with Client(create_server()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert set(tools) == {"geocode", "get_forecast", "get_alerts"}
    for tool in tools.values():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint and tool.annotations.open_world_hint
        assert tool.output_schema is not None
    assert set(tools["get_forecast"].input_schema["properties"]) == {"location", "days"}
    assert set(tools["geocode"].input_schema["properties"]) == {"location"}


# Both the 2026-07-28 input-required flow and the older mid-call elicitation request.
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_ambiguous_name_asks_user(mode: str):
    elicitor = Elicitor(pick="Illinois")
    async with Client(create_server(), elicitation_callback=elicitor, mode=mode) as client:
        forecast = result(
            await client.call_tool("get_forecast", {"location": "Springfield", "days": 3})
        )

    assert len(elicitor.questions) == 1
    options = Elicitor.options(elicitor.questions[0])
    assert "Springfield, Illinois, United States" in options
    assert all(o.startswith("Springfield") for o in options)  # fuzzy matches dropped
    assert forecast["location"]["admin1"] == "Illinois"
    assert len(forecast["daily"]) == 3


async def test_geocode_returns_only_the_chosen_place():
    async with Client(
        create_server(), elicitation_callback=Elicitor(pick="Massachusetts")
    ) as client:
        places = result(await client.call_tool("geocode", {"location": "Springfield"}))["result"]

    assert [p["admin1"] for p in places] == ["Massachusetts"]


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_declined_choice_falls_back_to_all_candidates(action: str):
    async with Client(create_server(), elicitation_callback=Elicitor(action=action)) as client:
        places = result(await client.call_tool("geocode", {"location": "Springfield"}))["result"]
        forecast = result(await client.call_tool("get_forecast", {"location": "Springfield"}))

    assert len(places) > 1
    assert forecast["location"]["id"] == places[0]["id"]


async def test_client_without_elicitation_gets_best_match():
    async with Client(create_server()) as client:
        forecast = result(await client.call_tool("get_forecast", {"location": "Springfield"}))

    assert forecast["location"]["admin1"] == "Missouri"  # most relevant result


@pytest.mark.parametrize(
    ("query", "expected"),
    [("Paris", "France"), ("Springfield, IL", "Illinois"), ("Springfield, Ohio", "Ohio")],
)
async def test_unambiguous_queries_do_not_ask(query: str, expected: str):
    elicitor = Elicitor()
    async with Client(create_server(), elicitation_callback=elicitor) as client:
        places = result(await client.call_tool("geocode", {"location": query}))["result"]

    assert elicitor.questions == []
    assert expected in (places[0]["admin1"], places[0]["country"])


async def test_coordinates_skip_geocoding(weather_api):
    async with Client(create_server()) as client:
        forecast = result(
            await client.call_tool("get_forecast", {"location": "39.80, -89.64", "days": 1})
        )

    assert forecast["location"]["lat"] == 39.8
    assert forecast["location"]["timezone"] == "America/Chicago"  # filled from the forecast
    assert weather_api["forecast"].calls.last.request.url.params["timezone"] == "auto"


async def test_unknown_place_is_a_tool_error():
    async with Client(create_server()) as client:
        r = await client.call_tool("get_forecast", {"location": "Nowhereville"})

    assert "No place found" in error_text(r)


async def test_days_out_of_range_is_rejected():
    async with Client(create_server()) as client:
        r = await client.call_tool("get_forecast", {"location": "Paris", "days": 30})

    assert r.is_error


async def test_alerts_from_nws():
    async with Client(create_server()) as client:
        alerts = result(await client.call_tool("get_alerts", {"location": "Sarasota, FL"}))[
            "result"
        ]

    assert [(a["source"], a["severity"], a["event"]) for a in alerts] == [
        ("nws", "Severe", "Flood Warning")
    ]


async def test_alerts_need_a_country():
    async with Client(create_server()) as client:
        r = await client.call_tool("get_alerts", {"location": "35.68,139.69"})

    assert "place name" in error_text(r)


async def test_current_conditions_resource():
    async with Client(create_server()) as client:
        templates = (await client.list_resource_templates()).resource_templates
        contents = (await client.read_resource("weather://Springfield%2C%20IL/current")).contents

    assert {tpl.uri_template for tpl in templates} == {
        "weather://{location}/current",
        "weather://{location}/forecast",
    }
    report = json.loads(contents[0].text)  # type: ignore[union-attr]
    assert report["location"]["admin1"] == "Illinois"
    assert report["current"]["description"]


async def test_unknown_place_resource_errors():
    async with Client(create_server()) as client:
        with pytest.raises(Exception, match="No place found"):
            await client.read_resource("weather://Nowhereville/forecast")


async def test_location_completion():
    async with Client(create_server()) as client:
        completion = (
            await client.complete(TEMPLATE, {"name": "location", "value": "Springfield"})
        ).completion
        too_short = (await client.complete(TEMPLATE, {"name": "location", "value": "S"})).completion

    assert "Springfield, Illinois, United States" in completion.values
    assert len(completion.values) == len(set(completion.values))
    assert too_short.values == []
