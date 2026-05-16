"""BillingConfig — opt-in billing posture on `GatewayConfig`.

When `enabled=False` (the default), `build_app` does not mount the
`/account/billing/*` routes or the `/webhooks/stripe` listener and the
gateway is byte-identical to pre-Wave-15. When `enabled=True`, the
Stripe client + BillingStore + service are instantiated and the routes
are mounted alongside the existing `/account/keys` endpoints.

The Stripe API key and webhook secret are not stored in the config
directly — they're passed in via the helm `Secret` template at
deployment time. The config carries the `stripe_api_key` and
`stripe_webhook_secret` *values* (resolved by the CLI) so the rest of
the module stays unit-testable without env-var fiddling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


@dataclass
class BillingConfig:
    """Per-deployment billing posture (off by default).

    Price ids:
    --------
    `pro_price_id` — the `Price` resource id for the per-seat Pro line.
    `enterprise_metered_price_id` — the metered `Price` for the
    %-of-savings add-on. `None` when the deployment doesn't offer the
    Enterprise tier (just Free + Pro).

    Default caps:
    --------
    `free_monthly_cap_usd` — the Free-tier monthly spend cap composed
    with the existing per-key/user/team quotas. `5.0` per pricing.md
    §5.5.4 "free-tier spend cap floor" framing. Operators tune this
    via helm values.

    Cents-of-savings meter:
    --------
    `enterprise_savings_rate_pct` is the cut Metis takes; the metered
    quantity recorded against Stripe is
    `int(actual_savings_usd * 100 * rate)`. Default `15` matches
    pricing.md §5.3 examples, but is configurable per contract.
    """

    enabled: bool = False
    stripe_api_key: str | None = None
    stripe_webhook_secret: str | None = None
    store_path: Path | None = None
    pro_price_id: str = "price_pro_seat_monthly"
    enterprise_metered_price_id: str | None = None
    free_monthly_cap_usd: Decimal = field(default_factory=lambda: Decimal("5.00"))
    free_daily_cap_usd: Decimal | None = None
    enterprise_savings_rate_pct: int = 15

    def resolved_store_path(self) -> Path:
        return (self.store_path or _default_store_path()).expanduser()


def _default_store_path() -> Path:
    return Path.home() / ".metis" / "gateway" / "billing.db"


__all__ = ["BillingConfig"]
