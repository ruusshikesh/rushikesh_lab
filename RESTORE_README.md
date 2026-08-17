# Rush Algo - Recovered Project

Full project restored from the backup you uploaded on Jul 27, **plus every fix
and feature built after that snapshot**.

## What's included

**Original code (from your zip):** all backend modules - strategy engine,
backtest engine, indicators library, risk manager, compliance engine, telegram
bot, schemas, storage, NSE fundamental engine - and the full frontend scaffold
(package.json, vite config, index.html, src/).

**Your NSE data that survived:** `backend/data_cache/fundamental_universe.json`
(1.1 MB ranked universe), `kite_instruments_nse.json`, `EQUITY_L.csv`,
`price_history/`, plus `data_store/strategies.json` (your saved strategies).

**Post-backup work, applied:**
- US market module (`backend/data_us/`) - Finnhub 6-endpoint fetcher,
  6-category scoring engine, US price fetcher
- `radar.py` + `radar_us.py` - Buy & Sell Radar for both markets
- `kite_ticker.py` - live WebSocket price streaming
- Zerodha data layer (historical + auth CLI)
- Stop/resume on both fetches, confirm dialogs, progress bars
- US market-cap segments (Mega/Large/Mid/Small/Micro)
- All bug fixes: kill switch 20%->3%, hybrid exit, exclusion guard,
  timezone normalisation, stale-tick expiry, subscription union,
  `_find_line` exact-match priority, growth-source priority, data-integrity
  penalties, MIC-based OTC filtering

## What is NOT included (and why)

- **`.env`** - it held your API keys and was never uploaded. Use
  `backend/.env.example` as a template and re-enter your keys.
- **`venv/` and `node_modules/`** - regenerable, and removing them cut the
  download from 135 MB to ~13 MB. Reinstall commands below.
- **`fundamentals_by_symbol.json`** (the 1.5 GB NSE per-stock cache) and the
  entire US cache - too large to have been uploaded. **This data is
  re-fetchable**, see "Re-fetching data" below.

## Setup

```bat
cd rush-algo-fixed\backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
:: now edit .env and paste your API keys

cd ..\frontend
npm install
```

## Running

```bat
:: Terminal 1 - daily Zerodha auth
cd rush-algo-fixed\backend
venv\Scripts\activate
python -m brokers.zerodha_client auth

:: Terminal 2 - backend
uvicorn main:app --port 8000

:: Terminal 3 - frontend
cd rush-algo-fixed\frontend
npm run dev
```

## Re-fetching data

**NSE** (IndianAPI): Stock Universe - NSE tab -> Refresh Data. Or:
`python refresh_fundamentals.py --report` (free, shows what's missing) then
`python refresh_fundamentals.py --fetch --rebuild-universe`.

**US** (Finnhub, free tier): Stock Universe - USA tab -> Refresh from Finnhub.
~4,950 stocks x 6 endpoints, rate-limited to 55/min, roughly 9 hours.
Fully resumable - Stop anytime, click Refresh to continue.

Both fetches save each stock to disk the moment it's fetched, so an interruption
never loses work.

## Back this up

Copy the whole folder somewhere off-machine (cloud drive / external disk).
The expensive part is the fetched caches under `backend/data_cache*/` - the code
is small, the data is what takes hours to rebuild.
