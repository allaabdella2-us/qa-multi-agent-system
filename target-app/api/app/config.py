"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_DATABASE_URL = "postgresql+psycopg://corvid:corvid@localhost:5432/corvid"


def _as_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Everything the app needs to boot, resolved from environment variables."""

    database_url: str
    jwt_secret: str
    debug: bool
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        jwt_secret=os.environ.get("JWT_SECRET", "corvid-local-development-secret-key"),
        debug=_as_bool(os.environ.get("DEBUG"), default=False),
    )


settings = get_settings()
