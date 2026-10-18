import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from adapters.backpack import BackpackAdapter
from adapters.hyperliquid import HyperliquidAdapter
from funding_history import aggregate_to_8h, fetch_raw_funding_history

load_dotenv()

app = FastAPI(
    title="Data Ingestion Service",
    description="Microservicio para la ingestión de funding rates y su histórico.",
    version="0.4.0",
    openapi_url="/openapi.json",
)

# -------------------------
# Configuración Mongo
# -------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://root:example@mongo:27017")
MONGO_DB = os.getenv("MONGO_DB", "tfh")
# Dias que conservamos funding_timeseries (TTL index). 0 = sin TTL.
TIMESERIES_TTL_DAYS = int(os.getenv("TIMESERIES_TTL_DAYS", "60"))

COL_HYPER_CURRENT = "funding_hyperliquid_current"
COL_BACKPACK_CURRENT = "funding_backpack_current"
COL_FUNDING_TS = "funding_timeseries"

mongo_client: Optional[AsyncIOMotorClient] = None
db = None
hyper_current_col = None
backpack_current_col = None
funding_ts_col = None

hyper_adapter = HyperliquidAdapter()
backpack_adapter = BackpackAdapter()

FUNDING_INTERVAL_HOURS_DEFAULT = 1.0


# -------------------------
# Helpers
# -------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def canonical_symbol_from_snapshot(snap: Dict[str, Any]) -> str:
    """
    Fallback para generar un canonical_symbol cuando el adapter no lo trae.

    Ejemplos:
      - Backpack:  "kLUNC_USDC_PERP" -> "kLUNC"
      - Hyper:     "BTC-USD"         -> "BTC"
    """
    symbol = str(snap.get("symbol") or "")
    if symbol.endswith("-USD"):
        return symbol[:-4]
    if symbol.endswith("_USDC_PERP"):
        return symbol.replace("_USDC_PERP", "")
    if symbol.endswith("_PERP"):
        return symbol[:-5]
    return symbol


