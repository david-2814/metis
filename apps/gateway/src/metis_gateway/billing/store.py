"""BillingStore — local SQLite persistence for billing state.

Three tables:

  customer_records:
    account_id (PK) ↔ stripe_customer_id ↔ tier ↔ email_sha256
  subscription_records:
    account_id (FK) ↔ stripe_subscription_id ↔ status ↔ tier ↔ seats
    + item ids keyed by role (pro_seat / enterprise_metered)
  processed_events:
    stripe_event_id (PK) — webhook idempotency log

The store is single-writer; concurrent reads are safe under WAL. Writes
happen from the webhook handler and from `/account/billing` mutations
on the request thread. The store is *not* the trace store; audit events
ride the existing event bus and land in the trace DB alongside the
rest of the audit subset (per audit-log.md §5.1).

The schema is intentionally small — Stripe owns the source of truth for
amounts, due dates, line items, and dunning state. The local store is
just enough to answer "what tier is this account on?" without making
a Stripe API call on every gateway request.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

BillingTier = Literal["free", "pro", "enterprise"]
SubscriptionStatus = Literal[
    "trialing",
    "active",
    "past_due",
    "unpaid",
    "canceled",
    "incomplete",
    "incomplete_expired",
    "paused",
]


@dataclass(frozen=True)
class CustomerRecord:
    account_id: str
    stripe_customer_id: str
    tier: BillingTier
    email_sha256: str
    created_at: datetime


@dataclass(frozen=True)
class SubscriptionRecord:
    account_id: str
    stripe_subscription_id: str
    tier: BillingTier
    status: SubscriptionStatus
    pro_seats: int
    pro_item_id: str
    enterprise_metered_item_id: str | None
    current_period_end: datetime
    cancel_at_period_end: bool
    pause_collection: bool
    created_at: datetime
    updated_at: datetime
    payment_failed_at: datetime | None = None
    payment_grace_until: datetime | None = None
    access_frozen_at: datetime | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS customer_records (
    account_id           TEXT PRIMARY KEY,
    stripe_customer_id   TEXT NOT NULL UNIQUE,
    tier                 TEXT NOT NULL,
    email_sha256         TEXT NOT NULL,
    created_at_us        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customer_records_stripe_id
    ON customer_records (stripe_customer_id);

CREATE TABLE IF NOT EXISTS subscription_records (
    account_id                   TEXT PRIMARY KEY,
    stripe_subscription_id       TEXT NOT NULL UNIQUE,
    tier                         TEXT NOT NULL,
    status                       TEXT NOT NULL,
    pro_seats                    INTEGER NOT NULL,
    pro_item_id                  TEXT NOT NULL,
    enterprise_metered_item_id   TEXT,
    current_period_end_us        INTEGER NOT NULL,
    cancel_at_period_end         INTEGER NOT NULL,
    pause_collection             INTEGER NOT NULL,
    created_at_us                INTEGER NOT NULL,
    updated_at_us                INTEGER NOT NULL,
    payment_failed_at_us         INTEGER,
    payment_grace_until_us       INTEGER,
    access_frozen_at_us          INTEGER,
    FOREIGN KEY (account_id) REFERENCES customer_records (account_id)
);
CREATE INDEX IF NOT EXISTS idx_subscription_records_subscription_id
    ON subscription_records (stripe_subscription_id);

CREATE TABLE IF NOT EXISTS processed_events (
    stripe_event_id   TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    processed_at_us   INTEGER NOT NULL
);
"""


