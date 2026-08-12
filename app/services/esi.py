from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AppSetting,
    AssetSnapshot,
    CharacterToken,
    ETagCache,
    InventoryThreshold,
    LocationCache,
    MarketOrderSnapshot,
    TypeCache,
)
from app.schemas import REQUIRED_SCOPES


logger = logging.getLogger(__name__)
MARKET_SCOPE = "esi-markets.read_character_orders.v1"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_app_settings(db: Session) -> AppSetting:
    record = db.get(AppSetting, 1)
    if record is None:
        record = AppSetting(id=1, client_id=settings.eve_client_id)
        db.add(record)
        db.commit()
        db.refresh(record)
    elif record.client_id != settings.eve_client_id:
        record.client_id = settings.eve_client_id
        db.add(record)
        db.commit()
        db.refresh(record)
    return record


def build_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")
    return verifier, challenge


@dataclass
class EsiResult:
    status_code: int
    payload: Any | None
    etag: str | None = None
    not_modified: bool = False


class EveEsiService:
    def __init__(self, db: Session):
        self.db = db
        self.app_settings = ensure_app_settings(db)

    def build_authorize_url(self, state: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "redirect_uri": self.app_settings.callback_url,
            "client_id": self.app_settings.client_id or "",
            "scope": " ".join(REQUIRED_SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{settings.eve_sso_authorize_url}?{urlencode(params)}"

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.app_settings.callback_url,
            "code_verifier": code_verifier,
        }
        if not self.app_settings.client_secret:
            payload["client_id"] = self.app_settings.client_id or ""

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                settings.eve_sso_token_url,
                data=payload,
                headers={"User-Agent": settings.user_agent},
                auth=self._basic_auth_or_none(),
            )
            if response.is_error:
                detail = self._extract_error_detail(response, "Could not exchange the EVE authorization code.")
                raise HTTPException(status_code=400, detail=detail)
            return response.json()

    async def verify_access_token(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                settings.eve_sso_verify_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": settings.user_agent,
                },
            )
            response.raise_for_status()
            return response.json()

    async def refresh_if_needed(self, token: CharacterToken) -> CharacterToken:
        expires_soon = token.expires_at <= utcnow() + timedelta(seconds=settings.refresh_margin_seconds)
        if not expires_soon:
            return token

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "redirect_uri": self.app_settings.callback_url,
        }
        if not self.app_settings.client_secret:
            payload["client_id"] = self.app_settings.client_id or ""

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                settings.eve_sso_token_url,
                data=payload,
                headers={"User-Agent": settings.user_agent},
                auth=self._basic_auth_or_none(),
            )
            if response.is_error:
                detail = self._extract_error_detail(response, f"Could not refresh the token for {token.character_name}.")
                raise HTTPException(status_code=400, detail=detail)
            data = response.json()

        token.access_token = data["access_token"]
        token.refresh_token = data.get("refresh_token", token.refresh_token)
        token.expires_at = utcnow() + timedelta(seconds=int(data.get("expires_in", 1200)))
        self.db.add(token)
        try:
            self.db.commit()
            self.db.refresh(token)
            return token
        except Exception:
            self.db.rollback()
            logger.exception("Failed to persist refreshed token for character_id=%s", token.character_id)
            raise

    async def save_token_from_callback(self, token_data: dict[str, Any], verify_data: dict[str, Any]) -> CharacterToken:
        character_id = int(verify_data["CharacterID"])
        record = self.db.get(CharacterToken, character_id)
        if record is None:
            record = CharacterToken(
                character_id=character_id,
                character_name=verify_data["CharacterName"],
                character_owner_hash=verify_data["CharacterOwnerHash"],
                access_token=token_data["access_token"],
                refresh_token=token_data["refresh_token"],
                expires_at=utcnow() + timedelta(seconds=int(token_data.get("expires_in", 1200))),
                scopes=verify_data.get("Scopes", " ".join(REQUIRED_SCOPES)),
                avatar_url=f"https://images.evetech.net/characters/{character_id}/portrait?size=128",
            )
        else:
            record.character_name = verify_data["CharacterName"]
            record.character_owner_hash = verify_data["CharacterOwnerHash"]
            record.access_token = token_data["access_token"]
            record.refresh_token = token_data["refresh_token"]
            record.expires_at = utcnow() + timedelta(seconds=int(token_data.get("expires_in", 1200)))
            record.scopes = verify_data.get("Scopes", record.scopes)
            record.avatar_url = f"https://images.evetech.net/characters/{character_id}/portrait?size=128"

        self.db.add(record)
        try:
            self.db.commit()
            self.db.refresh(record)
            return record
        except Exception:
            self.db.rollback()
            logger.exception("Failed to persist callback token for character_id=%s", character_id)
            raise

    async def api_get(
        self,
        token: CharacterToken,
        path: str,
        *,
        cache_key: str | None = None,
        query: dict[str, Any] | None = None,
        allow_304: bool = True,
    ) -> EsiResult:
        token = await self.refresh_if_needed(token)
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "User-Agent": settings.user_agent,
        }

        etag_record = None
        if cache_key and allow_304:
            etag_record = self.db.execute(select(ETagCache).where(ETagCache.cache_key == cache_key)).scalar_one_or_none()
            if etag_record:
                headers["If-None-Match"] = etag_record.etag

        url = f"{settings.eve_esi_base_url.rstrip('/')}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.get(url, params=query, headers=headers)

        if response.status_code == 304:
            return EsiResult(status_code=304, payload=None, etag=etag_record.etag if etag_record else None, not_modified=True)

        response.raise_for_status()
        etag = response.headers.get("ETag")
        if cache_key and etag:
            if etag_record is None:
                etag_record = ETagCache(cache_key=cache_key, etag=etag)
            else:
                etag_record.etag = etag
            self.db.add(etag_record)
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()
                logger.exception("Failed to persist ETag cache for key=%s", cache_key)
                raise

        return EsiResult(status_code=response.status_code, payload=response.json(), etag=etag)

    async def api_get_response(
        self,
        token: CharacterToken,
        path: str,
        *,
        cache_key: str | None = None,
        query: dict[str, Any] | None = None,
        allow_304: bool = True,
    ) -> tuple[EsiResult, httpx.Response]:
        token = await self.refresh_if_needed(token)
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "User-Agent": settings.user_agent,
        }

        etag_record = None
        if cache_key and allow_304:
            etag_record = self.db.execute(select(ETagCache).where(ETagCache.cache_key == cache_key)).scalar_one_or_none()
            if etag_record:
                headers["If-None-Match"] = etag_record.etag

        url = f"{settings.eve_esi_base_url.rstrip('/')}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.get(url, params=query, headers=headers)

        if response.status_code == 304:
            result = EsiResult(
                status_code=304,
                payload=None,
                etag=etag_record.etag if etag_record else None,
                not_modified=True,
            )
            return result, response

        response.raise_for_status()
        etag = response.headers.get("ETag")
        if cache_key and etag:
            if etag_record is None:
                etag_record = ETagCache(cache_key=cache_key, etag=etag)
            else:
                etag_record.etag = etag
            self.db.add(etag_record)
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()
                logger.exception("Failed to persist ETag cache for key=%s", cache_key)
                raise

        return EsiResult(status_code=response.status_code, payload=response.json(), etag=etag), response

    async def public_get(self, path: str) -> dict[str, Any]:
        url = f"{settings.eve_esi_base_url.rstrip('/')}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.get(url, headers={"User-Agent": settings.user_agent})
            response.raise_for_status()
            return response.json()

    async def resolve_type_name(self, type_id: int) -> str:
        with self.db.no_autoflush:
            cached = self.db.get(TypeCache, type_id)
        if cached:
            return cached.name

        try:
            data = await self.public_get(f"universe/types/{type_id}/")
            cached = TypeCache(type_id=type_id, name=data.get("name", f"Type {type_id}"), volume=data.get("packaged_volume"))
            self.db.add(cached)
            self.db.commit()
            return cached.name
        except IntegrityError:
            self.db.rollback()
            existing = self.db.get(TypeCache, type_id)
            if existing:
                return existing.name
            raise
        except Exception:
            self.db.rollback()
            logger.exception("Failed to resolve type_id=%s", type_id)
            raise

    async def resolve_location_name(self, token: CharacterToken, location_id: int) -> str:
        with self.db.no_autoflush:
            cached = self.db.get(LocationCache, location_id)
        if cached:
            return cached.name

        name = f"Location {location_id}"
        kind = "station"
        try:
            data = await self.public_get(f"universe/stations/{location_id}/")
            name = data.get("name", name)
            kind = "station"
        except httpx.HTTPStatusError:
            try:
                result = await self.api_get(token, f"universe/structures/{location_id}/", allow_304=False)
                payload = result.payload or {}
                name = payload.get("name", name)
                kind = "structure"
            except httpx.HTTPStatusError:
                pass

        try:
            cache = LocationCache(location_id=location_id, name=name, kind=kind)
            self.db.add(cache)
            self.db.commit()
            return name
        except IntegrityError:
            self.db.rollback()
            existing = self.db.get(LocationCache, location_id)
            if existing:
                return existing.name
            raise
        except Exception:
            self.db.rollback()
            logger.exception("Failed to resolve location_id=%s for character_id=%s", location_id, token.character_id)
            raise

    async def sync_wallet(self, token: CharacterToken) -> float:
        try:
            result = await self.api_get(token, f"characters/{token.character_id}/wallet/", allow_304=False)
            balance = float(result.payload)
            token.wallet_balance = balance
            token.last_wallet_sync_at = utcnow()
            self.db.add(token)
            self.db.commit()
            return balance
        except Exception:
            self.db.rollback()
            logger.exception("Failed to sync wallet for character_id=%s", token.character_id)
            raise

    async def sync_orders(self, token: CharacterToken) -> list[MarketOrderSnapshot]:
        try:
            token = await self.refresh_if_needed(token)
            self._assert_market_scope(token)
            headers = {
                "Authorization": f"Bearer {token.access_token}",
                "User-Agent": settings.user_agent,
            }
            url = f"{settings.eve_esi_base_url.rstrip('/')}/characters/{token.character_id}/orders/"

            combined_payload: list[dict[str, Any]] = []
            async with httpx.AsyncClient(timeout=45.0) as client:
                page = 1
                while True:
                    response = await client.get(url, headers=headers, params={"page": page})
                    response.raise_for_status()
                    payload = response.json() or []
                    if not payload:
                        break
                    combined_payload.extend(payload)
                    total_pages = int(response.headers.get("X-Pages", str(page)))
                    if page >= total_pages:
                        break
                    page += 1

            logger.info("Fetched %s raw market orders for character_id=%s", len(combined_payload), token.character_id)

            self.db.execute(
                delete(MarketOrderSnapshot).where(MarketOrderSnapshot.character_id == token.character_id)
            )

            snapshots: list[MarketOrderSnapshot] = []
            recorded_at = utcnow()
            for order in combined_payload:
                is_buy_order = bool(order.get("is_buy_order", False))
                if is_buy_order:
                    continue
                issued_raw = order.get("issued")
                issued = recorded_at
                if issued_raw:
                    issued = datetime.fromisoformat(issued_raw.replace("Z", "+00:00")).replace(tzinfo=None)

                snapshots.append(
                    MarketOrderSnapshot(
                        character_id=token.character_id,
                        order_id=int(order["order_id"]),
                        type_id=int(order["type_id"]),
                        location_id=int(order["location_id"]),
                        volume_total=int(order["volume_total"]),
                        volume_remain=int(order["volume_remain"]),
                        price=float(order["price"]),
                        is_buy_order=False,
                        issued=issued,
                        duration=int(order["duration"]),
                        recorded_at=recorded_at,
                    )
                )

            if snapshots:
                self.db.add_all(snapshots)
            self.db.commit()
            logger.info("Stored %s active sell orders for character_id=%s", len(snapshots), token.character_id)
            return snapshots
        except Exception:
            self.db.rollback()
            logger.exception("Failed to sync market orders for character_id=%s", token.character_id)
            raise

    async def sync_assets(self, token: CharacterToken) -> list[AssetSnapshot]:
        try:
            result, response = await self.api_get_response(
                token,
                f"characters/{token.character_id}/assets/",
                cache_key=f"assets:{token.character_id}",
                query={"page": 1},
            )

            if result.not_modified:
                return self._latest_assets_for_character(token.character_id)

            pages = int(response.headers.get("X-Pages", "1"))
            combined_payload = list(result.payload or [])
            for page in range(2, pages + 1):
                next_page = await self.api_get(
                    token,
                    f"characters/{token.character_id}/assets/",
                    query={"page": page},
                    allow_304=False,
                )
                combined_payload.extend(next_page.payload or [])

            snapshots: list[AssetSnapshot] = []
            recorded_at = utcnow()
            for asset in combined_payload:
                quantity = int(asset.get("quantity", 1))
                snapshots.append(
                    AssetSnapshot(
                        character_id=token.character_id,
                        item_id=asset["item_id"],
                        type_id=asset["type_id"],
                        location_id=asset["location_id"],
                        quantity=quantity,
                        is_singleton=bool(asset.get("is_singleton", False)),
                        location_flag=asset.get("location_flag"),
                        recorded_at=recorded_at,
                    )
                )

            self.db.add_all(snapshots)
            self.db.commit()
            return snapshots
        except Exception:
            self.db.rollback()
            logger.exception("Failed to sync assets for character_id=%s", token.character_id)
            raise

    def _latest_orders_for_character(self, character_id: int) -> list[MarketOrderSnapshot]:
        return list(
            self.db.execute(
                select(MarketOrderSnapshot).where(
                    MarketOrderSnapshot.character_id == character_id,
                    MarketOrderSnapshot.is_buy_order.is_(False),
                )
            ).scalars()
        )

    def _latest_assets_for_character(self, character_id: int) -> list[AssetSnapshot]:
        last_seen = self.db.execute(
            select(func.max(AssetSnapshot.recorded_at)).where(AssetSnapshot.character_id == character_id)
        ).scalar_one_or_none()
        if last_seen is None:
            return []
        return list(
            self.db.execute(
                select(AssetSnapshot).where(
                    AssetSnapshot.character_id == character_id,
                    AssetSnapshot.recorded_at == last_seen,
                )
            ).scalars()
        )

    async def sync_character(self, token: CharacterToken) -> dict[str, Any]:
        try:
            await self.sync_wallet(token)
            orders = await self.sync_orders(token)
            assets = await self.sync_assets(token)

            type_ids = {row.type_id for row in orders} | {row.type_id for row in assets}
            location_ids = {row.location_id for row in orders}
            for type_id in type_ids:
                try:
                    await self.resolve_type_name(type_id)
                except Exception:
                    logger.exception("Failed to resolve type_id=%s for character_id=%s", type_id, token.character_id)
            for location_id in location_ids:
                try:
                    await self.resolve_location_name(token, location_id)
                except Exception:
                    logger.exception("Failed to resolve location_id=%s for character_id=%s", location_id, token.character_id)

            return {"orders": len(orders), "assets": len(assets)}
        except Exception:
            logger.exception("Failed to sync character_id=%s", token.character_id)
            raise

    def characters(self) -> list[CharacterToken]:
        return list(self.db.execute(select(CharacterToken).order_by(CharacterToken.character_name)).scalars())

    def thresholds(self) -> list[InventoryThreshold]:
        return list(self.db.execute(select(InventoryThreshold).order_by(InventoryThreshold.type_id)).scalars())

    def latest_orders(self) -> list[MarketOrderSnapshot]:
        return list(
            self.db.execute(
                select(MarketOrderSnapshot)
                .where(MarketOrderSnapshot.is_buy_order.is_(False))
                .order_by(MarketOrderSnapshot.character_id, MarketOrderSnapshot.type_id, MarketOrderSnapshot.order_id)
            ).scalars()
        )

    def latest_assets(self) -> list[AssetSnapshot]:
        characters = self.characters()
        rows: list[AssetSnapshot] = []
        for token in characters:
            rows.extend(self._latest_assets_for_character(token.character_id))
        return rows

    def default_low_stock_percent(self) -> float:
        return ensure_app_settings(self.db).default_low_stock_percent

    def _basic_auth_or_none(self) -> httpx.BasicAuth | None:
        if self.app_settings.client_secret:
            return httpx.BasicAuth(self.app_settings.client_id or "", self.app_settings.client_secret)
        return None

    def _assert_market_scope(self, token: CharacterToken) -> None:
        scopes = {scope.strip() for scope in token.scopes.split() if scope.strip()}
        if MARKET_SCOPE not in scopes:
            logger.warning(
                "Character_id=%s is missing scope %s; stored scopes=%s",
                token.character_id,
                MARKET_SCOPE,
                token.scopes,
            )

    def _extract_error_detail(self, response: httpx.Response, fallback: str) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            error = payload.get("error")
            description = payload.get("error_description") or payload.get("message")
            if error and description:
                return f"{fallback} {error}: {description}"
            if error:
                return f"{fallback} {error}"
            if description:
                return f"{fallback} {description}"

        text = response.text.strip()
        if text:
            return f"{fallback} {text}"
        return fallback


