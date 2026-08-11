# EVE Local Ledger

`EVE Local Ledger` is a portable FastAPI + SQLite web app that runs locally on `http://localhost:8000` and tracks:

- multi-character wallet balances
- active sell orders
- local cached asset snapshots
- restock alerts driven by thresholds and order depletion

The app uses EVE SSO with OAuth 2.0 Authorization Code Flow + PKCE, stores tokens locally in SQLite, and caches item and location lookups to reduce repeated ESI calls.

## Features

- Multi-character login support
- Dashboard with wallet, order value, and estimated asset valuation
- Sell-order table with low-stock highlighting
- Restock alert board with required quantity and recent sales velocity
- Settings screen for EVE client configuration and threshold rules
- Local snapshot history for market orders and assets
- ETag-aware order and asset syncing to respect ESI `304 Not Modified`
- Background token refresh loop to keep active sessions working

## Project Layout

```text
app/
  main.py
  config.py
  database.py
  models.py
  schemas.py
  services/
    esi.py
  static/
    app.js
  templates/
    index.html
data/
requirements.txt
README.md
```

## Requirements

- Python 3.11 or newer
- An EVE developer application from the CCP Developer Portal

## Register an EVE Application

1. Go to [developers.eveonline.com](https://developers.eveonline.com/).
2. Create a new application.
3. Set the callback URL to `http://localhost:8000/auth/callback`.
4. Copy the client ID.
5. Copy the client secret if you want to use a confidential app configuration. PKCE works without it, but this app accepts it if you choose to store it locally.
6. Make sure your application can request these scopes:
   - `esi-wallet.read_character_wallet.v1`
   - `esi-assets.read_assets.v1`
   - `esi-markets.read_character_orders.v1`
   - `esi-universe.read_structures.v1`

## Start the Server

1. Create and activate a virtual environment.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

2. Install dependencies.

```powershell
pip install -r requirements.txt
```

3. Start the app.

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

4. Open `http://localhost:8000`.

## First-Time Setup

1. Open the **Settings** tab.
2. Enter your EVE application client ID.
3. Optionally enter your client secret.
4. Save settings.
5. Click **Begin SSO Login** and authenticate each character you want to track.
6. After you return to the app, click **Sync All**.
7. Add threshold rules for important items by `type_id`.

## Notes

- Tokens, thresholds, caches, and snapshots are stored in `data/eve_ledger.db`.
- Asset valuation is estimated using current active sell-order prices for matching item types already known to the app.
- Sales velocity is derived from recent order snapshot deltas, so it becomes more useful after multiple syncs over time.
- The app is intended for personal local use and does not encrypt secrets at rest.
