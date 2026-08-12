from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import BASE_DIR, settings
from app.database import Base, engine, get_db, SessionLocal
from app.models import AppSetting, CharacterToken, InventoryThreshold
from app.schemas import ManualSyncRequest, SettingsUpdate, ThresholdUpsert
from app.services.esi import (
    EveEsiService,
    build_dashboard_payload,
    build_orders_payload,
    build_pkce_pair,
    build_restock_payload,
    ensure_app_settings,
)


refresh_task: asyncio.Task | None = None
logger = logging.getLogger(__name__)


async def refresh_loop():
    while True:
        db = SessionLocal()
        try:
            service = EveEsiService(db)
            for token in service.characters():
                try:
                    await service.refresh_if_needed(token)
                except Exception:
                    db.rollback()
                    logger.exception("Background token refresh failed for character_id=%s", token.character_id)
                    continue
        except Exception:
            db.rollback()
            logger.exception("Background refresh loop iteration failed")
        finally:
            db.close()
        await asyncio.sleep(settings.refresh_interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global refresh_task
    Base.metadata.create_all(bind=engine)
    refresh_task = asyncio.create_task(refresh_loop())
    try:
        yield
    finally:
        if refresh_task:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="EVE Local Ledger", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/api/settings")
async def get_settings(db: Session = Depends(get_db)):
    record = ensure_app_settings(db)
    characters = list(db.execute(select(CharacterToken).order_by(CharacterToken.character_name)).scalars())
    return {
        "client_id": settings.eve_client_id,
        "client_secret_configured": bool(record.client_secret),
        "callback_url": record.callback_url,
        "default_low_stock_percent": record.default_low_stock_percent,
        "required_scopes": [
            "esi-wallet.read_character_wallet.v1",
            "esi-assets.read_assets.v1",
            "esi-markets.read_character_orders.v1",
            "esi-universe.read_structures.v1",
        ],
        "characters": [
            {
                "character_id": row.character_id,
                "character_name": row.character_name,
                "avatar_url": row.avatar_url,
            }
            for row in characters
        ],
    }


@app.post("/api/settings")
async def save_settings(payload: SettingsUpdate, db: Session = Depends(get_db)):
    record = ensure_app_settings(db)
    record.client_id = settings.eve_client_id
    record.client_secret = payload.client_secret
    record.callback_url = payload.callback_url
    record.default_low_stock_percent = payload.default_low_stock_percent
    db.add(record)
    db.commit()
    return {"ok": True}


@app.get("/auth/login")
async def auth_login(db: Session = Depends(get_db)):
    service = EveEsiService(db)
    if not service.app_settings.client_id:
        raise HTTPException(status_code=400, detail="Configure your EVE client ID in Settings first.")
    state = secrets.token_urlsafe(24)
    verifier, challenge = build_pkce_pair()
    url = service.build_authorize_url(state, challenge)
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie("eve_oauth_state", state, httponly=True, samesite="lax")
    response.set_cookie("eve_code_verifier", verifier, httponly=True, samesite="lax")
    return response


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str, state: str, db: Session = Depends(get_db)):
    cookie_state = request.cookies.get("eve_oauth_state")
    verifier = request.cookies.get("eve_code_verifier")
    if not cookie_state or cookie_state != state or not verifier:
        raise HTTPException(status_code=400, detail="OAuth state verification failed.")

    service = EveEsiService(db)
    token_data = await service.exchange_code(code, verifier)
    verify_data = await service.verify_access_token(token_data["access_token"])
    await service.save_token_from_callback(token_data, verify_data)

    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("eve_oauth_state")
    response.delete_cookie("eve_code_verifier")
    return response


@app.post("/api/sync")
async def manual_sync(payload: ManualSyncRequest, db: Session = Depends(get_db)):
    service = EveEsiService(db)
    characters = service.characters()
    if payload.character_id is not None:
        characters = [row for row in characters if row.character_id == payload.character_id]
    results = []
    for token in characters:
        summary = await service.sync_character(token)
        results.append({"character_id": token.character_id, "character_name": token.character_name, **summary})
    return {"ok": True, "results": results}


@app.get("/api/dashboard")
async def dashboard(db: Session = Depends(get_db)):
    return build_dashboard_payload(db)


@app.get("/api/orders")
async def orders(db: Session = Depends(get_db)):
    return {"items": build_orders_payload(db)}


@app.get("/api/restock")
async def restock(db: Session = Depends(get_db)):
    return {"items": build_restock_payload(db)}


@app.get("/api/thresholds")
async def thresholds(db: Session = Depends(get_db)):
    rows = list(db.execute(select(InventoryThreshold).order_by(InventoryThreshold.type_id)).scalars())
    return {
        "items": [
            {
                "id": row.id,
                "character_id": row.character_id,
                "type_id": row.type_id,
                "min_quantity": row.min_quantity,
                "low_stock_percent": row.low_stock_percent,
            }
            for row in rows
        ]
    }


@app.post("/api/thresholds")
async def upsert_threshold(payload: ThresholdUpsert, db: Session = Depends(get_db)):
    row = db.execute(
        select(InventoryThreshold).where(
            InventoryThreshold.character_id == payload.character_id,
            InventoryThreshold.type_id == payload.type_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = InventoryThreshold(
            character_id=payload.character_id,
            type_id=payload.type_id,
            min_quantity=payload.min_quantity,
            low_stock_percent=payload.low_stock_percent,
        )
    else:
        row.min_quantity = payload.min_quantity
        row.low_stock_percent = payload.low_stock_percent

    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "id": row.id}


@app.delete("/api/thresholds/{threshold_id}")
async def delete_threshold(threshold_id: int, db: Session = Depends(get_db)):
    db.execute(delete(InventoryThreshold).where(InventoryThreshold.id == threshold_id))
    db.commit()
    return JSONResponse({"ok": True})