def estimate_daily_velocity(db: Session, character_id: int, type_id: int) -> float:
    rows = list(
        db.execute(
            select(MarketOrderSnapshot)
            .where(
                MarketOrderSnapshot.character_id == character_id,
                MarketOrderSnapshot.type_id == type_id,
                MarketOrderSnapshot.is_buy_order.is_(False),
            )
            .order_by(desc(MarketOrderSnapshot.recorded_at))
            .limit(10)
        ).scalars()
    )
    if len(rows) < 2:
        return 0.0

    newest = rows[0]
    oldest = rows[-1]
    sold = max(0, oldest.volume_remain - newest.volume_remain)
    elapsed_days = max((newest.recorded_at - oldest.recorded_at).total_seconds() / 86400, 1 / 24)
    return sold / elapsed_days


def build_dashboard_payload(db: Session) -> dict[str, Any]:
    service = EveEsiService(db)
    characters = service.characters()
    type_map = {row.type_id: row.name for row in db.execute(select(TypeCache)).scalars()}
    thresholds = {(row.character_id, row.type_id): row for row in service.thresholds()}
    orders = service.latest_orders()
    assets = service.latest_assets()

    assets_by_character: dict[int, list[AssetSnapshot]] = {}
    for asset in assets:
        assets_by_character.setdefault(asset.character_id, []).append(asset)

    orders_by_character: dict[int, list[MarketOrderSnapshot]] = {}
    for order in orders:
        orders_by_character.setdefault(order.character_id, []).append(order)

    character_cards = []
    total_wallet = 0.0
    total_order_value = 0.0
    total_asset_estimate = 0.0

    for token in characters:
        char_orders = orders_by_character.get(token.character_id, [])
        char_assets = assets_by_character.get(token.character_id, [])
        order_value = sum(order.volume_remain * order.price for order in char_orders)
        price_lookup = {}
        for order in char_orders:
            price_lookup.setdefault(order.type_id, order.price)
        asset_estimate = sum(asset.quantity * price_lookup.get(asset.type_id, 0.0) for asset in char_assets)

        total_wallet += token.wallet_balance or 0.0
        total_order_value += order_value
        total_asset_estimate += asset_estimate

        low_stock = 0
        for order in char_orders:
            threshold = thresholds.get((token.character_id, order.type_id)) or thresholds.get((None, order.type_id))
            threshold_qty = threshold.min_quantity if threshold else 0
            threshold_pct = threshold.low_stock_percent if threshold and threshold.low_stock_percent is not None else service.default_low_stock_percent()
            remain_pct = (order.volume_remain / order.volume_total * 100) if order.volume_total else 0
            if order.volume_remain <= threshold_qty or remain_pct <= threshold_pct:
                low_stock += 1

        character_cards.append(
            {
                "character_id": token.character_id,
                "character_name": token.character_name,
                "avatar_url": token.avatar_url,
                "wallet_balance": token.wallet_balance or 0.0,
                "active_order_value": order_value,
                "asset_estimate": asset_estimate,
                "active_sell_orders": len(char_orders),
                "low_stock_count": low_stock,
                "known_asset_types": len({asset.type_id for asset in char_assets}),
            }
        )

    restock_items = build_restock_payload(db)

    return {
        "totals": {
            "wallet_balance": total_wallet,
            "active_order_value": total_order_value,
            "asset_estimate": total_asset_estimate,
            "restock_alerts": len(restock_items),
        },
        "characters": character_cards,
        "known_types": len(type_map),
    }


