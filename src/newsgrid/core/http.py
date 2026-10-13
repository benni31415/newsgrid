"""Shared async HTTP client with retries and a small in-memory TTL cache."""

from __future__ import annotations

import json
import ssl
import time
from collections.abc import Mapping
from typing import Any

import certifi
import httpx
import stamina

from newsgrid.core.settings import Settings, get_settings

Params = Mapping[str, str | int | float]


class UpstreamError(Exception):
    """An upstream API answered with an error or could not be reached."""


def _ssl_context() -> ssl.SSLContext:
    """Trust certifi's bundle plus the OS store (corporate/proxy CAs live there)."""
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_default_certs()
    return context


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


class HttpClient:
    """Thin wrapper over `httpx.AsyncClient` used by all providers.

    Transient failures (connection errors, 429, 5xx) are retried with backoff.
    Successful JSON responses can be cached for a per-call TTL so that repeated
    lookups (completions, resolver re-runs, resource reads) don't hit the APIs again.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, Any]] = {}

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying client, (re)opened on first use."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                verify=_ssl_context(),
                timeout=self.settings.http_timeout,
                headers={"User-Agent": self.settings.user_agent},
                follow_redirects=True,
            )
        return self._client

    async def get_json(
        self,
        url: str,
        params: Params | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        ttl: float = 0,
    ) -> Any:
        key = f"{url}?{json.dumps(params or {}, sort_keys=True)}"
        if ttl > 0 and (hit := self._cache.get(key)) and hit[0] > time.monotonic():
            return hit[1]

        try:
            data = await self._fetch(url, params, headers)
        except httpx.HTTPStatusError as exc:
            raise UpstreamError(
                f"{exc.request.url.host} returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Could not reach {httpx.URL(url).host}: {exc!r}") from exc

        if ttl > 0:
            self._cache[key] = (time.monotonic() + ttl, data)
        return data

    async def _fetch(
        self, url: str, params: Params | None, headers: Mapping[str, str] | None
    ) -> Any:
        @stamina.retry(on=_is_transient, attempts=self.settings.http_attempts)
        async def attempt() -> Any:
            response = await self.client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()

        return await attempt()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
