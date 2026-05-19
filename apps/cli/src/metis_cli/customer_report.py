"""`metis customer-report` — offline-share-able usage report.

Generates a single HTML or JSON file summarizing what a buyer's trace DB
shows for a workspace over a window. The report is the "what landed in the
trial" artifact handed to the buyer at the end of an evaluation: cost,
quality, savings counterfactual, and per-key / per-user / per-team rollups
derived from the same `/analytics/*` projections that back the dashboard.

The report is **offline-share-able** — no JS, no external assets, no
network fetches. Inline CSS only. The buyer should be able to email it to
their CFO and the CFO should be able to open it in any browser. JSON
output is the machine-readable companion for SIEM / spreadsheet ingest.

Implementation note: this surface re-uses `AnalyticsStore` directly rather
than going through the HTTP endpoints, because (a) the buyer's trace DB
file is the source of truth and may live somewhere `metis serve` isn't
pointing at, and (b) we don't want to require a running server for an
offline report.
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from metis_core.analytics import AnalyticsStore, TimeWindow
from metis_core.analytics.errors import (
    InvalidTimeWindowError,
    UnknownBaselineModelError,
)
from metis_core.analytics.windows import resolve_window
from metis_core.pricing import DEFAULT_PRICE_TABLE

ReportFormat = Literal["html", "json"]
DEFAULT_BASELINE_MODEL = "anthropic:claude-sonnet-4-6"
DEFAULT_LOOKBACK_DAYS = 7
ANONYMIZED_CUSTOMER_LABEL = "Anonymous customer"
ANONYMIZED_WORKSPACE_PATH = "/workspace/anonymous-customer"
ANONYMIZED_DB_PATH = "anonymized-trace.db"
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


@dataclass(frozen=True)
class CustomerReport:
    """Snapshot of cost + quality + identity rollups for a trial window.

    Every field is JSON-serializable; the HTML renderer reads the same
    dataclass so the two surfaces can't drift independently.
    """

    workspace_path: str
    customer_label: str
    customer_tier: str | None
    window_start: datetime
    window_end: datetime
    baseline_model: str
    generated_at: datetime

    # Headline numbers
    total_spend_usd: float
    baseline_repriced_usd: float
    savings_usd: float
    savings_pct: float
    quality_mean: float | None
    quality_count: int
    cost_per_quality_usd: float | None

    # Per-model / per-key / per-user / per-team rollups
    by_model: list[dict[str, Any]] = field(default_factory=list)
    by_gateway_key: list[dict[str, Any]] = field(default_factory=list)
    by_user: list[dict[str, Any]] = field(default_factory=list)
    by_team: list[dict[str, Any]] = field(default_factory=list)

    # Daily spend series (for the chart-less but readable trend table)
    daily_spend: list[dict[str, Any]] = field(default_factory=list)

    # Provenance — every number should be auditable back to the DB.
    db_path: str = ""
    rows_total: int = 0
    rows_missing_from_price_table: int = 0


def build_report(
    *,
    db_path: Path,
    workspace_path: str,
    customer_label: str,
    customer_tier: str | None = None,
    window: TimeWindow,
    baseline_model: str = DEFAULT_BASELINE_MODEL,
    now: datetime | None = None,
) -> CustomerReport:
    """Run every analytics query in one DB pass and assemble the report.

    The store is opened read-only via WAL; concurrent gateway writers are
    safe to keep running. We close the store before returning so the
    caller doesn't have to track lifecycle.
    """
    generated_at = now or datetime.now(UTC)
    store = AnalyticsStore(db_path)
    try:
        # Headline savings counterfactual — drives the top-of-report block.
        savings = store.savings(
            window,
            baseline=baseline_model,
            price_table=DEFAULT_PRICE_TABLE,
        )
        # Per-model / per-key / per-user / per-team rollups.
        by_model = store.cost(window, group_by="model")
        by_gateway_key = store.by_key(window)
        by_user = store.cost(window, group_by="user")
        by_team_raw = store.by_team(window)
        # Daily trend (cost only — light enough for a deterministic table).
        daily_spend = store.cost(window, group_by="day")
        # Workload-level quality verdict — joins via route.decided so the
        # model attributed is the *judged* one, not the judge's own.
        quality_rollup = store.quality(
            window,
            subject_kind="turn",
            group_by="none",
        )
    finally:
        store.close()

    quality_mean = (
        float(quality_rollup["mean_score"])
        if isinstance(quality_rollup, dict) and quality_rollup.get("mean_score") is not None
        else None
    )
    quality_count = (
        int(quality_rollup["verdict_count"])
        if isinstance(quality_rollup, dict) and quality_rollup.get("verdict_count")
        else 0
    )
    total_spend = float(savings["actual_repriced_usd"])
    cost_per_quality = (
        total_spend / quality_mean
        if quality_mean is not None and quality_mean > 0 and total_spend > 0
        else None
    )

    by_model_list = by_model if isinstance(by_model, list) else [by_model]
    by_user_list = by_user if isinstance(by_user, list) else [by_user]
    daily_list = daily_spend if isinstance(daily_spend, list) else [daily_spend]

    # by_team's shape differs slightly (dict per team with a _by_user array
    # inside it); strip the per-user sub-array for the report headline and
    # let the buyer follow up via the HTTP endpoint if they want it.
    by_team: list[dict[str, Any]] = []
    for row in by_team_raw:
        by_team.append(
            {
                "team_id": row["team_id"],
                "cost_usd": row["cost_usd"],
                "call_count": row["call_count"],
                "user_count": row.get("user_count"),
            }
        )

    return CustomerReport(
        workspace_path=workspace_path,
        customer_label=customer_label,
        customer_tier=customer_tier,
        window_start=window.start,
        window_end=window.end,
        baseline_model=baseline_model,
        generated_at=generated_at,
        total_spend_usd=total_spend,
        baseline_repriced_usd=float(savings["baseline_repriced_usd"]),
        savings_usd=float(savings["savings_usd"]),
        savings_pct=float(savings["savings_pct"]),
        quality_mean=quality_mean,
        quality_count=quality_count,
        cost_per_quality_usd=cost_per_quality,
        by_model=by_model_list,
        by_gateway_key=by_gateway_key,
        by_user=by_user_list,
        by_team=by_team,
        daily_spend=daily_list,
        db_path=str(db_path),
        rows_total=int(savings["rows_total"]),
        rows_missing_from_price_table=int(savings["rows_missing_from_price_table"]),
    )


def render_json(report: CustomerReport) -> str:
    """Deterministic JSON — every field present, datetimes as ISO 8601."""
    obj = {
        "workspace_path": report.workspace_path,
        "customer_label": report.customer_label,
        "customer_tier": report.customer_tier,
        "window_start": report.window_start.isoformat(),
        "window_end": report.window_end.isoformat(),
        "baseline_model": report.baseline_model,
        "generated_at": report.generated_at.isoformat(),
        "total_spend_usd": report.total_spend_usd,
        "baseline_repriced_usd": report.baseline_repriced_usd,
        "savings_usd": report.savings_usd,
        "savings_pct": report.savings_pct,
        "quality_mean": report.quality_mean,
        "quality_count": report.quality_count,
        "cost_per_quality_usd": report.cost_per_quality_usd,
        "by_model": report.by_model,
        "by_gateway_key": report.by_gateway_key,
        "by_user": report.by_user,
        "by_team": report.by_team,
        "daily_spend": report.daily_spend,
        "db_path": report.db_path,
        "rows_total": report.rows_total,
        "rows_missing_from_price_table": report.rows_missing_from_price_table,
    }
    return json.dumps(obj, indent=2, sort_keys=True, default=_json_default)


def anonymize_report(report: CustomerReport) -> CustomerReport:
    """Return a report safe to share as a public customer artifact.

    Customer-specific labels, paths, gateway key ids, user ids, and team ids
    are replaced with deterministic placeholders. Numeric aggregates, model ids,
    timestamps, and tier badges are preserved so the report remains useful as a
    case-study source without leaking the buyer's internal identifiers.
    """
    key_map = _placeholder_map(report.by_gateway_key, "gateway_key_id", "gateway_key")
    user_map = _placeholder_map(report.by_user, "user_id", "user")
    team_map = _placeholder_map(report.by_team, "team_id", "team")

    return replace(
        report,
        workspace_path=ANONYMIZED_WORKSPACE_PATH,
        customer_label=ANONYMIZED_CUSTOMER_LABEL,
        by_gateway_key=[
            _anonymize_row(row, "gateway_key_id", key_map) for row in report.by_gateway_key
        ],
        by_user=[_anonymize_row(row, "user_id", user_map) for row in report.by_user],
        by_team=[_anonymize_row(row, "team_id", team_map) for row in report.by_team],
        db_path=ANONYMIZED_DB_PATH,
    )


def template_values(report: CustomerReport) -> dict[str, str]:
    """Build stable string values for customer-report markdown templates."""
    return {
        "baseline_model": report.baseline_model,
        "baseline_short": _short_model(report.baseline_model),
        "baseline_repriced_usd": f"{report.baseline_repriced_usd:.4f}",
        "cost_per_quality": _format_cost_per_quality(report.cost_per_quality_usd),
        "customer_label": report.customer_label,
        "customer_tier": report.customer_tier or "",
        "generated_at": report.generated_at.isoformat(),
        "llm_calls": str(report.rows_total),
        "quality_count": str(report.quality_count),
        "quality_line": _format_quality_sub(report.quality_mean, report.quality_count),
        "quality_mean": "" if report.quality_mean is None else f"{report.quality_mean:.2f}",
        "savings_pct": _format_savings_pct(report.savings_pct),
        "savings_usd": f"{report.savings_usd:.4f}",
        "total_spend_usd": f"{report.total_spend_usd:.4f}",
        "window_end": report.window_end.isoformat(),
        "window_start": report.window_start.isoformat(),
        "workspace_path": report.workspace_path,
    }


def render_report_template(
    template: str,
    report: CustomerReport,
    *,
    extra_values: Mapping[str, Any] | None = None,
) -> str:
    """Substitute `{{placeholder}}` tokens from a report-derived value map.

    The helper intentionally supports a tiny syntax: alphanumeric, `_`, `.`, and
    `-` placeholder names wrapped in double braces. Missing placeholders raise
    `KeyError` so customer-facing artifacts fail closed instead of shipping
    unresolved template strings.
    """
    values: dict[str, str] = template_values(report)
    if extra_values:
        values.update({key: str(value) for key, value in extra_values.items()})

    def replace_match(match: re.Match[str]) -> str:
        key = match.group(1)
        try:
            return values[key]
        except KeyError as exc:
            raise KeyError(f"missing report template placeholder: {key}") from exc

    return _PLACEHOLDER_RE.sub(replace_match, template)


def _placeholder_map(
    rows: list[dict[str, Any]],
    field: str,
    prefix: str,
) -> dict[str, str]:
    values = sorted({str(row[field]) for row in rows if row.get(field)})
    return {value: f"{prefix}_{idx:03d}" for idx, value in enumerate(values, start=1)}


def _anonymize_row(
    row: dict[str, Any],
    field: str,
    mapping: Mapping[str, str],
) -> dict[str, Any]:
    anonymized = dict(row)
    value = anonymized.get(field)
    if value:
        anonymized[field] = mapping[str(value)]
    return anonymized


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------


_HTML_STYLE = """
:root { color-scheme: light dark; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial,
               sans-serif;
  max-width: 880px;
  margin: 2rem auto;
  padding: 0 1rem;
  color: #1d1d1f;
  background: #fbfbfb;
  line-height: 1.5;
}
h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
h2 { font-size: 1.15rem; margin-top: 2rem; border-bottom: 1px solid #ddd;
     padding-bottom: 0.25rem; }
