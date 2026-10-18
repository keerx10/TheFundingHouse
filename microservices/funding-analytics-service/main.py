"""
Funding Analytics Service

Calcula pares de arbitraje de funding delta-neutral entre exchanges:
  LONG  en el exchange con APR mas bajo
  SHORT en el exchange con APR mas alto
  spread_apr_percent = APR_short - APR_long

- /funding/live        -> pares calculados ahora mismo desde funding_*_current
- /funding/{ventana}   -> media del spread en la ventana usando funding_arbitrage_pairs_ts
- /funding/arbitrage/refresh -> guarda un snapshot por minuto (lo llama el scheduler)

Cada par incluye el coste de comisiones (abrir + cerrar las dos patas), las horas
necesarias para cubrirlo con funding (breakeven_hours) y el APR neto a un horizonte.
"""

import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

load_dotenv()

app = FastAPI(
    title="Funding Analytics Service",
    description="Microservicio para analizar funding/APR a partir de Mongo.",
    version="0.6.0",
    openapi_url="/openapi.json",
)

# -------------------------
# Config
# -------------------------

MONGO_URI = os.getenv("MONGO_URI", "mongodb://root:example@mongo:27017")
MONGO_DB = os.getenv("MONGO_DB", "tfh")

COL_FUNDING_PAIRS_TS = "funding_arbitrage_pairs_ts"
CURRENT_COL_PREFIX = "funding_"
CURRENT_COL_SUFFIX = "_current"

HOURS_PER_YEAR = 365.0 * 24.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# Comisiones por exchange, tanto por uno sobre el nocional de cada pata.
# Hyperliquid tier 0: taker 0.045% / maker 0.015%.  Backpack base: taker 0.05% / maker 0.02%.
FEES: Dict[str, Dict[str, float]] = {
    "hyperliquid": {
        "taker": _env_float("FEE_HYPERLIQUID_TAKER", 0.00045),
        "maker": _env_float("FEE_HYPERLIQUID_MAKER", 0.00015),
    },
    "backpack": {
        "taker": _env_float("FEE_BACKPACK_TAKER", 0.0005),
        "maker": _env_float("FEE_BACKPACK_MAKER", 0.0002),
    },
}
DEFAULT_FEES = {
    "taker": _env_float("FEE_DEFAULT_TAKER", 0.0005),
    "maker": _env_float("FEE_DEFAULT_MAKER", 0.0002),
}
# Horizonte de mantenimiento asumido para net_apr_percent (horas)
NET_APR_HORIZON_HOURS = _env_float("NET_APR_HORIZON_HOURS", 24.0)
# Cadencia de snapshots del scheduler; se usa para calcular la cobertura de cada ventana
SNAPSHOT_INTERVAL_SECONDS = _env_float("SNAPSHOT_INTERVAL_SECONDS", 60.0)

WINDOWS_HOURS: Dict[str, float] = {
    "1h": 1.0,
    "8h": 8.0,
    "24h": 24.0,
    "3d": 72.0,
    "7d": 168.0,
    "15d": 360.0,
    "31d": 744.0,
}

mongo_client: Optional[AsyncIOMotorClient] = None
db = None
funding_pairs_col = None


# -------------------------
# Helpers comunes
# -------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_aware(ts: Optional[datetime]) -> Optional[datetime]:
    """Mongo devuelve datetimes naive; los tratamos siempre como UTC."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _minute_floor(ts: datetime) -> datetime:
    """Normaliza el timestamp al inicio del minuto (UTC, con tz info)."""
    return _to_aware(ts).replace(second=0, microsecond=0)


def _f(value: Any) -> Optional[float]:
    """float finito o None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def annualize_funding(funding_rate: float, funding_interval_hours: float = 1.0) -> float:
    """
    funding_rate: tanto por uno por periodo (ej: 0.0001 == 0.01% cada hora)
    APR% = funding_rate * (#periodos/año) * 100
    """
    if funding_interval_hours <= 0:
        funding_interval_hours = 1.0
    return funding_rate * (HOURS_PER_YEAR / funding_interval_hours) * 100.0


def _fee(exchange: Optional[str], kind: str) -> float:
    return FEES.get((exchange or "").lower(), DEFAULT_FEES)[kind]