class BillingStore:
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()

    def close(self) -> None:
        self._conn.close()

    def _migrate_schema(self) -> None:
        """Apply additive migrations for stores created by earlier waves."""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(subscription_records)").fetchall()
        }
        migrations = {
            "payment_failed_at_us": "ALTER TABLE subscription_records ADD COLUMN payment_failed_at_us INTEGER",
            "payment_grace_until_us": "ALTER TABLE subscription_records ADD COLUMN payment_grace_until_us INTEGER",
            "access_frozen_at_us": "ALTER TABLE subscription_records ADD COLUMN access_frozen_at_us INTEGER",
        }
        for column, sql in migrations.items():
            if column not in columns:
                self._conn.execute(sql)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # --- Customer records ----------------------------------------------

    def upsert_customer(self, record: CustomerRecord) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO customer_records
                    (account_id, stripe_customer_id, tier, email_sha256, created_at_us)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    stripe_customer_id = excluded.stripe_customer_id,
                    tier = excluded.tier,
                    email_sha256 = excluded.email_sha256
                """,
                (
                    record.account_id,
                    record.stripe_customer_id,
                    record.tier,
                    record.email_sha256,
                    _to_us(record.created_at),
                ),
            )

    def get_customer(self, account_id: str) -> CustomerRecord | None:
        row = self._conn.execute(
            "SELECT * FROM customer_records WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_customer(row)

    def get_customer_by_stripe_id(self, stripe_customer_id: str) -> CustomerRecord | None:
        row = self._conn.execute(
            "SELECT * FROM customer_records WHERE stripe_customer_id = ?",
            (stripe_customer_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_customer(row)

    def set_tier(self, account_id: str, tier: BillingTier) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE customer_records SET tier = ? WHERE account_id = ?",
                (tier, account_id),
            )

    # --- Subscription records -------------------------------------------

    def upsert_subscription(self, record: SubscriptionRecord) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO subscription_records
                    (account_id, stripe_subscription_id, tier, status, pro_seats,
                     pro_item_id, enterprise_metered_item_id,
                     current_period_end_us, cancel_at_period_end, pause_collection,
                     created_at_us, updated_at_us,
                     payment_failed_at_us, payment_grace_until_us, access_frozen_at_us)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    stripe_subscription_id = excluded.stripe_subscription_id,
                    tier = excluded.tier,
                    status = excluded.status,
                    pro_seats = excluded.pro_seats,
                    pro_item_id = excluded.pro_item_id,
                    enterprise_metered_item_id = excluded.enterprise_metered_item_id,
                    current_period_end_us = excluded.current_period_end_us,
                    cancel_at_period_end = excluded.cancel_at_period_end,
                    pause_collection = excluded.pause_collection,
                    updated_at_us = excluded.updated_at_us,
                    payment_failed_at_us = excluded.payment_failed_at_us,
                    payment_grace_until_us = excluded.payment_grace_until_us,
                    access_frozen_at_us = excluded.access_frozen_at_us
                """,
                (
                    record.account_id,
                    record.stripe_subscription_id,
                    record.tier,
                    record.status,
                    record.pro_seats,
                    record.pro_item_id,
                    record.enterprise_metered_item_id,
                    _to_us(record.current_period_end),
                    1 if record.cancel_at_period_end else 0,
                    1 if record.pause_collection else 0,
                    _to_us(record.created_at),
                    _to_us(record.updated_at),
                    _to_us_optional(record.payment_failed_at),
                    _to_us_optional(record.payment_grace_until),
                    _to_us_optional(record.access_frozen_at),
                ),
            )

    def get_subscription(self, account_id: str) -> SubscriptionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM subscription_records WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_subscription(row)

    def get_subscription_by_stripe_id(
        self, stripe_subscription_id: str
    ) -> SubscriptionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM subscription_records WHERE stripe_subscription_id = ?",
            (stripe_subscription_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_subscription(row)

    def list_subscriptions(self) -> list[SubscriptionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM subscription_records ORDER BY account_id"
        ).fetchall()
        return [_row_to_subscription(r) for r in rows]

    def delete_subscription(self, account_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM subscription_records WHERE account_id = ?",
                (account_id,),
            )

    # --- Processed events (webhook idempotency) -------------------------

    def has_processed(self, stripe_event_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_events WHERE stripe_event_id = ?",
            (stripe_event_id,),
        ).fetchone()
        return row is not None

    def mark_processed(self, *, stripe_event_id: str, kind: str, processed_at: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_events
                    (stripe_event_id, kind, processed_at_us)
                VALUES (?, ?, ?)
                """,
                (stripe_event_id, kind, _to_us(processed_at)),
            )


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _to_us(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = dt - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _to_us_optional(dt: datetime | None) -> int | None:
    return _to_us(dt) if dt is not None else None


def _from_us(value: int) -> datetime:
    seconds, micros = divmod(value, 1_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=micros)


def _from_us_optional(value: int | None) -> datetime | None:
    return _from_us(value) if value is not None else None


def _row_to_customer(row: sqlite3.Row) -> CustomerRecord:
    return CustomerRecord(
        account_id=row["account_id"],
        stripe_customer_id=row["stripe_customer_id"],
        tier=row["tier"],
        email_sha256=row["email_sha256"],
        created_at=_from_us(row["created_at_us"]),
    )


def _row_to_subscription(row: sqlite3.Row) -> SubscriptionRecord:
    return SubscriptionRecord(
        account_id=row["account_id"],
        stripe_subscription_id=row["stripe_subscription_id"],
        tier=row["tier"],
        status=row["status"],
        pro_seats=row["pro_seats"],
        pro_item_id=row["pro_item_id"],
        enterprise_metered_item_id=row["enterprise_metered_item_id"],
        current_period_end=_from_us(row["current_period_end_us"]),
        cancel_at_period_end=bool(row["cancel_at_period_end"]),
        pause_collection=bool(row["pause_collection"]),
        created_at=_from_us(row["created_at_us"]),
        updated_at=_from_us(row["updated_at_us"]),
        payment_failed_at=_from_us_optional(row["payment_failed_at_us"]),
        payment_grace_until=_from_us_optional(row["payment_grace_until_us"]),
        access_frozen_at=_from_us_optional(row["access_frozen_at_us"]),
    )


__all__ = [
    "BillingStore",
    "BillingTier",
    "CustomerRecord",
    "SubscriptionRecord",
    "SubscriptionStatus",
]
