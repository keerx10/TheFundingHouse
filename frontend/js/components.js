// --- 1. Sidebar ---
function renderSidebar() {
    const container = document.getElementById("sidebar-container");
    if (!container) return;

    container.innerHTML = `
    <div class="p-6">
        <div class="flex items-center gap-2 mb-1 cursor-pointer" onclick="window.location.href='../index.html'">
            <div class="w-7 h-7 bg-gradient-to-br from-brandTeal to-brandPurple rounded-lg flex items-center justify-center">
                <span class="font-bold text-white text-sm">F</span>
            </div>
            <span class="text-lg font-bold text-white">TheFundingHouse</span>
        </div>
        <span class="text-xs text-slate-500 pl-9">Funding Rate Explorer</span>
    </div>

    <nav class="flex-1 px-4 py-4 space-y-2">
        <a href="#" class="flex items-center gap-3 px-4 py-3 bg-[#1e1b4b] text-brandTeal rounded-xl font-medium border border-teal-500/20 transition-colors">
            <i data-lucide="bar-chart-2" class="w-5 h-5"></i>
            Funding Explorer
        </a>
    </nav>

    <div id="sidebar-status" class="p-4 border-t border-white/5 mt-auto text-xs text-slate-500 space-y-1">
        <div>Cargando estado…</div>
    </div>
    `;
}

// --- 2. Estado de la UI ---
let currentTimeframe = "live";
let allRows = [];
let filteredRows = [];
let logoMap = {};
let filtersState = {
    aprMin: null,
    aprMax: null,
    netAprMin: null,
    breakevenMax: null,
    priceMin: null,
    priceMax: null,
    oiMin: null,
    oiMax: null,
    volMin: null,
    volMax: null,
};
let searchTerm = "";
let __tfhInitDone = false;
const LIVE_AUTO_REFRESH_MS = 60_000;

function initUIInteractions() {
    if (__tfhInitDone) return;

    // A. Popover de filtros
    const filtersBtn = document.getElementById("filters-btn");
    const filtersPopover = document.getElementById("filters-popover");
    const applyBtn = document.getElementById("apply-filters-btn");
    const resetBtn = document.getElementById("reset-filters-btn");

    const positionFiltersPopover = () => {
        if (!filtersPopover || !filtersBtn) return;
        filtersPopover.style.left = "0";
        filtersPopover.style.right = "auto";
        const rect = filtersPopover.getBoundingClientRect();
        if (rect.right > window.innerWidth - 8) {
            filtersPopover.style.left = "auto";
            filtersPopover.style.right = "0";
        }
    };

    const toggleFiltersPopover = (forceState) => {
        if (!filtersPopover) return;
        const shouldShow =
            typeof forceState === "boolean" ? forceState : filtersPopover.classList.contains("hidden");
        if (shouldShow) {
            filtersPopover.classList.remove("hidden");
            positionFiltersPopover();
        } else {
            filtersPopover.classList.add("hidden");
        }
    };

    if (filtersBtn && filtersPopover) {
        filtersBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            toggleFiltersPopover();
        });
        document.addEventListener("click", (e) => {
            if (
                filtersPopover.classList.contains("hidden") ||
                filtersBtn.contains(e.target) ||
                filtersPopover.contains(e.target)
            ) {
                return;
            }
            toggleFiltersPopover(false);
        });
    }

    if (applyBtn) {
        applyBtn.addEventListener("click", (e) => {
            e.preventDefault();
            applyFilters();
            filtersPopover && filtersPopover.classList.add("hidden");
        });
    }
    if (resetBtn) {
        resetBtn.addEventListener("click", (e) => {
            e.preventDefault();
            resetFilters();
        });
    }

    // B. Temporalidades
    const timeButtons = document.querySelectorAll(".time-btn");
    timeButtons.forEach((button) => {
        button.addEventListener("click", () => {
            timeButtons.forEach((b) => b.classList.remove("active"));
            button.classList.add("active");
            currentTimeframe = button.dataset.val || "live";
            loadFunding(currentTimeframe);
        });
    });

    // C. Buscador
    const searchInput = document.querySelector("[data-funding-search]");
    if (searchInput) {
        searchInput.addEventListener("input", (e) => {
            searchTerm = (e.target.value || "").toLowerCase().trim();
            recomputeAndRender();
        });
    }

    __tfhInitDone = true;
}

