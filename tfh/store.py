"""
Persistencia en SQLite (un fichero, modo WAL).

- funding_history : funding LIQUIDADO por periodo (fuente de verdad para las ventanas y el backtest)
- market_snapshots: estado live persistido cada pocos minutos (precio, OI, volumen, tasa prevista)
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_history (
    exchange         TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    canonical_symbol TEXT    NOT NULL,
    ts               INTEGER NOT NULL,   -- epoch s, fin del periodo de funding
    funding_rate     REAL    NOT NULL,   -- tanto por uno por periodo
    interval_hours   REAL    NOT NULL,
    PRIMARY KEY (exchange, symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_fh_canon_ts ON funding_history (canonical_symbol, ts);
CREATE INDEX IF NOT EXISTS idx_fh_ts ON funding_history (ts);

CREATE TABLE IF NOT EXISTS market_snapshots (
    exchange          TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    canonical_symbol  TEXT    NOT NULL,
    ts                INTEGER NOT NULL,
    funding_rate      REAL,
    interval_hours    REAL,
    mark_price        REAL,
    index_price       REAL,
    open_interest     REAL,
    open_interest_usd REAL,
    volume_24h        REAL,
    PRIMARY KEY (exchange, symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_ms_canon_ts ON market_snapshots (canonical_symbol, ts);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- funding_history ----------------

    def upsert_history(self, exchange: str, symbol: str, canonical: str, events: Iterable[Dict[str, Any]]) -> int:
        rows = [(exchange, symbol, canonical, int(e["ts"]), float(e["funding_rate"]), float(e["interval_hours"])) for e in events]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO funding_history (exchange, symbol, canonical_symbol, ts, funding_rate, interval_hours) VALUES (?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def last_history_ts(self, exchange: str, symbol: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS m FROM funding_history WHERE exchange=? AND symbol=?", (exchange, symbol)
            ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else None

    def history_window(self, since_ts: int) -> List[Dict[str, Any]]:
        """Media de funding por (exchange, canonical_symbol) desde since_ts."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT exchange, canonical_symbol, MAX(symbol) AS symbol,
                       AVG(funding_rate) AS avg_rate, AVG(interval_hours) AS avg_interval,
                       COUNT(*) AS n, MIN(ts) AS min_ts, MAX(ts) AS max_ts
                FROM funding_history
                WHERE ts > ?
                GROUP BY exchange, canonical_symbol
                """,
                (int(since_ts),),
            ).fetchall()
        return [dict(r) for r in rows]

    def history_series(self, canonical: str, since_ts: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT exchange, ts, funding_rate, interval_hours FROM funding_history WHERE canonical_symbol=? AND ts > ? ORDER BY ts",
                (canonical, int(since_ts)),
            ).fetchall()
        return [dict(r) for r in rows]

    def history_summary(self) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS oldest, MAX(ts) AS newest, COUNT(DISTINCT canonical_symbol) AS symbols FROM funding_history"
            ).fetchone()
        return dict(row) if row else {}

    # ---------------- market_snapshots ----------------

    def insert_snapshots(self, ts: int, snapshots: Iterable[Dict[str, Any]]) -> int:
        rows = [
            (
                s.get("exchange"), s.get("symbol"), s.get("canonical_symbol"), int(ts),
                s.get("funding_rate"), s.get("funding_interval_hours"), s.get("mark_price"), s.get("index_price"),
                s.get("open_interest"), s.get("open_interest_usd"), s.get("volume_24h"),
            )
            for s in snapshots
        ]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO market_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
            )
            self._conn.commit()
        return len(rows)

    def prune_snapshots(self, retention_days: int) -> int:
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM market_snapshots WHERE ts < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount

    # ---------------- sync_state ----------------

    def get_state(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?,?)", (key, value))
            self._conn.commit()
