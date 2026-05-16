# Billing operator guide

Audience: the operator running a billing-enabled Metis gateway.

Billing is opt-in. A deployment only mounts `/account/billing/*` and
`/webhooks/stripe` when both signup and billing are enabled. Signup owns
the account session; Stripe owns payment collection; Metis keeps a small
SQLite projection of the current account tier so gateway admission does
not call Stripe on every request.

## Runtime requirements

Start the gateway with signup and billing enabled:

```bash
metis gateway \
  --enable-signup \
  --enable-billing \
  --billing-stripe-api-key "$STRIPE_API_KEY" \
  --billing-stripe-webhook-secret "$STRIPE_WEBHOOK_SECRET" \
  --billing-pro-price-id price_... \
  --billing-enterprise-metered-price-id price_...
```

Billing state lives in `~/.metis/gateway/billing.db` by default. Override
it with `--billing-store-path` when running multiple environments on one
host. Keep the store on persistent disk; it contains no card data, but it
does contain the account-to-Stripe-customer mapping.

## Self-service endpoints

All account billing endpoints require the signup session bearer token:

```http
Authorization: Bearer sess_...
```

- `GET /account/billing` returns the local billing summary, payment grace
  state, payment method display fields, and the configured Free-tier cap.
- `GET /account/billing/portal` creates a Stripe Customer Portal session
  and returns `{ "url": "https://..." }`. If the account has no Stripe
  customer yet, Metis creates one first and emits `billing.customer_created`.
- `POST /account/billing/plan` changes plans:
  - `{ "plan": "free" }` immediately cancels the subscription and drops the
    account to Free.
  - `{ "plan": "pro", "seats": 3, "payment_method_id": "pm_..." }` creates
    or updates the per-seat Pro subscription.
  - `{ "plan": "enterprise", "seats": 12 }` keeps the Pro seat line and
    attaches the configured metered Enterprise item.

Plan changes reuse the existing audit event set:

- `billing.subscription_created` for first paid subscription creation.
- `billing.subscription_updated` for seat changes and Pro/Enterprise moves.
- `billing.subscription_canceled` for downgrades to Free.

## Free-tier cap

The Free tier is usable but bounded. `BillingConfig.free_monthly_cap_usd`
defaults to `5.00`; `free_daily_cap_usd` is optional. The gateway applies
that cap across every key owned by the signup account, so issuing more keys
does not create more Free-tier headroom.

Paid Pro and Enterprise accounts are unlimited at the billing-tier layer.
Per-key, per-user, per-team, and per-workspace caps still apply when they
are configured.

## Failed payments

Stripe sends `invoice.payment_failed` when collection fails. Metis records
`billing.invoice_payment_failed`, marks the subscription `past_due`, and
starts a seven-day grace period (`BillingConfig.failed_payment_grace_days`).

During grace:

- The account keeps its paid tier.
- `GET /account/billing` reports `payment_state: "grace"` and
  `payment_grace_until`.

After grace expires:

- The next billing summary or gateway request applies the local freeze.
- The account's effective tier becomes Free.
- Existing paid subscription metadata is retained as `subscription_tier`.
- If the account has already spent past the Free cap, normal quota
  enforcement returns `429 quota_exceeded`.
- Metis emits `billing.subscription_updated` with `tier: "free"` for the
  local entitlement transition.

When Stripe later sends `invoice.payment_succeeded`, Metis emits
`billing.invoice_paid`, restores the stored paid tier, and clears the grace
and frozen markers.

## Webhook setup

Point Stripe at:

```text
POST https://<gateway-host>/webhooks/stripe
```

Enable these event types:

- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.payment_succeeded`
- `invoice.payment_failed`

Replay safety is local: processed Stripe event ids are recorded in
`processed_events`, so retries return `{"status": "duplicate"}` without
re-running side effects.