// --- 3. Datos ---
function initDashboard() {
    loadFunding("live");
    loadFeesNotice();
    loadStatus();
    setInterval(() => {
        loadFunding(currentTimeframe, { silent: true });
        loadStatus();
    }, LIVE_AUTO_REFRESH_MS);
}

async function loadFunding(timeframe, { silent = false } = {}) {
    const tableBody =
        document.querySelector("[data-funding-table-body]") || document.getElementById("funding-table-body");
    if (!tableBody) return;

    if (!silent) {
        tableBody.innerHTML =
            '<tr><td colspan="10" class="text-center py-10 text-slate-400">Loading...</td></tr>';
    }

    try {
        const params = new URLSearchParams({ min_spread_apr_percent: "0", limit: "200" });
        const res = await fetch(`/api/funding/${timeframe}?${params.toString()}`);
        // Respuesta obsoleta: el usuario ya cambió de temporalidad
        if (timeframe !== currentTimeframe) return;
        if (!res.ok) {
            console.error("Failed to fetch funding data", res.status);
            tableBody.innerHTML =
                '<tr><td colspan="10" class="text-center py-4 text-red-400">Error loading data</td></tr>';
            allRows = [];
            filteredRows = [];
            return;
        }
        const data = await res.json();
        if (timeframe !== currentTimeframe) return;

        data.sort((a, b) => (Number(b.spread_apr_percent) || 0) - (Number(a.spread_apr_percent) || 0));
        allRows = data;
        recomputeAndRender();
    } catch (err) {
        if (timeframe !== currentTimeframe) return;
        console.error("Error loading funding:", err);
        if (!silent) {
            tableBody.innerHTML =
                '<tr><td colspan="10" class="text-center py-4 text-red-400">Error loading data</td></tr>';
        }
    }
}

async function loadStatus() {
    const el = document.getElementById("sidebar-status");
    if (!el) return;
    try {
        const res = await fetch("/api/status");
        if (!res.ok) return;
        const st = await res.json();
        const ago = (iso) => {
            if (!iso) return "—";
            const s = Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
            return s < 90 ? `hace ${s}s` : `hace ${Math.round(s / 60)}m`;
        };
        const liveLines = Object.entries(st.live || {})
            .map(([ex, v]) => `<div class="flex justify-between"><span>${ex}</span><span class="${v.error ? "text-red-400" : ""}">${v.markets} mkts · ${ago(v.updated_at)}</span></div>`)
            .join("");
        const h = st.history || {};
        const histDays = h.oldest ? ((Date.now() - new Date(h.oldest)) / 86400000).toFixed(1) : "0";
        el.innerHTML = `
            <div class="text-slate-400 font-medium mb-1">${st.pairs_live} pares live</div>
            ${liveLines}
            <div class="flex justify-between pt-1 border-t border-white/5 mt-1"><span>Histórico</span><span>${histDays}d · ${h.syncing ? "sincronizando…" : ago(h.last_sync)}</span></div>
        `;
    } catch (e) {
        /* sin estado */
    }
}

async function loadFeesNotice() {
    const el = document.getElementById("funding-fees-notice");
    if (!el) return;
    try {
        const res = await fetch("/api/funding/config");
        if (!res.ok) return;
        const cfg = await res.json();
        const pct = (v) => `${(Number(v) * 100).toFixed(3)}%`;
        const parts = Object.entries(cfg.fees || {}).map(
            ([ex, f]) => `${ex}: taker ${pct(f.taker)} / maker ${pct(f.maker)}`,
        );
        el.textContent = `Comisiones usadas → ${parts.join(" · ")} · Net APR a ${cfg.net_apr_horizon_hours}h de mantenimiento · Ventanas = funding liquidado real`;
    } catch (e) {
        /* sin aviso de comisiones */
    }
}