def _side_from_current(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normaliza un doc de funding_*_current a la 'pata' que exponemos en cada par.
    """
    funding_rate = _f(doc.get("funding_rate")) or 0.0
    interval = _f(doc.get("funding_interval_hours")) or 1.0
    if interval <= 0:
        interval = 1.0
    mark_price = _f(doc.get("mark_price")) or _f(doc.get("price"))
    open_interest = _f(doc.get("open_interest"))
    open_interest_usd = _f(doc.get("open_interest_usd"))
    if open_interest_usd is None and open_interest and mark_price:
        open_interest_usd = open_interest * mark_price

    return {
        "exchange": doc.get("exchange"),
        "symbol": doc.get("symbol"),
        "funding_rate": funding_rate,
        "funding_percent": funding_rate * 100.0,
        "funding_interval_hours": interval,
        "apr_percent": annualize_funding(funding_rate, interval),
        "mark_price": mark_price,
        "index_price": _f(doc.get("index_price")),
        "open_interest": open_interest,
        "open_interest_usd": open_interest_usd,
        "volume_24h": _f(doc.get("volume_24h")),
        "next_funding_time": _to_aware(doc.get("next_funding_time")),
        "max_leverage": doc.get("max_leverage"),
        "last_updated": _to_aware(doc.get("timestamp") or doc.get("funding_timestamp")),
    }


def _enrich_pair(pair: Dict[str, Any]) -> Dict[str, Any]:
    """
    Añade a un par: diferencia de precio entre patas, comisiones de ida y vuelta,
    horas de funding necesarias para cubrirlas y APR neto al horizonte configurado.
    """
    long_m = pair.get("long_market") or {}
    short_m = pair.get("short_market") or {}
    spread = _f(pair.get("spread_apr_percent"))

    lm, sm = _f(long_m.get("mark_price")), _f(short_m.get("mark_price"))
    price_spread = None
    if lm and sm and lm > 0 and sm > 0:
        # Positivo = la pata short cotiza mas cara que la long (entrada favorable)
        price_spread = (sm - lm) / ((lm + sm) / 2.0) * 100.0

    taker_l, taker_s = _fee(long_m.get("exchange"), "taker"), _fee(short_m.get("exchange"), "taker")
    maker_l, maker_s = _fee(long_m.get("exchange"), "maker"), _fee(short_m.get("exchange"), "maker")
    # % del nocional de una pata: abrir y cerrar las dos patas
    fee_taker_round_trip = 2.0 * (taker_l + taker_s) * 100.0
    # entrar con limit (maker) y salir a mercado (taker)
    fee_maker_entry_round_trip = ((maker_l + maker_s) + (taker_l + taker_s)) * 100.0

    hourly_yield = (spread / HOURS_PER_YEAR) if (spread is not None and spread > 0) else None
    breakeven = (fee_taker_round_trip / hourly_yield) if hourly_yield else None
    breakeven_maker = (fee_maker_entry_round_trip / hourly_yield) if hourly_yield else None
    net_apr = (
        spread - fee_taker_round_trip * (HOURS_PER_YEAR / NET_APR_HORIZON_HOURS)
        if spread is not None
        else None
    )

    pair.update(
        {
            "price_spread_percent": price_spread,
            "fee_round_trip_percent": fee_taker_round_trip,
            "fee_round_trip_percent_maker_entry": fee_maker_entry_round_trip,
            "breakeven_hours": breakeven,
            "breakeven_hours_maker_entry": breakeven_maker,
            "net_apr_percent": net_apr,
            "net_apr_horizon_hours": NET_APR_HORIZON_HOURS,
        }
    )
    return pair


# -------------------------
# LIVE: pares desde funding_*_current
# -------------------------

async def _fetch_current_sides() -> Dict[str, List[Dict[str, Any]]]:
    """
    Lee todas las colecciones funding_*_current y agrupa las patas por canonical_symbol.
    """
    if db is None:
        raise RuntimeError("Mongo client not initialized")

    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for coll_name in await db.list_collection_names():
        if not (coll_name.startswith(CURRENT_COL_PREFIX) and coll_name.endswith(CURRENT_COL_SUFFIX)):
            continue
        exchange_from_name = coll_name[len(CURRENT_COL_PREFIX) : -len(CURRENT_COL_SUFFIX)].strip().lower()
        if not exchange_from_name:
            continue

        docs = await db[coll_name].find({}).to_list(length=5000)
        for doc in docs:
            canonical = doc.get("canonical_symbol") or doc.get("symbol")
            if not canonical or not isinstance(canonical, str):
                continue
            doc.setdefault("exchange", exchange_from_name)
            by_symbol[canonical].append(_side_from_current(doc))

    return by_symbol


def compute_arbitrage_pairs(sides_by_symbol: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Genera todas las combinaciones de pares por canonical_symbol.
    LONG = APR mas bajo; SHORT = APR mas alto.
    """
    now = now_utc()
    results: List[Dict[str, Any]] = []

    for canonical, sides in sides_by_symbol.items():
        latest_by_exchange: Dict[str, Dict[str, Any]] = {}
        for side in sides:
            ex = side.get("exchange")
            if not ex:
                continue
            prev = latest_by_exchange.get(ex)
            if prev is None or (side.get("last_updated") or now) > (prev.get("last_updated") or now):
                latest_by_exchange[ex] = side

        if len(latest_by_exchange) < 2:
            continue

        for ex_a, ex_b in combinations(sorted(latest_by_exchange), 2):
            a, b = latest_by_exchange[ex_a], latest_by_exchange[ex_b]
            long_side, short_side = (a, b) if a["apr_percent"] <= b["apr_percent"] else (b, a)
            pair_ts = _minute_floor(max(a.get("last_updated") or now, b.get("last_updated") or now))

            results.append(
                _enrich_pair(
                    {
                        "pair_id": f"{canonical}:{ex_a}-{ex_b}",
                        "canonical_symbol": canonical,
                        "timestamp": pair_ts,
                        "source": "live",
                        "data_source": "live",
                        "spread_apr_percent": short_side["apr_percent"] - long_side["apr_percent"],
                        "long_market": dict(long_side),
                        "short_market": dict(short_side),
                    }
                )
            )

    return results


def _filter_pairs(
    pairs: List[Dict[str, Any]],
    min_spread_apr_percent: float = 0.0,
    canonical_symbol: Optional[str] = None,
    exchange_in: Optional[List[str]] = None,
    exchange_out: Optional[List[str]] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    allow_set = {e.lower() for e in exchange_in} if exchange_in else None
    block_set = {e.lower() for e in exchange_out} if exchange_out else None
    wanted_symbol = canonical_symbol.upper() if canonical_symbol else None

    filtered: List[Dict[str, Any]] = []
    for p in pairs:
        if (_f(p.get("spread_apr_percent")) or 0.0) < min_spread_apr_percent:
            continue
        if wanted_symbol and (p.get("canonical_symbol") or "").upper() != wanted_symbol:
            continue

        long_ex = ((p.get("long_market") or {}).get("exchange") or "").lower()
        short_ex = ((p.get("short_market") or {}).get("exchange") or "").lower()
        if allow_set is not None and (long_ex not in allow_set or short_ex not in allow_set):
            continue
        if block_set is not None and (long_ex in block_set or short_ex in block_set):
            continue

        p_clean = dict(p)
        p_clean.pop("_id", None)
        filtered.append(p_clean)

    filtered.sort(
        key=lambda r: (_f(r.get("spread_apr_percent")) or 0.0, _to_aware(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return filtered[:limit]


async def _compute_live_pairs_filtered(
    min_spread_apr_percent: float = 0.0,
    canonical_symbol: Optional[str] = None,
    exchange_in: Optional[List[str]] = None,
    exchange_out: Optional[List[str]] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    pairs = compute_arbitrage_pairs(await _fetch_current_sides())
    return _filter_pairs(pairs, min_spread_apr_percent, canonical_symbol, exchange_in, exchange_out, limit)


# -------------------------
# VENTANAS: medias sobre funding_arbitrage_pairs_ts
# -------------------------

async def get_timeseries_window(
    window_hours: float,
    min_spread_apr_percent: float = 0.0,
    canonical_symbol: Optional[str] = None,
    exchange_in: Optional[List[str]] = None,
    exchange_out: Optional[List[str]] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Media del APR de cada exchange en la ventana, con las patas FIJAS por exchange
    (no por quien era long/short en cada minuto). Así el spread medio refleja lo que
    habría rendido mantener una posición durante toda la ventana. Solo usa snapshots
    reales (excluye source='bootstrap').
    """
    if funding_pairs_col is None:
        raise HTTPException(500, "Mongo collection funding_arbitrage_pairs_ts not initialized")

    from_ts = now_utc() - timedelta(hours=window_hours)
    is_first_long = {"$lte": ["$long_market.exchange", "$short_market.exchange"]}
    pipeline = [
        {"$match": {"timestamp": {"$gte": from_ts}, "source": {"$ne": "bootstrap"}}},
        {"$sort": {"timestamp": -1}},
        # Reordenamos las patas alfabeticamente por exchange para promediar siempre el mismo lado
        {
            "$addFields": {
                "ex_first": {"$cond": [is_first_long, "$long_market", "$short_market"]},
                "ex_second": {"$cond": [is_first_long, "$short_market", "$long_market"]},
            }
        },
        {
            "$group": {
                "_id": "$pair_id",
                "canonical_symbol": {"$first": "$canonical_symbol"},
                "first_last": {"$first": "$ex_first"},
                "second_last": {"$first": "$ex_second"},
                "first_apr_avg": {"$avg": "$ex_first.apr_percent"},
                "second_apr_avg": {"$avg": "$ex_second.apr_percent"},
                "first_rate_avg": {"$avg": "$ex_first.funding_rate"},
                "second_rate_avg": {"$avg": "$ex_second.funding_rate"},
                "min_ts": {"$min": "$timestamp"},
                "max_ts": {"$max": "$timestamp"},
                "count": {"$sum": 1},
            }
        },
    ]

    groups = await funding_pairs_col.aggregate(pipeline).to_list(length=200_000)
    expected_count = (window_hours * 3600.0) / SNAPSHOT_INTERVAL_SECONDS
    pairs: List[Dict[str, Any]] = []

    def build_side(latest: Dict[str, Any], avg_rate: Any, avg_apr: Any) -> Dict[str, Any]:
        side = dict(latest or {})
        rate = _f(avg_rate)
        side["funding_rate"] = rate
        side["funding_percent"] = rate * 100.0 if rate is not None else None
        side["apr_percent"] = _f(avg_apr)
        side["last_updated"] = _to_aware(side.get("last_updated"))
        side["next_funding_time"] = _to_aware(side.get("next_funding_time"))
        return side

    for g in groups:
        a = build_side(g.get("first_last"), g.get("first_rate_avg"), g.get("first_apr_avg"))
        b = build_side(g.get("second_last"), g.get("second_rate_avg"), g.get("second_apr_avg"))
        if a.get("apr_percent") is None or b.get("apr_percent") is None:
            continue
        long_m, short_m = (a, b) if a["apr_percent"] <= b["apr_percent"] else (b, a)

        count = int(g.get("count") or 0)
        coverage = min(count / expected_count, 1.0) if expected_count else 0.0

        pairs.append(
            _enrich_pair(
                {
                    "pair_id": g.get("_id"),
                    "canonical_symbol": g.get("canonical_symbol"),
                    "timestamp": _to_aware(g.get("max_ts")),
                    "spread_apr_percent": short_m["apr_percent"] - long_m["apr_percent"],
                    "long_market": long_m,
                    "short_market": short_m,
                    "window_hours": window_hours,
                    "window_start_ts": _to_aware(g.get("min_ts")),
                    "window_end_ts": _to_aware(g.get("max_ts")),
                    "window_count": count,
                    "expected_count": expected_count,
                    "coverage_ratio": coverage,
                    "is_partial": coverage < 0.5,
                    "data_source": "rolling",
                }
            )
        )

    return _filter_pairs(pairs, min_spread_apr_percent, canonical_symbol, exchange_in, exchange_out, limit)


# -------------------------
# Eventos
# -------------------------

@app.on_event("startup")
async def startup_event():
    global mongo_client, db, funding_pairs_col

    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client[MONGO_DB]
    funding_pairs_col = db[COL_FUNDING_PAIRS_TS]

    # Los snapshots 'bootstrap' (carry-forward) eran datos inventados: fuera.
    try:
        res = await funding_pairs_col.delete_many({"source": "bootstrap"})
        if res.deleted_count:
            print(f"[startup] removed {res.deleted_count} bootstrap snapshots", flush=True)
    except Exception as exc:
        print(f"[startup] cleanup warning: {exc}", flush=True)

    try:
        await funding_pairs_col.create_index([("pair_id", 1), ("timestamp", 1), ("source", 1)], unique=True)
        await funding_pairs_col.create_index([("pair_id", 1), ("timestamp", -1)])
        await funding_pairs_col.create_index([("timestamp", -1)])
    except Exception as exc:
        print(f"[startup] Index creation warning: {exc}", flush=True)


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
        "service": "funding-analytics",
        "version": "0.6.0",
        "timestamp": now_utc().isoformat(),
    }


@app.get("/funding/config", response_model=dict)
async def funding_config():
    """Comisiones y parámetros usados en los cálculos (para mostrarlos en la UI)."""
    return {
        "fees": FEES,
        "default_fees": DEFAULT_FEES,
        "net_apr_horizon_hours": NET_APR_HORIZON_HOURS,
        "snapshot_interval_seconds": SNAPSHOT_INTERVAL_SECONDS,
        "windows_hours": WINDOWS_HOURS,
    }


common_canonical = Query(default=None, description="Filtra por canonical_symbol (BTC, ETH, ...)")
common_min_spread = Query(default=0.0, ge=0.0, description="Spread minimo de APR (ej: 20 => spread>=20)")
common_exchange_in = Query(default=None, description="Ambos exchanges deben pertenecer a este conjunto.")
common_exchange_out = Query(default=None, description="Excluye pares que contengan estos exchanges.")
common_limit_pairs = Query(default=100, ge=1, le=5000, description="Maximo numero de pares devueltos")


@app.get("/funding/live", response_model=List[Dict[str, Any]])
async def funding_live(
    canonical_symbol: Optional[str] = common_canonical,
    min_spread_apr_percent: float = common_min_spread,
    exchange_in: Optional[List[str]] = common_exchange_in,
    exchange_out: Optional[List[str]] = common_exchange_out,
    limit: int = common_limit_pairs,
):
    """Calcula en vivo los pares de arbitraje a partir de funding_*_current."""
    try:
        return await _compute_live_pairs_filtered(
            min_spread_apr_percent, canonical_symbol, exchange_in, exchange_out, limit
        )
    except Exception as e:
        raise HTTPException(500, f"Error calculando arbitraje Live: {e}")


def _make_window_route(window_hours: float):
    async def route(
        canonical_symbol: Optional[str] = common_canonical,
        min_spread_apr_percent: float = common_min_spread,
        exchange_in: Optional[List[str]] = common_exchange_in,
        exchange_out: Optional[List[str]] = common_exchange_out,
        limit: int = common_limit_pairs,
    ):
        return await get_timeseries_window(
            window_hours, min_spread_apr_percent, canonical_symbol, exchange_in, exchange_out, limit
        )

    return route


for _name, _hours in WINDOWS_HOURS.items():
    _route = _make_window_route(_hours)
    _route.__name__ = f"funding_{_name}"
    _route.__doc__ = f"Media del spread en las ultimas {_hours:g} horas."
    app.get(f"/funding/{_name}", response_model=List[Dict[str, Any]])(_route)


@app.post("/funding/arbitrage/refresh", response_model=dict)
async def refresh_arbitrage_pairs():
    """Recalcula todos los pares y guarda un snapshot (por minuto) en funding_arbitrage_pairs_ts."""
    if funding_pairs_col is None:
        raise HTTPException(500, "Mongo collection funding_arbitrage_pairs_ts not initialized")

    try:
        pairs = compute_arbitrage_pairs(await _fetch_current_sides())
    except Exception as e:
        raise HTTPException(500, f"Error calculando pares de arbitraje: {e}")

    ops = []
    for p in pairs:
        ts = _minute_floor(p.get("timestamp") or now_utc())
        p["timestamp"] = ts
        p["source"] = p.get("source") or "live"
        ops.append(
            UpdateOne({"pair_id": p["pair_id"], "timestamp": ts, "source": p["source"]}, {"$set": p}, upsert=True)
        )
    if ops:
        await funding_pairs_col.bulk_write(ops, ordered=False)

    return {"status": "ok", "pairs_inserted": len(pairs), "timestamp": now_utc().isoformat()}


@app.get("/funding/snapshots/health", response_model=dict)
async def snapshots_health(days: int = Query(30, ge=1, le=365)):
    """Cobertura básica de snapshots reales en funding_arbitrage_pairs_ts."""
    if funding_pairs_col is None:
        raise HTTPException(500, "Mongo collection funding_arbitrage_pairs_ts not initialized")

    now = now_utc()
    since = now - timedelta(days=days)
    real = {"source": {"$ne": "bootstrap"}}

    newest_doc = await funding_pairs_col.find_one(real, sort=[("timestamp", -1)], projection={"timestamp": 1})
    oldest_doc = await funding_pairs_col.find_one(real, sort=[("timestamp", 1)], projection={"timestamp": 1})
    newest_ts = _to_aware(newest_doc["timestamp"]) if newest_doc else None
    oldest_ts = _to_aware(oldest_doc["timestamp"]) if oldest_doc else None

    ts_pipeline = [
        {"$match": {"timestamp": {"$gte": since}, **real}},
        {"$group": {"_id": "$timestamp"}},
        {"$count": "cnt"},
    ]
    distinct_ts_doc = await funding_pairs_col.aggregate(ts_pipeline).to_list(length=1)
    distinct_ts = distinct_ts_doc[0]["cnt"] if distinct_ts_doc else 0
    total_snapshots = await funding_pairs_col.count_documents({"timestamp": {"$gte": since}, **real})

    expected_minutes = int(days * 24 * 60)
    return {
        "earliest_ts": oldest_ts,
        "latest_ts": newest_ts,
        "total_snapshots": total_snapshots,
        "distinct_timestamps": distinct_ts,
        "expected_timestamps": expected_minutes,
        "coverage_ratio": (distinct_ts / expected_minutes) if expected_minutes else 0,
        "coverage_ok": bool(oldest_ts and oldest_ts <= since),
        "coverage_hours": (now - oldest_ts).total_seconds() / 3600.0 if oldest_ts else 0,
    }


@app.post("/funding/snapshots/migrate-minute", response_model=dict)
async def migrate_snapshots_round_to_minute(
    batch_size: int = Query(1000, ge=1, le=20000),
    max_batches: int = Query(50, ge=1, le=1000),
):
    """
    Normaliza timestamps al inicio del minuto (source preservado) con upsert idempotente.
    Si hay duplicados prevalece el documento procesado más tarde.
    """
    if funding_pairs_col is None:
        raise HTTPException(500, "Mongo collection funding_arbitrage_pairs_ts not initialized")

    migrated = deleted = batches = 0
    while batches < max_batches:
        batches += 1
        docs = await funding_pairs_col.find(
            {
                "$or": [
                    {"$expr": {"$ne": [{"$second": "$timestamp"}, 0]}},
                    {"$expr": {"$ne": [{"$millisecond": "$timestamp"}, 0]}},
                ]
            },
            limit=batch_size,
        ).to_list(length=batch_size)
        if not docs:
            break

        ops, ids_to_delete = [], []
        for doc in docs:
            doc_id = doc.pop("_id", None)
            ids_to_delete.append(doc_id)
            ts = doc.get("timestamp")
            if not ts:
                continue
            doc["timestamp"] = _minute_floor(ts)
            doc.setdefault("source", "live")
            ops.append(
                UpdateOne(
                    {"pair_id": doc.get("pair_id"), "timestamp": doc["timestamp"], "source": doc["source"]},
                    {"$set": doc},
                    upsert=True,
                )
            )

        if ops:
            res = await funding_pairs_col.bulk_write(ops, ordered=False)
            migrated += res.upserted_count + res.modified_count
        if ids_to_delete:
            deleted += (await funding_pairs_col.delete_many({"_id": {"$in": ids_to_delete}})).deleted_count

    return {"status": "ok", "migrated": migrated, "deleted": deleted, "batches": batches}


# -------------------------
# Arranque local
# -------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8002)))
