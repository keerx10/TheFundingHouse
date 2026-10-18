"""
Backpack Exchange Adapter

Obtiene datos publicos de Backpack:
- /markets      -> metadata de mercados PERP (baseSymbol, fundingInterval, estado, filtros)
- /markPrices   -> fundingRate, markPrice, indexPrice, nextFundingTimestamp
- /openInterest -> openInterest (en unidades del activo base, NO en USD)
- /tickers      -> quoteVolume (volumen 24h en USDC)

NO calcula APR ni derivadas: eso lo hace funding-analytics.
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from .base import ExchangeAdapter

BACKPACK_BASE_URL = os.getenv("BACKPACK_BASE_URL", "https://api.backpack.exchange/api/v1")
MARKETS_CACHE_TTL_SECONDS = 600


class BackpackAdapter(ExchangeAdapter):
    """Adapter para datos públicos de Backpack."""

    def __init__(self) -> None:
        super().__init__("Backpack")
        # Cliente HTTP reutilizable
        self._client = httpx.AsyncClient(base_url=BACKPACK_BASE_URL, timeout=10.0)
        # raw_symbol -> metadata del mercado (de /markets), cacheada 10 min
        self._markets_cache: Dict[str, Dict[str, Any]] = {}
        self._markets_cache_ts: Optional[datetime] = None

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Wrapper simple sobre GET con httpx."""
        try:
            resp = await self._client.get(path, params=params)
            print(f"[backpack] GET {path} -> {resp.status_code}", flush=True)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response else None
            body = exc.response.text[:200] if exc.response is not None else ""
            raise RuntimeError(f"Backpack GET {path} failed with status={status}, body={body}")
        except httpx.RequestError as exc:
            raise RuntimeError(f"Backpack GET {path} request error: {exc}")

    # ------------------- Metadata de mercados -------------------

    async def _ensure_markets(self) -> Dict[str, Dict[str, Any]]:
        """
        Carga /markets (solo PERP) y lo cachea. Devuelve raw_symbol -> meta.
        Si la llamada falla, mantiene la caché anterior.
        """
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
                # fundingInterval viene en ms (3600000 = 1h)
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
            # Sin metadata no podemos saberlo; no descartamos.
            return True
        return meta.get("order_book_state") == "Open" and meta.get("visible", True)

    async def _get_ticker_volumes(self) -> Dict[str, Dict[str, Any]]:
        """Devuelve symbol -> ticker crudo (quoteVolume = volumen 24h en USDC)."""
        try:
            data = await self._get("/tickers")
        except Exception:
            return {}
        return {item["symbol"]: item for item in (data or []) if item.get("symbol")}

    # ------------------- Métodos abstractos -------------------

    async def get_markets(self) -> List[Dict[str, Any]]:
        """Lista de mercados PERP operables con símbolo normalizado."""
        metas = await self._ensure_markets()
        markets: List[Dict[str, Any]] = []
        for raw_symbol, meta in metas.items():
            if not self._is_tradeable(meta):
                continue
            markets.append(
                {
                    "symbol": self.normalize_symbol(raw_symbol),
                    "base_asset": meta["base_asset"],
                    "quote_asset": meta["quote_asset"],
                    "exchange": self.exchange_name,
                    "raw_symbol": raw_symbol,
                    "funding_interval_hours": meta["funding_interval_hours"],
                }
            )
        return markets

    async def get_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Funding actual de un símbolo usando /markPrices?symbol=..."""
        snap = await self.get_market_data(symbol)
        if not snap:
            return None
        return {
            "symbol": snap["symbol"],
            "exchange": self.exchange_name,
            "funding_rate": snap["funding_rate"],
            "funding_interval_hours": snap["funding_interval_hours"],
            "timestamp": snap["timestamp"],
            "next_funding_time": snap["next_funding_time"],
        }

    async def get_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Snapshot de mercado para un símbolo."""
        raw_symbol = self._denormalize_symbol(symbol)
        metas = await self._ensure_markets()

        mark_data = await self._get("/markPrices", params={"symbol": raw_symbol})
        if not mark_data:
            return None
        mp = mark_data[0] if isinstance(mark_data, list) else mark_data

        oi_data = await self._get("/openInterest", params={"symbol": raw_symbol})
        oi = (oi_data[0] if isinstance(oi_data, list) else oi_data) if oi_data else None

        ticker_map = await self._get_ticker_volumes()
        return self._build_snapshot(
            raw_symbol, metas.get(raw_symbol), mp, oi, ticker_map.get(raw_symbol, {}), datetime.now(timezone.utc)
        )

    # ------------------- Helper para “todos los mercados” -------------------

    async def get_all_market_data(self) -> List[Dict[str, Any]]:
        """
        Snapshot de TODOS los mercados PERP operables. Devuelve dicts listos para Mongo.
        """
        metas = await self._ensure_markets()

        try:
            mark_list = await self._get("/markPrices")
        except Exception as e:
            print(f"[backpack] error fetching markPrices: {e}", flush=True)
            mark_list = []

        try:
            oi_list = await self._get("/openInterest")
        except Exception as e:
            print(f"[backpack] error fetching openInterest: {e}", flush=True)
            oi_list = []

        ticker_map = await self._get_ticker_volumes()
        oi_map = {item.get("symbol"): item for item in (oi_list or [])}

        now = datetime.now(timezone.utc)
        results: List[Dict[str, Any]] = []
        skipped_closed = 0

        for mp in mark_list or []:
            raw_symbol = mp.get("symbol")
            if not raw_symbol:
                continue
            meta = metas.get(raw_symbol)
            if metas and meta is None:
                # markPrices de algo que no es PERP segun /markets
                continue
            if not self._is_tradeable(meta):
                skipped_closed += 1
                continue
            results.append(
                self._build_snapshot(raw_symbol, meta, mp, oi_map.get(raw_symbol), ticker_map.get(raw_symbol, {}), now)
            )

        populated = sum(1 for r in results if r.get("volume_24h") is not None)
        print(
            f"[backpack] {len(results)} markets (skipped closed/post-only: {skipped_closed}), "
            f"volume_24h populated for {populated}/{len(results)}",
            flush=True,
        )
        return results

    # ------------------- Helpers internos -------------------

    def _build_snapshot(
        self,
        raw_symbol: str,
        meta: Optional[Dict[str, Any]],
        mp: Dict[str, Any],
        oi: Optional[Dict[str, Any]],
        ticker_raw: Dict[str, Any],
        now: datetime,
    ) -> Dict[str, Any]:
        meta = meta or {}
        funding_rate = self._to_float(mp.get("fundingRate"))
        next_funding_ts = mp.get("nextFundingTimestamp")
        next_funding = (
            datetime.fromtimestamp(next_funding_ts / 1000, tz=timezone.utc) if next_funding_ts else None
        )
        mark_price = self._to_float(mp.get("markPrice"))
        open_interest = self._to_float_or_none(oi.get("openInterest")) if oi else None
        quote_vol = ticker_raw.get("quoteVolume")
        volume_24h = self._to_float_or_none(quote_vol)

        return {
            "exchange": self.exchange_name,
            "symbol": self.normalize_symbol(raw_symbol),
            "canonical_symbol": meta.get("base_asset") or raw_symbol.split("_")[0],
            "timestamp": now,
            "funding_rate": funding_rate,
            "funding_interval_hours": meta.get("funding_interval_hours") or 1.0,
            "next_funding_time": next_funding,
            # openInterest viene en unidades del activo base
            "open_interest": open_interest,
            "open_interest_usd": (open_interest * mark_price) if (open_interest and mark_price) else None,
            "volume_24h": volume_24h if (volume_24h is not None and volume_24h > 0) else None,
            "mark_price": mark_price,
            "index_price": self._to_float(mp.get("indexPrice")),
            "tick_size": meta.get("tick_size"),
            "step_size": meta.get("step_size"),
            "min_quantity": meta.get("min_quantity"),
            "raw": {"markPrices": mp, "openInterest": oi, "ticker": ticker_raw},
        }

    def _denormalize_symbol(self, symbol: str) -> str:
        """Backpack usa el mismo símbolo en todas las APIs (BTC_USDC_PERP)."""
        return symbol

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
