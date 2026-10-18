"""
Hyperliquid Exchange Adapter (API REST publica, POST /info)

- metaAndAssetCtxs -> funding (tasa HORARIA prevista), openInterest (monedas), dayNtlVlm (USD), markPx, oraclePx
- fundingHistory   -> funding liquidado por hora

Filtra los mercados con isDelisted. No usamos hyperliquid-python-sdk (su constructor
rompe con la API actual y solo necesitamos estas dos consultas).
"""

import asyncio
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from .base import ExchangeAdapter

HYPERLIQUID_API_URL = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz")
FUNDING_INTERVAL_HOURS = 1.0
RETRIES = 4
RETRY_BACKOFF_SECONDS = 1.0


class HyperliquidAdapter(ExchangeAdapter):
    def __init__(self):
        super().__init__("Hyperliquid")
        self._client = httpx.AsyncClient(timeout=15.0)
        self._markets_cache: List[Dict[str, Any]] = []
        self._ctx_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ts: Optional[datetime] = None
        self._cache_ttl_seconds = 30

    async def close(self) -> None:
        await self._client.aclose()

    async def _info(self, payload: Dict[str, Any]) -> Any:
        """POST /info con reintentos (backoff) ante 429 / 5xx / errores de red."""
        url = f"{HYPERLIQUID_API_URL.rstrip('/')}/info"
        last_error = ""
        for attempt in range(RETRIES):
            try:
                resp = await self._client.post(url, json=payload)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"status={resp.status_code}"
                else:
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:200] if exc.response is not None else ""
                raise RuntimeError(f"Hyperliquid /info {payload.get('type')} failed status={exc.response.status_code} body={body}")
            except httpx.RequestError as exc:
                last_error = str(exc)
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt) * (0.5 + random.random()))
        raise RuntimeError(f"Hyperliquid /info {payload.get('type')} failed after {RETRIES} attempts: {last_error}")

    async def _ensure_cache(self) -> None:
        now = datetime.now(timezone.utc)
        if self._cache_ts is not None and (now - self._cache_ts).total_seconds() < self._cache_ttl_seconds:
            return

        meta, asset_ctxs = await self._info({"type": "metaAndAssetCtxs"})
        markets: List[Dict[str, Any]] = []
        ctxs: Dict[str, Dict[str, Any]] = {}

        for asset, raw_ctx in zip(meta.get("universe", []), asset_ctxs):
            name = asset.get("name")
            if not name or asset.get("isDelisted"):
                continue
            markets.append(
                {
                    "symbol": name,
                    "canonical_symbol": name,
                    "exchange": self.exchange_name,
                    "funding_interval_hours": FUNDING_INTERVAL_HOURS,
                }
            )
            sz_decimals = asset.get("szDecimals")
            ctxs[name] = {
                "funding_raw": self._to_float(raw_ctx.get("funding")),
                "open_interest": self._to_float_or_none(raw_ctx.get("openInterest")),
                "volume_24h": self._to_float_or_none(raw_ctx.get("dayNtlVlm")),
                "mark_price": self._to_float(raw_ctx.get("markPx") or raw_ctx.get("midPx")),
                "oracle_price": self._to_float_or_none(raw_ctx.get("oraclePx")),
                "max_leverage": asset.get("maxLeverage"),
                "step_size": (10 ** (-int(sz_decimals))) if sz_decimals is not None else None,
            }

        self._markets_cache = markets
        self._ctx_cache = ctxs
        self._cache_ts = now

    async def get_markets(self) -> List[Dict[str, Any]]:
        await self._ensure_cache()
        return self._markets_cache

    async def get_all_market_data(self) -> List[Dict[str, Any]]:
        await self._ensure_cache()
        now = datetime.now(timezone.utc)
        next_funding = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)  # paga en punto
        out: List[Dict[str, Any]] = []
        for name, ctx in self._ctx_cache.items():
            mark_price = ctx["mark_price"] or 0.0
            oi = ctx["open_interest"]
            out.append(
                {
                    "exchange": self.exchange_name,
                    "symbol": name,
                    "canonical_symbol": name,
                    "timestamp": now,
                    "funding_rate": ctx["funding_raw"],  # tasa horaria prevista
                    "funding_interval_hours": FUNDING_INTERVAL_HOURS,
                    "next_funding_time": next_funding,
                    "open_interest": oi,
                    "open_interest_usd": (oi * mark_price) if (oi and mark_price) else None,
                    "volume_24h": ctx["volume_24h"],
                    "mark_price": mark_price,
                    "index_price": ctx["oracle_price"],
                    "max_leverage": ctx["max_leverage"],
                    "step_size": ctx["step_size"],
                }
            )
        return out

    async def get_funding_history(self, symbol: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        """fundingHistory devuelve hasta ~500 pagos por llamada, del mas antiguo al mas reciente."""
        events: List[Dict[str, Any]] = []
        cursor = start_ms
        for _ in range(50):  # tope de seguridad
            batch = await self._info({"type": "fundingHistory", "coin": symbol, "startTime": cursor, "endTime": end_ms})
            if not batch:
                break
            for ev in batch:
                t = ev.get("time")
                if t is None:
                    continue
                events.append({"ts": int(int(t) / 1000), "funding_rate": float(ev.get("fundingRate") or 0.0), "interval_hours": 1.0})
            last_ms = int(batch[-1].get("time") or 0)
            if len(batch) < 500 or last_ms <= cursor:
                break
            cursor = last_ms + 1
        return events
