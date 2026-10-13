"""Runtime settings, read from `NEWSGRID_*` environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from newsgrid import __version__


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEWSGRID_")

    # NWS rejects requests without an identifying User-Agent and asks for contact
    # details in it; set NEWSGRID_USER_AGENT to include yours.
    user_agent: str = f"newsgrid/{__version__}"
    http_timeout: float = 20.0
    http_attempts: int = 3

    geocode_ttl: float = 24 * 3600
    forecast_ttl: float = 10 * 60
    alerts_ttl: float = 2 * 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
