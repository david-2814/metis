// Metis dashboard — single-file vanilla SPA.
//
// Layout: a top-bar with two toggles (Cost ⇄ Activity ⇄ Spend-by-identity,
// time window) drives a render() that fetches the relevant /analytics/*
// endpoints and binds them into the active view. No build step, no framework.
//
// Chart.js is loaded via CDN in index.html; we reuse Chart instances across
// renders to avoid leaking canvas state.

const BASELINE_MODEL = "anthropic:claude-sonnet-4-6";

// ----- State ---------------------------------------------------------------

const state = {
  view: "cost", // "cost" | "activity" | "identity"
  windowKey: "7d", // "today" | "7d" | "30d" | "all"
  // Drill-down: when a row in the identity view is clicked we filter the
  // Cost / Activity endpoint calls. `null` means "all traffic". The
  // agent-loop bucket (gateway_key_id / user_id / team_id IS NULL on the
  // server) is not drill-downable in v1 — the cost endpoint's filter is an
  // exact-match `= ?` and can't express IS NULL through the same parameter
  // shape.
  identityFilter: null, // { kind: "key" | "user" | "team", value: string }
  // Identity view: which rollup tiles are visible.
  identityScope: "all", // "all" | "team" | "user" | "key"
  // Pills inside the per-key tile. `keysFilter` only filters which rows the
  // table displays; it doesn't talk to the server. `identitySort` is shared
  // across all three identity tables and is similarly client-side.
  keysFilter: "all", // "all" | "agent" | "gateway"
  identitySort: "cost", // "cost" | "call_count"
  // Per-team tile: which team rows are expanded to show their by_user
  // sub-array. Keyed by `team_id`; the null bucket uses the sentinel below.
  expandedTeams: new Set(),
};
const NULL_BUCKET = "__null__";

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

