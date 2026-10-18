"""
TheFundingHouse: API + frontend estatico en un solo proceso FastAPI.

  GET  /api/status                 estado de refrescos y del historico
  GET  /api/funding/config         comisiones y parametros
  GET  /api/funding/live           pares con la tasa prevista actual
  GET  /api/funding/{1h..31d}      pares con la media del funding LIQUIDADO en la ventana
  GET  /api/funding/series/{sym}   serie horaria de funding por exchange (para graficas)
  GET  /api/token/logos?symbols=   logos (CoinGecko, cacheados)
  POST /api/admin/refresh-live     fuerza un refresh live
  POST /api/admin/sync-history     fuerza una descarga de historico
  /                                frontend (carpeta frontend/)
"""

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, analytics, config, logos
from .adapters import BackpackAdapter, HyperliquidAdapter
from .scheduler import Scheduler
from .store import Store

store = Store(config.DB_PATH)
scheduler = Scheduler(store, [HyperliquidAdapter(), BackpackAdapter()])


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler.start()
    print(f"[startup] TheFundingHouse {__version__} · db={config.DB_PATH} · frontend={config.FRONTEND_DIR}", flush=True)
    yield
    await scheduler.stop()
    store.close()


app = FastAPI(title="TheFundingHouse", version=__version__, lifespan=lifespan)


# ---------------- estado ----------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/status")
async def api_status():
    live_pairs = analytics.compute_live_pairs(scheduler.latest)
    return {"version": __version__, "now": datetime.now(timezone.utc), "pairs_live": len(live_pairs), **scheduler.status()}


@app.get("/api/funding/config")
async def funding_config():
    return {
        "fees": config.FEES,
        "default_fees": config.DEFAULT_FEES,
        "net_apr_horizon_hours": config.NET_APR_HORIZON_HOURS,
        "windows_hours": config.WINDOWS_HOURS,
        "live_refresh_seconds": config.LIVE_REFRESH_SECONDS,
    }


# ---------------- pares ----------------

common_canonical = Query(default=None, description="Filtra por canonical_symbol (BTC, ETH, ...)")
common_min_spread = Query(default=0.0, ge=0.0, description="Spread minimo de APR (ej: 20 => spread>=20)")
common_exchange_in = Query(default=None, description="Ambos exchanges deben pertenecer a este conjunto.")
common_exchange_out = Query(default=None, description="Excluye pares que contengan estos exchanges.")
common_limit = Query(default=100, ge=1, le=5000)


@app.get("/api/funding/live", response_model=List[Dict[str, Any]])
async def funding_live(
    canonical_symbol: Optional[str] = common_canonical,
    min_spread_apr_percent: float = common_min_spread,
    exchange_in: Optional[List[str]] = common_exchange_in,
    exchange_out: Optional[List[str]] = common_exchange_out,
    limit: int = common_limit,
):
    pairs = analytics.compute_live_pairs(scheduler.latest)
    return analytics.filter_pairs(pairs, min_spread_apr_percent, canonical_symbol, exchange_in, exchange_out, limit)


@app.get("/api/funding/series/{canonical_symbol}")
async def funding_series(canonical_symbol: str, hours: float = Query(168.0, gt=0, le=24 * 400)):
    """Serie de funding liquidado por exchange (para graficar un par)."""
    since = int(time.time() - hours * 3600)
    rows = store.history_series(canonical_symbol.upper(), since)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["exchange"], []).append(
            {"ts": datetime.fromtimestamp(r["ts"], tz=timezone.utc), "funding_rate": r["funding_rate"], "apr_percent": analytics.annualize(r["funding_rate"], r["interval_hours"])}
        )
    return {"canonical_symbol": canonical_symbol.upper(), "hours": hours, "series": out}


@app.get("/api/funding/{window}", response_model=List[Dict[str, Any]])
async def funding_window(
    window: str,
    canonical_symbol: Optional[str] = common_canonical,
    min_spread_apr_percent: float = common_min_spread,
    exchange_in: Optional[List[str]] = common_exchange_in,
    exchange_out: Optional[List[str]] = common_exchange_out,
    limit: int = common_limit,
):
    hours = config.WINDOWS_HOURS.get(window)
    if hours is None:
        raise HTTPException(404, f"Unknown window '{window}'. Use one of: live, {', '.join(config.WINDOWS_HOURS)}")
    rows = store.history_window(int(time.time() - hours * 3600))
    pairs = analytics.compute_window_pairs(rows, scheduler.latest, hours)
    return analytics.filter_pairs(pairs, min_spread_apr_percent, canonical_symbol, exchange_in, exchange_out, limit)


# ---------------- logos ----------------

@app.get("/api/token/logos")
async def token_logos(symbols: str = ""):
    return await logos.resolve_logos(symbols.split(","))


@app.get("/api/token/logo/{symbol}")
async def token_logo(symbol: str):
    path = logos.find_cached(symbol)
    if not path:
        raise HTTPException(404, "Logo not available")
    return FileResponse(path)


# ---------------- admin ----------------

@app.post("/api/admin/refresh-live")
async def admin_refresh_live():
    return {"refreshed": await scheduler.refresh_live(), "errors": scheduler.live_errors}


@app.post("/api/admin/sync-history")
async def admin_sync_history(backfill_days: Optional[int] = Query(None, ge=1, le=400)):
    return await scheduler.sync_history(backfill_days)


# ---------------- frontend ----------------

if config.FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
