"""newsgrid weather MCP server.

Run on its own with `newsgrid-weather` (stdio) or `newsgrid-weather --transport http`.
"""

# No `from __future__ import annotations` here: the SDK reads the resolver closures'
# annotations at runtime, and string annotations naming locals can't be resolved.

import argparse
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    MCPServer,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp_types import (
    Completion,
    CompletionArgument,
    CompletionContext,
    PromptReference,
    ResourceTemplateReference,
    ToolAnnotations,
)
from pydantic import BaseModel, Field, create_model

from newsgrid import __version__
from newsgrid.core.http import HttpClient, UpstreamError
from newsgrid.core.models import Alert, CurrentConditions, Forecast, Location
from newsgrid.servers.weather.providers.open_meteo import MAX_FORECAST_DAYS
from newsgrid.servers.weather.service import (
    AlertsUnavailable,
    LocationNotFound,
    WeatherService,
    is_ambiguous,
)

INSTRUCTIONS = """\
Weather for any place on Earth: geocoding, current conditions, daily forecasts
(Open-Meteo) and active severe-weather alerts (NWS for the US, MeteoAlarm for Europe).
Locations are a place name, optionally narrowed by region and country
("Springfield, IL", "Paris, France"), or "lat,lon". When a name is ambiguous the
server may ask the user which place they meant."""

LOCATION_HELP = 'Place name, optionally with region/country ("Springfield, IL"), or "lat,lon"'

LocationArg = Annotated[str, Field(description=LOCATION_HELP)]

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

MAX_CHOICES = 6


class CurrentReport(BaseModel):
    location: Location
    current: CurrentConditions


