"""Unit tests for BillingStore — SQLite persistence layer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from metis_gateway.billing import BillingStore, CustomerRecord, SubscriptionRecord


def _customer(account_id: str = "acc_1", tier: str = "free") -> CustomerRecord:
    return CustomerRecord(
        account_id=account_id,
        stripe_customer_id=f"cus_{account_id}",
        tier=tier,  # type: ignore[arg-type]
        email_sha256="a" * 64,
        created_at=datetime(2026, 5, 16, 12, 0, tzinfo=UTC),
    )


def _subscription(account_id: str = "acc_1") -> SubscriptionRecord:
    return SubscriptionRecord(
        account_id=account_id,
        stripe_subscription_id=f"sub_{account_id}",
        tier="pro",
        status="active",
        pro_seats=2,
        pro_item_id="si_pro",
        enterprise_metered_item_id=None,
        current_period_end=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
        cancel_at_period_end=False,
        pause_collection=False,
        created_at=datetime(2026, 5, 16, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 16, 12, 0, tzinfo=UTC),
    )


def test_upsert_and_get_customer(tmp_path: Path) -> None:
    store = BillingStore(tmp_path / "billing.db")
    try:
        cust = _customer()
        store.upsert_customer(cust)
        loaded = store.get_customer("acc_1")
        assert loaded is not None
        assert loaded.stripe_customer_id == "cus_acc_1"
        assert loaded.tier == "free"
    finally:
        store.close()


def test_upsert_customer_updates_tier(tmp_path: Path) -> None:
    store = BillingStore(tmp_path / "billing.db")
    try:
        store.upsert_customer(_customer(tier="free"))
        store.set_tier("acc_1", "pro")
        loaded = store.get_customer("acc_1")
        assert loaded is not None
        assert loaded.tier == "pro"
    finally:
        store.close()


def test_get_customer_by_stripe_id(tmp_path: Path) -> None:
    store = BillingStore(tmp_path / "billing.db")
    try:
        store.upsert_customer(_customer())
        loaded = store.get_customer_by_stripe_id("cus_acc_1")
        assert loaded is not None
        assert loaded.account_id == "acc_1"
    finally:
        store.close()


def test_upsert_subscription_round_trip(tmp_path: Path) -> None:
    store = BillingStore(tmp_path / "billing.db")
    try:
        store.upsert_customer(_customer())
        store.upsert_subscription(_subscription())
        loaded = store.get_subscription("acc_1")
        assert loaded is not None
        assert loaded.pro_seats == 2
        assert loaded.status == "active"
    finally:
        store.close()


def test_processed_event_idempotency(tmp_path: Path) -> None:
    store = BillingStore(tmp_path / "billing.db")
    try:
        now = datetime.now(UTC)
        assert store.has_processed("evt_1") is False
        store.mark_processed(
            stripe_event_id="evt_1", kind="invoice.payment_succeeded", processed_at=now
        )
        assert store.has_processed("evt_1") is True
        # Idempotent second mark — doesn't raise on UNIQUE conflict.
        store.mark_processed(
            stripe_event_id="evt_1", kind="invoice.payment_succeeded", processed_at=now
        )
        assert store.has_processed("evt_1") is True
    finally:
        store.close()


def test_persists_across_open_close(tmp_path: Path) -> None:
    path = tmp_path / "billing.db"
    s1 = BillingStore(path)
    try:
        s1.upsert_customer(_customer())
        s1.upsert_subscription(_subscription())
    finally:
        s1.close()
    s2 = BillingStore(path)
    try:
        assert s2.get_customer("acc_1") is not None
        assert s2.get_subscription("acc_1") is not None
    finally:
        s2.close()


def test_delete_subscription_keeps_customer(tmp_path: Path) -> None:
    store = BillingStore(tmp_path / "billing.db")
    try:
        store.upsert_customer(_customer())
        store.upsert_subscription(_subscription())
        store.delete_subscription("acc_1")
        assert store.get_subscription("acc_1") is None
        assert store.get_customer("acc_1") is not None
    finally:
        store.close()


def test_list_subscriptions(tmp_path: Path) -> None:
    store = BillingStore(tmp_path / "billing.db")
    try:
        store.upsert_customer(_customer("acc_1"))
        store.upsert_customer(_customer("acc_2"))
        store.upsert_subscription(_subscription("acc_1"))
        store.upsert_subscription(_subscription("acc_2"))
        all_subs = store.list_subscriptions()
        assert len(all_subs) == 2
    finally:
        store.close()
