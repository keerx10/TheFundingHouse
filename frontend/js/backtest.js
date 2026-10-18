// Página de backtest: formulario -> /api/backtest -> resumen, curva de PnL, tablas.

function initBacktest() {
    const form = document.getElementById("bt-form");
    if (!form) return;
    form.addEventListener("submit", (e) => {
        e.preventDefault();
        runBacktest(new FormData(form));
    });
    runBacktest(new FormData(form));
}

async function runBacktest(formData) {
    const btn = document.getElementById("bt-run");
    const msg = document.getElementById("bt-msg");
    const params = new URLSearchParams();
    for (const [k, v] of formData.entries()) {
        if (v !== "" && v != null) params.set(k, String(v).trim());
    }
    btn.disabled = true;
    msg.textContent = "Calculando…";
    const t0 = performance.now();
    try {
        const res = await fetch(`/api/backtest?${params.toString()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
        const r = await res.json();
        if (r.error) {
            msg.textContent = `Sin datos: ${r.error}`;
            return;
        }
        renderBacktest(r);
        msg.textContent = `${r.data.pairs_available} pares · ${r.data.hours} h de datos (${fmtDate(r.data.from)} → ${fmtDate(r.data.to)}) · ${Math.round(performance.now() - t0)} ms`;
    } catch (err) {
        console.error(err);
        msg.textContent = `Error: ${err.message}`;
    } finally {
        btn.disabled = false;
    }
}

const fmtUsd = (v) => (v == null ? "-" : `${v < 0 ? "-" : ""}$${Math.abs(v).toFixed(2)}`);
const fmtPct = (v, d = 1) => (v == null ? "-" : `${Number(v).toFixed(d)}%`);
const fmtDate = (iso) => (iso ? new Date(iso).toISOString().slice(0, 16).replace("T", " ") : "-");
const sign = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

function renderBacktest(r) {
    const s = r.summary;
    const summary = document.getElementById("bt-summary");
    const stats = [
        ["PnL neto", fmtUsd(s.net_pnl), sign(s.net_pnl)],
        ["Funding cobrado", fmtUsd(s.funding_pnl), sign(s.funding_pnl)],
        ["Comisiones", fmtUsd(-s.fees), "neg"],
        ["Retorno s/ nocional", fmtPct(s.return_on_notional_pct, 2), sign(s.return_on_notional_pct)],
        ["Anualizado", fmtPct(s.annualized_on_notional_pct), sign(s.annualized_on_notional_pct)],
        ["Trades", `${s.trades}`, ""],
        ["Win rate", s.win_rate == null ? "-" : fmtPct(s.win_rate * 100, 0), ""],
        ["Media horas/trade", s.avg_hold_hours == null ? "-" : s.avg_hold_hours.toFixed(1), ""],
    ];
    summary.innerHTML = stats
        .map(([k, v, c]) => `<div class="stat"><div class="v ${c}">${v}</div><div class="k">${k}</div></div>`)
        .join("");
    summary.classList.remove("hidden");
    summary.title = `Nocional bruto a 1x: $${s.capital_notional.toFixed(0)} (${r.params.max_positions} posiciones × 2 patas × $${r.params.notional})`;

    renderEquity(r.equity || []);

    const byPair = document.getElementById("bt-by-pair");
    byPair.innerHTML = r.by_pair.length
        ? r.by_pair
              .map(
                  (p) => `<tr class="border-t border-white/5">
                <td class="py-2 font-semibold">${p.canonical_symbol}</td><td>${p.trades}</td><td>${p.hours}</td>
                <td class="font-mono">${fmtUsd(p.funding_pnl)}</td><td class="font-mono text-slate-400">${fmtUsd(-p.fees)}</td>
                <td class="font-mono ${sign(p.net_pnl)}">${fmtUsd(p.net_pnl)}</td></tr>`,
              )
              .join("")
        : '<tr><td colspan="6" class="py-6 text-center text-slate-500">Ningún par superó el umbral de entrada</td></tr>';

    const trades = document.getElementById("bt-trades");
    trades.innerHTML = r.trades.length
        ? r.trades
              .map(
                  (t) => `<tr class="border-t border-white/5">
                <td class="py-2 font-semibold">${t.canonical}</td>
                <td class="text-slate-400">${t.long_ex.slice(0, 2)} / ${t.short_ex.slice(0, 2)}</td>
                <td class="text-slate-400 whitespace-nowrap">${fmtDate(t.entry_ts)}</td><td>${t.hours}</td>
                <td class="font-mono text-slate-400">${fmtPct(t.entry_spread_apr, 0)} → ${fmtPct(t.exit_spread_apr, 0)}</td>
                <td class="font-mono ${sign(t.net_pnl)}">${fmtUsd(t.net_pnl)}</td>
                <td class="text-slate-500">${t.exit_reason.replace("_", " ")}</td></tr>`,
              )
              .join("")
        : '<tr><td colspan="7" class="py-6 text-center text-slate-500">Sin trades</td></tr>';
}

function renderEquity(points) {
    const wrap = document.getElementById("bt-equity-wrap");
    const svg = document.getElementById("bt-equity");
    if (!points.length) {
        wrap.classList.add("hidden");
        return;
    }
    wrap.classList.remove("hidden");
    const W = 1000, H = 220, pad = 10;
    const ys = points.map((p) => p.cum_pnl);
    const minY = Math.min(0, ...ys), maxY = Math.max(0, ...ys);
    const span = maxY - minY || 1;
    const x = (i) => pad + (i / Math.max(points.length - 1, 1)) * (W - 2 * pad);
    const y = (v) => H - pad - ((v - minY) / span) * (H - 2 * pad);
    const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.cum_pnl).toFixed(1)}`).join(" ");
    const zero = y(0).toFixed(1);
    const last = ys[ys.length - 1];
    svg.innerHTML = `
        <line x1="0" y1="${zero}" x2="${W}" y2="${zero}" stroke="rgba(255,255,255,0.15)" stroke-dasharray="4 4"/>
        <path d="${path}" fill="none" stroke="${last >= 0 ? "#2dd4bf" : "#f87171"}" stroke-width="2" vector-effect="non-scaling-stroke"/>
    `;
    document.getElementById("bt-equity-range").textContent = `${fmtDate(points[0].ts)} → ${fmtDate(points[points.length - 1].ts)} · mín ${fmtUsd(Math.min(...ys))} · máx ${fmtUsd(Math.max(...ys))}`;
}
