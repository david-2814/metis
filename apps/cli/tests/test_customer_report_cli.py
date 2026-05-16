"""Tests for the `metis customer-report` CLI subcommand.

Covers: parser shape, end-to-end rendering against a seeded trace DB
(both HTML and JSON), missing-DB error path, and the dataclass→render
invariants the concierge-onboarding flow depends on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from metis_cli.customer_report import (
    DEFAULT_BASELINE_MODEL,
    DEFAULT_LOOKBACK_DAYS,
    anonymize_report,
    build_report,
    render_html,
    render_json,
    render_report_template,
)
from metis_cli.main import build_parser, main
from metis_core.analytics.windows import TimeWindow
from metis_core.events.envelope import Actor
from metis_core.events.payloads import LLMCallCompleted, make_event
from metis_core.trace.store import TraceStore


def _seed_trace(
    path: Path,
    *,
    base_ts: datetime,
    keys: tuple[tuple[str, str | None, str | None, str, int, int, float], ...],
) -> None:
    """Write N llm.call_completed events; each tuple is
    (gateway_key_id, user_id, team_id, model, input_tokens, output_tokens, cost_usd).
    """
    store = TraceStore(path)
    try:
        for i, (gw, user, team, model, in_t, out_t, cost) in enumerate(keys):
            store.write(
                make_event(
                    type="llm.call_completed",
                    session_id=f"sess_{i}",
                    actor=Actor.AGENT,
                    timestamp=base_ts + timedelta(minutes=i),
                    payload=LLMCallCompleted(
                        model=model,
                        provider=model.split(":", 1)[0],
                        input_tokens=in_t,
                        output_tokens=out_t,
                        cached_input_tokens=0,
                        cache_creation_input_tokens=0,
                        cost_usd=cost,
                        pricing_version="v1",
                        latency_ms=100,
                        stop_reason="end_turn",
                        produced_tool_calls=0,
                        produced_thinking_blocks=0,
                        gateway_key_id=gw,
                        inbound_shape="anthropic",
                        user_id=user,
                        team_id=team,
                    ),
                )
            )
    finally:
        store.close()


def test_customer_report_subcommand_parses() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "customer-report",
            "--workspace",
            "/srv/customer",
            "--db-path",
            "/tmp/metis.db",
            "--since",
            "2026-05-10T00:00:00+00:00",
            "--until",
            "2026-05-17T00:00:00+00:00",
            "--out",
            "/tmp/report.html",
            "--format",
            "html",
            "--customer-label",
            "Acme Corp",
            "--customer-tier",
            "trial",
            "--baseline",
            "anthropic:claude-sonnet-4-6",
        ]
    )
    assert args.command == "customer-report"
    assert args.workspace == "/srv/customer"
    assert args.format == "html"
    assert args.customer_tier == "trial"
    assert args.customer_label == "Acme Corp"
    assert args.baseline == "anthropic:claude-sonnet-4-6"
    assert args.anonymize is False


def test_customer_report_subcommand_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["customer-report", "--workspace", "/srv/customer"])
    assert args.format == "html"
    assert args.customer_label is None
    assert args.customer_tier is None
    assert args.baseline == DEFAULT_BASELINE_MODEL
    assert args.since is None
    assert args.until is None
    assert args.anonymize is False


def test_customer_report_subcommand_parses_anonymize_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "customer-report",
            "--workspace",
            "/srv/customer",
            "--anonymize",
        ]
    )
    assert args.command == "customer-report"
    assert args.anonymize is True


def test_customer_report_rejects_unknown_tier() -> None:
    parser = build_parser()
    try:
        parser.parse_args(
            ["customer-report", "--workspace", "/srv/c", "--customer-tier", "platinum"]
        )
    except SystemExit:
        return
    raise AssertionError("--customer-tier should reject unknown values")


def test_build_report_against_seeded_db(tmp_path: Path) -> None:
    """Headline numbers + per-key rollup populate against a tiny seeded DB."""
    db = tmp_path / "metis.db"
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_trace(
        db,
        base_ts=base,
        keys=(
            ("gk_alice", "alice", "eng", "anthropic:claude-haiku-4-5", 100, 50, 0.001),
            ("gk_alice", "alice", "eng", "anthropic:claude-haiku-4-5", 200, 100, 0.002),
            ("gk_bob", "bob", "eng", "anthropic:claude-sonnet-4-6", 50, 30, 0.003),
        ),
    )

    window = TimeWindow(
        start=base - timedelta(hours=1),
        end=base + timedelta(days=1),
    )
    report = build_report(
        db_path=db,
        workspace_path="/srv/acme",
        customer_label="Acme Corp",
        customer_tier="trial",
        window=window,
    )

    assert report.workspace_path == "/srv/acme"
    assert report.customer_label == "Acme Corp"
    assert report.customer_tier == "trial"
    assert report.rows_total == 3
    # Stamped cost rolls up; the report's `total_spend_usd` is the
    # re-priced figure, not the stamped sum — both should be positive.
    assert report.total_spend_usd > 0
    assert report.baseline_repriced_usd > 0
    # baseline (sonnet) is more expensive than haiku → savings positive
    assert report.savings_usd > 0
    # 0 ≤ savings_pct ≤ 1
    assert 0.0 <= report.savings_pct <= 1.0

    # Two distinct gateway keys → at least two rows in by_gateway_key
    key_ids = {row["gateway_key_id"] for row in report.by_gateway_key}
    assert {"gk_alice", "gk_bob"}.issubset(key_ids)

    # Two distinct users (alice + bob)
    user_ids = {row["user_id"] for row in report.by_user if row.get("user_id")}
    assert {"alice", "bob"} == user_ids

    # One team aggregates both users
    teams = [row for row in report.by_team if row.get("team_id") == "eng"]
    assert len(teams) == 1
    assert teams[0]["call_count"] == 3
    # by_team rollup carries user_count
    assert teams[0]["user_count"] == 2


def test_build_report_handles_empty_db(tmp_path: Path) -> None:
    """An empty trace DB renders cleanly with zeroed-out numbers."""
    db = tmp_path / "metis.db"
    store = TraceStore(db)
    store.close()

    window = TimeWindow(
        start=datetime(2026, 5, 10, tzinfo=UTC),
        end=datetime(2026, 5, 17, tzinfo=UTC),
    )
    report = build_report(
        db_path=db,
        workspace_path="/srv/quiet",
        customer_label="Quiet Co",
        window=window,
    )
    assert report.rows_total == 0
    assert report.total_spend_usd == 0
    assert report.savings_usd == 0
    assert report.quality_count == 0
    assert report.cost_per_quality_usd is None
    # No rollup rows on empty data
    assert report.by_model == []
    assert report.by_gateway_key == []


def test_render_html_is_self_contained(tmp_path: Path) -> None:
    """HTML output has no external assets — buyer can open it offline."""
    db = tmp_path / "metis.db"
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_trace(
        db,
        base_ts=base,
        keys=(("gk_x", "alice", "eng", "anthropic:claude-haiku-4-5", 100, 50, 0.001),),
    )
    window = TimeWindow(start=base - timedelta(hours=1), end=base + timedelta(days=1))
    report = build_report(
        db_path=db,
        workspace_path="/srv/x",
        customer_label="X Inc",
        customer_tier="trial",
        window=window,
    )
    html_out = render_html(report)
    # Offline-share contract: no <script>, no <link rel="stylesheet">, no img src=http
    assert "<script" not in html_out
    assert '<link rel="stylesheet"' not in html_out
    assert 'src="http' not in html_out
    # Inline style block present
    assert "<style>" in html_out
    # Tier badge surfaces
    assert "trial" in html_out
    # Headline numbers in the page
    assert "Spend" in html_out
    assert "Savings vs" in html_out


def test_render_html_escapes_customer_label(tmp_path: Path) -> None:
    """Customer-provided strings can't inject HTML — every render goes
    through `html.escape` so a <script> tag in the label stays inert."""
    db = tmp_path / "metis.db"
    store = TraceStore(db)
    store.close()
    window = TimeWindow(
        start=datetime(2026, 5, 10, tzinfo=UTC),
        end=datetime(2026, 5, 17, tzinfo=UTC),
    )
    hostile = "<script>alert('xss')</script>"
    report = build_report(
        db_path=db,
        workspace_path="/srv/x",
        customer_label=hostile,
        window=window,
    )
    html_out = render_html(report)
    assert "<script>alert" not in html_out
    assert "&lt;script&gt;" in html_out


def test_render_json_is_valid_and_deterministic(tmp_path: Path) -> None:
    """JSON output is parseable and stable across two render calls."""
    db = tmp_path / "metis.db"
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_trace(
        db,
        base_ts=base,
        keys=(("gk_y", None, None, "anthropic:claude-haiku-4-5", 50, 25, 0.0005),),
    )
    window = TimeWindow(start=base - timedelta(hours=1), end=base + timedelta(days=1))
    report = build_report(
        db_path=db,
        workspace_path="/srv/y",
        customer_label="Y Inc",
        window=window,
    )
    out1 = render_json(report)
    out2 = render_json(report)
    assert out1 == out2  # deterministic — sort_keys + same input
    parsed = json.loads(out1)
    assert parsed["workspace_path"] == "/srv/y"
    assert parsed["rows_total"] == 1
    assert isinstance(parsed["total_spend_usd"], float)
    # Window dates round-trip as ISO 8601
    assert parsed["window_start"].startswith("2026-05-10")


def test_anonymize_report_replaces_customer_identifiers_deterministically(
    tmp_path: Path,
) -> None:
    db = tmp_path / "metis.db"
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_trace(
        db,
        base_ts=base,
        keys=(
            ("gk_beta", "zoe", "platform", "anthropic:claude-haiku-4-5", 100, 50, 0.001),
            ("gk_alpha", "amy", "growth", "anthropic:claude-haiku-4-5", 100, 50, 0.001),
            ("gk_beta", "zoe", "platform", "anthropic:claude-haiku-4-5", 100, 50, 0.001),
        ),
    )
    window = TimeWindow(start=base - timedelta(hours=1), end=base + timedelta(days=1))
    report = build_report(
        db_path=db,
        workspace_path="/srv/acme-private",
        customer_label="Acme Private",
        customer_tier="paid",
        window=window,
    )

    first = anonymize_report(report)
    second = anonymize_report(report)
    assert first == second
    assert first.customer_label == "Anonymous customer"
    assert first.workspace_path == "/workspace/anonymous-customer"
    assert first.db_path == "anonymized-trace.db"

    rendered_json = render_json(first)
    rendered_html = render_html(first)
    for leaked in (
        "Acme Private",
        "/srv/acme-private",
        str(db),
        "gk_alpha",
        "gk_beta",
        "amy",
        "zoe",
        "growth",
        "platform",
    ):
        assert leaked not in rendered_json
        assert leaked not in rendered_html

    parsed = json.loads(rendered_json)
    assert {row["gateway_key_id"] for row in parsed["by_gateway_key"]} == {
        "gateway_key_001",
        "gateway_key_002",
    }
    assert {row["user_id"] for row in parsed["by_user"]} == {"user_001", "user_002"}
    assert {row["team_id"] for row in parsed["by_team"]} == {"team_001", "team_002"}


def test_report_template_placeholder_substitution_is_deterministic(tmp_path: Path) -> None:
    db = tmp_path / "metis.db"
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_trace(
        db,
        base_ts=base,
        keys=(("gk_x", "alice", "eng", "anthropic:claude-haiku-4-5", 100, 50, 0.001),),
    )
    window = TimeWindow(start=base - timedelta(hours=1), end=base + timedelta(days=1))
    report = anonymize_report(
        build_report(
            db_path=db,
            workspace_path="/srv/acme-private",
            customer_label="Acme Private",
            customer_tier="trial",
            window=window,
        )
    )

    template = (
        "# {{ customer_label }}\n"
        "Window: {{window_start}} -> {{window_end}}\n"
        "Spend: ${{total_spend_usd}} across {{ llm_calls }} calls\n"
        "Owner note: {{owner_note}}\n"
    )
    rendered_1 = render_report_template(
        template,
        report,
        extra_values={"owner_note": "approved for anonymized case-study draft"},
    )
    rendered_2 = render_report_template(
        template,
        report,
        extra_values={"owner_note": "approved for anonymized case-study draft"},
    )
    assert rendered_1 == rendered_2
    assert "{{" not in rendered_1
    assert "Anonymous customer" in rendered_1
    assert "Acme Private" not in rendered_1


def test_report_template_missing_placeholder_raises(tmp_path: Path) -> None:
    db = tmp_path / "metis.db"
    store = TraceStore(db)
    store.close()
    window = TimeWindow(
        start=datetime(2026, 5, 10, tzinfo=UTC),
        end=datetime(2026, 5, 17, tzinfo=UTC),
    )
    report = build_report(
        db_path=db,
        workspace_path="/srv/x",
        customer_label="X Inc",
        window=window,
    )

    try:
        render_report_template("{{missing_value}}", report)
    except KeyError as exc:
        assert "missing_value" in str(exc)
    else:
        raise AssertionError("missing template placeholders should fail closed")


def test_customer_report_end_to_end_html(tmp_path: Path, capsys) -> None:
    db = tmp_path / "metis.db"
    out = tmp_path / "report.html"
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_trace(
        db,
        base_ts=base,
        keys=(("gk_x", "alice", "eng", "anthropic:claude-haiku-4-5", 100, 50, 0.001),),
    )
    rc = main(
        [
            "customer-report",
            "--workspace",
            str(tmp_path),
            "--db-path",
            str(db),
            "--since",
            "2026-05-09T00:00:00+00:00",
            "--until",
            "2026-05-17T00:00:00+00:00",
            "--out",
            str(out),
            "--format",
            "html",
            "--customer-label",
            "Acme Corp",
            "--customer-tier",
            "trial",
            "--anonymize",
        ]
    )
    assert rc == 0
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "Anonymous customer" in body
    assert "Acme Corp" not in body
    assert "trial" in body
    stdout = capsys.readouterr().out
    assert "customer-report complete" in stdout
    assert "total spend:" in stdout
    assert "customer_tier:  trial" in stdout
    assert "anonymized:     true" in stdout


def test_customer_report_end_to_end_json_to_stdout(tmp_path: Path, capsys) -> None:
    """JSON to stdout (no --out) for piping to jq."""
    db = tmp_path / "metis.db"
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_trace(
        db,
        base_ts=base,
        keys=(("gk_x", "alice", "eng", "anthropic:claude-haiku-4-5", 100, 50, 0.001),),
    )
    rc = main(
        [
            "customer-report",
            "--workspace",
            str(tmp_path),
            "--db-path",
            str(db),
            "--since",
            "2026-05-09T00:00:00+00:00",
            "--until",
            "2026-05-17T00:00:00+00:00",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["rows_total"] == 1
    assert "by_gateway_key" in parsed


def test_customer_report_missing_db_returns_nonzero(tmp_path: Path, capsys) -> None:
    rc = main(
        [
            "customer-report",
            "--workspace",
            "/srv/x",
            "--db-path",
            str(tmp_path / "missing.db"),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "trace DB not found" in err


def test_customer_report_unknown_baseline_returns_nonzero(tmp_path: Path, capsys) -> None:
    db = tmp_path / "metis.db"
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_trace(
        db,
        base_ts=base,
        keys=(("gk_x", None, None, "anthropic:claude-haiku-4-5", 50, 25, 0.0005),),
    )
    rc = main(
        [
            "customer-report",
            "--workspace",
            "/srv/x",
            "--db-path",
            str(db),
            "--baseline",
            "fictional:model-not-in-table",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "fictional:model-not-in-table" in err


def test_default_lookback_is_seven_days() -> None:
    """The concierge-onboarding doc quotes a 7-day default; assert."""
    assert DEFAULT_LOOKBACK_DAYS == 7