def _derive_funding_fields(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """funding_rate/interval normalizados + funding_percent y apr_percent."""
    funding_rate = float(snapshot.get("funding_rate") or 0.0)
    interval_hours = float(snapshot.get("funding_interval_hours") or FUNDING_INTERVAL_HOURS_DEFAULT)
    if interval_hours <= 0:
        interval_hours = FUNDING_INTERVAL_HOURS_DEFAULT
    periods_per_year = (365.0 * 24.0) / interval_hours
    return {
        "funding_rate": funding_rate,
        "funding_interval_hours": interval_hours,
        "funding_percent": funding_rate * 100.0,
        "apr_percent": funding_rate * periods_per_year * 100.0,
    }


async def insert_funding_timeseries(snapshot: Dict[str, Any]) -> None:
    """
    Inserta una fila en funding_timeseries con campos normalizados (sin payload raw).
    """
    if funding_ts_col is None:
        raise RuntimeError("Mongo funding_ts_col not initialized")

    doc = {
        "exchange": snapshot.get("exchange"),
        "symbol": snapshot.get("symbol"),
        "canonical_symbol": snapshot.get("canonical_symbol"),
        **_derive_funding_fields(snapshot),
        "timestamp": snapshot.get("timestamp") or now_utc(),
        "next_funding_time": snapshot.get("next_funding_time"),
        "mark_price": snapshot.get("mark_price"),
        "index_price": snapshot.get("index_price") or snapshot.get("oracle_price"),
        "open_interest": snapshot.get("open_interest"),
        "open_interest_usd": snapshot.get("open_interest_usd"),
        "volume_24h": snapshot.get("volume_24h"),
        "inserted_at": now_utc(),
    }
    await funding_ts_col.insert_one(doc)


async def upsert_current_and_ts(col, snapshot: Dict[str, Any]) -> None:
    """
    Actualiza la colección *_current y añade entrada en funding_timeseries.
    """
    if col is None:
        raise RuntimeError("Mongo current_col not initialized")

    canonical = snapshot.get("canonical_symbol") or canonical_symbol_from_snapshot(snapshot)

    normalized = {
        **snapshot,
        **_derive_funding_fields(snapshot),
        "canonical_symbol": canonical,
        "timestamp": snapshot.get("timestamp") or now_utc(),
    }
    # open_interest_usd: ambos exchanges reportan OI en monedas
    if normalized.get("open_interest_usd") is None:
        oi = normalized.get("open_interest")
        mark = normalized.get("mark_price")
        if oi and mark:
            normalized["open_interest_usd"] = float(oi) * float(mark)

    key = {"exchange": normalized["exchange"], "symbol": normalized["symbol"]}
    await col.update_one(key, {"$set": normalized}, upsert=True)
    await insert_funding_timeseries(normalized)


async def _refresh_exchange(exchange_name: str, adapter, col) -> Dict[str, Any]:
    """
    Refresca todos los mercados de un exchange, actualiza *_current, añade al histórico
    y elimina de *_current los mercados que ya no existen (delistados / cerrados).
    """
    if col is None:
        raise HTTPException(500, "Mongo not initialized")

    try:
        snapshots: List[Dict[str, Any]] = await adapter.get_all_market_data()
    except Exception as e:
        raise HTTPException(502, f"Error calling {exchange_name} API: {e}")

    ok = 0
    seen_symbols: List[str] = []
    for snap in snapshots:
        try:
            snap["exchange"] = exchange_name
            await upsert_current_and_ts(col, snap)
            seen_symbols.append(snap["symbol"])
            ok += 1
        except Exception as e:
            print(f"[{exchange_name}] error upserting {snap.get('symbol')}: {e}", flush=True)
            continue

    removed = 0
    if seen_symbols:
        res = await col.delete_many({"symbol": {"$nin": seen_symbols}})
        removed = res.deleted_count

    return {
        "exchange": exchange_name,
        "markets_processed": len(snapshots),
        "successful": ok,
        "removed_stale": removed,
    }


async def _list_symbols_for_exchange(exchange_name: str) -> List[str]:
    """
    Devuelve la lista de símbolos normalizados para un exchange usando el adapter o la colección *_current.
    """
    exchange = exchange_name.lower()
    symbols: List[str] = []

    try:
        adapter = backpack_adapter if exchange == "backpack" else hyper_adapter
        markets = await adapter.get_markets()
        symbols = [m.get("symbol") for m in markets if m.get("symbol")]
    except Exception:
        symbols = []

    if not symbols:
        try:
            col = backpack_current_col if exchange == "backpack" else hyper_current_col
            if col is not None:
                symbols = await col.distinct("symbol")
        except Exception:
            symbols = []

    return list(dict.fromkeys(symbols))


async def _bootstrap_funding_history_internal(days: int = 30) -> Dict[str, Any]:
    """
    Descarga histórico de funding para cada exchange/símbolo y lo guarda en funding_timeseries (agregado a 8h).
    """
    if funding_ts_col is None:
        raise HTTPException(500, "Mongo funding_timeseries not initialized")

    to_ts = now_utc()
    from_ts = to_ts - timedelta(days=days)
    summary: List[Dict[str, Any]] = []

    for exchange_name in ("Hyperliquid", "Backpack"):
        symbols = await _list_symbols_for_exchange(exchange_name)
        inserted = 0

        for symbol in symbols:
            try:
                raw_events = await fetch_raw_funding_history(exchange_name, symbol, from_ts, to_ts)
            except Exception:
                continue

            canonical = canonical_symbol_from_snapshot({"symbol": symbol})
            docs: List[Dict[str, Any]] = []
            for snap in aggregate_to_8h(raw_events):
                docs.append(
                    {
                        "exchange": exchange_name,
                        "symbol": symbol,
                        "canonical_symbol": canonical,
                        **_derive_funding_fields(snap),
                        "timestamp": snap.get("timestamp") or to_ts,
                        "source": "history",
                        "inserted_at": now_utc(),
                    }
                )

            if docs:
                try:
                    await funding_ts_col.insert_many(docs, ordered=False)
                    inserted += len(docs)
                except Exception:
                    pass

        summary.append(
            {"exchange": exchange_name, "symbols_processed": len(symbols), "snapshots_inserted": inserted}
        )

    return {"status": "ok", "days": days, "run_at": to_ts.isoformat(), "exchanges": summary}


async def _maybe_bootstrap_on_startup():
    """
    Si funding_timeseries está vacío o desactualizado, lanza bootstrap automático (30 días) en segundo plano.
    """
    if funding_ts_col is None:
        return

    try:
        last = await funding_ts_col.find_one(sort=[("timestamp", -1)])
    except Exception:
        last = None

    last_ts = last.get("timestamp") if last else None
    if isinstance(last_ts, datetime) and last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)

    if not last_ts or last_ts < now_utc() - timedelta(days=3):
        print("[startup] No recent funding_timeseries, bootstrapping 30 days in background...", flush=True)
        try:
            res = await _bootstrap_funding_history_internal(days=30)
            print(f"[startup] Bootstrap finished: {res}", flush=True)
        except Exception as e:
            print(f"[startup] Bootstrap failed: {e}", flush=True)
    else:
        print("[startup] funding_timeseries is recent, skipping bootstrap", flush=True)


