"""
Tareas de fondo (asyncio) dentro del mismo proceso:
- refresh_live   : cada LIVE_REFRESH_SECONDS, snapshot de todos los mercados en memoria
- persist        : cada SNAPSHOT_EVERY_SECONDS, guarda el estado live en market_snapshots
- sync_history   : cada HISTORY_SYNC_SECONDS, descarga el funding liquidado nuevo por simbolo
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config
from .adapters.base import ExchangeAdapter
from .store import Store


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


class Scheduler:
    def __init__(self, store: Store, adapters: List[ExchangeAdapter]):
        self.store = store
        self.adapters = adapters
        # latest[exchange][symbol] = snapshot
        self.latest: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.live_updated_at: Dict[str, datetime] = {}
        self.live_errors: Dict[str, str] = {}
        self.history_last_sync: Optional[datetime] = None
        self.history_syncing = False
        self.history_last_result: Dict[str, Any] = {}
        self._tasks: List[asyncio.Task] = []

    # ---------------- live ----------------

    async def refresh_live(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for adapter in self.adapters:
            name = adapter.exchange_name
            try:
                snaps = await adapter.get_all_market_data()
            except Exception as exc:
                self.live_errors[name] = str(exc)
                log(f"[live] {name} error: {exc}")
                continue
            self.latest[name] = {s["symbol"]: s for s in snaps if s.get("symbol")}
            self.live_updated_at[name] = datetime.now(timezone.utc)
            self.live_errors.pop(name, None)
            counts[name] = len(snaps)
        return counts

    def persist_snapshots(self) -> int:
        ts = int(time.time()) // 60 * 60
        total = 0
        for by_symbol in self.latest.values():
            total += self.store.insert_snapshots(ts, by_symbol.values())
        return total

    # ---------------- historico ----------------

    async def sync_history(self, backfill_days: Optional[int] = None) -> Dict[str, Any]:
        if self.history_syncing:
            return {"status": "already_running"}
        self.history_syncing = True
        started = time.monotonic()
        result: Dict[str, Any] = {}
        try:
            now_ms = int(time.time() * 1000)
            default_start_ms = now_ms - (backfill_days or config.HISTORY_BACKFILL_DAYS) * 86400 * 1000

            for adapter in self.adapters:
                name = adapter.exchange_name
                markets = list(self.latest.get(name, {}).values())
                if not markets:
                    try:
                        markets = await adapter.get_markets()
                    except Exception as exc:
                        result[name] = {"error": str(exc)}
                        continue

                sem = asyncio.Semaphore(config.HISTORY_CONCURRENCY)
                counters = {"inserted": 0, "errors": 0}

                async def sync_symbol(m: Dict[str, Any]) -> None:
                    symbol, canonical = m["symbol"], m.get("canonical_symbol") or m["symbol"]
                    async with sem:
                        last_ts = self.store.last_history_ts(name, symbol)
                        # Volvemos a pedir la ultima hora conocida por si el exchange corrige el valor
                        start_ms = (last_ts - 3600) * 1000 if last_ts else default_start_ms
                        try:
                            events = await adapter.get_funding_history(symbol, start_ms, now_ms)
                            counters["inserted"] += self.store.upsert_history(name, symbol, canonical, events)
                        except Exception as exc:
                            counters["errors"] += 1
                            if counters["errors"] <= 3:
                                log(f"[history] {name} {symbol}: {exc}")
                        await asyncio.sleep(0.05)

                await asyncio.gather(*(sync_symbol(m) for m in markets))
                result[name] = {"symbols": len(markets), "rows_upserted": counters["inserted"], "errors": counters["errors"]}

            self.history_last_sync = datetime.now(timezone.utc)
            result["duration_seconds"] = round(time.monotonic() - started, 1)
            self.history_last_result = result
            log(f"[history] sync done: {result}")
            return result
        finally:
            self.history_syncing = False

    # ---------------- loops ----------------

    async def _live_loop(self) -> None:
        while True:
            try:
                counts = await self.refresh_live()
                log(f"[live] refreshed {counts}")
            except Exception as exc:
                log(f"[live] loop error: {exc}")
            await asyncio.sleep(config.LIVE_REFRESH_SECONDS)

    async def _persist_loop(self) -> None:
        await asyncio.sleep(10)  # deja que llegue el primer refresh
        while True:
            try:
                n = self.persist_snapshots()
                pruned = self.store.prune_snapshots(config.SNAPSHOT_RETENTION_DAYS)
                log(f"[snapshots] persisted {n}" + (f", pruned {pruned}" if pruned else ""))
            except Exception as exc:
                log(f"[snapshots] error: {exc}")
            await asyncio.sleep(config.SNAPSHOT_EVERY_SECONDS)

    async def _history_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.sync_history()
            except Exception as exc:
                log(f"[history] loop error: {exc}")
            await asyncio.sleep(config.HISTORY_SYNC_SECONDS)

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._live_loop(), name="live"),
            asyncio.create_task(self._persist_loop(), name="persist"),
            asyncio.create_task(self._history_loop(), name="history"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for adapter in self.adapters:
            await adapter.close()

    def status(self) -> Dict[str, Any]:
        return {
            "live": {
                ex: {
                    "markets": len(self.latest.get(ex, {})),
                    "updated_at": self.live_updated_at.get(ex),
                    "error": self.live_errors.get(ex),
                }
                for ex in sorted({a.exchange_name for a in self.adapters})
            },
            "history": {
                **{k: (datetime.fromtimestamp(v, tz=timezone.utc) if k in ("oldest", "newest") and v else v) for k, v in self.store.history_summary().items()},
                "last_sync": self.history_last_sync,
                "syncing": self.history_syncing,
                "last_result": self.history_last_result,
            },
        }