.meta { color: #666; font-size: 0.92rem; margin-bottom: 1.5rem; }
.headline { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem;
            margin-bottom: 1rem; }
.stat { background: #fff; border: 1px solid #ddd; border-radius: 8px;
        padding: 1rem; }
.stat .label { color: #666; font-size: 0.8rem; text-transform: uppercase;
               letter-spacing: 0.05em; }
.stat .value { font-size: 1.5rem; font-weight: 600; margin-top: 0.25rem; }
.stat .sub { color: #666; font-size: 0.85rem; }
table { width: 100%; border-collapse: collapse; margin: 0.5rem 0 1.5rem; }
th, td { padding: 0.4rem 0.6rem; border-bottom: 1px solid #eee;
         text-align: left; font-size: 0.95rem; }
th { font-weight: 600; color: #555; background: #f6f6f6; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.footer { color: #888; font-size: 0.85rem; margin-top: 3rem;
          border-top: 1px solid #ddd; padding-top: 0.75rem; }
.tier-badge { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px;
              font-size: 0.78rem; font-weight: 600; letter-spacing: 0.04em;
              text-transform: uppercase; }
.tier-trial { background: #fff7e0; color: #8a6500; }
.tier-paid { background: #e6f4ec; color: #196d3f; }
.tier-internal { background: #eef0ff; color: #2a3f8f; }
"""


def render_html(report: CustomerReport) -> str:
    """Self-contained HTML — no external CSS / JS / images.

    The page renders in any browser without network access. Inline CSS
    only (the `_HTML_STYLE` constant); no JS — the buyer can save the page
    to PDF via their browser's print dialog if they want PDF.
    """
    tier_html = _tier_badge_html(report.customer_tier)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Metis usage report — {html.escape(report.customer_label)}</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
<h1>Metis usage report</h1>
<div class="meta">
  <strong>{html.escape(report.customer_label)}</strong> {tier_html}<br>
  Workspace: {html.escape(report.workspace_path)}<br>
  Window: {html.escape(report.window_start.isoformat())}
  → {html.escape(report.window_end.isoformat())}<br>
  Generated: {html.escape(report.generated_at.isoformat())}
</div>

<div class="headline">
  <div class="stat">
    <div class="label">Spend</div>
    <div class="value">${report.total_spend_usd:.2f}</div>
    <div class="sub">{report.rows_total} LLM calls in window</div>
  </div>
  <div class="stat">
    <div class="label">Savings vs {html.escape(_short_model(report.baseline_model))}</div>
    <div class="value">{_format_savings_pct(report.savings_pct)}</div>
    <div class="sub">${report.savings_usd:.2f} below baseline</div>
  </div>
  <div class="stat">
    <div class="label">Cost / quality</div>
    <div class="value">{_format_cost_per_quality(report.cost_per_quality_usd)}</div>
    <div class="sub">{_format_quality_sub(report.quality_mean, report.quality_count)}</div>
  </div>
</div>

{_render_section("Spend by model", _render_by_model_table(report.by_model))}
{_render_section("Spend by gateway key", _render_by_key_table(report.by_gateway_key))}
{_render_section("Spend by user", _render_by_user_table(report.by_user))}
{_render_section("Spend by team", _render_by_team_table(report.by_team))}
{_render_section("Daily spend", _render_daily_table(report.daily_spend))}

<div class="footer">
  Numbers derived from {html.escape(report.db_path)} — the canonical
  trace DB. Cost is re-priced from the stamped token counts via the same
  PriceTable the dashboard uses (PriceTable v1). Baseline is
  {html.escape(report.baseline_model)}; savings is the counterfactual cost
  if every call had gone to baseline.
  {_format_missing_warning(report.rows_missing_from_price_table)}
</div>
</body>
</html>
"""


def _render_section(title: str, body: str) -> str:
    if not body.strip():
        return ""
    return f"<h2>{html.escape(title)}</h2>\n{body}"


def _render_by_model_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    head = "<tr><th>Model</th><th class='num'>Calls</th><th class='num'>Spend</th></tr>"
    body = "".join(
        f"<tr><td>{html.escape(_short_model(str(r.get('model') or '—')))}</td>"
        f"<td class='num'>{int(r.get('call_count') or 0)}</td>"
        f"<td class='num'>${float(r.get('cost_usd') or 0):.4f}</td></tr>"
        for r in rows
    )
    return f"<table>{head}{body}</table>"


def _render_by_key_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    head = (
        "<tr><th>Gateway key</th><th class='num'>Calls</th>"
        "<th class='num'>Spend</th><th>Last call</th></tr>"
    )
    body = "".join(
        f"<tr><td>{html.escape(str(r.get('gateway_key_id') or '— (agent loop)'))}</td>"
        f"<td class='num'>{int(r.get('call_count') or 0)}</td>"
        f"<td class='num'>${float(r.get('cost_usd') or 0):.4f}</td>"
        f"<td>{html.escape(str(r.get('last_call_at') or '—'))}</td></tr>"
        for r in rows
    )
    return f"<table>{head}{body}</table>"


def _render_by_user_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    head = "<tr><th>User</th><th class='num'>Calls</th><th class='num'>Spend</th></tr>"
    body = "".join(
        f"<tr><td>{html.escape(str(r.get('user_id') or '— (unattributed)'))}</td>"
        f"<td class='num'>{int(r.get('call_count') or 0)}</td>"
        f"<td class='num'>${float(r.get('cost_usd') or 0):.4f}</td></tr>"
        for r in rows
    )
    return f"<table>{head}{body}</table>"


def _render_by_team_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    head = (
        "<tr><th>Team</th><th class='num'>Users</th>"
        "<th class='num'>Calls</th><th class='num'>Spend</th></tr>"
    )
    body = "".join(
        f"<tr><td>{html.escape(str(r.get('team_id') or '— (unattributed)'))}</td>"
        f"<td class='num'>{int(r.get('user_count') or 0)}</td>"
        f"<td class='num'>{int(r.get('call_count') or 0)}</td>"
        f"<td class='num'>${float(r.get('cost_usd') or 0):.4f}</td></tr>"
        for r in rows
    )
    return f"<table>{head}{body}</table>"


def _render_daily_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    head = "<tr><th>Day</th><th class='num'>Calls</th><th class='num'>Spend</th></tr>"
    body = "".join(
        f"<tr><td>{html.escape(str(r.get('bucket') or r.get('day') or '—'))}</td>"
        f"<td class='num'>{int(r.get('call_count') or 0)}</td>"
        f"<td class='num'>${float(r.get('cost_usd') or 0):.4f}</td></tr>"
        for r in rows
    )
    return f"<table>{head}{body}</table>"


def _tier_badge_html(tier: str | None) -> str:
    if not tier:
        return ""
    cls = {"trial": "tier-trial", "paid": "tier-paid", "internal": "tier-internal"}.get(
        tier, "tier-internal"
    )
    return f'<span class="tier-badge {cls}">{html.escape(tier)}</span>'


def _short_model(model_id: str) -> str:
    # Strip the canonical provider prefix for display ("anthropic:claude-haiku-4-5"
    # → "claude-haiku-4-5"). The full id remains in the JSON output.
    if ":" in model_id:
        return model_id.split(":", 1)[1]
    return model_id


def _format_savings_pct(value: float) -> str:
    if value == 0:
        return "0%"
    return f"{value * 100:.1f}%"


def _format_cost_per_quality(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:.4f}"


def _format_quality_sub(quality_mean: float | None, count: int) -> str:
    if quality_mean is None or count == 0:
        return "no quality verdicts in window"
    return f"quality {quality_mean:.2f} across {count} verdicts"


def _format_missing_warning(rows_missing: int) -> str:
    if rows_missing == 0:
        return ""
    return (
        f"<br><strong>Note:</strong> {rows_missing} call(s) in the window "
        "referenced a model not in the current PriceTable; the stamped cost "
        "still rolls up, but the re-priced counterfactual excludes them."
    )


# ---------------------------------------------------------------------------
# CLI shim
# ---------------------------------------------------------------------------


def _default_db_path() -> Path:
    return Path.home() / ".metis" / "metis.db"


def run_customer_report_command(
    *,
    workspace: str,
    since: str | None,
    until: str | None,
    db_path: str | None,
    output: str | None,
    format: str,
    customer_label: str | None,
    customer_tier: str | None,
    baseline: str,
    anonymize: bool = False,
) -> int:
    """CLI shim — assemble the report and write it to disk (or stdout)."""
    if format not in ("html", "json"):
        print(f"customer-report failed: unsupported format {format!r}", file=sys.stderr)
        return 2

    source_db = Path(db_path).expanduser() if db_path else _default_db_path()
    if not source_db.exists():
        print(f"customer-report failed: trace DB not found: {source_db}", file=sys.stderr)
        return 1

    try:
        window = resolve_window(
            since,
            until,
            default_lookback=timedelta(days=DEFAULT_LOOKBACK_DAYS),
        )
    except InvalidTimeWindowError as exc:
        print(f"customer-report failed: {exc}", file=sys.stderr)
        return 2

    workspace_path = str(Path(workspace).expanduser().resolve())
    label = customer_label or Path(workspace_path).name or workspace_path

    try:
        report = build_report(
            db_path=source_db,
            workspace_path=workspace_path,
            customer_label=label,
            customer_tier=customer_tier,
            window=window,
            baseline_model=baseline,
        )
    except UnknownBaselineModelError as exc:
        print(f"customer-report failed: {exc}", file=sys.stderr)
        return 2

    if anonymize:
        report = anonymize_report(report)

    rendered = render_html(report) if format == "html" else render_json(report)

    if output:
        out_path = Path(output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        print("customer-report complete")
        print(f"  destination:    {out_path}")
        print(f"  format:         {format}")
        print(f"  window start:   {report.window_start.isoformat()}")
        print(f"  window end:     {report.window_end.isoformat()}")
        print(f"  total spend:    ${report.total_spend_usd:.4f}")
        print(f"  savings_pct:    {_format_savings_pct(report.savings_pct)}")
        print(f"  llm calls:      {report.rows_total}")
        if report.customer_tier:
            print(f"  customer_tier:  {report.customer_tier}")
        if anonymize:
            print("  anonymized:     true")
    else:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
    return 0


__all__ = [
    "DEFAULT_BASELINE_MODEL",
    "DEFAULT_LOOKBACK_DAYS",
    "CustomerReport",
    "anonymize_report",
    "build_report",
    "render_html",
    "render_json",
    "render_report_template",
    "run_customer_report_command",
    "template_values",
]