# -------------------------
# Eventos de arranque
# -------------------------

@app.on_event("startup")
async def startup_event():
    global mongo_client, db, hyper_current_col, backpack_current_col, funding_ts_col

    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client[MONGO_DB]

    hyper_current_col = db[COL_HYPER_CURRENT]
    backpack_current_col = db[COL_BACKPACK_CURRENT]
    funding_ts_col = db[COL_FUNDING_TS]

    try:
        await funding_ts_col.create_index([("canonical_symbol", 1), ("exchange", 1), ("timestamp", -1)])
        await funding_ts_col.create_index([("timestamp", -1)])
        if TIMESERIES_TTL_DAYS > 0:
            await funding_ts_col.create_index(
                "inserted_at", expireAfterSeconds=TIMESERIES_TTL_DAYS * 86400, name="inserted_at_ttl"
            )
    except Exception as exc:
        print(f"[startup] index warning: {exc}", flush=True)

    # No bloquea el arranque: el servicio responde mientras se descarga el histórico.
    asyncio.create_task(_maybe_bootstrap_on_startup())
    print("[startup] data-ingestion-service ready", flush=True)


@app.on_event("shutdown")
async def shutdown_event():
    global mongo_client
    if mongo_client is not None:
        mongo_client.close()


# -------------------------
# Endpoints
# -------------------------

@app.get("/status", response_model=dict)
@app.get("/health", response_model=dict)
async def get_status():
    return {
        "status": "ok",
        "service": "data-ingestion",
        "version": "0.4.0",
        "timestamp": now_utc().isoformat(),
    }


@app.post("/funding/bootstrap-history", response_model=dict)
async def bootstrap_funding_history(days: int = 30):
    """
    Descarga histórico de funding (days días hacia atrás), lo agrega a 8h y lo guarda en funding_timeseries.
    """
    try:
        return await _bootstrap_funding_history_internal(days=days)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error en bootstrap de histórico: {e}")


@app.post("/backpack/refresh", response_model=dict)
async def refresh_backpack():
    """Refresca todos los mercados perp operables de Backpack."""
    return await _refresh_exchange("Backpack", backpack_adapter, backpack_current_col)


@app.post("/hyperliquid/refresh", response_model=dict)
async def refresh_hyperliquid():
    """Refresca todos los mercados perp activos (no delistados) de Hyperliquid."""
    return await _refresh_exchange("Hyperliquid", hyper_adapter, hyper_current_col)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8001)))
