// --- 1. Sidebar Renderer (Sin Spot-Perp) ---
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
        <!-- Se ha eliminado Spot-Perp Arbs como pediste -->
    </nav>

    <div class="p-4 border-t border-white/5 mt-auto">
        <button id="connect-wallet-btn" class="w-full bg-[#1e1b4b] hover:bg-[#2d2a6e] text-indigo-100 py-3 rounded-xl font-medium flex items-center justify-center gap-2 border border-indigo-500/30 transition-colors">
            <i data-lucide="wallet" class="w-5 h-5"></i>
            Connect Wallet
        </button>
    </div>
    `;
}

// --- 2. Lógica de Interfaz (Dropdown, Temporalidades, Filtros) ---
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
let walletState = { address: null, provider: null };
let walletListenersAttached = false;
let walletMenuListenersAttached = false;
const API_BASE = window.__TFH_API_BASE || "http://localhost:3000";
const TFH_PROFILE_URL = window.__TFH_PROFILE_URL || "/profile.html";
let __tfhInitDone = false;

function safeJsonParse(str) {
    try {
        return JSON.parse(str);
    } catch (err) {
        console.warn("Invalid tfh_wallet, clearing.");
        return null;
    }
}

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
        const margin = 8;
        if (rect.right > window.innerWidth - margin) {
            filtersPopover.style.left = "auto";
            filtersPopover.style.right = "0";
        }
    };

    const toggleFiltersPopover = (forceState) => {
        if (!filtersPopover) return;
        const shouldShow =
            typeof forceState === "boolean"
                ? forceState
                : filtersPopover.classList.contains("hidden");
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

    // C. Filtros de Tiempo
    const timeButtons = document.querySelectorAll(".time-btn");
    timeButtons.forEach((button) => {
        button.addEventListener("click", () => {
            // Remover clase active de todos
            timeButtons.forEach((b) => b.classList.remove("active"));
            // Añadir al clickeado
            button.classList.add("active");

            const tf = button.dataset.val || "live";
            currentTimeframe = tf;
            const tableBody =
                document.querySelector("[data-funding-table-body]") ||
                document.getElementById("funding-table-body");
            if (tableBody) {
                tableBody.innerHTML = "";
            }

            loadFunding(tf);
        });
    });

    // D. Search filter
    const searchInput = document.querySelector("[data-funding-search]");
    if (searchInput) {
        searchInput.addEventListener("input", (e) => {
            searchTerm = (e.target.value || "").toLowerCase().trim();
            recomputeAndRender();
        });
    }

    // E. Connect Wallet modal
    injectWalletModal();
    injectWalletMenu();
    const connectBtn = document.getElementById("connect-wallet-btn");
    if (!connectBtn) {
        console.warn("connect-wallet-btn not found yet, will rely on delegation.");
    }
    // Delegated click handler
    document.addEventListener("click", async (e) => {
        const btnEl = e.target.closest("#connect-wallet-btn");
        if (!btnEl) return;
        e.preventDefault();
        await openAccountMenuOrConnectModal();
    });
    window.__tfhWallet = walletState;
    attachWalletListenersOnce();
    attachWalletMenuListenersOnce();
    __tfhInitDone = true;
}

// --- 3. Datos y Tablas ---
function initDashboard() {
    // cargar estado almacenado
    try {
        const stored = localStorage.getItem("tfh_wallet");
        if (stored) {
            const parsed = safeJsonParse(stored);
            if (parsed && parsed.address) {
                walletState = { address: parsed.address, provider: parsed.provider || null, chainId: parsed.chainId ?? null };
                window.__tfhWallet = walletState;
                updateConnectButton();
            } else if (parsed === null) {
                localStorage.removeItem("tfh_wallet");
            }
        }
    } catch (e) {
        console.warn("Invalid tfh_wallet, clearing.");
        localStorage.removeItem("tfh_wallet");
    }
    loadFunding("live");
    loadFeesNotice();
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
        el.textContent = `Comisiones usadas → ${parts.join(" · ")} · Net APR a ${cfg.net_apr_horizon_hours}h de mantenimiento`;
    } catch (e) {
        /* sin aviso de comisiones */
    }
}

async function loadFunding(timeframe) {
    const tableBody =
        document.querySelector("[data-funding-table-body]") ||
        document.getElementById("funding-table-body");
    if (!tableBody) return;

    tableBody.innerHTML =
        '<tr><td colspan="10" class="text-center py-10 text-slate-400">Loading...</td></tr>';

    try {
        const params = new URLSearchParams({
            min_spread_apr_percent: "0",
            limit: "100",
        });
        const endpoint = (() => {
            switch (timeframe) {
                case "1h":
                    return "/api/funding/1h";
                case "8h":
                    return "/api/funding/8h";
                case "24h":
                    return "/api/funding/24h";
                case "3d":
                    return "/api/funding/3d";
                case "7d":
                    return "/api/funding/7d";
                case "15d":
                    return "/api/funding/15d";
                case "31d":
                    return "/api/funding/31d";
                case "live":
                default:
                    return "/api/funding/live";
            }
        })();

        const res = await fetch(`${endpoint}?${params.toString()}`);
        // Respuesta obsoleta: el usuario ya cambio de temporalidad
        if (timeframe !== currentTimeframe) return;
        if (!res.ok) {
            console.error("Failed to fetch funding data", res.status);
            tableBody.innerHTML =
                '<tr><td colspan="10" class="text-center py-4 text-red-400">Not implemented</td></tr>';
            allRows = [];
            filteredRows = [];
            return;
        }
        const data = await res.json();
        if (timeframe !== currentTimeframe) return;

        data.sort(
            (a, b) =>
                (Number(b.spread_apr_percent) || 0) -
                (Number(a.spread_apr_percent) || 0),
        );

        allRows = data;
        recomputeAndRender();
    } catch (err) {
        if (timeframe !== currentTimeframe) return;
        console.error("Error loading funding live:", err);
        tableBody.innerHTML =
            '<tr><td colspan="10" class="text-center py-4 text-red-400">Error loading data</td></tr>';
        allRows = [];
        filteredRows = [];
    }
}

function renderFundingRows(data) {
    const tableBody =
        document.querySelector("[data-funding-table-body]") ||
        document.getElementById("funding-table-body");
    if (!tableBody) return;

    // Aviso global si la ventana tiene poca cobertura en todos los pares
    const allPartial =
        currentTimeframe !== "live" &&
        Array.isArray(data) &&
        data.length > 0 &&
        data.every((p) => p.is_partial);
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

    const rows = data.map((pair) => {
        const volL = Number(pair.long_market?.volume_24h);
        const volS = Number(pair.short_market?.volume_24h);
        const volTotal =
            (Number.isFinite(volL) ? volL : 0) +
            (Number.isFinite(volS) ? volS : 0);
        return {
            ...pair,
            __volL: volL,
            __volS: volS,
            __volTotal: volTotal,
        };
    });

    rows.forEach((pair, i) => {
        const long = pair.long_market || {};
        const short = pair.short_market || {};

        const longExchange = (long.exchange || "L").toString();
        const shortExchange = (short.exchange || "S").toString();

        const longOiVal = computeOiUsd(long);
        const shortOiVal = computeOiUsd(short);
        const longOi = longOiVal != null ? fmtNum(longOiVal) : "-";
        const shortOi = shortOiVal != null ? fmtNum(shortOiVal) : "-";
        const totalOiVal = (longOiVal || 0) + (shortOiVal || 0);
        const totalOi =
            longOiVal == null && shortOiVal == null ? "-" : fmtNum(totalOiVal);

        const priceSpreadPctVal = computePriceSpreadPct(pair);
        const priceSpreadPctText =
            priceSpreadPctVal == null
                ? "-"
                : `${priceSpreadPctVal.toFixed(2)}%`;

        const volLDisplay = Number.isFinite(pair.__volL) ? pair.__volL : null;
        const volSDisplay = Number.isFinite(pair.__volS) ? pair.__volS : null;
        const volLText =
            volLDisplay != null && volLDisplay > 0 ? fmtNum(volLDisplay) : "-";
        const volSText =
            volSDisplay != null && volSDisplay > 0 ? fmtNum(volSDisplay) : "-";
        const volTotal = (volLDisplay ?? 0) + (volSDisplay ?? 0);
        const totalVolText =
            volLDisplay == null && volSDisplay == null ? "-" : fmtNum(volTotal);

        const symbolKey = (
            pair.canonical_symbol ||
            pair.pair_id ||
            ""
        ).toString().toUpperCase();
        const logoUrl = symbolKey ? logoMap[symbolKey] : undefined;
        const tokenVisual = logoUrl
            ? `<img src="${logoUrl}" alt="${pair.canonical_symbol || ""}" class="w-[18px] h-[18px] rounded-full flex-none" style="margin-right:4px;" onerror="this.style.display='none';" />`
            : `<div class="w-[22px] h-[22px] rounded-full bg-slate-700 flex items-center justify-center text-[10px] flex-none">${(pair.canonical_symbol || "?")[0]}</div>`;

        const tr = document.createElement("tr");
        tr.className =
            "hover:bg-white/5 transition-colors group border-b border-white/5";
        const netApr = Number(pair.net_apr_percent);
        const netAprClass = Number.isFinite(netApr) && netApr > 0 ? "text-green-400" : "text-red-400";
        const breakevenText = fmtHours(pair.breakeven_hours);
        const breakevenTitle =
            pair.breakeven_hours_maker_entry != null
                ? `Entrando con órdenes límite (maker): ${fmtHours(pair.breakeven_hours_maker_entry)}`
                : "";

        // Columna "Data": en Live, próximo funding; en ventanas, cobertura real de snapshots
        let dataCell = "";
        if (pair.coverage_ratio != null) {
            const cov = Math.round(Number(pair.coverage_ratio) * 100);
            const covClass = pair.is_partial ? "text-amber-300" : "text-slate-400";
            dataCell = `<span class="${covClass}" title="${pair.window_count ?? 0} snapshots de ${Math.round(pair.expected_count ?? 0)} esperados">${cov}% · ${fmtNum(pair.window_count ?? 0)} pts</span>`;
        } else {
            const nft = long.next_funding_time || short.next_funding_time;
            const mins = nft ? Math.max(0, Math.round((new Date(nft) - Date.now()) / 60000)) : null;
            dataCell = mins == null ? "live" : `<span title="Próximo pago de funding">live · ${mins}m</span>`;
        }

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
            <td class="px-4 py-3 text-slate-300 font-mono" title="${breakevenTitle}">${breakevenText}</td>
            <td class="px-4 py-3 text-xs whitespace-nowrap">
                <div class="text-green-400/80">L: ${longExchange} <span class="text-slate-500">${fmtPercent(long.apr_percent)}</span></div>
                <div class="text-red-400/80">S: ${shortExchange} <span class="text-slate-500">${fmtPercent(short.apr_percent)}</span></div>
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
    const qs = encodeURIComponent(uniq.join(","));
    try {
        const res = await fetch(`/api/token/logos?symbols=${qs}`);
        if (!res.ok) return {};
        return await res.json();
    } catch (e) {
        console.error("Error fetching token logos", e);
        return {};
    }
}

async function loadLogosForRows(rows) {
    const slice = rows.slice(0, 100);
    const symbols = slice
        .map((r) =>
            (r.canonical_symbol || r.pair_id || "").toString().toUpperCase(),
        )
        .filter(Boolean);
    // avoid fetching if already cached
    const missing = symbols.filter((s) => !logoMap[s]);
    if (!missing.length) return;
    const map = await fetchTokenLogos(missing);
    const hasNew = Object.keys(map || {}).length > 0;
    logoMap = { ...logoMap, ...map };
    if (hasNew) {
        renderFundingRows(filteredRows);
    }
}

// --- 4. Helpers de filtrado ---
function parseFiltersFromInputs() {
    const inputs = document.querySelectorAll("[data-filter-input]");
    const next = { ...filtersState };
    inputs.forEach((inp) => {
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
    // El backend ya calcula open_interest_usd; ambos exchanges reportan OI en monedas.
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
    const mid = (a + b) / 2;
    if (mid <= 0) return null;
    return ((b - a) / mid) * 100;
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
        const spreadVal = Number(pair.spread_apr_percent);
        if (!matchesBounds(spreadVal, filters.aprMin, filters.aprMax)) return false;

        const netApr = Number(pair.net_apr_percent);
        if (!matchesBounds(netApr, filters.netAprMin, null)) return false;

        if (filters.breakevenMax != null) {
            const breakeven = Number(pair.breakeven_hours);
            if (!Number.isFinite(breakeven) || breakeven > filters.breakevenMax) return false;
        }

        const priceSpreadVal = computePriceSpreadPct(pair);
        if (!matchesBounds(priceSpreadVal, filters.priceMin, filters.priceMax))
            return false;

        const oiTotal = computeTotalOi(pair);
        if (!matchesBounds(oiTotal, filters.oiMin, filters.oiMax)) return false;

        const volTotal = computeTotalVol(pair);
        if (!matchesBounds(volTotal, filters.volMin, filters.volMax)) return false;

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
    const base = filterRows(allRows, filtersState);
    const searched = applySearch(base, searchTerm);
    filteredRows = searched;
    renderFundingRows(filteredRows);
    loadLogosForRows(filteredRows);
}

function applyFilters() {
    filtersState = parseFiltersFromInputs();
    recomputeAndRender();
}

function resetFilters() {
    filtersState = {
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
    const inputs = document.querySelectorAll("[data-filter-input]");
    inputs.forEach((inp) => {
        inp.value = "";
    });
    recomputeAndRender();
}

// --- 5. Wallet modal / connect logic ---
function injectWalletModal() {
    if (document.getElementById("wallet-modal-backdrop")) return;
    const style = document.createElement("style");
    style.textContent = `
    .wallet-backdrop {
        position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: none; align-items: center; justify-content: center; z-index: 9999;
    }
    .wallet-modal {
        background: #111227; color: #fff; border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; width: 320px; padding: 20px; box-shadow: 0 10px 40px rgba(0,0,0,0.35);
    }
    .wallet-modal h3 { margin: 0 0 12px 0; font-size: 18px; }
    .wallet-btn {
        width: 100%; padding: 10px 14px; margin-bottom: 10px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.1); background: #1c1f3a; color: #fff; cursor: pointer; font-weight: 600;
        display: flex; align-items: center; justify-content: space-between;
    }
    .wallet-btn:hover { background: #24284c; }
    .wallet-btn.disabled { opacity: 0.5; cursor: not-allowed; }
    .wallet-secondary { background: transparent; border: 1px solid rgba(255,255,255,0.15); }
    .wallet-close {
        position: absolute; top: 12px; right: 12px; cursor: pointer; color: #8a8fad; font-size: 16px;
    }
    .wallet-note { font-size: 12px; color: #9ba3c1; margin-top: 8px; }
    `;
    document.head.appendChild(style);

    const backdrop = document.createElement("div");
    backdrop.id = "wallet-modal-backdrop";
    backdrop.className = "wallet-backdrop";
    backdrop.innerHTML = `
        <div class="wallet-modal" role="dialog">
            <div class="wallet-close" id="wallet-modal-close">✕</div>
            <h3>Connect Wallet</h3>
            <button id="wallet-metamask" class="wallet-btn">MetaMask <span class="label"></span></button>
            <button id="wallet-rabby" class="wallet-btn">Rabby <span class="label"></span></button>
            <button id="wallet-cancel" class="wallet-btn wallet-secondary">Cancel</button>
            <div id="wallet-msg" class="wallet-note"></div>
        </div>
    `;
    document.body.appendChild(backdrop);

    backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) closeWalletModal();
    });
    const closeBtn = document.getElementById("wallet-modal-close");
    if (closeBtn) closeBtn.addEventListener("click", closeWalletModal);
    document.getElementById("wallet-cancel")?.addEventListener("click", closeWalletModal);
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeWalletModal();
    });

    const mmBtn = document.getElementById("wallet-metamask");
    const rbBtn = document.getElementById("wallet-rabby");
    if (mmBtn) mmBtn.addEventListener("click", () => connectWallet("metamask"));
    if (rbBtn) rbBtn.addEventListener("click", () => connectWallet("rabby"));
}

function openWalletModal() {
    const backdrop = document.getElementById("wallet-modal-backdrop");
    if (!backdrop) return;
    const msg = document.getElementById("wallet-msg");
    const mmBtn = document.getElementById("wallet-metamask");
    const rbBtn = document.getElementById("wallet-rabby");
    const hasEth = typeof window !== "undefined" && window.ethereum;
    const hasMM = hasEth && window.ethereum.isMetaMask;
    const hasRabby = hasEth && window.ethereum.isRabby;

    const setBtn = (btn, available, label) => {
        if (!btn) return;
        btn.classList.toggle("disabled", !available);
        btn.disabled = !available;
        const span = btn.querySelector(".label");
        if (span) span.textContent = available ? "" : "Not installed";
        btn.setAttribute("title", label);
    };
    setBtn(mmBtn, !!hasMM, "MetaMask");
    setBtn(rbBtn, !!hasRabby, "Rabby");

    if (!hasEth) {
        if (msg) msg.textContent = "No wallet detected. Install MetaMask or Rabby.";
    } else {
        if (msg) msg.textContent = "";
    }

    backdrop.style.display = "flex";
}

function closeWalletModal() {
    const backdrop = document.getElementById("wallet-modal-backdrop");
    if (backdrop) backdrop.style.display = "none";
}

function shortAddress(addr) {
    if (!addr || addr.length < 10) return addr || "";
    return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function updateConnectButton() {
    const btn = document.getElementById("connect-wallet-btn");
    if (!btn) return;
    if (walletState.address) {
        btn.textContent = shortAddress(walletState.address);
    } else {
        btn.textContent = "Connect Wallet";
    }
}

async function connectWallet(providerName) {
    const eth = typeof window !== "undefined" ? window.ethereum : null;
    if (!eth) {
        closeWalletModal();
        return;
    }
    if (providerName === "metamask" && !eth.isMetaMask) {
        closeWalletModal();
        return;
    }
    if (providerName === "rabby" && !eth.isRabby) {
        closeWalletModal();
        return;
    }
    try {
        const accounts = await eth.request({ method: "eth_requestAccounts" });
        const addr = accounts && accounts[0];
        const chainHex = await eth.request({ method: "eth_chainId" });
        const chainId = parseInt(chainHex, 16);
        if (addr) {
            walletState = { address: addr, provider: providerName, chainId };
            window.__tfhWallet = walletState;
            updateConnectButton();
            // persist wallet
            try {
                localStorage.setItem("tfh_wallet", JSON.stringify({ address: addr, provider: providerName, chainId }));
            } catch (e) { /* ignore */ }
            await siweLogin(addr, chainId);
        }
    } catch (err) {
        // ignore user rejection (4001)
        console.error("wallet connect error", err);
    } finally {
        closeWalletModal();
        closeWalletMenu();
    }
}

function attachWalletListenersOnce() {
    const eth = typeof window !== "undefined" ? window.ethereum : null;
    if (!eth || walletListenersAttached) return;
    eth.on("accountsChanged", (accounts) => {
        const addr = accounts && accounts[0];
        if (!addr) {
            walletState = { address: null, provider: null };
        } else {
            walletState.address = addr;
        }
        window.__tfhWallet = walletState;
        try {
            if (walletState.address) {
                localStorage.setItem("tfh_wallet", JSON.stringify({ address: walletState.address, provider: walletState.provider || null, chainId: null }));
            } else {
                localStorage.removeItem("tfh_wallet");
                localStorage.removeItem("tfh_jwt");
            }
        } catch (e) { /* ignore */ }
        updateConnectButton();
    });
    eth.on("chainChanged", () => {
        window.location.reload();
    });
    walletListenersAttached = true;
}

function isConnected() {
    return !!(walletState && walletState.address);
}

function getJwtToken() {
    try {
        return localStorage.getItem("tfh_jwt");
    } catch (e) {
        return null;
    }
}

function injectWalletMenu() {
    if (document.getElementById("wallet-menu")) return;
    const style = document.createElement("style");
    style.textContent = `
    .wallet-menu {
        position: absolute;
        min-width: 200px;
        background: #111227;
        color: #e5e7f3;
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 10px;
        padding: 6px 0;
        box-shadow: 0 10px 30px rgba(0,0,0,0.35);
        z-index: 9999;
        display: none;
    }
    .wallet-menu .item {
        padding: 10px 14px;
        cursor: pointer;
        font-size: 14px;
    }
    .wallet-menu .item:hover {
        background: rgba(255,255,255,0.05);
    }
    .wallet-menu .divider {
        height: 1px;
        background: rgba(255,255,255,0.08);
        margin: 4px 0;
    }
    .wallet-menu .danger { color: #f87171; }
    `;
    document.head.appendChild(style);

    const menu = document.createElement("div");
    menu.id = "wallet-menu";
    menu.className = "wallet-menu";
    menu.innerHTML = `
        <div id="wallet-menu-profile" class="item">My profile</div>
        <div class="divider"></div>
        <div id="wallet-menu-disconnect" class="item danger">Disconnect</div>
    `;
    document.body.appendChild(menu);

    document.getElementById("wallet-menu-profile")?.addEventListener("click", () => {
        window.location.href = TFH_PROFILE_URL;
        closeWalletMenu();
    });
    document.getElementById("wallet-menu-disconnect")?.addEventListener("click", () => {
        disconnectWallet();
    });
}

function openWalletMenu() {
    const menu = document.getElementById("wallet-menu");
    const btn = document.getElementById("connect-wallet-btn");
    if (!menu || !btn) return;
    const rect = btn.getBoundingClientRect();
    menu.style.display = "block";
    menu.style.top = `${rect.bottom + window.scrollY + 8}px`;
    menu.style.left = `${rect.left + window.scrollX}px`;
}

function closeWalletMenu() {
    const menu = document.getElementById("wallet-menu");
    if (menu) menu.style.display = "none";
}

function disconnectWallet() {
    walletState = { address: null, provider: null };
    window.__tfhWallet = walletState;
    try {
        localStorage.removeItem("tfh_jwt");
        localStorage.removeItem("tfh_wallet");
    } catch (e) {
        // ignore
    }
    updateConnectButton();
    closeWalletMenu();
}

function attachWalletMenuListenersOnce() {
    if (walletMenuListenersAttached) return;
    document.addEventListener("click", (e) => {
        const menu = document.getElementById("wallet-menu");
        const btn = document.getElementById("connect-wallet-btn");
        if (!menu || !btn) return;
        if (menu.style.display !== "block") return;
        if (!menu.contains(e.target) && !btn.contains(e.target)) {
            closeWalletMenu();
        }
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeWalletMenu();
    });
    walletMenuListenersAttached = true;
}

async function openAccountMenuOrConnectModal() {
    const hasToken = getJwtToken() != null;
    if (isConnected()) {
        if (!hasToken) {
            const eth = typeof window !== "undefined" ? window.ethereum : null;
            if (!eth) {
                openWalletModal();
                return;
            }
            try {
                const chainHex = await eth.request({ method: "eth_chainId" });
                const chainId = parseInt(chainHex, 16);
                await siweLogin(walletState.address, chainId);
            } catch (err) {
                console.error("Unable to re-login SIWE", err);
                openWalletModal();
                return;
            }
        }
        openWalletMenu();
    } else {
        openWalletModal();
    }
}
async function siweLogin(address, chainId) {
    const eth = typeof window !== "undefined" ? window.ethereum : null;
    const msgBox = document.getElementById("wallet-msg");
    if (!eth) {
        if (msgBox) msgBox.textContent = "No wallet detected.";
        return;
    }
    try {
        const nonceResp = await fetch(`${API_BASE}/auth/nonce`);
        if (!nonceResp.ok) throw new Error("Failed to get nonce");
        const nonceData = await nonceResp.json();
        const nonce = nonceData.nonce;
        // Use hostname (no port) to satisfy gateway allowlist (ALLOWED_SIWE_DOMAINS defaults to "localhost")
        const domain = window.location.hostname;
        const uri = window.location.origin;
        const issuedAt = new Date().toISOString();
        const statement = "Sign in to TheFundingHouse.";
        const message = `${domain} wants you to sign in with your Ethereum account:\n${address}\n\n${statement}\n\nURI: ${uri}\nVersion: 1\nChain ID: ${chainId}\nNonce: ${nonce}\nIssued At: ${issuedAt}`;
        const signature = await eth.request({
            method: "personal_sign",
            params: [message, address],
        });
        const verifyResp = await fetch(`${API_BASE}/auth/verify`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message, signature }),
        });
        if (!verifyResp.ok) {
            if (msgBox) msgBox.textContent = "Login failed. Please try again.";
            return;
        }
        const verifyData = await verifyResp.json();
        const token = verifyData.access_token;
        if (token) {
            localStorage.setItem("tfh_jwt", token);
            localStorage.setItem("tfh_wallet", JSON.stringify({ address, chainId, provider: walletState.provider || null }));
        }
        if (msgBox) msgBox.textContent = "";
    } catch (err) {
        if (err && err.code === 4001) {
            // user rejected signature
            if (msgBox) msgBox.textContent = "Login canceled.";
            return;
        }
        console.error("SIWE login error", err);
        if (msgBox) msgBox.textContent = "Login failed. Please try again.";
    }
}
