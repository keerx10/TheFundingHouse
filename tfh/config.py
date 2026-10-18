"""
Configuración por variables de entorno (con .env opcional).
"""

import os
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


PORT = int(os.getenv("PORT", "8080"))

DATA_DIR = Path(os.getenv("TFH_DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "tfh.sqlite"
LOGO_CACHE_DIR = DATA_DIR / "token_logos"
FRONTEND_DIR = Path(os.getenv("TFH_FRONTEND_DIR", Path(__file__).resolve().parent.parent / "frontend")).resolve()

# Cadencias (segundos)
LIVE_REFRESH_SECONDS = _f("LIVE_REFRESH_SECONDS", 60)        # tasa prevista, precio, OI (en memoria)
SNAPSHOT_EVERY_SECONDS = _f("SNAPSHOT_EVERY_SECONDS", 300)   # cada cuanto se persiste el estado live
HISTORY_SYNC_SECONDS = _f("HISTORY_SYNC_SECONDS", 1800)      # descarga de funding liquidado
HISTORY_BACKFILL_DAYS = int(os.getenv("HISTORY_BACKFILL_DAYS", "31"))
HISTORY_CONCURRENCY = int(os.getenv("HISTORY_CONCURRENCY", "4"))      # peticiones simultaneas por exchange
SNAPSHOT_RETENTION_DAYS = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "60"))

# Comisiones por exchange, tanto por uno sobre el nocional de cada pata.
# Hyperliquid tier 0: taker 0.045% / maker 0.015%.  Backpack base: taker 0.05% / maker 0.02%.
FEES: Dict[str, Dict[str, float]] = {
    "hyperliquid": {"taker": _f("FEE_HYPERLIQUID_TAKER", 0.00045), "maker": _f("FEE_HYPERLIQUID_MAKER", 0.00015)},
    "backpack": {"taker": _f("FEE_BACKPACK_TAKER", 0.0005), "maker": _f("FEE_BACKPACK_MAKER", 0.0002)},
}
DEFAULT_FEES = {"taker": _f("FEE_DEFAULT_TAKER", 0.0005), "maker": _f("FEE_DEFAULT_MAKER", 0.0002)}
# Horizonte de mantenimiento asumido para net_apr_percent (horas)
NET_APR_HORIZON_HOURS = _f("NET_APR_HORIZON_HOURS", 24.0)

WINDOWS_HOURS: Dict[str, float] = {
    "1h": 1.0,
    "8h": 8.0,
    "24h": 24.0,
    "3d": 72.0,
    "7d": 168.0,
    "15d": 360.0,
    "31d": 744.0,
}

HOURS_PER_YEAR = 365.0 * 24.0
