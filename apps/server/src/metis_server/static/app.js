// Metis dashboard — single-file vanilla SPA.
//
// Layout: a top-bar with two toggles (Cost ⇄ Activity, time window) drives
// a render() that fetches the relevant /analytics/* endpoints and binds them
// into the Cost or Activity view. No build step, no framework.
//
// Chart.js is loaded via CDN in index.html; we reuse Chart instances across
// renders to avoid leaking canvas state.

const BASELINE_MODEL = "anthropic:claude-sonnet-4-6";

// ----- State ---------------------------------------------------------------

const state = {
  view: "cost", // "cost" | "activity"
  windowKey: "7d", // "today" | "7d" | "30d" | "all"
};

const charts = {}; // canvasId -> Chart instance

// ----- Time-window resolution (local TZ → UTC) ----------------------------
// Per analytics-api.md §3.1: the API speaks UTC; the SPA computes UTC bounds
// from the user's local timezone before calling. "today" means local midnight
// to now; "all" omits both bounds.

function resolveWindow(key) {
  const now = new Date();
  if (key === "all") return { from: null, to: null, label: "all time" };
  const to = now;
  let from;
  if (key === "today") {
    from = new Date(now);
    from.setHours(0, 0, 0, 0);
  } else if (key === "7d") {
    from = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
  } else if (key === "30d") {
    from = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
  }
  return { from, to, label: labelForWindow(key) };
}

function labelForWindow(key) {
  return {
    today: "today",
    "7d": "last 7 days",
    "30d": "last 30 days",
    all: "all time",
  }[key];
}

// ----- API fetch helpers --------------------------------------------------

async function fetchAnalytics(path, params = {}) {
  const url = new URL(`/analytics/${path}`, location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined) url.searchParams.set(k, v);
  }
  const r = await fetch(url);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body?.error?.message || `HTTP ${r.status}`);
  }
  return r.json();
}

function withWindowParams(win) {
  const out = {};
  if (win.from) out.from = win.from.toISOString();
  if (win.to) out.to = win.to.toISOString();
  return out;
}

// ----- Formatters --------------------------------------------------------

const usd = (n) =>
  n == null
    ? "—"
    : `$${Number(n).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;

const pct = (frac) =>
  frac == null || isNaN(frac)
    ? "—"
    : `${(frac * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;

const shortModel = (m) => (m || "?").replace(/^anthropic:/, "").replace(/^openai:/, "");

const localTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
};

// ----- Chart helpers ------------------------------------------------------

const COLORS = [
  "#7aa2ff", "#4ade80", "#fbbf24", "#f87171", "#a78bfa",
  "#34d399", "#fb923c", "#60a5fa", "#f472b6", "#c084fc",
];
const color = (i) => COLORS[i % COLORS.length];

function destroyChart(id) {
  if (charts[id]) {
    charts[id].destroy();
    delete charts[id];
  }
}

function makeChart(id, config) {
  destroyChart(id);
  const ctx = document.getElementById(id).getContext("2d");
  charts[id] = new Chart(ctx, config);
}

const baseChartOpts = (extra = {}) => ({
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { labels: { color: "#8c92a3", font: { size: 11 } } },
    tooltip: { backgroundColor: "#181b22", borderColor: "#2a2f3a", borderWidth: 1 },
  },
  scales: {
    x: { ticks: { color: "#8c92a3" }, grid: { color: "rgba(255,255,255,0.04)" } },
    y: { ticks: { color: "#8c92a3" }, grid: { color: "rgba(255,255,255,0.04)" } },
  },
  ...extra,
});

// ----- Cost view ----------------------------------------------------------

