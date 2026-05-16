"""Shared fixtures for the Wave 15 billing tests.

Builds a fully wired gateway app with billing enabled, against a
`FakeBillingClient` substrate so tests don't need `stripe` installed.
The fixture chain mirrors `test_signup.py`'s shape — a `signup_client`-
equivalent that also enables billing and exposes the underlying
`BillingService` for assertion.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from metis_gateway.app import build_app
from metis_gateway.billing import (
    BillingConfig,
    FakeBillingClient,
)
from metis_gateway.signup import MAGIC_LINK_TOKEN_PREFIX, SignupConfig

_MAGIC_LINK_TOKEN_RE = re.compile(r"url=\S+token=(" + MAGIC_LINK_TOKEN_PREFIX + r"\S+)")


@pytest.fixture
def fake_billing_client() -> FakeBillingClient:
    return FakeBillingClient(webhook_secret="whsec_test_fake")


@pytest.fixture
def billing_config(tmp_path: Path) -> BillingConfig:
    return BillingConfig(
        enabled=True,
        stripe_api_key="sk_test_fake",
        stripe_webhook_secret="whsec_test_fake",
        store_path=tmp_path / "billing.db",
        pro_price_id="price_test_pro",
        enterprise_metered_price_id="price_test_enterprise_metered",
        free_monthly_cap_usd=Decimal("5.00"),
        enterprise_savings_rate_pct=15,
    )


@pytest.fixture
def signup_config(tmp_path: Path) -> SignupConfig:
    return SignupConfig(
        enabled=True,
        accounts_path=tmp_path / "accounts.json",
        keystore_path=tmp_path / "keys.json",
        dashboard_base_url="http://127.0.0.1:8422",
    )


@pytest.fixture
async def billing_client_http(
    runtime,
    signup_config: SignupConfig,
    billing_config: BillingConfig,
    fake_billing_client: FakeBillingClient,
):
    """httpx client bound to the gateway with billing + signup enabled."""
    app = build_app(
        runtime,
        signup=signup_config,
        billing=billing_config,
        billing_client=fake_billing_client,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def signed_up_account(
    billing_client_http: httpx.AsyncClient,
    runtime,
    signup_config: SignupConfig,
    capsys,
) -> dict:
    """Sign up a verified account; return {account_id, session_token, key_id}.

    Mirrors `test_signup.py`'s pattern of consuming the stdout magic-link
    via capsys to drive the verify step. Also reloads the runtime's
    keystore from the signup-issued keystore file so the new key
    actually authenticates against the gateway's inbound HTTP path —
    test_signup.py never exercises this leg, so the keystore-merge isn't
    in its fixture.
    """
    resp = await billing_client_http.post(
        "/signup",
        json={"email": "alice@example.com", "workspace_name": "alpha"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    captured = capsys.readouterr().out
    match = _MAGIC_LINK_TOKEN_RE.search(captured)
    assert match is not None, f"no magic-link token in: {captured!r}"
    token = match.group(1)
    verify = await billing_client_http.post(
        "/signup/verify",
        json={"magic_link_token": token},
    )
    assert verify.status_code == 200, verify.text
    verify_body = verify.json()
    _merge_keystore_from_disk(runtime, signup_config.resolved_keystore_path())
    return {
        "account_id": body["account_id"],
        "session_token": verify_body["session_token"],
        "key_id": verify_body["key_id"],
        "key_token": verify_body["token"],
    }


def _merge_keystore_from_disk(runtime, keystore_path: Path) -> None:
    """Reload a Keystore from disk and merge into the runtime's in-memory store.

    Lets the test exercise the inbound HTTP auth path with a signup-issued
    key. The base runtime fixture wires a single in-memory key; this
    helper adds every key in `keystore_path` on top.
    """
    from metis_gateway.auth import Keystore

    if not keystore_path.exists():
        return
    fresh = Keystore.from_file(keystore_path)
    # `Keystore` has no public `add()`; tests reach into the private dicts
    # to avoid spelunking a new method onto the production API.
    for k in fresh.keys():
        runtime.keystore._by_hash[k.secret_hash] = k
        runtime.keystore._by_id[k.key_id] = k


@pytest.fixture
async def billing_client_with_keystore_refresh(
    billing_client_http,
    runtime,
    signup_config,
):
    """Convenience fixture for tests that issue keys mid-test and need them
    reflected on the runtime's keystore.
    """

    def _refresh() -> None:
        _merge_keystore_from_disk(runtime, signup_config.resolved_keystore_path())

    return billing_client_http, _refresh


def make_stripe_event(
    *,
    kind: str,
    event_id: str,
    object_data: dict,
    livemode: bool = False,
) -> bytes:
    """Build the raw JSON Stripe would POST for a given event."""
    body = {
        "id": event_id,
        "type": kind,
        "livemode": livemode,
        "data": {"object": object_data},
        "created": int(datetime.now(UTC).timestamp()),
    }
    return json.dumps(body).encode("utf-8")


def fetch_events_of_type(runtime, event_type: str) -> list[dict]:
    """Read every event of `event_type` from the trace DB as a list of
    decoded payload dicts. Used by tests that need to inspect payload
    fields beyond a count_by_type assertion.
    """
    cur = runtime.trace._conn.execute(  # type: ignore[attr-defined]
        "SELECT payload_json FROM events WHERE type = ? ORDER BY id",
        (event_type,),
    )
    return [json.loads(row[0]) for row in cur.fetchall()]
