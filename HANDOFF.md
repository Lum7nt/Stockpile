# Handoff

## 1. Project Overview & Architecture

- Core purpose:
  Build a local web app for EVE Online that authenticates via EVE SSO, syncs character wallet/assets/market orders from ESI, stores local state in SQLite, and shows dashboard/order/restock views on `localhost:8000`.

- Tech stack:
  - Language: Python 3.11+
  - Backend: FastAPI
  - Database: SQLite via SQLAlchemy ORM
  - Frontend: Server-rendered HTML, Tailwind via CDN, Alpine.js via CDN, lightweight fetch-based JS
  - Auth: OAuth 2.0 Authorization Code Flow with PKCE for EVE SSO

- Key entry points:
  - [app/main.py](/C:/Users/Antariska/Documents/eve%20ledger/app/main.py)
    FastAPI app, routes, lifespan setup, background refresh loop, HTML entry route.
  - [app/services/esi.py](/C:/Users/Antariska/Documents/eve%20ledger/app/services/esi.py)
    EVE SSO helpers, token refresh, ESI fetch logic, sync routines, payload builders.
  - [app/models.py](/C:/Users/Antariska/Documents/eve%20ledger/app/models.py)
    SQLAlchemy models for settings, tokens, caches, snapshots, thresholds.
  - [app/templates/index.html](/C:/Users/Antariska/Documents/eve%20ledger/app/templates/index.html)
    Main UI shell and tabs.
  - [app/static/app.js](/C:/Users/Antariska/Documents/eve%20ledger/app/static/app.js)
    Frontend app state, fetch calls, filters, actions.
  - [app/config.py](/C:/Users/Antariska/Documents/eve%20ledger/app/config.py)
    App settings, DB path, ESI/SSO URLs.
  - [requirements.txt](/C:/Users/Antariska/Documents/eve%20ledger/requirements.txt)
  - [README.md](/C:/Users/Antariska/Documents/eve%20ledger/README.md)

## 2. Database Schema & Data Flow

- Main DB models:
  - `AppSetting`
    Stores EVE client ID/secret, callback URL, default low-stock percent.
  - `CharacterToken`
    Stores per-character access token, refresh token, expiry, avatar URL, cached wallet balance.
  - `TypeCache`
    Local `type_id -> item name` cache from ESI universe types.
  - `LocationCache`
    Local `location_id -> station/structure name` cache.
  - `ETagCache`
    Stores ETag values for some ESI endpoints.
  - `MarketOrderSnapshot`
    Currently used as the live current-state order table per character.
    Recent fix changed sync to delete old rows for a character and insert fresh active sell orders.
  - `AssetSnapshot`
    Asset sync snapshot rows.
  - `InventoryThreshold`
    Per-item or per-character low-stock rules.

- Data flow:
  - User configures client credentials in Settings.
  - User logs in through EVE SSO.
  - Callback exchanges auth code for token and stores `CharacterToken`.
  - `/api/sync` triggers:
    - wallet fetch from ESI
    - market orders fetch from ESI
    - assets fetch from ESI
    - local type/location resolution and caching
  - Synced data is persisted in SQLite.
  - `/api/dashboard`, `/api/orders`, `/api/restock`, `/api/settings`, `/api/thresholds` read from SQLite and return JSON to the frontend.

- Background work:
  - `lifespan()` in `app/main.py` creates DB tables and starts a refresh loop.
  - `refresh_loop()` periodically refreshes access tokens for stored characters.
  - It does not currently run full wallet/order/asset sync automatically; sync is user-triggered via the UI.

## 3. Current Project State

- Implemented and currently working:
  - Local FastAPI app booting on `localhost:8000`
  - Dark-mode dashboard/order/restock/settings UI
  - EVE SSO login flow with PKCE
  - Client secret handling corrected so credentials are not sent twice
  - Callback error handling improved to surface EVE/CCP error details
  - Wallet ISK sync and persistence
  - Threshold CRUD endpoints and UI
  - Character cards and multi-character support in the UI
  - Frontend script-order/boot issues fixed so buttons/tabs now work
  - Favicon 404 removed with a `/favicon.ico` handler

- Recent bug fixes applied:
  - Alpine/Tailwind script race fixed in `index.html`
  - OAuth token exchange updated to include `redirect_uri`
  - OAuth requests updated to avoid duplicated client credentials when secret is configured
  - Type/location cache inserts made safer against duplicate insert races
  - Market order parsing updated to tolerate missing `is_buy_order`
  - Market order sync updated to replace per-character current order rows instead of relying only on timestamped snapshots
  - `latest_orders()` and per-character order reads updated to query live sell-order rows

