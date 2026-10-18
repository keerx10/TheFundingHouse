"""
TheFundingHouse API Gateway (simplified)
- /health                        -> gateway status
- /api/v1/data-ingestion/*       -> proxy to data-ingestion microservice
- /api/funding/live              -> proxy to funding-analytics live endpoint
- /api/token/logo/{symbol}       -> cached token logos via CoinGecko
"""

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import jwt
from eth_account.messages import encode_defunct
from eth_account import Account
from siwe import SiweMessage
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import secrets

GATEWAY_PORT = 3000
DATA_INGESTION_URL = os.getenv("DATA_INGESTION_URL", "http://localhost:8001")
FUNDING_ANALYTICS_BASE_URL = os.getenv(
    "FUNDING_ANALYTICS_BASE_URL",
    "http://funding_analytics:8002",
)
TOKEN_LOGO_CACHE_DIR = Path(__file__).parent / "cache" / "token_logos"
TOKEN_LOGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_LOGO_MEMO: Dict[str, Dict[str, Any]] = {}  # sym -> {"file": str, "ts": datetime}
COIN_LIST_CACHE = {"ts": None, "map": {}}  # symbol_lower -> List[coin_id]
SYMBOL_OVERRIDES = {
    "OP": "optimism",
}
LOGO_TTL_SECONDS = 7 * 24 * 3600
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-please-change")
JWT_ALGO = "HS256"

ALLOWED_SIWE_DOMAINS = [d.strip() for d in os.getenv("ALLOWED_SIWE_DOMAINS", "localhost").split(",") if d.strip()]
ALLOWED_SIWE_URIS = [u.strip() for u in os.getenv("ALLOWED_SIWE_URIS", "").split(",") if u.strip()]
ALLOWED_CHAIN_IDS = {int(x) for x in os.getenv("ALLOWED_CHAIN_IDS", "1,137").split(",") if x.strip().isdigit()}
PORTFOLIO_PROVIDER = os.getenv("PORTFOLIO_PROVIDER", "covalent").lower()
COVALENT_API_KEY = os.getenv("COVALENT_API_KEY")

# nonce store (in-memory, ttl 5m)
NONCE_STORE: Dict[str, datetime] = {}
NONCE_TTL = timedelta(minutes=5)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [GW] %(message)s",
)
logger = logging.getLogger("tfh-gateway")

app = FastAPI(
    title="TheFundingHouse API Gateway (simple)",
    description="Gateway minimo para enrutar a servicios internos",
    version="0.1.0",
)

