"""
Backtest de la estrategia de arbitraje de funding sobre el funding LIQUIDADO guardado.

Regla simulada, por hora:
  1. Las posiciones abiertas cobran/pagan el funding real de esa hora:
       pnl_hora = notional * (rate_short - rate_long)
  2. Señal por par = media del spread en las ultimas `lookback_hours` (patas fijas por exchange).
     Se cierra si la señal cae por debajo de `exit_apr` (o se supera `max_hold_hours`).
  3. Se abre, por orden de spread, mientras haya huecos (`max_positions`) y la señal >= `entry_apr`.
  Comisiones: abrir y cerrar las dos patas (taker, o maker en la entrada).

NO modela: diferencia de precio entre exchanges al entrar/salir (basis), slippage,
liquidaciones ni ADL. Es un techo optimista del PnL de funding, util para comparar umbrales.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import analytics, config
from .store import Store

HOURS_PER_YEAR = config.HOURS_PER_YEAR
STEP = 3600  # rejilla horaria (ambos exchanges liquidan cada hora)


@dataclass
class Params:
    days: int = 31
    lookback_hours: int = 24
    entry_apr: float = 30.0          # % APR medio para abrir
    exit_apr: float = 10.0           # % APR medio por debajo del cual se cierra
    max_positions: int = 5
    notional: float = 1000.0         # USD por pata
    fee_mode: str = "taker"          # "taker" | "maker_entry"
    min_hold_hours: int = 1
    max_hold_hours: Optional[int] = None
    min_coverage: float = 0.7        # fraccion del lookback con datos en ambos exchanges
    symbol: Optional[str] = None     # limitar a un canonical_symbol


@dataclass
class Position:
    canonical: str
    long_ex: str
    short_ex: str
    entry_ts: int
    entry_spread_apr: float
    funding_pnl: float = 0.0
    hours: int = 0
    missing_hours: int = 0


@dataclass
class Trade:
    canonical: str
    long_ex: str
    short_ex: str
    entry_ts: datetime
    exit_ts: datetime
    hours: int
    entry_spread_apr: float
    exit_spread_apr: Optional[float]
    funding_pnl: float
    fees: float
    net_pnl: float
    realized_apr: float
    exit_reason: str


def _iso(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _fees_for(long_ex: str, short_ex: str, notional: float, mode: str) -> Tuple[float, float]:
    """(comision de entrada, comision de salida) en USD para las dos patas."""
    taker = (analytics.fee(long_ex, "taker") + analytics.fee(short_ex, "taker")) * notional
    maker = (analytics.fee(long_ex, "maker") + analytics.fee(short_ex, "maker")) * notional
    return (maker if mode == "maker_entry" else taker), taker


class _Series:
    """Tasa por hora de un (canonical, exchange) sobre la rejilla, con sumas prefijo para medias moviles."""

    def __init__(self, n: int):
        self.rate: List[Optional[float]] = [None] * n
        self.ps: List[float] = [0.0] * (n + 1)
        self.pc: List[int] = [0] * (n + 1)

    def finalize(self) -> None:
        for i, r in enumerate(self.rate):
            self.ps[i + 1] = self.ps[i] + (r or 0.0)
            self.pc[i + 1] = self.pc[i] + (1 if r is not None else 0)

    def window_mean(self, i: int, lookback: int) -> Tuple[Optional[float], int]:
        """Media de las horas (i-lookback, i] y numero de datos."""
        lo = max(0, i + 1 - lookback)
        cnt = self.pc[i + 1] - self.pc[lo]
        if cnt == 0:
            return None, 0
        return (self.ps[i + 1] - self.ps[lo]) / cnt, cnt


def run_backtest(store: Store, p: Params) -> Dict[str, Any]:
    now = int(datetime.now(timezone.utc).timestamp()) // STEP * STEP
    since = now - p.days * 86400
    rows = store.history_rows(since, p.symbol.upper() if p.symbol else None)
    if not rows:
        return {"params": asdict(p), "error": "no history"}

    t0 = min(r["ts"] for r in rows) // STEP * STEP
    t1 = max(r["ts"] for r in rows) // STEP * STEP
    n = (t1 - t0) // STEP + 1

    series: Dict[str, Dict[str, _Series]] = {}
    for r in rows:
        i = (r["ts"] // STEP * STEP - t0) // STEP
        if 0 <= i < n:
            series.setdefault(r["canonical_symbol"], {}).setdefault(r["exchange"], _Series(n)).rate[i] = r["funding_rate"]
    pairs: List[Tuple[str, str, str]] = []  # (canonical, ex_a, ex_b) con ex_a < ex_b
    for canonical, by_ex in series.items():
        exs = sorted(by_ex)
        if len(exs) < 2:
            continue
        for s in by_ex.values():
            s.finalize()
        pairs.append((canonical, exs[0], exs[1]))

    open_pos: Dict[str, Position] = {}
    trades: List[Trade] = []
    equity: List[Dict[str, Any]] = []
    cum_funding = cum_fees = 0.0
    to_apr = HOURS_PER_YEAR * 100.0

    def signal(canonical: str, ex_a: str, ex_b: str, i: int) -> Optional[Tuple[str, str, float]]:
        """(long_ex, short_ex, spread_apr) con la media de las ultimas lookback horas, o None sin cobertura."""
        ma, na = series[canonical][ex_a].window_mean(i, p.lookback_hours)
        mb, nb = series[canonical][ex_b].window_mean(i, p.lookback_hours)
        need = p.min_coverage * p.lookback_hours
        if ma is None or mb is None or na < need or nb < need:
            return None
        if ma <= mb:
            return ex_a, ex_b, (mb - ma) * to_apr
        return ex_b, ex_a, (ma - mb) * to_apr

    def close(pos: Position, i: int, reason: str, exit_spread: Optional[float]) -> None:
        nonlocal cum_fees
        fee_in, fee_out = _fees_for(pos.long_ex, pos.short_ex, p.notional, p.fee_mode)
        fees = fee_in + fee_out
        net = pos.funding_pnl - fees
        hours = max(pos.hours, 1)
        trades.append(
            Trade(
                canonical=pos.canonical, long_ex=pos.long_ex, short_ex=pos.short_ex,
                entry_ts=_iso(pos.entry_ts), exit_ts=_iso(t0 + i * STEP), hours=pos.hours,
                entry_spread_apr=pos.entry_spread_apr, exit_spread_apr=exit_spread,
                funding_pnl=pos.funding_pnl, fees=fees, net_pnl=net,
                realized_apr=net / p.notional * (HOURS_PER_YEAR / hours) * 100.0,
                exit_reason=reason,
            )
        )
        cum_fees += fee_out  # la de entrada ya se conto al abrir
        del open_pos[pos.canonical]

    for i in range(n):
        ts = t0 + i * STEP

        # 1. funding de la hora para las posiciones abiertas
        for pos in open_pos.values():
            rl = series[pos.canonical][pos.long_ex].rate[i]
            rs = series[pos.canonical][pos.short_ex].rate[i]
            if rl is None or rs is None:
                pos.missing_hours += 1
            else:
                pnl = p.notional * (rs - rl)
                pos.funding_pnl += pnl
                cum_funding += pnl
            pos.hours += 1

        # 2. salidas
        for canonical in list(open_pos):
            pos = open_pos[canonical]
            if pos.hours < p.min_hold_hours:
                continue
            sig = signal(canonical, *sorted([pos.long_ex, pos.short_ex]), i)
            if sig is None:
                continue
            long_ex, _, spread = sig
            # spread con las patas de la posicion (negativo si se han cruzado)
            spread_pos = spread if long_ex == pos.long_ex else -spread
            if spread_pos < p.exit_apr:
                close(pos, i, "exit_threshold", spread_pos)
            elif p.max_hold_hours and pos.hours >= p.max_hold_hours:
                close(pos, i, "max_hold", spread_pos)

        # 3. entradas
        if len(open_pos) < p.max_positions and i >= p.lookback_hours - 1 and i < n - 1:
            candidates = []
            for canonical, ex_a, ex_b in pairs:
                if canonical in open_pos:
                    continue
                sig = signal(canonical, ex_a, ex_b, i)
                if sig and sig[2] >= p.entry_apr:
                    candidates.append((sig[2], canonical, sig[0], sig[1]))
            candidates.sort(reverse=True)
            for spread, canonical, long_ex, short_ex in candidates:
                if len(open_pos) >= p.max_positions:
                    break
                fee_in, _ = _fees_for(long_ex, short_ex, p.notional, p.fee_mode)
                cum_fees += fee_in
                open_pos[canonical] = Position(canonical, long_ex, short_ex, ts, spread)

        equity.append({"ts": _iso(ts), "cum_pnl": cum_funding - cum_fees, "open_positions": len(open_pos)})

    # cierre forzoso al final de la ventana
    for canonical in list(open_pos):
        pos = open_pos[canonical]
        sig = signal(canonical, *sorted([pos.long_ex, pos.short_ex]), n - 1)
        close(pos, n - 1, "end_of_data", (sig[2] if sig and sig[0] == pos.long_ex else (-sig[2] if sig else None)))
    if equity:
        equity[-1]["cum_pnl"] = cum_funding - cum_fees

    # resumen
    net = cum_funding - cum_fees
    days_span = max((t1 - t0) / 86400.0, 1 / 24)
    capital = p.max_positions * p.notional * 2  # nocional bruto de las dos patas a 1x
    winners = [t for t in trades if t.net_pnl > 0]
    by_pair: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        agg = by_pair.setdefault(t.canonical, {"canonical_symbol": t.canonical, "trades": 0, "hours": 0, "funding_pnl": 0.0, "fees": 0.0, "net_pnl": 0.0})
        agg["trades"] += 1
        agg["hours"] += t.hours
        agg["funding_pnl"] += t.funding_pnl
        agg["fees"] += t.fees
        agg["net_pnl"] += t.net_pnl
    by_pair_list = sorted(by_pair.values(), key=lambda a: a["net_pnl"], reverse=True)

    return {
        "params": asdict(p),
        "data": {"from": _iso(t0), "to": _iso(t1), "hours": n, "pairs_available": len(pairs)},
        "summary": {
            "net_pnl": net,
            "funding_pnl": cum_funding,
            "fees": cum_fees,
            "trades": len(trades),
            "winners": len(winners),
            "win_rate": (len(winners) / len(trades)) if trades else None,
            "avg_hold_hours": (sum(t.hours for t in trades) / len(trades)) if trades else None,
            "capital_notional": capital,
            "return_on_notional_pct": net / capital * 100.0 if capital else None,
            "annualized_on_notional_pct": net / capital * (365.0 / days_span) * 100.0 if capital else None,
            "days": days_span,
        },
        "by_pair": by_pair_list,
        "trades": [asdict(t) for t in sorted(trades, key=lambda t: t.entry_ts)],
        "equity": equity,
    }
