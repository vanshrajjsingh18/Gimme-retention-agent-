"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "GIMME Retention Engine"
    ENVIRONMENT: str = "local"
    DEBUG: bool = True

    # Security
    SECRET_KEY: str = "dev-only-insecure-secret-change-me"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 12
    ALGORITHM: str = "HS256"

    # Database
    DATABASE_URL: str = f"sqlite:///{REPO_ROOT / 'data' / 'gimme.db'}"

    # CORS
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173"

    # Bootstrap admin (local development only)
    ADMIN_EMAIL: str = "admin@gimmedelivery.co.nz"
    ADMIN_PASSWORD: str = "GimmeAdmin123!"

    # LLM
    LLM_PROVIDER: str = "mock"  # mock | openai
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TIMEOUT_SECONDS: int = 45

    # Messaging providers: "mock" or "live"
    EMAIL_PROVIDER_MODE: str = "mock"
    SMS_PROVIDER_MODE: str = "mock"
    WHATSAPP_PROVIDER_MODE: str = "mock"

    # Background jobs
    ENABLE_SCHEDULER: bool = True
    SCHEDULER_METRICS_INTERVAL_MINUTES: int = 60

    # Deterministic simulation
    MOCK_SEED: int = 20240101

    # Ingestion
    MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024
    INBOX_DIR: str = str(REPO_ROOT / "data" / "inbox")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_sqlite:
        # Ensure the sqlite directory exists before the engine connects.
        path = settings.DATABASE_URL.replace("sqlite:///", "")
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return settings


settings = get_settings()