# CORS abierto para desarrollo
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Health basico del gateway"""
    return {"status": "ok", "service": "gateway"}


# Middleware de logging muy simple
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"{request.method} {request.url.path}")
    response = await call_next(request)
    logger.info(f"[DONE] {request.method} {request.url.path} - {response.status_code}")
    return response


def _prune_nonces():
    now = datetime.now(timezone.utc)
    expired = [n for n, ts in NONCE_STORE.items() if ts < now]
    for n in expired:
        NONCE_STORE.pop(n, None)


def _generate_nonce() -> str:
    _prune_nonces()
    nonce = secrets.token_urlsafe(16)
    NONCE_STORE[nonce] = datetime.now(timezone.utc) + NONCE_TTL
    return nonce


def _consume_nonce(nonce: str) -> bool:
    _prune_nonces()
    ts = NONCE_STORE.pop(nonce, None)
    return ts is not None


def _issue_jwt(address: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": address.lower(),
        "roles": ["user"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=24)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


async def get_current_user(request: Request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = auth.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload
    except Exception as exc:
        raise HTTPException(401, f"Invalid token: {exc}")


@app.get("/auth/nonce")
async def auth_nonce():
    """
    Devuelve un nonce SIWE (ttl 5 min).
    """
    nonce = _generate_nonce()
    return {"nonce": nonce}


@app.post("/auth/verify")
async def auth_verify(body: Dict[str, Any]):
    """
    Verifica SIWE message + signature y devuelve JWT bearer.
    """
    message = body.get("message")
    signature = body.get("signature")
    if not message or not signature:
        raise HTTPException(400, "Missing message or signature")

    try:
        siwe_msg = SiweMessage(message)
    except Exception as exc:
        raise HTTPException(400, f"Invalid SIWE message: {exc}")

    # domain / uri / chain checks
    if siwe_msg.domain not in ALLOWED_SIWE_DOMAINS:
        raise HTTPException(400, "Domain not allowed")
    if ALLOWED_SIWE_URIS and siwe_msg.uri not in ALLOWED_SIWE_URIS:
        raise HTTPException(400, "URI not allowed")
    if ALLOWED_CHAIN_IDS and siwe_msg.chain_id not in ALLOWED_CHAIN_IDS:
        raise HTTPException(400, "chainId not allowed")

    nonce = siwe_msg.nonce
    if not nonce or nonce not in NONCE_STORE:
        raise HTTPException(400, "Nonce not found or expired")

    try:
        # siwe verify already checks nonce/domain; we enforce nonce manually, too
        siwe_msg.verify(signature=signature, nonce=nonce)
    except Exception as exc:
        raise HTTPException(400, f"Signature verification failed: {exc}")

    # consume nonce (single use)
    if not _consume_nonce(nonce):
        raise HTTPException(400, "Nonce invalidated")

    # recover address for consistency
    try:
        message_hash = encode_defunct(text=siwe_msg.prepare_message())
        recovered = Account.recover_message(message_hash, signature=signature)
    except Exception:
        recovered = siwe_msg.address

    token = _issue_jwt(recovered)
    logger.info(f"[auth] SIWE verified for {recovered}")
    return {"access_token": token, "token_type": "bearer", "address": recovered}


@app.get("/auth/me")
async def auth_me(user=Depends(get_current_user)):
    try:
        return {"address": user.get("sub"), "exp": user.get("exp")}
    except Exception:
        raise HTTPException(400, "Invalid token payload")


@app.get("/portfolio/overview")
async def portfolio_overview(user=Depends(get_current_user)):
    """
    Protected: returns portfolio overview via configured provider (covalent).
    """
    address = (user.get("sub") or "").lower()
    if not address:
        raise HTTPException(400, "No address in token")
    if PORTFOLIO_PROVIDER != "covalent":
        raise HTTPException(503, "Portfolio provider not configured")
    if not COVALENT_API_KEY:
        raise HTTPException(503, "COVALENT_API_KEY missing")

    chain_id = sorted(ALLOWED_CHAIN_IDS)[0] if ALLOWED_CHAIN_IDS else 1
    url = f"https://api.covalenthq.com/v1/{chain_id}/address/{address}/balances_v2/"
    params = {"key": COVALENT_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
    except Exception as exc:
        raise HTTPException(502, f"Covalent error: {exc}")

    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Covalent returned {resp.status_code}: {resp.text[:200]}")

    data = resp.json() or {}
    items = (data.get("data") or {}).get("items") or []
    native = None
    tokens = []
    total_usd = 0.0
    for it in items:
        contract_decimals = it.get("contract_decimals") or 0
        balance_raw = it.get("balance") or "0"
        try:
            bal = int(balance_raw)
        except Exception:
            bal = 0
        usd = it.get("quote") or 0.0
        total_usd += float(usd or 0.0)
        entry = {
            "symbol": it.get("contract_ticker_symbol"),
            "balance": balance_raw,
            "decimals": contract_decimals,
            "usd": usd,
        }
        if it.get("type") == "cryptocurrency" and it.get("native_token"):
            native = entry
        else:
            tokens.append(entry)

    return {
        "address": address,
        "chain_id": chain_id,
        "native": native,
        "tokens": tokens,
        "total_usd": total_usd,
    }


@app.api_route("/api/v1/data-ingestion/{path:path}", methods=["GET"])
async def proxy_data_ingestion(request: Request, path: str) -> Response:
    """
    Proxy de solo lectura hacia data-ingestion. Los POST (refresh, bootstrap) solo
    los lanza el scheduler por la red interna, nunca desde el navegador.
    /api/v1/data-ingestion/status  ->  http://localhost:8001/status
    """
    target_url = f"{DATA_INGESTION_URL}/{path}".rstrip("/")
    if request.url.query:
        target_url += f"?{request.url.query}"

    logger.info(f"[PROXY] -> {request.method} {target_url}")

    try:
        body = await request.body()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                content=body or None,
                headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            )
    except httpx.RequestError as e:
        logger.error(f"[PROXY ERROR] {e}")
        raise HTTPException(status_code=502, detail="Error contactando data-ingestion")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in {"content-length", "transfer-encoding", "connection"}
        },
        media_type=resp.headers.get("content-type"),
    )


FUNDING_VIEWS = {"live", "1h", "8h", "24h", "3d", "7d", "15d", "31d"}


async def _proxy_funding_get(path: str, params) -> JSONResponse:
    upstream_url = FUNDING_ANALYTICS_BASE_URL.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(upstream_url, params=params)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Error calling funding-analytics: {exc}")

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={"message": f"funding-analytics {path} returned error", "status": resp.status_code, "body": resp.text},
        )
    return JSONResponse(content=resp.json())


@app.get("/api/funding/config")
async def proxy_funding_config():
    """Comisiones y parametros de calculo usados por funding-analytics."""
    return await _proxy_funding_get("/funding/config", None)


@app.get("/api/funding/{view}")
async def proxy_funding_view(view: str, request: Request):
    """
    Proxy a /funding/live y a las ventanas (1h, 8h, 24h, 3d, 7d, 15d, 31d).
    Reenvia todos los query params (min_spread_apr_percent, limit, canonical_symbol, exchange_in...).
    """
    if view not in FUNDING_VIEWS:
        raise HTTPException(status_code=404, detail=f"Unknown funding view '{view}'")
    return await _proxy_funding_get(f"/funding/{view}", request.query_params.multi_items())


def _pick_extension(content_type: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "image/png":
        return ".png"
    if ctype == "image/jpeg":
        return ".jpg"
    if ctype == "image/webp":
        return ".webp"
    return ".png"


def _find_cached(sym_upper: str) -> Optional[Path]:
    meta = TOKEN_LOGO_MEMO.get(sym_upper)
    if meta:
        filename = meta.get("file")
        ts = meta.get("ts")
        if filename and ts and (datetime.utcnow() - ts).total_seconds() < LOGO_TTL_SECONDS:
            path = TOKEN_LOGO_CACHE_DIR / filename
            if path.exists():
                return path
    candidates = list(TOKEN_LOGO_CACHE_DIR.glob(f"{sym_upper}.*"))
    if candidates:
        TOKEN_LOGO_MEMO[sym_upper] = {"file": candidates[0].name, "ts": datetime.utcnow()}
        return candidates[0]
    return None


async def _ensure_coin_list() -> Dict[str, List[str]]:
    now = datetime.utcnow()
    ts = COIN_LIST_CACHE["ts"]
    if ts and now - ts < timedelta(hours=24):
        return COIN_LIST_CACHE["map"]

    url = "https://api.coingecko.com/api/v3/coins/list"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params={"include_platform": "false"})
            logger.info(f"[token_logos] fetch coin list -> {resp.status_code}")
            resp.raise_for_status()
            data = resp.json() or []
            mapping: Dict[str, List[str]] = {}
            for item in data:
                sym = (item.get("symbol") or "").lower()
                cid = item.get("id")
                if not sym or not cid:
                    continue
                bucket = mapping.setdefault(sym, [])
                if len(bucket) < 15:  # keep up to 15 candidates per symbol
                    bucket.append(cid)
            COIN_LIST_CACHE["map"] = mapping
            COIN_LIST_CACHE["ts"] = now
            return mapping
    except httpx.HTTPError as exc:
        logger.error(f"[token_logos] error fetching coin list: {exc}")
        return COIN_LIST_CACHE["map"]


async def _download_markets(ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Given a list of coin ids, fetch markets and return id -> {"image": str, "market_cap": float}
    """
    if not ids:
        return {}
    url = "https://api.coingecko.com/api/v3/coins/markets"
    out: Dict[str, Dict[str, Any]] = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # chunk ids in groups of 200
            for i in range(0, len(ids), 200):
                chunk = ids[i : i + 200]
                params = {
                    "vs_currency": "usd",
                    "ids": ",".join(chunk),
                    "per_page": 250,
                    "page": 1,
                }
                # basic backoff retry for 429
                attempts = 0
                while attempts < 3:
                    resp = await client.get(url, params=params)
                    logger.info(f"[token_logos] markets fetch ({len(chunk)} ids) -> {resp.status_code}")
                    if resp.status_code == 429:
                        attempts += 1
                        sleep_s = min(2 ** attempts, 8) * (0.5 + random.random() * 0.5)
                        logger.warning(f"[token_logos] 429 from CoinGecko, retrying in {sleep_s:.2f}s (attempt {attempts})")
                        await asyncio.sleep(sleep_s)
                        continue
                    break
                logger.info(f"[token_logos] markets fetch ({len(chunk)} ids) -> {resp.status_code}")
                if resp.status_code == 429:
                    logger.warning("[token_logos] 429 from CoinGecko, skipping remaining downloads")
                    break
                resp.raise_for_status()
                data = resp.json() or []
                for item in data:
                    cid = item.get("id")
                    img = item.get("image")
                    mc = item.get("market_cap") or 0
                    if cid and img:
                        out[cid] = {"image": img, "market_cap": mc}
                # rate-limit between chunks
                await asyncio.sleep(0.25 + random.random() * 0.15)
        return out
    except httpx.HTTPError as exc:
        logger.error(f"[token_logos] error fetching markets: {exc}")
        return {}


