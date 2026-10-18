"""
Logos de tokens via CoinGecko, cacheados en disco (LOGO_CACHE_DIR).
"""

import asyncio
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from . import config

SYMBOL_OVERRIDES = {"OP": "optimism"}
LOGO_TTL = timedelta(days=7)

_memo: Dict[str, Dict[str, Any]] = {}  # SYM -> {"file", "ts"}
_coin_list: Dict[str, Any] = {"ts": None, "map": {}}


def _cache_dir() -> Path:
    config.LOGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return config.LOGO_CACHE_DIR


def find_cached(sym: str) -> Optional[Path]:
    sym = sym.upper()
    meta = _memo.get(sym)
    if meta and datetime.now(timezone.utc) - meta["ts"] < LOGO_TTL:
        p = _cache_dir() / meta["file"]
        if p.exists():
            return p
    candidates = list(_cache_dir().glob(f"{sym}.*"))
    if candidates:
        _memo[sym] = {"file": candidates[0].name, "ts": datetime.now(timezone.utc)}
        return candidates[0]
    return None


def _ext(content_type: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    return {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(ctype, ".png")


async def _coin_map(client: httpx.AsyncClient) -> Dict[str, List[str]]:
    now = datetime.now(timezone.utc)
    if _coin_list["ts"] and now - _coin_list["ts"] < timedelta(hours=24):
        return _coin_list["map"]
    try:
        resp = await client.get("https://api.coingecko.com/api/v3/coins/list", params={"include_platform": "false"})
        resp.raise_for_status()
        mapping: Dict[str, List[str]] = {}
        for item in resp.json() or []:
            sym, cid = (item.get("symbol") or "").lower(), item.get("id")
            if sym and cid and len(mapping.setdefault(sym, [])) < 15:
                mapping[sym].append(cid)
        _coin_list.update(map=mapping, ts=now)
    except httpx.HTTPError as exc:
        print(f"[logos] coin list error: {exc}", flush=True)
    return _coin_list["map"]


async def _markets(client: httpx.AsyncClient, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i : i + 200]
        params = {"vs_currency": "usd", "ids": ",".join(chunk), "per_page": 250, "page": 1}
        for attempt in range(3):
            resp = await client.get("https://api.coingecko.com/api/v3/coins/markets", params=params)
            if resp.status_code != 429:
                break
            await asyncio.sleep(min(2 ** (attempt + 1), 8) * (0.5 + random.random() * 0.5))
        if resp.status_code != 200:
            print(f"[logos] markets fetch -> {resp.status_code}, stopping", flush=True)
            break
        for item in resp.json() or []:
            if item.get("id") and item.get("image"):
                out[item["id"]] = {"image": item["image"], "market_cap": item.get("market_cap") or 0}
        await asyncio.sleep(0.25)
    return out


async def resolve_logos(symbols: List[str]) -> Dict[str, Optional[str]]:
    """symbol -> ruta /api/token/logo/{symbol} o None."""
    syms = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))[:120]
    result: Dict[str, Optional[str]] = {}
    missing = []
    for s in syms:
        if find_cached(s):
            result[s] = f"/api/token/logo/{s}"
        else:
            missing.append(s)

    if missing:
        async with httpx.AsyncClient(timeout=15.0) as client:
            coin_map = await _coin_map(client)
            candidates = {s: ([SYMBOL_OVERRIDES[s]] if s in SYMBOL_OVERRIDES else coin_map.get(s.lower(), [])) for s in missing}
            ids = list(dict.fromkeys(cid for cands in candidates.values() for cid in cands))
            markets = await _markets(client, ids) if ids else {}
            for s, cands in candidates.items():
                best = max((markets[c] for c in cands if c in markets), key=lambda m: m["market_cap"], default=None)
                if not best:
                    continue
                try:
                    resp = await client.get(best["image"])
                    if resp.status_code != 200:
                        continue
                    fname = f"{s}{_ext(resp.headers.get('content-type', ''))}"
                    (_cache_dir() / fname).write_bytes(resp.content)
                    _memo[s] = {"file": fname, "ts": datetime.now(timezone.utc)}
                    result[s] = f"/api/token/logo/{s}"
                except httpx.HTTPError as exc:
                    print(f"[logos] download error {s}: {exc}", flush=True)

    for s in syms:
        result.setdefault(s, None)
    return result
