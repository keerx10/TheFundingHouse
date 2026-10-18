"""
Hyperliquid Exchange Adapter

Llama directamente a la API REST publica (POST /info) y expone solo campos crudos:
- funding      -> tasa de funding HORARIA (Hyperliquid paga cada hora)
- openInterest -> en unidades del activo base (NO en USD)
- dayNtlVlm    -> volumen 24h en USD
- markPx / oraclePx / midPx

Filtra los mercados marcados como isDelisted en el universe.
No usamos hyperliquid-python-sdk: su constructor rompe con la API actual y solo
necesitamos dos tipos de consulta (metaAndAssetCtxs y fundingHistory).
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from .base import ExchangeAdapter

HYPERLIQUID_API_URL = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz")
FUNDING_INTERVAL_HOURS = 1.0


async def hyperliquid_info(payload: Dict[str, Any], client: Optional[httpx.AsyncClient] = None) -> Any:
    """POST /info con el payload indicado. Lanza RuntimeError si falla."""
    url = f"{HYPERLIQUID_API_URL.rstrip('/')}/info"
    try:
        if client is not None:
            resp = await client.post(url, json=payload)
        else:
            async with httpx.AsyncClient(timeout=15.0) as c:
                resp = await c.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:200] if exc.response is not None else ""
        raise RuntimeError(f"Hyperliquid /info {payload.get('type')} failed status={exc.response.status_code} body={body}")
    except httpx.RequestError as exc:
        raise RuntimeError(f"Hyperliquid /info {payload.get('type')} request error: {exc}")


class HyperliquidAdapter(ExchangeAdapter):
    """Adapter for Hyperliquid exchange"""

    def __init__(self):
        super().__init__("Hyperliquid")
        self._client = httpx.AsyncClient(timeout=15.0)

        # Cache for market metadata + contexts
        self._markets_cache: List[Dict[str, Any]] = []
        self._market_ctx_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl_seconds = 30

    async def _ensure_cache(self) -> None:
        """
        Refresh markets and asset contexts if cache is stale (una sola llamada REST).
        Si la llamada falla, mantenemos la caché anterior.
        """
        now = datetime.now(timezone.utc)
        if (
            self._cache_timestamp is not None
            and (now - self._cache_timestamp).total_seconds() < self._cache_ttl_seconds
        ):
            return

        try:
            meta, asset_ctxs = await hyperliquid_info({"type": "metaAndAssetCtxs"}, self._client)
        except Exception as e:
            print(f"[hyperliquid] error refreshing cache: {e}", flush=True)
            return

        markets: List[Dict[str, Any]] = []
        market_ctx: Dict[str, Dict[str, Any]] = {}
        skipped_delisted = 0

        for asset, raw_ctx in zip(meta.get("universe", []), asset_ctxs):
            raw_symbol = asset.get("name")
            if not raw_symbol:
                continue
            if asset.get("isDelisted"):
                skipped_delisted += 1
                continue

            markets.append(
                {
                    "symbol": self.normalize_symbol(raw_symbol),
                    "base_asset": raw_symbol,
                    "quote_asset": "USD",
                    "exchange": self.exchange_name,
                    "raw_symbol": raw_symbol,
                    "funding_interval_hours": FUNDING_INTERVAL_HOURS,
                }
            )

            sz_decimals = asset.get("szDecimals")
            # Solo campos CRUDOS, convertidos a float donde toca
            market_ctx[raw_symbol] = {
                "funding_raw": self._to_float(raw_ctx.get("funding")),
                "open_interest": self._to_float_or_none(raw_ctx.get("openInterest")),
                "volume_24h": self._to_float_or_none(raw_ctx.get("dayNtlVlm")),
                "mark_price": self._to_float(raw_ctx.get("markPx") or raw_ctx.get("midPx")),
                "oracle_price": self._to_float_or_none(raw_ctx.get("oraclePx")),
                "mid_price": self._to_float_or_none(raw_ctx.get("midPx")),
                "max_leverage": asset.get("maxLeverage"),
                "sz_decimals": sz_decimals,
                "step_size": (10 ** (-int(sz_decimals))) if sz_decimals is not None else None,
                "raw_ctx": raw_ctx,
            }

        self._markets_cache = markets
        self._market_ctx_cache = market_ctx
        self._cache_timestamp = now
        print(f"[hyperliquid] {len(markets)} markets (skipped delisted: {skipped_delisted})", flush=True)

    async def get_markets(self) -> List[Dict[str, Any]]:
        """Get all active perpetual markets from Hyperliquid (normalized symbols)"""
        await self._ensure_cache()
        return self._markets_cache

    async def get_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get current funding rate for a symbol using cached asset contexts"""
        await self._ensure_cache()
        raw_symbol = self._denormalize_symbol(symbol)
        ctx = self._market_ctx_cache.get(raw_symbol)
        if not ctx:
            return None
        return {
            "symbol": self.normalize_symbol(raw_symbol),
            "exchange": self.exchange_name,
            "funding_rate": ctx.get("funding_raw", 0.0),
            "funding_interval_hours": FUNDING_INTERVAL_HOURS,
            "timestamp": datetime.now(timezone.utc),
            "next_funding_time": self._next_funding_time(),
        }

    async def get_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get raw market data snapshot for a single symbol"""
        await self._ensure_cache()
        raw_symbol = self._denormalize_symbol(symbol)
        ctx = self._market_ctx_cache.get(raw_symbol)
        if not ctx:
            return None
        return self._build_market_payload(raw_symbol, ctx)

    async def get_all_market_data(self) -> List[Dict[str, Any]]:
        """Return raw market snapshots for all active assets using cached contexts."""
        await self._ensure_cache()
        return [self._build_market_payload(raw, ctx) for raw, ctx in self._market_ctx_cache.items()]

    async def get_funding_history(self, symbol: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        """
        Histórico de funding: lista de {coin, fundingRate, premium, time(ms)}.
        La API devuelve como mucho ~500 entradas por llamada; paginamos por startTime.
        """
        coin = self._denormalize_symbol(symbol)
        out: List[Dict[str, Any]] = []
        cursor = start_ms
        for _ in range(50):  # tope de seguridad
            batch = await hyperliquid_info(
                {"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end_ms}, self._client
            )
            if not batch:
                break
            out.extend(batch)
            last_ts = int(batch[-1].get("time") or 0)
            if len(batch) < 500 or last_ts <= cursor:
                break
            cursor = last_ts + 1
        return out

    # --------- Helpers ---------

    def normalize_symbol(self, raw_symbol: str) -> str:
        """Hyperliquid usa 'BTC', 'kPEPE'... Normalizamos a 'BASE-USD'."""
        if "-" in raw_symbol:
            return raw_symbol
        return f"{raw_symbol}-USD"

    def _denormalize_symbol(self, symbol: str) -> str:
        """Convert 'BTC-USD' to 'BTC' which is what the API expects"""
        return symbol.split("-")[0] if "-" in symbol else symbol

    @staticmethod
    def _next_funding_time() -> datetime:
        """Hyperliquid paga funding en punto cada hora (UTC)."""
        now = datetime.now(timezone.utc)
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_float_or_none(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _build_market_payload(self, raw_symbol: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Mapea campos crudos de Hyperliquid a nombres consistentes con Backpack."""
        mark_price = ctx.get("mark_price") or 0.0
        open_interest = ctx.get("open_interest")
        return {
            "exchange": self.exchange_name,
            "symbol": self.normalize_symbol(raw_symbol),
            "canonical_symbol": raw_symbol,
            "timestamp": datetime.now(timezone.utc),
            "funding_rate": ctx.get("funding_raw", 0.0),  # tasa horaria cruda
            "funding_interval_hours": FUNDING_INTERVAL_HOURS,
            "next_funding_time": self._next_funding_time(),
            # openInterest viene en unidades del activo base
            "open_interest": open_interest,
            "open_interest_usd": (open_interest * mark_price) if (open_interest and mark_price) else None,
            "volume_24h": ctx.get("volume_24h"),  # dayNtlVlm, ya en USD
            "mark_price": mark_price,
            "index_price": ctx.get("oracle_price"),
            "oracle_price": ctx.get("oracle_price"),
            "max_leverage": ctx.get("max_leverage"),
            "step_size": ctx.get("step_size"),
            # contexto completo tal y como viene de la API (para trazabilidad)
            "raw": ctx.get("raw_ctx", {}),
        }