async function renderCostView(win) {
  const params = withWindowParams(win);
  // Fire all queries in parallel — they're independent.
  const [totals, byDay, byModel, cache, savings] = await Promise.all([
    fetchAnalytics("cost", { ...params, group_by: "none" }),
    fetchAnalytics("cost", { ...params, group_by: "day" }),
    fetchAnalytics("cost", { ...params, group_by: "model" }),
    fetchAnalytics("cache_effectiveness", params),
    fetchAnalytics("savings", { ...params, baseline: BASELINE_MODEL }),
  ]);
  document.getElementById("pricing-version").textContent =
    totals.current_pricing_version || "—";

  // Hero — total spend.
  document.getElementById("total-spend").textContent = usd(totals.data.cost_usd);
  document.getElementById("call-count").textContent = totals.data.call_count.toLocaleString();

  // Hero — savings.
  renderSavings(savings.data);

  // Spend over time.
  renderTimeSeries(byDay.data);

  // Cost by model.
  renderCostByModel(byModel.data);

  // Cache effectiveness.
  renderCacheChart(cache.data);
}

function renderSavings(d) {
  const baseline = d.baseline_model;
  document.getElementById("baseline-label").textContent = shortModel(baseline);
  const subEl = document.getElementById("savings-sub");
  const amtEl = document.getElementById("savings-amount");
  const warnEl = document.getElementById("savings-warn");
  amtEl.classList.remove("positive", "negative");
  warnEl.classList.add("hidden");

  if (d.baseline_repriced_usd === 0) {
    amtEl.textContent = "—";
    subEl.textContent = "no LLM calls in this window";
    return;
  }

  const savingsUsd = d.savings_usd;
  const savingsPct = d.savings_pct;
  // Per spec §4.7: negative savings are valid — "you spent N% MORE than baseline".
  if (savingsUsd >= 0) {
    amtEl.classList.add("positive");
    amtEl.textContent = `${usd(savingsUsd)} saved`;
    subEl.textContent = `${pct(savingsPct)} less than ${shortModel(baseline)} would have cost`;
  } else {
    amtEl.classList.add("negative");
    amtEl.textContent = `${usd(-savingsUsd)} over baseline`;
    subEl.textContent = `${pct(-savingsPct)} more than ${shortModel(baseline)} would have cost`;
  }

  if (d.rows_missing_from_price_table > 0) {
    warnEl.classList.remove("hidden");
    warnEl.textContent =
      `Warning: ${d.rows_missing_from_price_table} call(s) used models not in the ` +
      `current price table; savings is partial.`;
  }
}