def create_server(http: HttpClient | None = None) -> MCPServer:
    service = WeatherService(http or HttpClient())

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await service.http.aclose()

    mcp = MCPServer(
        "newsgrid-weather",
        title="newsgrid weather",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
    )

    # --- location resolution, shared by every tool ------------------------------------
    # Resolvers run before the tool body. `place_choice` asks the user to pick a place
    # (elicitation) when the query is ambiguous and the client supports it.

    async def place_candidates(location: str) -> list[Location]:
        try:
            return await service.search(location)
        except (LocationNotFound, UpstreamError) as exc:
            raise ToolError(str(exc)) from exc

    async def place_choice(
        candidates: Annotated[list[Location], Resolve(place_candidates)], ctx: Context
    ) -> Elicit[BaseModel] | None:
        if not is_ambiguous(candidates) or not _can_elicit(ctx):
            return None
        options = _options(candidates[:MAX_CHOICES])
        return Elicit(
            f'"{candidates[0].name}" matches several places. Which one do you mean?',
            _choice_schema(list(options)),
        )

    def pick(candidates: list[Location], choice: ElicitationResult[BaseModel]) -> Location:
        """The place the user chose, else the best match."""
        if chosen := _chosen(candidates, choice):
            return chosen
        if not candidates:
            raise ToolError("No place found for that location")
        return candidates[0]

    # --- tools --------------------------------------------------------------------------

    @mcp.tool(title="Find places", annotations=READ_ONLY)
    async def geocode(
        location: LocationArg,
        candidates: Annotated[list[Location], Resolve(place_candidates)],
        choice: Annotated[ElicitationResult[BaseModel], Resolve(place_choice)],
    ) -> list[Location]:
        """Look up places matching a name, best match first.

        If the name is ambiguous, the user may be asked which place they mean; the
        result then contains only that place.
        """
        chosen = _chosen(candidates, choice)
        return [chosen] if chosen else candidates

    @mcp.tool(title="Weather forecast", annotations=READ_ONLY)
    async def get_forecast(
        location: LocationArg,
        candidates: Annotated[list[Location], Resolve(place_candidates)],
        choice: Annotated[ElicitationResult[BaseModel], Resolve(place_choice)],
        days: Annotated[int, Field(ge=1, le=MAX_FORECAST_DAYS, description="Days to forecast")] = 3,
    ) -> Forecast:
        """Current conditions and a daily forecast (metric units, local time)."""
        try:
            return await service.forecast(pick(candidates, choice), days)
        except UpstreamError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(title="Severe-weather alerts", annotations=READ_ONLY)
    async def get_alerts(
        location: LocationArg,
        candidates: Annotated[list[Location], Resolve(place_candidates)],
        choice: Annotated[ElicitationResult[BaseModel], Resolve(place_choice)],
    ) -> list[Alert]:
        """Active watches, warnings and advisories for a place, most severe first.

        Covers the US (National Weather Service) and MeteoAlarm member countries in Europe.
        """
        try:
            return await service.alerts(pick(candidates, choice))
        except (AlertsUnavailable, UpstreamError) as exc:
            raise ToolError(str(exc)) from exc

    # --- resources ----------------------------------------------------------------------
    # Resources can't ask the user, so they use the best match for the location.

    async def locate(location: str) -> Location:
        try:
            return await service.locate(location)
        except LocationNotFound as exc:
            raise ResourceNotFoundError(str(exc)) from exc
        except UpstreamError as exc:
            raise ResourceError(str(exc)) from exc

    @mcp.resource(
        "weather://{location}/current",
        title="Current conditions",
        description="Current weather at a location (best match for the name)",
        mime_type="application/json",
    )
    async def current_resource(location: str) -> str:
        place = await locate(location)
        try:
            forecast = await service.forecast(place, days=1)
        except UpstreamError as exc:
            raise ResourceError(str(exc)) from exc
        return CurrentReport(location=forecast.location, current=forecast.current).model_dump_json()

    @mcp.resource(
        "weather://{location}/forecast",
        title="7-day forecast",
        description="Current conditions and a 7-day forecast (best match for the name)",
        mime_type="application/json",
    )
    async def forecast_resource(location: str) -> str:
        place = await locate(location)
        try:
            return (await service.forecast(place, days=7)).model_dump_json()
        except UpstreamError as exc:
            raise ResourceError(str(exc)) from exc

    # --- completions --------------------------------------------------------------------

    @mcp.completion()
    async def complete(
        ref: PromptReference | ResourceTemplateReference,
        argument: CompletionArgument,
        context: CompletionContext | None,
    ) -> Completion | None:
        if not isinstance(ref, ResourceTemplateReference) or argument.name != "location":
            return None
        if len(argument.value.strip()) < 2:
            return Completion(values=[])
        try:
            places = await service.search(argument.value, prefer_exact=False)
        except (LocationNotFound, UpstreamError):
            return Completion(values=[])
        return Completion(values=list(dict.fromkeys(p.label for p in places)), has_more=False)

    return mcp


def _can_elicit(ctx: Context) -> bool:
    caps = ctx.client_capabilities
    elicitation = caps.elicitation if caps is not None else None
    # A bare `elicitation: {}` predates modes and means form support.
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def _options(candidates: list[Location]) -> dict[str, Location]:
    """Map a unique display label to each candidate."""
    options: dict[str, Location] = {}
    for place in candidates:
        label = place.label
        if label in options and place.admin2:
            label = f"{label} ({place.admin2})"
        if label in options:
            label = f"{label} ({place.lat:.2f}, {place.lon:.2f})"
        options[label] = place
    return options


def _choice_schema(labels: list[str]) -> type[BaseModel]:
    return create_model(
        "PlaceChoice",
        place=(Literal[tuple(labels)], Field(title="Place")),  # type: ignore[valid-type]
    )


def _chosen(candidates: list[Location], choice: ElicitationResult[Any]) -> Location | None:
    if isinstance(choice, AcceptedElicitation) and isinstance(choice.data, BaseModel):
        label = getattr(choice.data, "place", None)
        return _options(candidates[:MAX_CHOICES]).get(label) if isinstance(label, str) else None
    return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="newsgrid-weather", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    server = create_server()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