def build_orders_payload(db: Session) -> list[dict[str, Any]]:
    service = EveEsiService(db)
    orders = service.latest_orders()
    type_map = {row.type_id: row.name for row in db.execute(select(TypeCache)).scalars()}
    location_map = {row.location_id: row.name for row in db.execute(select(LocationCache)).scalars()}
    thresholds = {(row.character_id, row.type_id): row for row in service.thresholds()}
    characters = {row.character_id: row.character_name for row in service.characters()}
    default_percent = service.default_low_stock_percent()
    now = utcnow()

    payload = []
    for order in orders:
        threshold = thresholds.get((order.character_id, order.type_id)) or thresholds.get((None, order.type_id))
        threshold_qty = threshold.min_quantity if threshold else 0
        threshold_pct = threshold.low_stock_percent if threshold and threshold.low_stock_percent is not None else default_percent
        remain_pct = (order.volume_remain / order.volume_total * 100) if order.volume_total else 0
        expires_at = order.issued + timedelta(days=order.duration)
        payload.append(
            {
                "order_id": order.order_id,
                "character_id": order.character_id,
                "character_name": characters.get(order.character_id, str(order.character_id)),
                "type_id": order.type_id,
                "item_name": type_map.get(order.type_id, f"Type {order.type_id}"),
                "location_id": order.location_id,
                "location_name": location_map.get(order.location_id, f"Location {order.location_id}"),
                "volume_total": order.volume_total,
                "volume_remain": order.volume_remain,
                "remaining_percent": round(remain_pct, 1),
                "price": order.price,
                "total_value": round(order.volume_remain * order.price, 2),
                "expires_at": expires_at.isoformat(),
                "days_remaining": max((expires_at - now).days, 0),
                "low_stock": order.volume_remain <= threshold_qty or remain_pct <= threshold_pct,
            }
        )
    return payload


