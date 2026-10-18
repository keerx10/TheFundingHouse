"""
Backpack Exchange Adapter (API publica)

- /markets       -> metadata PERP (baseSymbol, fundingInterval, estado, filtros)
- /markPrices    -> fundingRate (prevista), markPrice, indexPrice, nextFundingTimestamp
- /openInterest  -> openInterest en unidades del activo base (NO en USD)
- /tickers       -> quoteVolume (volumen 24h en USDC)
- /fundingRates  -> funding liquidado por hora (historico)
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from .base import ExchangeAdapter

BACKPACK_BASE_URL = os.getenv("BACKPACK_BASE_URL", "https://api.backpack.exchange/api/v1")
MARKETS_CACHE_TTL_SECONDS = 600


class BackpackAdapter(ExchangeAdapter):
    def __init__(self) -> None:
        super().__init__("Backpack")
        self._client = httpx.AsyncClient(base_url=BACKPACK_BASE_URL, timeout=15.0)
        self._markets_cache: Dict[str, Dict[str, Any]] = {}  # raw_symbol -> meta
        self._markets_cache_ts: Optional[datetime] = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            resp = await self._client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:200] if exc.response is not None else ""
            raise RuntimeError(f"Backpack GET {path} failed status={exc.response.status_code} body={body}")
        except httpx.RequestError as exc:
            raise RuntimeError(f"Backpack GET {path} request error: {exc}")

    # ------------------- Metadata de mercados -------------------

    async def _ensure_markets(self) -> Dict[str, Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        if (
            self._markets_cache
            and self._markets_cache_ts is not None
            and (now - self._markets_cache_ts).total_seconds() < MARKETS_CACHE_TTL_SECONDS
        ):
            return self._markets_cache

        try:
            data = await self._get("/markets")
        except Exception as exc:
            print(f"[backpack] error fetching markets: {exc}", flush=True)
            return self._markets_cache

        markets: Dict[str, Dict[str, Any]] = {}
        for m in data or []:
            if m.get("marketType") != "PERP":
                continue
            raw_symbol = m.get("symbol")
            if not raw_symbol:
                continue
            filters = m.get("filters") or {}
            price_f = filters.get("price") or {}
            qty_f = filters.get("quantity") or {}
            interval_ms = self._to_float_or_none(m.get("fundingInterval"))
            markets[raw_symbol] = {
                "raw_symbol": raw_symbol,
                "base_asset": m.get("baseSymbol") or raw_symbol.split("_")[0],
                "quote_asset": m.get("quoteSymbol") or "USDC",
                "funding_interval_hours": (interval_ms / 3_600_000.0) if interval_ms else 1.0,
                "order_book_state": m.get("orderBookState"),
                "visible": bool(m.get("visible", True)),
                "tick_size": self._to_float_or_none(price_f.get("tickSize")),
                "step_size": self._to_float_or_none(qty_f.get("stepSize")),
                "min_quantity": self._to_float_or_none(qty_f.get("minQuantity")),
            }

        if markets:
            self._markets_cache = markets
            self._markets_cache_ts = now
        return self._markets_cache

    @staticmethod
    def _is_tradeable(meta: Optional[Dict[str, Any]]) -> bool:
        """Solo mercados con libro abierto y visibles (fuera Closed / PostOnly)."""
        if not meta:
            return True
        return meta.get("order_book_state") == "Open" and meta.get("visible", True)

    async def get_markets(self) -> List[Dict[str, Any]]:
        metas = await self._ensure_markets()
        return [
            {
                "symbol": raw,
                "canonical_symbol": meta["base_asset"],
                "exchange": self.exchange_name,
                "funding_interval_hours": meta["funding_interval_hours"],
            }
            for raw, meta in metas.items()
            if self._is_tradeable(meta)
        ]

    # ------------------- Snapshot live -------------------

    async def get_all_market_data(self) -> List[Dict[str, Any]]:
        metas = await self._ensure_markets()
        mark_list = await self._get("/markPrices")
        try:
            oi_list = await self._get("/openInterest")
        except Exception as e:
            print(f"[backpack] error fetching openInterest: {e}", flush=True)
            oi_list = []
        try:
            tickers = await self._get("/tickers")
        except Exception as e:
            print(f"[backpack] error fetching tickers: {e}", flush=True)
            tickers = []

        oi_map = {i.get("symbol"): i for i in (oi_list or [])}
        ticker_map = {i.get("symbol"): i for i in (tickers or []) if i.get("symbol")}
        now = datetime.now(timezone.utc)
        results: List[Dict[str, Any]] = []

        for mp in mark_list or []:
            raw_symbol = mp.get("symbol")
            if not raw_symbol:
                continue
            meta = metas.get(raw_symbol)
            if metas and meta is None:
                continue  # no es PERP segun /markets
            if not self._is_tradeable(meta):
                continue
            results.append(self._build_snapshot(raw_symbol, meta or {}, mp, oi_map.get(raw_symbol), ticker_map.get(raw_symbol, {}), now))
        return results

    def _build_snapshot(self, raw_symbol, meta, mp, oi, ticker_raw, now) -> Dict[str, Any]:
        next_ms = mp.get("nextFundingTimestamp")
        mark_price = self._to_float(mp.get("markPrice"))
        open_interest = self._to_float_or_none(oi.get("openInterest")) if oi else None
        volume_24h = self._to_float_or_none(ticker_raw.get("quoteVolume"))
        return {
            "exchange": self.exchange_name,
            "symbol": raw_symbol,
            "canonical_symbol": meta.get("base_asset") or raw_symbol.split("_")[0],
            "timestamp": now,
            "funding_rate": self._to_float(mp.get("fundingRate")),
            "funding_interval_hours": meta.get("funding_interval_hours") or 1.0,
            "next_funding_time": datetime.fromtimestamp(next_ms / 1000, tz=timezone.utc) if next_ms else None,
            "open_interest": open_interest,
            "open_interest_usd": (open_interest * mark_price) if (open_interest and mark_price) else None,
            "volume_24h": volume_24h if (volume_24h and volume_24h > 0) else None,
            "mark_price": mark_price,
            "index_price": self._to_float_or_none(mp.get("indexPrice")),
            "tick_size": meta.get("tick_size"),
            "step_size": meta.get("step_size"),
            "min_quantity": meta.get("min_quantity"),
        }

    # ------------------- Historico -------------------

    async def get_funding_history(self, symbol: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        """
        GET /fundingRates devuelve los pagos (horarios) del mas reciente al mas antiguo,
        paginados con offset. Paramos cuando la pagina ya es anterior a start_ms.
        """
        start_s, end_s = start_ms / 1000.0, end_ms / 1000.0
        events: List[Dict[str, Any]] = []
        offset, limit = 0, 1000

        for _ in range(20):  # tope de seguridad
            data = await self._get("/fundingRates", params={"symbol": symbol, "limit": limit, "offset": offset})
            if not data:
                break
            oldest = None
            for item in data:
                ts_str, rate = item.get("intervalEndTimestamp"), item.get("fundingRate")
                if not ts_str or rate is None:
                    continue
                ts = datetime.fromisoformat(str(ts_str)).replace(tzinfo=timezone.utc).timestamp()
                oldest = ts if oldest is None else min(oldest, ts)
                if start_s <= ts <= end_s:
                    events.append({"ts": int(ts), "funding_rate": float(rate), "interval_hours": 1.0})
            if len(data) < limit or (oldest is not None and oldest < start_s):
                break
            offset += limit

        return events