- Known issues / uncertainty:
  - The user recently reported that dashboard sell-order totals still may not reflect synced orders and the Orders tab dropdowns may still be empty. A backend fix was applied, but it has not yet been validated against a real live `/api/orders` response after that patch.
  - The frontend currently expects order payload fields like:
    - `character_name`
    - `item_name`
    - `location_name`
    - `remaining_percent`
    - `days_remaining`
    - `low_stock`
  - If `app/services/esi.py` gets rewritten again, the payload shape must remain aligned with `app/static/app.js` and `app/templates/index.html`.
  - `AssetSnapshot` still uses timestamp-based latest-snapshot logic while `MarketOrderSnapshot` is now being used more like a current-state table.
  - Asset valuation is still approximate and derived from matching sell-order prices by `type_id`.

## 4. API & External Dependencies

- Internal app endpoints:
  - `GET /`
    Main HTML app.
  - `GET /favicon.ico`
    Empty 204 response.
  - `GET /api/settings`
  - `POST /api/settings`
  - `GET /auth/login`
  - `GET /auth/callback`
  - `POST /api/sync`
  - `GET /api/dashboard`
  - `GET /api/orders`
  - `GET /api/restock`
  - `GET /api/thresholds`
  - `POST /api/thresholds`
  - `DELETE /api/thresholds/{threshold_id}`

- Third-party APIs:
  - EVE SSO authorize URL:
    `https://login.eveonline.com/v2/oauth/authorize`
  - EVE SSO token URL:
    `https://login.eveonline.com/v2/oauth/token`
  - EVE SSO verify URL:
    `https://login.eveonline.com/oauth/verify`
  - ESI base URL:
    `https://esi.evetech.net/latest`

- Required ESI scopes:
  - `esi-wallet.read_character_wallet.v1`
  - `esi-assets.read_assets.v1`
  - `esi-markets.read_character_orders.v1`
  - `esi-universe.read_structures.v1`

- Auth flow notes:
  - Uses Authorization Code + PKCE.
  - If `client_secret` is configured, credentials are sent via HTTP Basic auth only.
  - If `client_secret` is blank, `client_id` is sent in the request body.
  - `redirect_uri` is included in token exchange and refresh requests.

## 5. Instructions for Next AI Agent

- How to run locally:
  - From repo root:
    - `python -m venv .venv`
    - `.venv\Scripts\Activate.ps1`
    - `pip install -r requirements.txt`
    - `uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`
  - Open:
    - `http://127.0.0.1:8000`

- Basic verification flow:
  - Open Settings tab.
  - Enter EVE client ID and optional secret.
  - Save settings.
  - Add character through SSO.
  - Press `Sync All`.
  - Verify:
    - wallet values populate
    - orders appear in Orders tab
    - character/location filters populate
    - dashboard `Order Value` and `active sell orders` update
    - restock list reflects low-stock rows

- Recommended debugging steps:
  - Check server logs during `/api/sync`.
  - Hit these URLs directly in browser/devtools after syncing:
    - `/api/orders`
    - `/api/dashboard`
    - `/api/restock`
  - Confirm DB contents if needed:
    - inspect `data/eve_ledger.db`
    - verify `market_order_snapshots` rows exist for the character
  - If orders still do not show:
    - compare actual `/api/orders` JSON keys with what `app.js` expects
    - confirm `location_name` and `character_name` are present
    - confirm `build_dashboard_payload()` is aggregating the same rows

- Priority tasks left:
  - Validate the latest order-sync fix against live ESI data
  - Ensure `/api/orders`, `/api/dashboard`, and `/api/restock` all use the same source-of-truth order rows
  - Reconcile whether `MarketOrderSnapshot` should remain a current-state table or become true historical snapshots again
  - Improve error surfacing in the UI for sync failures
  - Add tests for:
    - OAuth payload behavior
    - market-order sync replacement logic
    - dashboard/order/restock payload generation
  - Consider replacing CDN frontend dependencies with local/static assets if offline or deterministic startup matters

- Important context:
  - The user asked specifically for fixes around market orders not appearing after sync.
  - The most recent work focused on backend order syncing and frontend data compatibility.
  - Before making more structural changes, verify the live payload returned by `/api/orders` after a real sync with a logged-in character.