// --- 4. Render ---
function renderFundingRows(data) {
    const tableBody =
        document.querySelector("[data-funding-table-body]") || document.getElementById("funding-table-body");
    if (!tableBody) return;

    // Aviso global si la ventana tiene poca cobertura en todos los pares
    const allPartial =
        currentTimeframe !== "live" && Array.isArray(data) && data.length > 0 && data.every((p) => p.is_partial);
    const globalNotice = document.getElementById("funding-global-notice");
    if (globalNotice) globalNotice.classList.toggle("hidden", !allPartial);

    if (!data || data.length === 0) {
        tableBody.innerHTML =
            '<tr><td colspan="10" class="text-center py-10 text-slate-400">No data available</td></tr>';
        return;
    }

    const fmtPercent = (v) => (v == null ? "-" : `${Number(v).toFixed(1)}%`);
    const fmtPercent2 = (v) => (v == null ? "-" : `${Number(v).toFixed(2)}%`);
    const fmtNum = (v) => {
        const num = Number(v);
        if (!isFinite(num)) return "-";
        if (num >= 1_000_000_000) return (num / 1_000_000_000).toFixed(1) + "B";
        if (num >= 1_000_000) return (num / 1_000_000).toFixed(1) + "M";
        if (num >= 1_000) return (num / 1_000).toFixed(1) + "K";
        return num.toFixed(0);
    };
    const fmtHours = (h) => {
        const num = Number(h);
        if (!Number.isFinite(num) || num <= 0) return "-";
        if (num < 1) return `${Math.round(num * 60)}m`;
        if (num < 48) return `${num.toFixed(1)}h`;
        return `${(num / 24).toFixed(1)}d`;
    };

    tableBody.innerHTML = "";

    data.forEach((pair, i) => {
        const long = pair.long_market || {};
        const short = pair.short_market || {};

        const longOiVal = computeOiUsd(long);
        const shortOiVal = computeOiUsd(short);
        const longOi = longOiVal != null ? fmtNum(longOiVal) : "-";
        const shortOi = shortOiVal != null ? fmtNum(shortOiVal) : "-";
        const totalOi = longOiVal == null && shortOiVal == null ? "-" : fmtNum((longOiVal || 0) + (shortOiVal || 0));

        const priceSpreadPctVal = pair.price_spread_percent ?? computePriceSpreadPct(pair);
        const priceSpreadPctText = priceSpreadPctVal == null ? "-" : `${Number(priceSpreadPctVal).toFixed(2)}%`;

        const volL = Number(long.volume_24h);
        const volS = Number(short.volume_24h);
        const volLText = Number.isFinite(volL) && volL > 0 ? fmtNum(volL) : "-";
        const volSText = Number.isFinite(volS) && volS > 0 ? fmtNum(volS) : "-";
        const totalVolText =
            !Number.isFinite(volL) && !Number.isFinite(volS)
                ? "-"
                : fmtNum((Number.isFinite(volL) ? volL : 0) + (Number.isFinite(volS) ? volS : 0));

        const symbolKey = (pair.canonical_symbol || pair.pair_id || "").toString().toUpperCase();
        const logoUrl = symbolKey ? logoMap[symbolKey] : undefined;
        const tokenVisual = logoUrl
            ? `<img src="${logoUrl}" alt="${pair.canonical_symbol || ""}" class="w-[18px] h-[18px] rounded-full flex-none" style="margin-right:4px;" onerror="this.style.display='none';" />`
            : `<div class="w-[22px] h-[22px] rounded-full bg-slate-700 flex items-center justify-center text-[10px] flex-none">${(pair.canonical_symbol || "?")[0]}</div>`;

        const netApr = Number(pair.net_apr_percent);
        const netAprClass = Number.isFinite(netApr) && netApr > 0 ? "text-green-400" : "text-red-400";
        const breakevenTitle =
            pair.breakeven_hours_maker_entry != null
                ? `Entrando con órdenes límite (maker): ${fmtHours(pair.breakeven_hours_maker_entry)}`
                : "";

        // Columna "Data": en Live, próximo funding; en ventanas, cobertura real de funding liquidado
        let dataCell = "";
        if (pair.coverage_ratio != null) {
            const cov = Math.round(Number(pair.coverage_ratio) * 100);
            const covClass = pair.is_partial ? "text-amber-300" : "text-slate-400";
            dataCell = `<span class="${covClass}" title="${pair.window_count ?? 0} pagos de funding de ${Math.round(pair.expected_count ?? 0)} esperados">${cov}% · ${pair.window_count ?? 0} pagos</span>`;
        } else {
            const nft = long.next_funding_time || short.next_funding_time;
            const mins = nft ? Math.max(0, Math.round((new Date(nft) - Date.now()) / 60000)) : null;
            dataCell = mins == null ? "live" : `<span title="Próximo pago de funding">live · ${mins}m</span>`;
        }

        const tr = document.createElement("tr");
        tr.className = "hover:bg-white/5 transition-colors group border-b border-white/5";
        tr.innerHTML = `
            <td class="px-4 py-3 text-slate-400 first:rounded-l-lg">${i + 1}</td>
            <td class="px-4 py-3 text-white whitespace-nowrap">
                <div class="inline-flex items-center gap-2 whitespace-nowrap" style="line-height:1;">
                    ${tokenVisual}
                    <span class="font-semibold inline-block">${pair.canonical_symbol || pair.pair_id || "-"}</span>
                </div>
            </td>
            <td class="px-4 py-3 text-brandTeal font-mono">${fmtPercent(pair.spread_apr_percent)}</td>
            <td class="px-4 py-3 font-mono ${netAprClass}" title="Comisiones ida y vuelta: ${fmtPercent2(pair.fee_round_trip_percent)} · horizonte ${pair.net_apr_horizon_hours ?? "-"}h">${fmtPercent(pair.net_apr_percent)}</td>
            <td class="px-4 py-3 text-slate-300 font-mono" title="${breakevenTitle}">${fmtHours(pair.breakeven_hours)}</td>
            <td class="px-4 py-3 text-xs whitespace-nowrap">
                <div class="text-green-400/80">L: ${long.exchange || "-"} <span class="text-slate-500">${fmtPercent(long.apr_percent)}</span></div>
                <div class="text-red-400/80">S: ${short.exchange || "-"} <span class="text-slate-500">${fmtPercent(short.apr_percent)}</span></div>
            </td>
            <td class="px-4 py-3 text-slate-400 font-mono hidden md:table-cell">${priceSpreadPctText}</td>
            <td class="px-4 py-3 text-slate-400 font-mono text-xs hidden md:table-cell">
                <div class="text-green-400/70">L: ${longOi}</div>
                <div class="text-red-400/70">S: ${shortOi}</div>
                <div class="text-slate-400/70 text-[12px]">T: ${totalOi}</div>
            </td>
            <td class="px-4 py-3 text-slate-500 font-mono text-xs hidden md:table-cell">
                <div class="text-green-400/70">L: ${volLText}</div>
                <div class="text-red-400/70">S: ${volSText}</div>
                <div class="text-slate-400/70 text-[12px]">T: ${totalVolText}</div>
            </td>
            <td class="px-4 py-3 text-xs text-right hidden lg:table-cell last:rounded-r-lg">${dataCell}</td>
        `;
        tableBody.appendChild(tr);
    });

    if (window.lucide) window.lucide.createIcons();
}

