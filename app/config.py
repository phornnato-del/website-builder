from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "WebBuilder API"
    environment: str = "development"
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_database: str = "webbuilder"
    cors_origins: list[str] = ["http://localhost:3000"]
    session_cookie_name: str = "webbuilder_session"
    session_lifetime_seconds: int = 60 * 60 * 24 * 7

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