def build_restock_payload(db: Session) -> list[dict[str, Any]]:
    service = EveEsiService(db)
    orders = service.latest_orders()
    type_map = {row.type_id: row.name for row in db.execute(select(TypeCache)).scalars()}
    thresholds = {(row.character_id, row.type_id): row for row in service.thresholds()}
    default_percent = service.default_low_stock_percent()
    characters = {row.character_id: row.character_name for row in service.characters()}

    grouped_assets: dict[tuple[int, int], int] = {}
    for asset in service.latest_assets():
        grouped_assets[(asset.character_id, asset.type_id)] = grouped_assets.get((asset.character_id, asset.type_id), 0) + asset.quantity

    alerts = []
    for order in orders:
        threshold = thresholds.get((order.character_id, order.type_id)) or thresholds.get((None, order.type_id))
        threshold_qty = threshold.min_quantity if threshold else 0
        threshold_pct = threshold.low_stock_percent if threshold and threshold.low_stock_percent is not None else default_percent
        remain_pct = (order.volume_remain / order.volume_total * 100) if order.volume_total else 0
        if order.volume_remain > threshold_qty and remain_pct > threshold_pct:
            continue

        stock_on_hand = grouped_assets.get((order.character_id, order.type_id), 0)
        target_qty = max(order.volume_total, threshold_qty)
        required_qty = max(target_qty - stock_on_hand, 0)
        alerts.append(
            {
                "character_id": order.character_id,
                "character_name": characters.get(order.character_id, str(order.character_id)),
                "type_id": order.type_id,
                "item_name": type_map.get(order.type_id, f"Type {order.type_id}"),
                "quantity_remaining": order.volume_remain,
                "remaining_percent": round(remain_pct, 1),
                "stock_on_hand": stock_on_hand,
                "required_restock_qty": required_qty,
                "daily_velocity": round(estimate_daily_velocity(db, order.character_id, order.type_id), 2),
            }
        )

    alerts.sort(key=lambda row: (row["required_restock_qty"] == 0, row["remaining_percent"]))
    return alerts