async function fetchTokenLogos(symbols) {
    const uniq = Array.from(new Set(symbols.filter(Boolean)));
    if (!uniq.length) return {};
    try {
        const res = await fetch(`/api/token/logos?symbols=${encodeURIComponent(uniq.join(","))}`);
        if (!res.ok) return {};
        return await res.json();
    } catch (e) {
        console.error("Error fetching token logos", e);
        return {};
    }
}

let __logosRequested = new Set();
async function loadLogosForRows(rows) {
    const symbols = rows
        .slice(0, 100)
        .map((r) => (r.canonical_symbol || r.pair_id || "").toString().toUpperCase())
        .filter(Boolean);
    const missing = symbols.filter((s) => !logoMap[s] && !__logosRequested.has(s));
    if (!missing.length) return;
    missing.forEach((s) => __logosRequested.add(s));
    const map = await fetchTokenLogos(missing);
    const found = Object.fromEntries(Object.entries(map || {}).filter(([, v]) => v));
    if (Object.keys(found).length) {
        logoMap = { ...logoMap, ...found };
        renderFundingRows(filteredRows);
    }
}

// --- 5. Filtros ---
function parseFiltersFromInputs() {
    const next = { ...filtersState };
    document.querySelectorAll("[data-filter-input]").forEach((inp) => {
        const key = inp.dataset.key;
        if (!key) return;
        const raw = inp.value;
        if (raw === "" || raw == null) {
            next[key] = null;
            return;
        }
        const num = Number(raw);
        next[key] = Number.isFinite(num) ? num : null;
    });
    return next;
}

