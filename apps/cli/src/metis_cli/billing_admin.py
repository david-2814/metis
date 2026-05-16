"""Operator-side `metis billing` subcommands.

  metis billing status --account-id <id>
  metis billing usage-record --account-id <id> --savings-usd <amount> \\
      --stripe-api-key sk_test_...

These are read/write surfaces an operator runs out-of-band — they
don't require the gateway server to be running. Both open a private
`BillingStore` connection against `~/.metis/gateway/billing.db` (or
`--store-path`) and emit a human-readable summary.

`usage-record` is the manual fallback for the Enterprise add-on's
metered Stripe usage records; the recurring sweep that posts these
automatically against `AnalyticsStore.savings()` is a Wave 16
follow-on (it needs the period-anchor calendar logic + retry policy
that production billing systems require).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from metis_gateway.billing.client import StripeBillingClient
from metis_gateway.billing.config import BillingConfig
from metis_gateway.billing.store import BillingStore
from metis_gateway.billing.subscriptions import BillingError, BillingService


def _default_store_path() -> Path:
    return Path.home() / ".metis" / "gateway" / "billing.db"


def _resolve_store_path(store_path: str | None) -> Path:
    return Path(store_path).expanduser() if store_path else _default_store_path()


def run_billing_status_command(*, account_id: str, store_path: str | None) -> int:
    path = _resolve_store_path(store_path)
    if not path.exists():
        print(f"error: billing store not found at {path}", file=sys.stderr)
        return 1
    store = BillingStore(path)
    try:
        customer = store.get_customer(account_id)
        if customer is None:
            print(f"no billing record for account {account_id}")
            return 0
        sub = store.get_subscription(account_id)
        print(f"account_id           : {customer.account_id}")
        print(f"tier                 : {customer.tier}")
        print(f"stripe_customer_id   : {customer.stripe_customer_id}")
        print(f"created_at           : {customer.created_at.isoformat()}")
        if sub is None:
            print("subscription         : (none)")
        else:
            print(f"subscription_id      : {sub.stripe_subscription_id}")
            print(f"  status             : {sub.status}")
            print(f"  pro_seats          : {sub.pro_seats}")
            print(f"  enterprise_addon   : {sub.enterprise_metered_item_id is not None}")
            print(f"  current_period_end : {sub.current_period_end.isoformat()}")
            print(f"  cancel_at_period_end: {sub.cancel_at_period_end}")
            print(f"  pause_collection   : {sub.pause_collection}")
    finally:
        store.close()
    return 0


def run_billing_usage_record_command(
    *,
    account_id: str,
    savings_usd: float,
    stripe_api_key: str,
    stripe_webhook_secret: str,
    store_path: str | None,
    enterprise_savings_rate_pct: int,
) -> int:
    path = _resolve_store_path(store_path)
    if not path.exists():
        print(f"error: billing store not found at {path}", file=sys.stderr)
        return 1
    config = BillingConfig(
        enabled=True,
        stripe_api_key=stripe_api_key,
        stripe_webhook_secret=stripe_webhook_secret,
        enterprise_savings_rate_pct=enterprise_savings_rate_pct,
        store_path=path,
    )
    try:
        client = StripeBillingClient(
            api_key=stripe_api_key,
            webhook_secret=stripe_webhook_secret,
        )
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    store = BillingStore(path)
    # `metis billing usage-record` is a one-shot CLI; the bus isn't
    # connected to a TraceStore here so events go to a no-op bus. The
    # Stripe-side audit trail is still produced (Stripe records every
    # usage record on its own dashboard).
    from metis_core.events.bus import EventBus

    bus = EventBus()
    bus.start()
    service = BillingService(config=config, client=client, store=store, bus=bus)
    try:
        cents = service.record_savings_usage(
            account_id=account_id,
            savings_usd=Decimal(str(savings_usd)),
            period_anchor=datetime.now(UTC),
        )
    except BillingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(
        f"posted metered usage record: account={account_id} "
        f"savings_usd={savings_usd:.2f} -> {cents} cents at "
        f"{enterprise_savings_rate_pct}% rate"
    )
    return 0


__all__ = [
    "run_billing_status_command",
    "run_billing_usage_record_command",
]
