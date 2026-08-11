from __future__ import annotations

import secrets
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    app_name: str = "EVE Local Ledger"
    app_url: str = "http://localhost:8000"
    database_url: str = f"sqlite:///{(DATA_DIR / 'eve_ledger.db').as_posix()}"
    secret_key: str = Field(default_factory=lambda: secrets.token_hex(32))
    eve_esi_base_url: str = "https://esi.evetech.net/latest"
    eve_sso_authorize_url: str = "https://login.eveonline.com/v2/oauth/authorize"
    eve_sso_token_url: str = "https://login.eveonline.com/v2/oauth/token"
    eve_sso_verify_url: str = "https://login.eveonline.com/oauth/verify"
    user_agent: str = "eve-local-ledger/1.0"
    refresh_margin_seconds: int = 600
    refresh_interval_seconds: int = 300

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