function matchesBounds(val, min, max) {
    if (min == null && max == null) return true;
    if (!Number.isFinite(val)) return false;
    if (min != null && val < min) return false;
    if (max != null && val > max) return false;
    return true;
}

function computeOiUsd(mkt) {
    // El backend calcula open_interest_usd; ambos exchanges reportan OI en monedas.
    const usd = Number(mkt?.open_interest_usd);
    if (Number.isFinite(usd) && usd > 0) return usd;
    const oi = Number(mkt?.open_interest);
    const price = Number(mkt?.mark_price);
    if (!Number.isFinite(oi) || oi <= 0 || !Number.isFinite(price) || price <= 0) return null;
    return oi * price;
}

function computePriceSpreadPct(pair) {
    const a = Number(pair?.long_market?.mark_price);
    const b = Number(pair?.short_market?.mark_price);
    if (!Number.isFinite(a) || !Number.isFinite(b) || a <= 0 || b <= 0) return null;
    return ((b - a) / ((a + b) / 2)) * 100;
}

function computeTotalOi(pair) {
    const longVal = computeOiUsd(pair.long_market || {});
    const shortVal = computeOiUsd(pair.short_market || {});
    if (longVal == null && shortVal == null) return null;
    return (longVal || 0) + (shortVal || 0);
}

function computeTotalVol(pair) {
    const volL = Number(pair?.long_market?.volume_24h);
    const volS = Number(pair?.short_market?.volume_24h);
    const hasL = Number.isFinite(volL);
    const hasS = Number.isFinite(volS);
    if (!hasL && !hasS) return null;
    return (hasL ? volL : 0) + (hasS ? volS : 0);
}

function filterRows(data, filters) {
    return (data || []).filter((pair) => {
        if (!matchesBounds(Number(pair.spread_apr_percent), filters.aprMin, filters.aprMax)) return false;
        if (!matchesBounds(Number(pair.net_apr_percent), filters.netAprMin, null)) return false;
        if (filters.breakevenMax != null) {
            const breakeven = Number(pair.breakeven_hours);
            if (!Number.isFinite(breakeven) || breakeven > filters.breakevenMax) return false;
        }
        const priceSpreadVal = pair.price_spread_percent ?? computePriceSpreadPct(pair);
        if (!matchesBounds(priceSpreadVal, filters.priceMin, filters.priceMax)) return false;
        if (!matchesBounds(computeTotalOi(pair), filters.oiMin, filters.oiMax)) return false;
        if (!matchesBounds(computeTotalVol(pair), filters.volMin, filters.volMax)) return false;
        return true;
    });
}

function applySearch(rows, term) {
    const t = (term || "").toLowerCase().trim();
    if (!t) return rows;
    return rows.filter((pair) => {
        const sym = (pair.canonical_symbol || "").toLowerCase();
        const longSym = ((pair.long_market || {}).symbol || "").toLowerCase();
        const shortSym = ((pair.short_market || {}).symbol || "").toLowerCase();
        return sym.includes(t) || longSym.includes(t) || shortSym.includes(t);
    });
}

function recomputeAndRender() {
    filteredRows = applySearch(filterRows(allRows, filtersState), searchTerm);
    renderFundingRows(filteredRows);
    loadLogosForRows(filteredRows);
}

function applyFilters() {
    filtersState = parseFiltersFromInputs();
    recomputeAndRender();
}

function resetFilters() {
    filtersState = Object.fromEntries(Object.keys(filtersState).map((k) => [k, null]));
    document.querySelectorAll("[data-filter-input]").forEach((inp) => {
        inp.value = "";
    });
    recomputeAndRender();
}
