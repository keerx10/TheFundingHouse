"""
Scheduler de funding: cada MARKETS_EVERY_SECONDS refresca los mercados de cada exchange
en data-ingestion y, justo despues, pide a funding-analytics que guarde un snapshot
de pares de arbitraje. Es la unica fuente de refrescos periodicos del sistema.
"""

import os
import time
from datetime import datetime, timezone

import requests


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_float(primary: str, default_val: str, legacy: str | None = None) -> float:
    val = os.getenv(primary)
    if val is None and legacy:
        val = os.getenv(legacy)
    return float(val or default_val)


DATA_INGESTION_BASE_URL = _env("DATA_INGESTION_BASE_URL", "http://data_ingestion:8001")
FUNDING_ANALYTICS_BASE_URL = _env("FUNDING_ANALYTICS_BASE_URL", "http://funding_analytics:8002")

MARKETS_EVERY_SECONDS = _env_float("MARKETS_EVERY_SECONDS", "60", legacy="MARKETS_REFRESH_SECONDS")
SNAPSHOTS_EVERY_SECONDS = _env_float("SNAPSHOTS_EVERY_SECONDS", "60", legacy="SNAPSHOT_REFRESH_SECONDS")
REQUEST_TIMEOUT_SECONDS = _env_float("REQUEST_TIMEOUT_SECONDS", "45")


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}", flush=True)


def _safe_post(url: str) -> bool:
    try:
        resp = requests.post(url, timeout=REQUEST_TIMEOUT_SECONDS)
        body = resp.text[:200].replace("\n", " ")
        log(f"POST {url} -> {resp.status_code} {body}")
        return resp.ok
    except requests.RequestException as exc:
        log(f"ERROR calling {url}: {exc}")
        return False


def _wait_for_health(urls: list[str], max_wait: float = 120.0) -> bool:
    start = time.monotonic()
    delay = 1.0
    last_error = ""
    while time.monotonic() - start < max_wait:
        for url in urls:
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    log(f"[health] ready: {url}")
                    return True
                last_error = f"status {resp.status_code}"
            except Exception as exc:
                last_error = str(exc)
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)
    log(f"[health] not ready after {max_wait}s urls={urls} last_error={last_error}")
    return False


def _run_loop() -> None:
    next_markets = time.monotonic()
    next_snapshot = time.monotonic()

    while True:
        now = time.monotonic()

        if now >= next_markets:
            log("Refreshing markets (Backpack, Hyperliquid)")
            _safe_post(f"{DATA_INGESTION_BASE_URL}/backpack/refresh")
            _safe_post(f"{DATA_INGESTION_BASE_URL}/hyperliquid/refresh")
            next_markets = now + MARKETS_EVERY_SECONDS

        if now >= next_snapshot:
            log("Refreshing funding arbitrage snapshot")
            _safe_post(f"{FUNDING_ANALYTICS_BASE_URL}/funding/arbitrage/refresh")
            next_snapshot = now + SNAPSHOTS_EVERY_SECONDS

        time.sleep(1)


def main() -> None:
    log(
        "Starting funding scheduler "
        f"(markets every {MARKETS_EVERY_SECONDS}s, snapshots every {SNAPSHOTS_EVERY_SECONDS}s)"
    )
    _wait_for_health([f"{FUNDING_ANALYTICS_BASE_URL}/status"])
    _wait_for_health([f"{DATA_INGESTION_BASE_URL}/status"])
    _run_loop()


if __name__ == "__main__":
    main()