function renderTimeSeries(rows) {
  const emptyEl = document.getElementById("chart-time-empty");
  if (!rows || rows.length === 0) {
    emptyEl.classList.remove("hidden");
    destroyChart("chart-time");
    return;
  }
  emptyEl.classList.add("hidden");
  makeChart("chart-time", {
    type: "line",
    data: {
      labels: rows.map((r) => r.bucket),
      datasets: [
        {
          label: "USD",
          data: rows.map((r) => r.cost_usd),
          borderColor: color(0),
          backgroundColor: "rgba(122,162,255,0.15)",
          tension: 0.25,
          fill: true,
          pointRadius: 3,
        },
      ],
    },
    options: baseChartOpts({
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => `${usd(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: "#8c92a3" }, grid: { color: "rgba(255,255,255,0.04)" } },
        y: {
          ticks: { color: "#8c92a3", callback: (v) => usd(v) },
          grid: { color: "rgba(255,255,255,0.04)" },
        },
      },
    }),
  });
}

function renderCostByModel(rows) {
  const emptyEl = document.getElementById("chart-model-empty");
  if (!rows || rows.length === 0) {
    emptyEl.classList.remove("hidden");
    destroyChart("chart-model");
    return;
  }
  emptyEl.classList.add("hidden");
  makeChart("chart-model", {
    type: "bar",
    data: {
      labels: rows.map((r) => shortModel(r.model)),
      datasets: [
        {
          label: "USD",
          data: rows.map((r) => r.cost_usd),
          backgroundColor: rows.map((_, i) => color(i)),
        },
      ],
    },
    options: baseChartOpts({
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#8c92a3", callback: (v) => usd(v) },
             grid: { color: "rgba(255,255,255,0.04)" } },
        y: { ticks: { color: "#8c92a3" }, grid: { display: false } },
      },
    }),
  });
}

function renderCacheChart(rows) {
  const hintEl = document.getElementById("cache-hint");
  if (!rows || rows.length === 0) {
    destroyChart("chart-cache");
    hintEl.textContent = "No LLM calls in this window.";
    return;
  }
  // Per spec §4.2: hit_rate excludes nothing — denominator includes cache
  // writes. We render hit / write / uncached as a stacked horizontal bar.
  const labels = rows.map((r) => shortModel(r.model));
  const hitData = rows.map((r) => r.cached_input_tokens);
  const writeData = rows.map((r) => r.cache_creation_tokens);
  const coldData = rows.map((r) => r.uncached_input_tokens);
  makeChart("chart-cache", {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "cache hit", data: hitData, backgroundColor: COLORS[1] },
        { label: "cache write", data: writeData, backgroundColor: COLORS[2] },
        { label: "uncached", data: coldData, backgroundColor: "#3a4055" },
      ],
    },
    options: baseChartOpts({
      indexAxis: "y",
      plugins: {
        legend: { labels: { color: "#8c92a3", font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: (ctx) =>
              `${ctx.dataset.label}: ${Number(ctx.parsed.x).toLocaleString()} tok`,
          },
        },
      },
      scales: {
        x: { stacked: true, ticks: { color: "#8c92a3" },
             grid: { color: "rgba(255,255,255,0.04)" } },
        y: { stacked: true, ticks: { color: "#8c92a3" }, grid: { display: false } },
      },
    }),
  });
  // Hit rate hint line. All hit rates likely 0 today (no adapter writes cache_control).
  const totalHit = hitData.reduce((a, b) => a + b, 0);
  const totalAll = totalHit + writeData.reduce((a, b) => a + b, 0) +
                   coldData.reduce((a, b) => a + b, 0);
  const overall = totalAll > 0 ? totalHit / totalAll : null;
  hintEl.textContent =
    overall === null
      ? "No input tokens recorded."
      : `Overall hit rate: ${pct(overall)} of input tokens served from cache.`;
}

// ----- Activity view -----------------------------------------------------

async function renderActivityView(win) {
  const params = withWindowParams(win);
  const [routing, reliability, sessions] = await Promise.all([
    fetchAnalytics("routing", params),
    fetchAnalytics("reliability", params),
    fetchAnalytics("sessions", { order: "recency", limit: 25 }),
  ]);
  document.getElementById("pricing-version").textContent =
    routing.current_pricing_version || "—";
  renderRoutingChart(routing.data);
  renderReliability(reliability.data);
  renderSessions(sessions.data);
}

function renderRoutingChart(d) {
  // Filter out zero-count policies for the chart itself; keep the totals line
  // honest with the all-seven-slots data from the API.
  const active = d.wins_by_policy.filter((p) => p.count > 0);
  const sub = document.getElementById("routing-sub");
  const totalWins = d.wins_by_policy.reduce((a, p) => a + p.count, 0);
  const parts = [`${totalWins} routed turns`];
  if (d.hard_failures > 0) parts.push(`${d.hard_failures} hard failure(s)`);
  if (d.rejections.length > 0) {
    parts.push(
      `${d.rejections.length} rejection reason(s): ` +
        d.rejections
          .slice(0, 3)
          .map((r) => `${r.policy}/${r.validation_failure} ×${r.count}`)
          .join(", "),
    );
  }
  sub.textContent = parts.join(" · ");

  if (active.length === 0) {
    destroyChart("chart-routing");
    return;
  }
  makeChart("chart-routing", {
    type: "doughnut",
    data: {
      labels: active.map((p) => p.policy),
      datasets: [
        {
          data: active.map((p) => p.count),
          backgroundColor: active.map((_, i) => color(i)),
          borderColor: "#181b22",
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "right",
          labels: { color: "#8c92a3", font: { size: 11 } },
        },
      },
    },
  });
}

function renderReliability(d) {
  const body = document.getElementById("reliability-body");
  const empty = document.getElementById("reliability-empty");
  body.innerHTML = "";
  const hasErrors = d.errors_by_class && d.errors_by_class.length > 0;
  const hasLatency = d.latency_ms_by_model && d.latency_ms_by_model.length > 0;
  if (!hasErrors && !hasLatency) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  if (hasLatency) {
    const head = document.createElement("div");
    head.className = "rel-row";
    head.innerHTML =
      `<span class="lbl">Model</span>` +
      `<span class="v">p50 / p95 ms</span>` +
      `<span class="v">n</span>`;
    body.appendChild(head);
    for (const row of d.latency_ms_by_model) {
      const el = document.createElement("div");
      el.className = "rel-row";
      el.innerHTML =
        `<span>${shortModel(row.model)}</span>` +
        `<span class="v">${row.p50 ?? "—"} / ${row.p95 ?? "—"}</span>` +
        `<span class="v">${row.sample_size}</span>`;
      body.appendChild(el);
    }
  }
  if (hasErrors) {
    const head = document.createElement("div");
    head.className = "rel-row";
    head.style.marginTop = "10px";
    head.innerHTML =
      `<span class="lbl">Failures</span>` +
      `<span class="v">model</span>` +
      `<span class="v">count</span>`;
    body.appendChild(head);
    for (const row of d.errors_by_class) {
      const el = document.createElement("div");
      el.className = "rel-row";
      el.innerHTML =
        `<span>${row.error_class}</span>` +
        `<span class="v">${shortModel(row.model)}</span>` +
        `<span class="v">${row.count}</span>`;
      body.appendChild(el);
    }
  }
}

function renderSessions(rows) {
  const root = document.getElementById("sessions-table");
  const empty = document.getElementById("sessions-empty");
  root.innerHTML = "";
  if (!rows || rows.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const head = document.createElement("div");
  head.className = "row head";
  head.innerHTML =
    `<span>Session</span><span>Workspace</span><span>Model</span>` +
    `<span class="cost">Cost</span><span class="cost">Turns</span><span>Last activity</span>`;
  root.appendChild(head);

  for (const s of rows) {
    const el = document.createElement("div");
    el.className = "row";
    el.innerHTML =
      `<span class="id">${s.id}</span>` +
      `<span class="path" title="${s.workspace_path}">${s.workspace_path}</span>` +
      `<span>${shortModel(s.active_model)}</span>` +
      `<span class="cost">${usd(s.cost_usd)}</span>` +
      `<span class="cost">${s.turn_count}</span>` +
      `<span>${localTime(s.updated_at)}</span>`;
    root.appendChild(el);
  }
}

// ----- Top-level render ---------------------------------------------------

async function render() {
  const win = resolveWindow(state.windowKey);
  document.getElementById("window-label").textContent = win.label;
  document.getElementById("view-cost").classList.toggle("hidden", state.view !== "cost");
  document
    .getElementById("view-activity")
    .classList.toggle("hidden", state.view !== "activity");

  const root = document.getElementById("view-root");
  root.setAttribute("aria-busy", "true");
  try {
    if (state.view === "cost") await renderCostView(win);
    else await renderActivityView(win);
    document.getElementById("last-refresh").textContent =
      `refreshed ${new Date().toLocaleTimeString()}`;
  } catch (exc) {
    console.error(exc);
    document.getElementById("last-refresh").textContent = `error: ${exc.message}`;
  } finally {
    root.setAttribute("aria-busy", "false");
  }
}

// ----- Wiring ------------------------------------------------------------

function wireToggles() {
  for (const btn of document.querySelectorAll("#audience button")) {
    btn.addEventListener("click", () => {
      state.view = btn.dataset.view;
      for (const b of document.querySelectorAll("#audience button"))
        b.classList.toggle("on", b === btn);
      render();
    });
  }
  for (const btn of document.querySelectorAll("#window button")) {
    btn.addEventListener("click", () => {
      state.windowKey = btn.dataset.window;
      for (const b of document.querySelectorAll("#window button"))
        b.classList.toggle("on", b === btn);
      render();
    });
  }
}

wireToggles();
render();
