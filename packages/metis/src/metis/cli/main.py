"""`metis` CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from metis.cli.chat import run_chat


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metis",
        description="Local-first AI agent CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_session_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "workspace",
            help="Workspace directory the agent operates in.",
        )
        p.add_argument(
            "--model",
            help=(
                "Initial model alias or canonical id "
                "(e.g. 'sonnet' or 'anthropic:claude-sonnet-4-6'). "
                "Sets the Active model for the session."
            ),
            default=None,
        )
        p.add_argument(
            "--db-path",
            help="SQLite path for trace + sessions. Default: ~/.metis/metis.db",
            default=None,
        )
        p.add_argument(
            "--global-default",
            help="Model used when no Active model / override is set. "
            "Default: anthropic:claude-sonnet-4-6",
            default="anthropic:claude-sonnet-4-6",
        )

    chat = sub.add_parser("chat", help="Start an interactive line-based REPL.")
    _add_session_args(chat)

    tui = sub.add_parser("tui", help="Start the Textual TUI.")
    _add_session_args(tui)

    serve = sub.add_parser("serve", help="Run the HTTP/WebSocket server (loopback only).")
    serve.add_argument(
        "workspace",
        help="Workspace directory the agent operates in.",
    )
    serve.add_argument(
        "--db-path",
        help="SQLite path for trace + sessions. Default: ~/.metis/metis.db",
        default=None,
    )
    serve.add_argument(
        "--global-default",
        help="Model used when no Active model / override is set. Default: anthropic:claude-sonnet-4-6",
        default="anthropic:claude-sonnet-4-6",
    )
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (loopback-only in v1).")
    serve.add_argument("--port", type=int, default=8421, help="Bind port.")

    evaluate = sub.add_parser(
        "evaluate",
        help="Re-evaluate trace-DB subjects with the heuristic judge (evaluator.md §6.2).",
    )
    evaluate.add_argument("--db-path", required=True, help="Trace DB path.")
    evaluate.add_argument(
        "--subject",
        choices=("turn", "tool_cycle", "session"),
        default="turn",
        help="Subject kind to re-evaluate (default: turn).",
    )
    evaluate.add_argument("--since", help="ISO 8601 UTC start of window (inclusive).")
    evaluate.add_argument("--until", help="ISO 8601 UTC end of window (inclusive).")
    evaluate.add_argument(
        "--session-id",
        help="Restrict to a single session (default: all sessions in window).",
    )

    gateway = sub.add_parser(
        "gateway",
        help="Run the transparent OpenAI/Anthropic-shape HTTP gateway, or issue keys.",
    )
    gateway_sub = gateway.add_subparsers(dest="gateway_command", required=False)

    # `metis gateway` with no subcommand = start the server.
    gateway.add_argument(
        "--keystore",
        help="Gateway keystore path. Default: ~/.metis/gateway/keys.json",
        default=None,
    )
    gateway.add_argument(
        "--db-path",
        help="SQLite path for trace + sessions. Default: ~/.metis/metis.db",
        default=None,
    )
    gateway.add_argument(
        "--global-default",
        help="Model used when routing finds no other slot win. "
        "Default: anthropic:claude-sonnet-4-6",
        default="anthropic:claude-sonnet-4-6",
    )
    gateway.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Bind host. Default 127.0.0.1 (loopback). Pass 0.0.0.0 to expose "
            "on every interface; gateway-hardening.md §2.1 lists the perimeter "
            "checklist (TLS termination, rate-limit middleware, audit logging) "
            "the operator owns when binding non-loopback."
        ),
    )
    gateway.add_argument("--port", type=int, default=8422, help="Bind port.")
    gateway.add_argument(
        "--tls-cert",
        default=None,
        help=(
            "Path to a PEM-encoded TLS certificate. Enables in-process TLS "
            "termination (requires --tls-key). Default: no TLS; terminate at "
            "an upstream sidecar (nginx-ingress / Caddy / cloud LB) per "
            "gateway-hardening.md §2."
        ),
    )
    gateway.add_argument(
        "--tls-key",
        default=None,
        help="Path to the PEM-encoded TLS private key. Required when --tls-cert is set.",
    )
    gateway.add_argument(
        "--max-connections",
        type=int,
        default=1000,
        help=(
            "Per-process cap on in-flight requests + open connections (uvicorn "
            "limit_concurrency). Excess connections return HTTP 503 immediately. "
            "Default 1000; gateway-hardening.md §2.1."
        ),
    )
    gateway.add_argument(
        "--reuse-port",
        action="store_true",
        default=False,
        help=(
            "Bind the listen socket with SO_REUSEPORT so a second gateway "
            "process can hold the same port for graceful restart. Default off; "
            "single-process operation does not need it."
        ),
    )
    # Wave-14 signup options moved to the closed-source metis-pro overlay
    # (repo-split-plan.md §4.3, 2026-05-18). Wave-15 billing options moved
    # there too (§4.2b). The Pro deployment surface adds --enable-signup /
    # --enable-billing + the related Stripe / dashboard / accounts-path
    # knobs from `metis_pro` instead.

    issue = gateway_sub.add_parser(
        "issue-key",
        help="Create a new gateway key and append it to the keystore (prints token once).",
    )
    issue.add_argument(
        "--keystore",
        help="Gateway keystore path. Default: ~/.metis/gateway/keys.json",
        default=None,
    )
    issue.add_argument("--name", required=True, help="Display name for the key.")
    issue.add_argument(
        "--workspace",
        required=True,
        help="Workspace path the key is scoped to.",
    )
    issue.add_argument(
        "--allow-model",
        action="append",
        default=None,
        help="Restrict the key to a model id or alias (repeat for multiple).",
    )
    issue.add_argument(
        "--daily-cap-usd",
        type=str,
        default=None,
        help=(
            "Optional per-day spend cap (USD). Hard breaker per multi-user.md §5.1. "
            "Must parse as a positive number."
        ),
    )
    issue.add_argument(
        "--monthly-cap-usd",
        type=str,
        default=None,
        help=(
            "Optional per-calendar-month spend cap (USD). Hard breaker per "
            "multi-user.md §5.1. Must parse as a positive number."
        ),
    )
    issue.add_argument(
        "--user",
        default=None,
        help=(
            "Optional user_id tag for per-developer cost attribution "
            "(multi-user.md §4.2). Lowercase [a-z0-9_-]+."
        ),
    )
    issue.add_argument(
        "--team",
        default=None,
        help=(
            "Optional team_id tag for per-team cost attribution "
            "(multi-user.md §4.2). Lowercase [a-z0-9_-]+."
        ),
    )
    issue.add_argument(
        "--customer-tier",
        choices=("trial", "paid", "internal"),
        default=None,
        help=(
            "Optional concierge-onboarding tag (Wave 14b). Surfaced by "
            "`metis customer-report` / `metis trial-status` for headline "
            "framing. Not an entitlement field — the gateway does not "
            "gate behavior on tier."
        ),
    )
    issue.add_argument(
        "--db-path",
        help=(
            "SQLite path for the `gateway.key_issued` audit-event trace. "
            "Default: ~/.metis/metis.db (skip emission if the file isn't writable)."
        ),
        default=None,
    )

    revoke = gateway_sub.add_parser(
        "revoke-key",
        help="Mark a gateway key revoked; subsequent requests with it return 401 key_revoked.",
    )
    revoke.add_argument("key_id", help="The `gk_<ulid>` id of the key to revoke.")
    revoke.add_argument(
        "--keystore",
        help="Gateway keystore path. Default: ~/.metis/gateway/keys.json",
        default=None,
    )
    revoke.add_argument(
        "--db-path",
        help="SQLite path for the audit-event trace. Default: ~/.metis/metis.db",
        default=None,
    )

    rotate = gateway_sub.add_parser(
        "rotate-key",
        help=(
            "Issue a successor key inheriting an existing key's metadata; "
            "the predecessor stays active for the grace period, then auto-revokes."
        ),
    )
    rotate.add_argument("key_id", help="The `gk_<ulid>` id of the key to rotate.")
    rotate.add_argument(
        "--grace-period",
        default=None,
        help=(
            "How long the predecessor remains active alongside the successor. "
            "Forms: '30m', '24h', '7d', '2w'. Default: 24h."
        ),
    )
    rotate.add_argument(
        "--keystore",
        help="Gateway keystore path. Default: ~/.metis/gateway/keys.json",
        default=None,
    )
    rotate.add_argument(
        "--db-path",
        help="SQLite path for the audit-event trace. Default: ~/.metis/metis.db",
        default=None,
    )

    list_keys_parser = gateway_sub.add_parser(
        "list-keys",
        help="List every key in the keystore with status, identity, caps, and timestamps.",
    )
    list_keys_parser.add_argument(
        "--keystore",
        help="Gateway keystore path. Default: ~/.metis/gateway/keys.json",
        default=None,
    )
    list_keys_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format. `json` is machine-readable; `text` is a terminal-friendly table.",
    )

    # `metis billing` subcommands moved to the closed-source metis-pro
    # overlay (repo-split-plan.md §4.2b, 2026-05-18). Pro deployments
    # expose them via the `metis-pro` CLI.

    backup = sub.add_parser(
        "backup",
        help="Snapshot the trace DB to a single file (VACUUM INTO, WAL-safe).",
    )
    backup.add_argument("dest", help="Destination path for the backup file.")
    backup.add_argument(
        "--db-path",
        help="Source trace DB. Default: ~/.metis/metis.db",
        default=None,
    )

    trace = sub.add_parser(
        "trace",
        help="Trace-DB administrative operations (prune; future: stats / size).",
    )
    trace_sub = trace.add_subparsers(dest="trace_command", required=True)

    prune = trace_sub.add_parser(
        "prune",
        help=(
            "Delete trace events older than --days (default 90); preserves "
            "audit-flagged events. CLI defaults to apply; pass --dry-run "
            "to preview. See docs/specs/trace-retention.md."
        ),
    )
    prune.add_argument(
        "--days",
        type=int,
        default=90,
        help="Retention cutoff in days. Default: 90.",
    )
    prune.add_argument(
        "--db-path",
        help="Trace DB path. Default: ~/.metis/metis.db",
        default=None,
    )
    prune.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts without deleting any rows (no trace.swept event emitted).",
    )

    vacuum = trace_sub.add_parser(
        "vacuum",
        help=(
            "Reclaim free pages by rebuilding the DB in place (SQLite VACUUM). "
            "Run from a separate pod / CronJob — see "
            "docs/operations/trace-performance.md §4."
        ),
    )
    vacuum.add_argument(
        "--db-path",
        help="Trace DB path. Default: ~/.metis/metis.db",
        default=None,
    )

    audit = sub.add_parser(
        "audit",
        help="Audit-log operations (export the audit subset for SIEM ingest).",
    )
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_export = audit_sub.add_parser(
        "export",
        help="Export audit events in a window to JSONL or CSV (audit-log.md §9).",
    )
    audit_export.add_argument("dest", help="Destination path for the export file.")
    audit_export.add_argument(
        "--db-path",
        help="Source trace DB. Default: ~/.metis/metis.db",
        default=None,
    )
    audit_export.add_argument(
        "--format",
        choices=("jsonl", "csv"),
        default="jsonl",
        help="Export format. Default: jsonl.",
    )
    audit_export.add_argument(
        "--since",
        default=None,
        help="ISO 8601 UTC start of window (inclusive). Default: 7 days ago.",
    )
    audit_export.add_argument(
        "--until",
        default=None,
        help="ISO 8601 UTC end of window (exclusive). Default: now.",
    )
    audit_export.add_argument(
        "--event-type",
        action="append",
        default=None,
        dest="event_types",
        help="Restrict the export to a specific audit event type (repeat for multiple).",
    )
    audit_export.add_argument(
        "--redact",
        choices=("passthrough", "pseudonymize", "redact_private", "aggregate_only"),
        default="passthrough",
        help=(
            "Redaction mode (redaction.md §2). `pseudonymize` hashes "
            "identity fields; `redact_private` also strips PRIVATE-tier "
            "text; `aggregate_only` produces a single JSON rollup."
        ),
    )

    analytics = sub.add_parser(
        "analytics",
        help="Analytics admin subcommands (user-export, ...).",
    )
    analytics_sub = analytics.add_subparsers(dest="analytics_command", required=True)

    user_export = analytics_sub.add_parser(
        "user-export",
        help=(
            "Stream every trace event stamped with `user_id` as JSONL "
            "(GDPR / CCPA portability — analytics-api.md §4.10.1)."
        ),
    )
    user_export.add_argument(
        "user_id",
        help="Stable principal id whose events should be exported.",
    )
    user_export.add_argument(
        "--from",
        dest="from_",
        default=None,
        help="ISO 8601 UTC start of window (inclusive). Omit for all-time.",
    )
    user_export.add_argument(
        "--to",
        default=None,
        help="ISO 8601 UTC end of window (exclusive). Omit for all-time.",
    )
    user_export.add_argument(
        "--out",
        default=None,
        help="Output file path. Omit to stream to stdout (suitable for | jq).",
    )
    user_export.add_argument(
        "--db-path",
        default=None,
        help="Trace DB path. Default: ~/.metis/metis.db",
    )

    user = sub.add_parser(
        "user",
        help="User-admin subcommands (forget, ...).",
    )
    user_sub = user.add_subparsers(dest="user_command", required=True)

    user_forget = user_sub.add_parser(
        "forget",
        help=(
            "Pseudonymize every trace event stamped with `user_id` "
            "(GDPR / CCPA right-to-be-forgotten — analytics-api.md §4.10.2)."
        ),
    )
    user_forget.add_argument(
        "user_id",
        help="Stable principal id to forget.",
    )
    user_forget.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Required. Without this flag the command refuses to act.",
    )
    user_forget.add_argument(
        "--db-path",
        default=None,
        help="Trace DB path. Default: ~/.metis/metis.db",
    )

    restore = sub.add_parser(
        "restore",
        help="Restore a trace-DB backup over the live DB (schema-version checked).",
    )
    restore.add_argument("source", help="Backup file produced by `metis backup`.")
    restore.add_argument(
        "--db-path",
        help="Destination trace DB. Default: ~/.metis/metis.db",
        default=None,
    )
    restore.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing destination DB (default: refuse).",
    )

    customer_report = sub.add_parser(
        "customer-report",
        help=(
            "Generate an offline-share-able usage report (HTML / JSON) for a "
            "trial workspace over a window."
        ),
    )
    customer_report.add_argument(
        "--workspace",
        required=True,
        help="Workspace path the report describes (used for the header).",
    )
    customer_report.add_argument(
        "--since",
        default=None,
        help="ISO 8601 UTC start of window (inclusive). Default: 7 days ago.",
    )
    customer_report.add_argument(
        "--until",
        default=None,
        help="ISO 8601 UTC end of window (exclusive). Default: now.",
    )
    customer_report.add_argument(
        "--db-path",
        default=None,
        help="Trace DB path. Default: ~/.metis/metis.db.",
    )
    customer_report.add_argument(
        "--out",
        default=None,
        help="Output file path. Omit to stream to stdout (suitable for redirection).",
    )
    customer_report.add_argument(
        "--format",
        choices=("html", "json"),
        default="html",
        help="Output format. Default: html (self-contained, no JS).",
    )
    customer_report.add_argument(
        "--customer-label",
        default=None,
        help="Display name for the report header. Default: the workspace's basename.",
    )
    customer_report.add_argument(
        "--customer-tier",
        choices=("trial", "paid", "internal"),
        default=None,
        help=(
            "Optional concierge-onboarding tag; if set, surfaces as a badge "
            "on the report header. Otherwise omitted."
        ),
    )
    customer_report.add_argument(
        "--baseline",
        default="anthropic:claude-sonnet-4-6",
        help=(
            "Baseline model for the savings counterfactual. Default: anthropic:claude-sonnet-4-6."
        ),
    )
    customer_report.add_argument(
        "--anonymize",
        action="store_true",
        default=False,
        help=(
            "Replace customer labels, paths, gateway keys, users, and teams with "
            "deterministic placeholders for shareable case-study artifacts."
        ),
    )

    trial_status = sub.add_parser(
        "trial-status",
        help=(
            "Print spend / quality / days-into-trial / readiness for a trial workspace. Read-only."
        ),
    )
    trial_status.add_argument(
        "workspace",
        help="Workspace path (header only — trace lookup is per the DB).",
    )
    trial_status.add_argument(
        "--db-path",
        default=None,
        help="Trace DB path. Default: ~/.metis/metis.db.",
    )
    trial_status.add_argument(
        "--since",
        default=None,
        help=(
            "ISO 8601 UTC start of the trial. Default: --trial-length-days ago. "
            "Must be timezone-aware."
        ),
    )
    trial_status.add_argument(
        "--trial-length-days",
        type=int,
        default=7,
        help="Length of the trial window in days. Default: 7.",
    )
    trial_status.add_argument(
        "--baseline",
        default="anthropic:claude-sonnet-4-6",
        help=(
            "Baseline model for the savings counterfactual. Default: anthropic:claude-sonnet-4-6."
        ),
    )

    trial = sub.add_parser(
        "trial",
        help=(
            "Run a pre-baked buyer-trial workload (under benchmarks/workloads-trial/) "
            "and print actual / baseline / savings_pct. See docs/operations/quickstart.md."
        ),
    )
    trial.add_argument(
        "--workload",
        default="refactor-extract-helper",
        help="Trial workload name. Default: refactor-extract-helper.",
    )
    trial.add_argument(
        "--model",
        default="anthropic:claude-haiku-4-5",
        help="Actual model (canonical id or alias). Default: anthropic:claude-haiku-4-5.",
    )
    trial.add_argument(
        "--baseline",
        default="anthropic:claude-sonnet-4-6",
        help=(
            "Baseline model for the savings counterfactual (priced from the "
            "trial trace via the same PriceTable). Default: anthropic:claude-sonnet-4-6."
        ),
    )
    trial.add_argument(
        "--db-path",
        default=None,
        help=(
            "SQLite path for the trial's local trace DB. Default: a fresh "
            "/tmp/metis-trial-<UTC-ts>.db. Pass an explicit path to keep it."
        ),
    )
    trial.add_argument(
        "--gateway-url",
        default=None,
        help=(
            "Run through a gateway (e.g. http://127.0.0.1:8422). Sets "
            "ANTHROPIC_BASE_URL so the SDK routes through it. Per-key cost "
            "lands in the gateway's trace DB; the local DB is used for the "
            "savings counterfactual only. Requires --gateway-key."
        ),
    )
    trial.add_argument(
        "--gateway-key",
        default=None,
        help=(
            "Gateway-issued bearer token (gw_…). Replaces ANTHROPIC_API_KEY "
            "for the duration of the trial. Required with --gateway-url."
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    # Load .env from the current working directory (or any parent) before
    # we touch os.environ for API keys. Idempotent; no-op if no .env exists.
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    # Ctrl-C arrives while the REPL is awaiting `asyncio.to_thread(input, ...)`,
    # which means SIGINT can't be seen by the blocked worker thread. The
    # resulting KeyboardInterrupt propagates out of asyncio.run; catch it here
    # so the user sees a clean exit instead of a traceback. 130 is the
    # conventional SIGINT exit code.
    try:
        if args.command == "chat":
            return asyncio.run(
                run_chat(
                    workspace_path=args.workspace,
                    initial_model=args.model,
                    db_path=args.db_path,
                    global_default_model=args.global_default,
                )
            )
        if args.command == "tui":
            # Import lazily so users without the textual extra don't pay
            # import cost when running `metis chat`.
            from metis.cli.tui.app import run_tui

            return asyncio.run(
                run_tui(
                    workspace_path=args.workspace,
                    initial_model=args.model,
                    db_path=args.db_path,
                    global_default_model=args.global_default,
                )
            )
        if args.command == "serve":
            from metis.cli.serve import run_serve

            return asyncio.run(
                run_serve(
                    workspace_path=args.workspace,
                    db_path=args.db_path,
                    global_default_model=args.global_default,
                    host=args.host,
                    port=args.port,
                )
            )
        if args.command == "evaluate":
            from metis.core.eval import evaluate_main

            evaluate_argv: list[str] = ["--db-path", args.db_path, "--subject", args.subject]
            if args.since:
                evaluate_argv.extend(["--since", args.since])
            if args.until:
                evaluate_argv.extend(["--until", args.until])
            if args.session_id:
                evaluate_argv.extend(["--session-id", args.session_id])
            return evaluate_main(evaluate_argv)
        if args.command == "backup":
            from metis.cli.backup import run_backup_command

            return run_backup_command(dest=args.dest, db_path=args.db_path)
        if args.command == "restore":
            from metis.cli.backup import run_restore_command

            return run_restore_command(source=args.source, db_path=args.db_path, force=args.force)
        if args.command == "trial":
            from metis.cli.trial import run_trial_command

            return run_trial_command(
                workload=args.workload,
                model=args.model,
                baseline=args.baseline,
                db_path=args.db_path,
                gateway_url=args.gateway_url,
                gateway_key=args.gateway_key,
            )
        if args.command == "customer-report":
            from metis.cli.customer_report import run_customer_report_command

            return run_customer_report_command(
                workspace=args.workspace,
                since=args.since,
                until=args.until,
                db_path=args.db_path,
                output=args.out,
                format=args.format,
                customer_label=args.customer_label,
                customer_tier=args.customer_tier,
                baseline=args.baseline,
                anonymize=args.anonymize,
            )
        if args.command == "trial-status":
            from metis.cli.trial_status import run_trial_status_command

            return run_trial_status_command(
                workspace=args.workspace,
                db_path=args.db_path,
                since=args.since,
                trial_length_days=args.trial_length_days,
                baseline=args.baseline,
            )
        if args.command == "analytics":
            if args.analytics_command == "user-export":
                from metis.cli.user import run_user_export_command

                return run_user_export_command(
                    user_id=args.user_id,
                    from_=args.from_,
                    to=args.to,
                    out=args.out,
                    db_path=args.db_path,
                )
        if args.command == "user":
            if args.user_command == "forget":
                from metis.cli.user import run_user_forget_command

                return run_user_forget_command(
                    user_id=args.user_id,
                    confirm=args.confirm,
                    db_path=args.db_path,
                )
        if args.command == "audit":
            from metis.cli.audit import run_audit_export_command

            if args.audit_command == "export":
                return run_audit_export_command(
                    dest=args.dest,
                    db_path=args.db_path,
                    format=args.format,
                    since=args.since,
                    until=args.until,
                    event_types=args.event_types,
                    redact=args.redact,
                )
        if args.command == "trace":
            from metis.cli.trace_admin import run_trace_prune_command

            if args.trace_command == "prune":
                return run_trace_prune_command(
                    db_path=args.db_path,
                    days=args.days,
                    dry_run=args.dry_run,
                )
            if args.trace_command == "vacuum":
                from metis.cli.trace_admin import run_trace_vacuum_command

                return run_trace_vacuum_command(db_path=args.db_path)
        if args.command == "gateway":
            if args.gateway_command == "issue-key":
                from pathlib import Path

                from metis.gateway.issue_key import issue_key_command
                from metis.gateway.runtime import default_db_path, default_keystore_path

                keystore = (
                    Path(args.keystore).expanduser() if args.keystore else default_keystore_path()
                )
                allowed = tuple(args.allow_model) if args.allow_model else None
                audit_db = Path(args.db_path).expanduser() if args.db_path else default_db_path()
                return issue_key_command(
                    keystore_path=keystore,
                    name=args.name,
                    workspace_path=args.workspace,
                    allowed_models=allowed,
                    daily_cap_usd=args.daily_cap_usd,
                    monthly_cap_usd=args.monthly_cap_usd,
                    user_id=args.user,
                    team_id=args.team,
                    customer_tier=args.customer_tier,
                    db_path=audit_db,
                )
            if args.gateway_command == "revoke-key":
                from pathlib import Path

                from metis.gateway.keystore_admin import revoke_key_command
                from metis.gateway.runtime import default_db_path, default_keystore_path

                keystore = (
                    Path(args.keystore).expanduser() if args.keystore else default_keystore_path()
                )
                audit_db = Path(args.db_path).expanduser() if args.db_path else default_db_path()
                return revoke_key_command(
                    keystore_path=keystore,
                    key_id=args.key_id,
                    db_path=audit_db,
                )
            if args.gateway_command == "rotate-key":
                from pathlib import Path

                from metis.gateway.keystore_admin import rotate_key_command
                from metis.gateway.runtime import default_db_path, default_keystore_path

                keystore = (
                    Path(args.keystore).expanduser() if args.keystore else default_keystore_path()
                )
                audit_db = Path(args.db_path).expanduser() if args.db_path else default_db_path()
                return rotate_key_command(
                    keystore_path=keystore,
                    key_id=args.key_id,
                    grace_period=args.grace_period,
                    db_path=audit_db,
                )
            if args.gateway_command == "list-keys":
                from pathlib import Path

                from metis.gateway.keystore_admin import list_keys_command
                from metis.gateway.runtime import default_keystore_path

                keystore = (
                    Path(args.keystore).expanduser() if args.keystore else default_keystore_path()
                )
                return list_keys_command(
                    keystore_path=keystore,
                    output_format=args.format,
                )
            # Default: run the gateway server.
            from metis.gateway.cli import run_gateway_command

            return asyncio.run(
                run_gateway_command(
                    keystore_path=args.keystore,
                    db_path=args.db_path,
                    global_default_model=args.global_default,
                    host=args.host,
                    port=args.port,
                    tls_cert=args.tls_cert,
                    tls_key=args.tls_key,
                    max_connections=args.max_connections,
                    reuse_port=args.reuse_port,
                )
            )
    except KeyboardInterrupt:
        print()
        return 130
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
