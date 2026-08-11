from __future__ import annotations

from pydantic import BaseModel, Field


REQUIRED_SCOPES = [
    "esi-wallet.read_character_wallet.v1",
    "esi-assets.read_assets.v1",
    "esi-markets.read_character_orders.v1",
    "esi-universe.read_structures.v1",
]


class SettingsUpdate(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None
    callback_url: str = Field(default="http://localhost:8000/auth/callback")
    default_low_stock_percent: float = Field(default=20.0, ge=1, le=100)


class ThresholdUpsert(BaseModel):
    character_id: int | None = None
    type_id: int
    min_quantity: int = Field(default=0, ge=0)
    low_stock_percent: float | None = Field(default=None, ge=1, le=100)


class ManualSyncRequest(BaseModel):
    character_id: int | None = None