// Translate `state.identityFilter` into the param name the analytics
// endpoints accept (gateway_key / user / team). Cost endpoints support all
// three independently (multi-user.md §5.3 / analytics-api.md §4.1).
function identityFilterParam() {
  if (state.identityFilter === null) return {};
  const { kind, value } = state.identityFilter;
  if (kind === "key") return { gateway_key: value };
  if (kind === "user") return { user: value };
  if (kind === "team") return { team: value };
  return {};
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
  // Only the cost endpoint accepts gateway_key / user / team as filters
  // (see analytics-api.md §4.1, multi-user.md §5.3). cache_effectiveness
  // and savings remain global in v1 — the chip stays visible so the user
  // knows the cost charts are filtered while the other panels aren't.
  const costParams = { ...params, ...identityFilterParam() };
  const [totals, byDay, byModel, cache, savings] = await Promise.all([
    fetchAnalytics("cost", { ...costParams, group_by: "none" }),
    fetchAnalytics("cost", { ...costParams, group_by: "day" }),
    fetchAnalytics("cost", { ...costParams, group_by: "model" }),
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

  // Spend over time — pad missing days with zeros so the x-axis spans the
  // full selected window, not just the days that happened to have calls.
  renderTimeSeries(fillDailySeries(byDay.data, win));

  // Cost by model.
  renderCostByModel(byModel.data);

  // Cache effectiveness.
  renderCacheChart(cache.data);
}

// Fill the day-bucket series so the x-axis covers the whole selected window.
// The /cost?group_by=day endpoint only emits buckets that had at least one
// call; rendering raw produces a chart that ends on whatever day data happens
// to exist, which misleads about the selected range.
//
// Bucket keys are UTC dates (the API uses `date(..., 'unixepoch')`); we
// generate the same key shape between win.from and win.to inclusive. The
// "all" window has no win.from — fall back to the earliest bucket in the
// data so we don't fabricate empty months before the user's first call.
function fillDailySeries(rows, win) {
  const dataByBucket = new Map(rows.map((r) => [r.bucket, r]));
  if (!win.from && rows.length === 0) return rows;

  const fromKey = win.from
    ? utcDayKey(win.from)
    : rows[0].bucket;
  const toKey = win.to ? utcDayKey(win.to) : rows[rows.length - 1].bucket;

  const out = [];
  let cursor = new Date(`${fromKey}T00:00:00Z`);
  const end = new Date(`${toKey}T00:00:00Z`);
  while (cursor <= end) {
    const key = utcDayKey(cursor);
    out.push(
      dataByBucket.get(key) || {
        bucket: key,
        cost_usd: 0,
        input_tokens: 0,
        output_tokens: 0,
        cached_input_tokens: 0,
        cache_creation_input_tokens: 0,
        avg_latency_ms: null,
        call_count: 0,
      },
    );
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return out;
}

const utcDayKey = (d) => d.toISOString().slice(0, 10);

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

// ----- Spend-by-identity view --------------------------------------------
// Three rollups share one tab: per-team, per-user, per-key. The scope pills
// switch which tiles are visible; "all" shows all three.

async function renderIdentityView(win) {
  // Show/hide tiles up-front so an in-flight fetch doesn't leave the wrong
  // table visible during the round-trip.
  for (const tile of ["team", "user", "key"]) {
    const visible = state.identityScope === "all" || state.identityScope === tile;
    document.getElementById(`tile-${tile}`).classList.toggle("hidden", !visible);
  }

  const params = withWindowParams(win);
  // Fetch in parallel only what we need for the active scope; the per-key
  // tile fetches via by_key, per-team via by_team, per-user via
  // cost?group_by=user. The pricing-version stamp comes from whichever
  // request lands first.
  const want = state.identityScope;
  const fetches = [];
  if (want === "all" || want === "team")
    fetches.push(["team", fetchAnalytics("by_team", params)]);
  if (want === "all" || want === "user")
    fetches.push(["user", fetchAnalytics("cost", { ...params, group_by: "user" })]);
  if (want === "all" || want === "key")
    fetches.push(["key", fetchAnalytics("by_key", params)]);

  const results = await Promise.all(fetches.map(([_, p]) => p));
  document.getElementById("pricing-version").textContent =
    results[0]?.current_pricing_version || "—";

  for (let i = 0; i < fetches.length; i++) {
    const kind = fetches[i][0];
    const resp = results[i];
    if (kind === "team") renderTeamsTile(resp.data);
    else if (kind === "user") renderUsersTile(resp.data);
    else if (kind === "key") renderKeysTile(resp.data);
  }
}

// ----- Per-team tile ----------------------------------------------------

function renderTeamsTile(rows) {
  renderTopSpenderRow({
    rows,
    valueKey: "cost_usd",
    idKey: "team_id",
    nullLabel: "untagged",
    calloutId: "top-spender-team",
    idEl: "top-spender-team-id",
    shareEl: "top-spender-team-share",
  });

  const sorted = sortIdentityRows(rows, "team_id");
  const root = document.getElementById("teams-table");
  const empty = document.getElementById("teams-empty");
  root.innerHTML = "";
  if (!sorted.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const head = document.createElement("div");
  head.className = "row head";
  head.innerHTML =
    `<span>Team</span>` +
    `<span class="cost">Cost</span>` +
    `<span class="num">Calls</span>` +
    `<span class="num">Users</span>` +
    `<span>Quota</span>` +
    `<span>Filter</span>`;
  root.appendChild(head);

  for (const r of sorted) {
    const teamId = r.team_id;
    const isNull = teamId === null;
    const expandKey = isNull ? NULL_BUCKET : teamId;
    const isExpanded = state.expandedTeams.has(expandKey);

    const row = document.createElement("div");
    row.className = "row identity-row " + (isNull ? "agent-loop" : "expandable");
    row.innerHTML =
      `<span class="id" title="${teamId || "untagged traffic"}">` +
      `${isNull ? "untagged" : teamId}` +
      `<span class="caret">${isExpanded ? "▾" : "▸"}</span></span>` +
      `<span class="cost">${usd(r.cost_usd)}</span>` +
      `<span class="num">${r.call_count.toLocaleString()}</span>` +
      `<span class="num">${r.user_count.toLocaleString()}</span>` +
      // Quota pill placeholder. Agent 9a-2 ships the QuotaStatus surface;
      // until then we render the slot empty so layout doesn't shift when
      // it lands. Wire here: read r.daily_cap_usd / r.monthly_cap_usd
      // (echoed from teams.json per multi-user.md §5.2) and render the
      // pill via quotaPillHtml(r.cost_usd, cap).
      // TODO(agent-9a-2): replace with quotaPillHtml(...) once the
      // by_team response carries cap fields.
      `<span class="quota-slot" data-team-id="${teamId || ""}"></span>` +
      `<span class="filter-action">` +
      (isNull
        ? `<span class="muted">—</span>`
        : `<button class="link-button" data-filter-team="${teamId}">filter</button>`) +
      `</span>`;
    root.appendChild(row);

    // Expanded panel: per-user breakdown from r.by_user.
    if (isExpanded) {
      const panel = document.createElement("div");
      panel.className = "row identity-row expand-panel";
      panel.innerHTML = renderByUserPanel(r.by_user || []);
      root.appendChild(panel);
    }

    // Click anywhere on the row body toggles expand (except on the Filter
    // button, which has its own handler). Null rows aren't expandable —
    // their by_user is at most one null entry.
    if (!isNull) {
      row.addEventListener("click", (evt) => {
        if (evt.target.closest("[data-filter-team]")) return;
        if (state.expandedTeams.has(expandKey)) state.expandedTeams.delete(expandKey);
        else state.expandedTeams.add(expandKey);
        render();
      });
    }
  }

  // Filter-action: navigate to Cost view filtered by team.
  for (const btn of root.querySelectorAll("[data-filter-team]")) {
    btn.addEventListener("click", (evt) => {
      evt.stopPropagation();
      setIdentityFilter("team", btn.dataset.filterTeam);
    });
  }
}

function renderByUserPanel(byUser) {
  if (!byUser.length) {
    return `<div class="sub-empty">No per-user breakdown.</div>`;
  }
  const sorted = [...byUser].sort((a, b) => b.cost_usd - a.cost_usd);
  const rows = sorted
    .map((u) => {
      const isNull = u.user_id === null;
      const idCell = isNull
        ? `<span class="id muted">untagged</span>`
        : `<span class="id"><button class="link-button user-link" ` +
          `data-filter-user="${u.user_id}">${u.user_id}</button></span>`;
      return (
        `<div class="row sub-row">` +
        idCell +
        `<span class="cost">${usd(u.cost_usd)}</span>` +
        `<span class="num">${u.call_count.toLocaleString()}</span>` +
        `</div>`
      );
    })
    .join("");
  return (
    `<div class="sub-table">` +
    `<div class="row sub-row head">` +
    `<span>User</span><span class="cost">Cost</span><span class="num">Calls</span>` +
    `</div>` +
    rows +
    `</div>`
  );
}

// ----- Per-user tile ----------------------------------------------------

function renderUsersTile(rows) {
  renderTopSpenderRow({
    rows,
    valueKey: "cost_usd",
    idKey: "user_id",
    nullLabel: "untagged",
    calloutId: "top-spender-user",
    idEl: "top-spender-user-id",
    shareEl: "top-spender-user-share",
  });

  const sorted = sortIdentityRows(rows, "user_id");
  const root = document.getElementById("users-table");
  const empty = document.getElementById("users-empty");
  root.innerHTML = "";
  if (!sorted.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const head = document.createElement("div");
  head.className = "row head";
  head.innerHTML =
    `<span>User</span>` +
    `<span class="cost">Cost</span>` +
    `<span class="num">Calls</span>` +
    `<span>Quota</span>` +
    `<span>Filter</span>`;
  root.appendChild(head);

  for (const r of sorted) {
    const userId = r.user_id;
    const isNull = userId === null;
    const row = document.createElement("div");
    row.className = "row identity-row " + (isNull ? "agent-loop" : "clickable");
    row.innerHTML =
      `<span class="id" title="${userId || "untagged traffic"}">` +
      `${isNull ? "untagged" : userId}</span>` +
      `<span class="cost">${usd(r.cost_usd)}</span>` +
      `<span class="num">${r.call_count.toLocaleString()}</span>` +
      // TODO(agent-9a-2): replace with quotaPillHtml(...) once
      // /analytics/cost?group_by=user (or a sibling) carries per-user caps.
      `<span class="quota-slot" data-user-id="${userId || ""}"></span>` +
      `<span class="filter-action">` +
      (isNull
        ? `<span class="muted">—</span>`
        : `<button class="link-button" data-filter-user="${userId}">filter</button>`) +
      `</span>`;
    root.appendChild(row);

    if (!isNull) {
      // Skip the row-level click when the user actually clicked the
      // filter button — the document-level [data-filter-user] delegate
      // handles that path. Without the bail-out, both handlers would
      // fire and we'd render twice.
      row.addEventListener("click", (evt) => {
        if (evt.target.closest("[data-filter-user]")) return;
        setIdentityFilter("user", userId);
      });
    }
  }
}

// ----- Per-key tile (Wave-6 view, preserved) ----------------------------

function renderKeysTile(rows) {
  // Top-spender callout uses the unfiltered set so the share denominator
  // matches "all traffic in this window" — flipping the source pill changes
  // *which rows are visible*, not what counts toward "top spender".
  renderTopSpenderRow({
    rows,
    valueKey: "cost_usd",
    idKey: "gateway_key_id",
    nullLabel: "agent-loop",
    calloutId: "top-spender",
    idEl: "top-spender-id",
    shareEl: "top-spender-share",
  });

  let filtered = rows;
  if (state.keysFilter === "gateway")
    filtered = filtered.filter((r) => r.gateway_key_id !== null);
  else if (state.keysFilter === "agent")
    filtered = filtered.filter((r) => r.gateway_key_id === null);

  const sorted = sortIdentityRows(filtered, "gateway_key_id");
  const root = document.getElementById("keys-table");
  const empty = document.getElementById("keys-empty");
  root.innerHTML = "";
  if (!sorted.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const head = document.createElement("div");
  head.className = "row head";
  head.innerHTML =
    `<span>Gateway key</span>` +
    `<span class="cost">Cost</span>` +
    `<span class="num">Calls</span>` +
    `<span>Last call</span>` +
    `<span>Inbound shapes</span>`;
  root.appendChild(head);

  for (const r of sorted) {
    const isAgent = r.gateway_key_id === null;
    const row = document.createElement("div");
    row.className = isAgent ? "row agent-loop" : "row clickable";
    const shapes = (r.by_inbound_shape || [])
      .map(
        (s) =>
          `<span class="shape-tag">${s.inbound_shape || "in-process"}` +
          `<span class="count">${s.call_count}</span></span>`,
      )
      .join("");
    row.innerHTML =
      `<span class="id" title="${r.gateway_key_id || "in-process agent loop"}">` +
      `${isAgent ? "agent-loop" : r.gateway_key_id}</span>` +
      `<span class="cost">${usd(r.cost_usd)}</span>` +
      `<span class="num">${r.call_count.toLocaleString()}</span>` +
      `<span>${localTime(r.last_call_at)}</span>` +
      `<span class="shapes">${shapes}</span>`;
    if (!isAgent) {
      row.addEventListener("click", () => setIdentityFilter("key", r.gateway_key_id));
    }
    root.appendChild(row);
  }
}

// ----- Shared identity-tile helpers -------------------------------------

// Sort rows in-place by the active client-side `identitySort` selector.
// `idKey` lets us put the null bucket last regardless of value so it never
// dominates the leaderboard.
function sortIdentityRows(rows, idKey) {
  const cmpKey = state.identitySort === "call_count" ? "call_count" : "cost_usd";
  return [...rows].sort((a, b) => {
    if (a[idKey] === null && b[idKey] !== null) return 1;
    if (a[idKey] !== null && b[idKey] === null) return -1;
    return b[cmpKey] - a[cmpKey];
  });
}

// Shared top-spender callout renderer. The >50% threshold flags concentrated
// spend that's worth a conversation; below it we hide the callout — equal
// distribution is the boring case.
function renderTopSpenderRow({ rows, valueKey, idKey, nullLabel, calloutId, idEl, shareEl }) {
  const el = document.getElementById(calloutId);
  if (!rows || !rows.length) {
    el.classList.add("hidden");
    return;
  }
  const total = rows.reduce((a, r) => a + r[valueKey], 0);
  if (total <= 0) {
    el.classList.add("hidden");
    return;
  }
  const top = [...rows].sort((a, b) => b[valueKey] - a[valueKey])[0];
  const share = top[valueKey] / total;
  if (share <= 0.5) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  document.getElementById(idEl).textContent = top[idKey] || nullLabel;
  document.getElementById(shareEl).textContent = pct(share);
}

// Set the active drill-down filter and bounce the user to Cost so they see
// the immediate effect. Closes the loop the per-key tile already had —
// extended to team / user filters via multi-user.md §5.3 params.
function setIdentityFilter(kind, value) {
  state.identityFilter = { kind, value };
  state.view = "cost";
  for (const b of document.querySelectorAll("#audience button"))
    b.classList.toggle("on", b.dataset.view === "cost");
  render();
}

// ----- Top-level render ---------------------------------------------------

async function render() {
  const win = resolveWindow(state.windowKey);
  document.getElementById("window-label").textContent = win.label;
  for (const id of ["view-cost", "view-activity", "view-identity"]) {
    const key = id.replace("view-", "");
    document.getElementById(id).classList.toggle("hidden", state.view !== key);
  }

  // Active-identity filter chip: visible on Cost / Activity when a filter
  // is set. On the identity view itself we hide it — the table is the
  // place to drill in/out.
  const chip = document.getElementById("identity-filter-chip");
  if (state.identityFilter && state.view !== "identity") {
    chip.classList.remove("hidden");
    document.getElementById("identity-filter-kind").textContent =
      state.identityFilter.kind;
    document.getElementById("identity-filter-id").textContent =
      state.identityFilter.value;
  } else {
    chip.classList.add("hidden");
  }

  const root = document.getElementById("view-root");
  root.setAttribute("aria-busy", "true");
  try {
    if (state.view === "cost") await renderCostView(win);
    else if (state.view === "activity") await renderActivityView(win);
    else await renderIdentityView(win);
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
  for (const btn of document.querySelectorAll("#identity-scope button")) {
    btn.addEventListener("click", () => {
      state.identityScope = btn.dataset.identityScope;
      for (const b of document.querySelectorAll("#identity-scope button"))
        b.classList.toggle("on", b === btn);
      render();
    });
  }
  for (const btn of document.querySelectorAll("#identity-sort button")) {
    btn.addEventListener("click", () => {
      state.identitySort = btn.dataset.identitySort;
      for (const b of document.querySelectorAll("#identity-sort button"))
        b.classList.toggle("on", b === btn);
      render();
    });
  }
  for (const btn of document.querySelectorAll("#keys-source button")) {
    btn.addEventListener("click", () => {
      state.keysFilter = btn.dataset.keysFilter;
      for (const b of document.querySelectorAll("#keys-source button"))
        b.classList.toggle("on", b === btn);
      render();
    });
  }
  document.getElementById("identity-filter-clear").addEventListener("click", () => {
    state.identityFilter = null;
    render();
  });
  // Delegate user-link clicks from inside expanded team panels — they're
  // rendered into innerHTML so we can't attach listeners at row-creation time
  // for the team's expand-panel children.
  document.addEventListener("click", (evt) => {
    const userBtn = evt.target.closest("[data-filter-user]");
    if (userBtn) {
      evt.stopPropagation();
      setIdentityFilter("user", userBtn.dataset.filterUser);
    }
  });
}

wireToggles();
render();
