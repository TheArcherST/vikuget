from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration kept deliberately small and entirely environment based."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    vikunja_url: str
    vikunja_token: SecretStr
    vikunja_project_id: int = Field(gt=0)
    vikunja_project_view_id: int | None = Field(default=None, gt=0)
    access_token: SecretStr
    request_store_path: Path = Path("/data/idempotency.sqlite3")
    request_retention_days: int = Field(default=30, ge=1, le=365)
    vikunja_timeout_seconds: float = Field(default=15, gt=0, le=120)

    @field_validator("vikunja_url")
    @classmethod
    def validate_vikunja_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("VIKUNJA_URL must be an absolute HTTP(S) URL")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("VIKUNJA_URL must not contain a path, query, or fragment")
        return value.rstrip("/")

    @field_validator("access_token")
    @classmethod
    def validate_access_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
            raise ValueError("ACCESS_TOKEN must be at least 32 URL-safe characters")
        return value

    @field_validator("vikunja_project_view_id", mode="before")
    @classmethod
    def empty_project_view_is_none(cls, value: Any) -> Any:
        return None if value == "" else value
