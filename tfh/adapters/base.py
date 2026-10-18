"""
Interfaz comun de los adapters de exchange.

Un snapshot de mercado (get_all_market_data) es un dict con al menos:
  exchange, symbol, canonical_symbol, timestamp (datetime UTC),
  funding_rate (tanto por uno por periodo), funding_interval_hours, next_funding_time,
  mark_price, index_price, open_interest (monedas), open_interest_usd, volume_24h (USD)

Un evento de funding liquidado (get_funding_history) es un dict:
  ts (epoch segundos, fin del periodo), funding_rate, interval_hours
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class ExchangeAdapter(ABC):
    def __init__(self, exchange_name: str):
        self.exchange_name = exchange_name

    @abstractmethod
    async def get_markets(self) -> List[Dict[str, Any]]:
        """Mercados perp operables: symbol, canonical_symbol, funding_interval_hours."""

    @abstractmethod
    async def get_all_market_data(self) -> List[Dict[str, Any]]:
        """Snapshot actual de todos los mercados operables."""

    @abstractmethod
    async def get_funding_history(self, symbol: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        """Funding liquidado por periodo entre start_ms y end_ms."""

    async def close(self) -> None:
        return None

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
