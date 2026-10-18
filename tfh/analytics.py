"""
Calculo de pares de arbitraje de funding delta-neutral:
  LONG  en el exchange con APR mas bajo
  SHORT en el exchange con APR mas alto
  spread_apr_percent = APR_short - APR_long

Cada par incluye comisiones de ida y vuelta, horas para cubrirlas con funding
(breakeven_hours) y APR neto a un horizonte de mantenimiento.
"""

import math
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Dict, List, Optional

from . import config

HOURS_PER_YEAR = config.HOURS_PER_YEAR


def _f(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def annualize(funding_rate: float, interval_hours: float) -> float:
    if not interval_hours or interval_hours <= 0:
        interval_hours = 1.0
    return funding_rate * (HOURS_PER_YEAR / interval_hours) * 100.0


def fee(exchange: Optional[str], kind: str) -> float:
    return config.FEES.get((exchange or "").lower(), config.DEFAULT_FEES)[kind]


def side_from_snapshot(snap: Dict[str, Any]) -> Dict[str, Any]:
    """Pata de un par a partir de un snapshot live del adapter."""
    rate = _f(snap.get("funding_rate")) or 0.0
    interval = _f(snap.get("funding_interval_hours")) or 1.0
    return {
        "exchange": snap.get("exchange"),
        "symbol": snap.get("symbol"),
        "funding_rate": rate,
        "funding_percent": rate * 100.0,
        "funding_interval_hours": interval,
        "apr_percent": annualize(rate, interval),
        "mark_price": _f(snap.get("mark_price")),
        "index_price": _f(snap.get("index_price")),
        "open_interest": _f(snap.get("open_interest")),
        "open_interest_usd": _f(snap.get("open_interest_usd")),
        "volume_24h": _f(snap.get("volume_24h")),
        "next_funding_time": snap.get("next_funding_time"),
        "max_leverage": snap.get("max_leverage"),
        "last_updated": snap.get("timestamp"),
    }


def enrich_pair(pair: Dict[str, Any]) -> Dict[str, Any]:
    long_m, short_m = pair.get("long_market") or {}, pair.get("short_market") or {}
    spread = _f(pair.get("spread_apr_percent"))

    lm, sm = _f(long_m.get("mark_price")), _f(short_m.get("mark_price"))
    price_spread = None
    if lm and sm and lm > 0 and sm > 0:
        # Positivo = la pata short cotiza mas cara que la long (entrada favorable)
        price_spread = (sm - lm) / ((lm + sm) / 2.0) * 100.0

    taker = fee(long_m.get("exchange"), "taker") + fee(short_m.get("exchange"), "taker")
    maker = fee(long_m.get("exchange"), "maker") + fee(short_m.get("exchange"), "maker")
    fee_taker_rt = 2.0 * taker * 100.0            # abrir y cerrar las dos patas a mercado, % del nocional de una pata
    fee_maker_entry_rt = (maker + taker) * 100.0  # entrar con limit, salir a mercado

    hourly_yield = (spread / HOURS_PER_YEAR) if (spread is not None and spread > 0) else None
    pair.update(
        {
            "price_spread_percent": price_spread,
            "fee_round_trip_percent": fee_taker_rt,
            "fee_round_trip_percent_maker_entry": fee_maker_entry_rt,
            "breakeven_hours": (fee_taker_rt / hourly_yield) if hourly_yield else None,
            "breakeven_hours_maker_entry": (fee_maker_entry_rt / hourly_yield) if hourly_yield else None,
            "net_apr_percent": (spread - fee_taker_rt * (HOURS_PER_YEAR / config.NET_APR_HORIZON_HOURS)) if spread is not None else None,
            "net_apr_horizon_hours": config.NET_APR_HORIZON_HOURS,
        }
    )
    return pair


def _make_pair(canonical: str, a: Dict[str, Any], b: Dict[str, Any], **extra) -> Dict[str, Any]:
    long_m, short_m = (a, b) if a["apr_percent"] <= b["apr_percent"] else (b, a)
    ex1, ex2 = sorted([a["exchange"], b["exchange"]])
    pair = {
        "pair_id": f"{canonical}:{ex1}-{ex2}",
        "canonical_symbol": canonical,
        "spread_apr_percent": short_m["apr_percent"] - long_m["apr_percent"],
        "long_market": dict(long_m),
        "short_market": dict(short_m),
        **extra,
    }
    return enrich_pair(pair)


def live_sides_by_symbol(latest: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """latest[exchange][symbol] = snapshot  ->  sides[canonical][exchange] = side"""
    out: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for exchange, by_symbol in latest.items():
        for snap in by_symbol.values():
            canonical = snap.get("canonical_symbol") or snap.get("symbol")
            if canonical:
                out[canonical][exchange] = side_from_snapshot(snap)
    return out


def compute_live_pairs(latest: Dict[str, Dict[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    pairs: List[Dict[str, Any]] = []
    for canonical, by_ex in live_sides_by_symbol(latest).items():
        if len(by_ex) < 2:
            continue
        for ex_a, ex_b in combinations(sorted(by_ex), 2):
            a, b = by_ex[ex_a], by_ex[ex_b]
            ts = max(a.get("last_updated") or now, b.get("last_updated") or now)
            pairs.append(_make_pair(canonical, a, b, timestamp=ts, data_source="live"))
    return pairs


def compute_window_pairs(
    history_rows: List[Dict[str, Any]],
    latest: Dict[str, Dict[str, Dict[str, Any]]],
    window_hours: float,
) -> List[Dict[str, Any]]:
    """
    history_rows: salida de Store.history_window (media de funding LIQUIDADO por exchange y simbolo).
    Las patas quedan fijas por exchange durante toda la ventana, asi que el spread medio
    es lo que habria rendido mantener la posicion. Precio/OI/volumen se toman del live actual.
    """
    live = live_sides_by_symbol(latest)
    by_canon: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for r in history_rows:
        by_canon[r["canonical_symbol"]][r["exchange"]] = r

    pairs: List[Dict[str, Any]] = []
    for canonical, by_ex in by_canon.items():
        if len(by_ex) < 2:
            continue
        for ex_a, ex_b in combinations(sorted(by_ex), 2):
            sides = []
            for ex in (ex_a, ex_b):
                r = by_ex[ex]
                interval = _f(r.get("avg_interval")) or 1.0
                rate = _f(r.get("avg_rate")) or 0.0
                expected = window_hours / interval
                side = dict(live.get(canonical, {}).get(ex) or {"exchange": ex, "symbol": r.get("symbol")})
                side.update(
                    {
                        "funding_rate": rate,
                        "funding_percent": rate * 100.0,
                        "funding_interval_hours": interval,
                        "apr_percent": annualize(rate, interval),
                        "window_count": int(r.get("n") or 0),
                        "coverage_ratio": min((r.get("n") or 0) / expected, 1.0) if expected else 0.0,
                        "window_start_ts": datetime.fromtimestamp(r["min_ts"], tz=timezone.utc) if r.get("min_ts") else None,
                        "window_end_ts": datetime.fromtimestamp(r["max_ts"], tz=timezone.utc) if r.get("max_ts") else None,
                    }
                )
                sides.append(side)
            a, b = sides
            coverage = min(a["coverage_ratio"], b["coverage_ratio"])
            pairs.append(
                _make_pair(
                    canonical, a, b,
                    timestamp=max(a["window_end_ts"] or datetime.min.replace(tzinfo=timezone.utc), b["window_end_ts"] or datetime.min.replace(tzinfo=timezone.utc)),
                    data_source="settled_funding",
                    window_hours=window_hours,
                    window_count=min(a["window_count"], b["window_count"]),
                    expected_count=window_hours / max(a["funding_interval_hours"], b["funding_interval_hours"]),
                    coverage_ratio=coverage,
                    is_partial=coverage < 0.5,
                )
            )
    return pairs


def filter_pairs(
    pairs: List[Dict[str, Any]],
    min_spread_apr_percent: float = 0.0,
    canonical_symbol: Optional[str] = None,
    exchange_in: Optional[List[str]] = None,
    exchange_out: Optional[List[str]] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    allow = {e.lower() for e in exchange_in} if exchange_in else None
    block = {e.lower() for e in exchange_out} if exchange_out else None
    wanted = canonical_symbol.upper() if canonical_symbol else None

    out: List[Dict[str, Any]] = []
    for p in pairs:
        if (_f(p.get("spread_apr_percent")) or 0.0) < min_spread_apr_percent:
            continue
        if wanted and (p.get("canonical_symbol") or "").upper() != wanted:
            continue
        lx = (p["long_market"].get("exchange") or "").lower()
        sx = (p["short_market"].get("exchange") or "").lower()
        if allow is not None and (lx not in allow or sx not in allow):
            continue
        if block is not None and (lx in block or sx in block):
            continue
        out.append(p)

    out.sort(key=lambda r: _f(r.get("spread_apr_percent")) or 0.0, reverse=True)
    return out[:limit]
