"""
Helpers to fetch and normalize funding history into 8h buckets.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List

import httpx

from adapters.hyperliquid import HyperliquidAdapter

BASE_INTERVAL_HOURS = 8.0
BASE_INTERVAL = timedelta(hours=BASE_INTERVAL_HOURS)

BACKPACK_FUNDING_RATES_URL = "https://api.backpack.exchange/api/v1/fundingRates"

# Instancia solo para el historico (no comparte cache con el adapter de main)
_hl_history_adapter = HyperliquidAdapter()


def _as_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


async def _fetch_backpack_funding_history_for_symbol(
    symbol: str,
    from_ts: datetime,
    to_ts: datetime,
    client: httpx.AsyncClient,
) -> List[Dict[str, Any]]:
    """
    GET /api/v1/fundingRates: histórico de funding por símbolo (horario en Backpack).
    Pagina con offset hasta salir de la ventana pedida.
    """
    from_ts, to_ts = _as_utc(from_ts), _as_utc(to_ts)
    events: List[Dict[str, Any]] = []
    offset = 0
    limit = 1000

    for _ in range(20):  # tope de seguridad
        resp = await client.get(BACKPACK_FUNDING_RATES_URL, params={"symbol": symbol, "limit": limit, "offset": offset})
        resp.raise_for_status()
        data = resp.json() or []
        if not data:
            break

        oldest_in_page = None
        for item in data:
            ts_str = item.get("intervalEndTimestamp")
            rate_str = item.get("fundingRate")
            if not ts_str or rate_str is None:
                continue
            ts = datetime.fromisoformat(str(ts_str)).replace(tzinfo=timezone.utc)
            oldest_in_page = ts if oldest_in_page is None else min(oldest_in_page, ts)
            if ts < from_ts or ts > to_ts:
                continue
            events.append({"timestamp": ts, "funding_rate": float(rate_str), "interval_hours": 1.0})

        if len(data) < limit or (oldest_in_page is not None and oldest_in_page < from_ts):
            break
        offset += limit

    return events


async def fetch_raw_funding_history(
    exchange: str,
    symbol: str,
    from_ts: datetime,
    to_ts: datetime,
) -> List[Dict[str, Any]]:
    """
    Fetch raw funding events from an exchange API.
    Returns events with at least: timestamp (datetime UTC), funding_rate (float), interval_hours (float).
    """
    ex = exchange.lower()

    if ex == "hyperliquid":
        start_ms = int(_as_utc(from_ts).timestamp() * 1000)
        end_ms = int(_as_utc(to_ts).timestamp() * 1000)
        try:
            history = await _hl_history_adapter.get_funding_history(symbol, start_ms, end_ms)
        except Exception as e:
            raise RuntimeError(f"Error fetching Hyperliquid funding history for {symbol}: {e}")

        events: List[Dict[str, Any]] = []
        for ev in history or []:
            ts_ms = ev.get("time")
            if ts_ms is None:
                continue
            events.append(
                {
                    "timestamp": datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc),
                    "funding_rate": float(ev.get("fundingRate") or 0.0),
                    # Hyperliquid funding is hourly
                    "interval_hours": 1.0,
                }
            )
        return events

    if ex == "backpack":
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await _fetch_backpack_funding_history_for_symbol(symbol, from_ts, to_ts, client)

    raise ValueError(f"Unsupported exchange for funding history: {exchange}")


def aggregate_to_8h(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Group raw funding events into 8h buckets.
    - bucket_start = floor(timestamp / 8h) * 8h
    - bucket_end   = bucket_start + 8h
    - funding_rate = mean of funding_rate inside the bucket (sigue siendo tasa por periodo nativo)
    - timestamp (snapshot) = bucket_end
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    bucket_seconds = BASE_INTERVAL.total_seconds()
    buckets: Dict[datetime, Dict[str, Any]] = {}

    for ev in events:
        ts = ev.get("timestamp")
        if not isinstance(ts, datetime):
            continue
        ts = _as_utc(ts)
        start_seconds = ((ts - epoch).total_seconds() // bucket_seconds) * bucket_seconds
        bucket_start = epoch + timedelta(seconds=start_seconds)

        b = buckets.setdefault(
            bucket_start,
            {"sum": 0.0, "count": 0, "interval_hours": float(ev.get("interval_hours") or 1.0), "timestamp": bucket_start + BASE_INTERVAL},
        )
        b["sum"] += float(ev.get("funding_rate") or 0.0)
        b["count"] += 1

    snapshots: List[Dict[str, Any]] = []
    for agg in buckets.values():
        avg_rate = agg["sum"] / float(agg["count"] or 1)
        snapshots.append(
            {
                "timestamp": agg["timestamp"],
                "funding_rate": avg_rate,
                "funding_percent": avg_rate * 100.0,
                "funding_interval_hours": agg["interval_hours"],
                "samples": agg["count"],
            }
        )

    snapshots.sort(key=lambda x: x["timestamp"])
    return snapshots