@app.get("/api/token/logo/{symbol}")
async def token_logo(symbol: str):
    """
    Serve token logo from local cache only.
    """
    sym = (symbol or "").strip()
    if not sym:
        raise HTTPException(status_code=404, detail="Symbol not provided")
    sym_upper = sym.upper()

    meta = TOKEN_LOGO_MEMO.get(sym_upper)
    if meta:
        filename = meta.get("file")
        ts = meta.get("ts")
        if filename and ts and (datetime.utcnow() - ts).total_seconds() < LOGO_TTL_SECONDS:
            path = TOKEN_LOGO_CACHE_DIR / filename
            if path.exists():
                return FileResponse(path)
        # stale -> ignore

    # try to find any cached file on disk
    candidates = list(TOKEN_LOGO_CACHE_DIR.glob(f"{sym_upper}.*"))
    if candidates:
        path = candidates[0]
        TOKEN_LOGO_MEMO[sym_upper] = {"file": path.name, "ts": datetime.utcnow()}
        if path.exists():
            return FileResponse(path)

    raise HTTPException(status_code=404, detail="Logo not available")


@app.get("/api/token/logos")
async def token_logos(symbols: str = ""):
    """
    Batch logo resolution. Returns mapping symbol -> /api/token/logo/{symbol} or null.
    """
    raw_syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not raw_syms:
        return JSONResponse(content={})

    # unique + cap
    seen = set()
    sym_list: List[str] = []
    for s in raw_syms:
        if s in seen:
            continue
        seen.add(s)
        sym_list.append(s)
        if len(sym_list) >= 120:
            break

    result: Dict[str, Optional[str]] = {}
    missing: List[str] = []

    # Check cache first
    for sym in sym_list:
        cached = _find_cached(sym)
        if cached:
            result[sym] = f"/api/token/logo/{sym}"
        else:
            missing.append(sym)

    # Resolve missing via CoinGecko coin list + markets (single call)
    if missing:
        coin_map = await _ensure_coin_list()
        ids: List[str] = []
        sym_candidates: Dict[str, List[str]] = {}
        for sym in missing:
            override_id = SYMBOL_OVERRIDES.get(sym)
            candidates = []
            if override_id:
                candidates = [override_id]
            else:
                candidates = coin_map.get(sym.lower()) or []
            if candidates:
                sym_candidates[sym] = candidates
                for cid in candidates:
                    if cid not in ids:
                        ids.append(cid)

        markets = await _download_markets(ids)
        async with httpx.AsyncClient(timeout=10.0) as client:
            for sym, candidates in sym_candidates.items():
                best_id = None
                best_mc = -1
                best_img = None
                for cid in candidates:
                    data = markets.get(cid)
                    if not data:
                        continue
                    mc = data.get("market_cap") or 0
                    img = data.get("image")
                    if mc > best_mc and img:
                        best_mc = mc
                        best_id = cid
                        best_img = img
                if best_img:
                    try:
                        resp = await client.get(best_img)
                        if resp.status_code != 200:
                            continue
                        ext = _pick_extension(resp.headers.get("content-type"))
                        filename = f"{sym}{ext}"
                        filepath = TOKEN_LOGO_CACHE_DIR / filename
                        filepath.write_bytes(resp.content)
                        TOKEN_LOGO_MEMO[sym] = {"file": filename, "ts": datetime.utcnow()}
                        result[sym] = f"/api/token/logo/{sym}"
                    except httpx.HTTPError as exc:
                        logger.error(f"[token_logos] download error for {sym}: {exc}")

    # Fill nulls
    for sym in sym_list:
        result.setdefault(sym, None)

    return JSONResponse(content=result)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"[ERROR] {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal Server Error", "message": str(exc)},
    )


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Iniciando API Gateway en http://localhost:{GATEWAY_PORT}")
    uvicorn.run("main:app", host="0.0.0.0", port=GATEWAY_PORT, reload=True)
