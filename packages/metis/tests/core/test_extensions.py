"""Contract tests for the repo-split extension Protocols.

These tests are the safety net for the OSS / `metis-pro` boundary:
they verify the four Protocols exist with the documented method shapes,
that the Noop defaults satisfy them, and that a ``FakePro`` implementation
built outside ``metis-core`` also satisfies them. They catch accidental
Protocol changes before ``metis-pro`` consumes a new OSS version.

If a method signature changes here without a coordinated PR in
``metis-pro``, this is the gate that catches it.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from metis.core.extensions import (
    AnalyticsExtension,
    BillingBackend,
    JudgeRubricProvider,
    NoopAnalyticsExtension,
    NoopBillingBackend,
    NoopJudgeRubricProvider,
    NoopSignupBackend,
    SignupBackend,
)

# ----------------------------------------------------------------------------
# FakePro — a minimal "Pro-like" implementation kept inside the OSS test
# suite. Its job is to act as the contract proxy for metis-pro. If a
# Protocol method signature changes, the FakePro will fail to satisfy
# isinstance() and these tests trip — exactly what we want.
# ----------------------------------------------------------------------------


class FakeProBilling:
    """Minimal stand-in for ``metis_pro.billing.StripeBillingBackend``."""

    def __init__(self) -> None:
        self.recorded: list[tuple[str, Decimal]] = []
        self.tier_for: dict[str, str] = {}
        self.registered = False

    async def record_usage(self, account_id: str, savings_usd: Decimal) -> None:
        self.recorded.append((account_id, savings_usd))

    async def check_active(self, account_id: str) -> bool:
        return account_id != "lapsed-account"

    async def current_tier(self, account_id: str) -> str:
        return self.tier_for.get(account_id, "free")

    def register_routes(self, app):
        self.registered = True


class FakeProSignup:
    """Minimal stand-in for ``metis_pro.signup.MagicLinkSignupBackend``."""

    def __init__(self) -> None:
        self.registered = False

    def register_routes(self, app):
        self.registered = True


class FakeProAnalytics:
    """Minimal stand-in for ``metis_pro.analytics_overlays.ProAnalyticsRoutes``."""

    def __init__(self) -> None:
        self.registered = False

    def register_routes(self, app):
        self.registered = True


class FakeProJudgeRubrics:
    """Minimal stand-in for ``metis_pro.judges.CuratedRubricLibrary``."""

    def __init__(self, prompts: dict[tuple[str, str | None], str] | None = None) -> None:
        self.prompts = prompts or {}

    def rubric_for(self, subject_kind: str, workload_id: str | None) -> str | None:
        return self.prompts.get((subject_kind, workload_id))

    def rubric_version(self) -> str:
        return "fake-pro-1.0"


# ----------------------------------------------------------------------------
# Protocol satisfaction — runtime_checkable isinstance tests.
# ----------------------------------------------------------------------------


def test_noop_billing_satisfies_billing_backend() -> None:
    assert isinstance(NoopBillingBackend(), BillingBackend)


def test_noop_signup_satisfies_signup_backend() -> None:
    assert isinstance(NoopSignupBackend(), SignupBackend)


def test_noop_analytics_satisfies_analytics_extension() -> None:
    assert isinstance(NoopAnalyticsExtension(), AnalyticsExtension)


def test_noop_judge_rubrics_satisfies_judge_rubric_provider() -> None:
    assert isinstance(NoopJudgeRubricProvider(), JudgeRubricProvider)


def test_fake_pro_billing_satisfies_billing_backend() -> None:
    assert isinstance(FakeProBilling(), BillingBackend)


def test_fake_pro_signup_satisfies_signup_backend() -> None:
    assert isinstance(FakeProSignup(), SignupBackend)


def test_fake_pro_analytics_satisfies_analytics_extension() -> None:
    assert isinstance(FakeProAnalytics(), AnalyticsExtension)


def test_fake_pro_judge_rubrics_satisfies_judge_rubric_provider() -> None:
    assert isinstance(FakeProJudgeRubrics(), JudgeRubricProvider)


# ----------------------------------------------------------------------------
# Noop semantics — the OSS-standalone-usable invariant.
# These pin the default behavior the gateway / server / evaluator can rely on
# in the absence of a Pro overlay.
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_billing_records_nothing_and_returns_free_active() -> None:
    backend = NoopBillingBackend()
    await backend.record_usage("any-account", Decimal("12.34"))  # no exception, no state
    assert await backend.check_active("any-account") is True
    assert await backend.current_tier("any-account") == "free"
    # No routes mounted — free deployments don't expose /account/billing/*.
    sentinel = object()
    assert backend.register_routes(sentinel) is None  # type: ignore[arg-type]


def test_noop_signup_register_routes_is_a_noop() -> None:
    sentinel = object()
    # The noop deliberately accepts anything — it never inspects the arg.
    result = NoopSignupBackend().register_routes(sentinel)  # type: ignore[arg-type]
    assert result is None


def test_noop_analytics_register_routes_is_a_noop() -> None:
    sentinel = object()
    result = NoopAnalyticsExtension().register_routes(sentinel)  # type: ignore[arg-type]
    assert result is None


def test_noop_judge_rubrics_returns_none_and_stable_version() -> None:
    provider = NoopJudgeRubricProvider()
    assert provider.rubric_for("turn", "fix-a-bug-small") is None
    assert provider.rubric_for("workload", None) is None
    assert provider.rubric_version() == "noop-1.0"


# ----------------------------------------------------------------------------
# FakePro behavioral round-trip — verifies the Protocol's contract is
# expressive enough for a real implementation to do useful work.
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_pro_billing_records_usage_and_reports_tier() -> None:
    backend = FakeProBilling()
    backend.tier_for["acc-pro-1"] = "pro"
    backend.tier_for["acc-ent-1"] = "enterprise"

    await backend.record_usage("acc-pro-1", Decimal("0.05"))
    await backend.record_usage("acc-ent-1", Decimal("12.50"))

    assert backend.recorded == [
        ("acc-pro-1", Decimal("0.05")),
        ("acc-ent-1", Decimal("12.50")),
    ]
    assert await backend.current_tier("acc-pro-1") == "pro"
    assert await backend.current_tier("acc-ent-1") == "enterprise"
    assert await backend.current_tier("acc-free-1") == "free"  # fallback
    assert await backend.check_active("acc-pro-1") is True
    assert await backend.check_active("lapsed-account") is False

    # Boot-time route registration is observable via the side-effect flag.
    assert backend.registered is False
    backend.register_routes(object())
    assert backend.registered is True


def test_fake_pro_judge_rubrics_returns_workload_specific_prompts() -> None:
    provider = FakeProJudgeRubrics(
        prompts={
            ("workload", "regex-with-edge-cases"): "Score on PASS N/N parsing...",
            ("turn", None): "Evaluate the turn for correctness...",
        }
    )
    assert provider.rubric_for("workload", "regex-with-edge-cases") == (
        "Score on PASS N/N parsing..."
    )
    assert provider.rubric_for("turn", None) == "Evaluate the turn for correctness..."
    assert provider.rubric_for("session", "anything") is None
    assert provider.rubric_version() == "fake-pro-1.0"


# ----------------------------------------------------------------------------
# Signature pinning — catches accidental Protocol changes that would
# silently break metis-pro consumers.
# ----------------------------------------------------------------------------


def test_billing_backend_method_signatures_are_stable() -> None:
    record = inspect.signature(BillingBackend.record_usage)  # type: ignore[arg-type]
    assert list(record.parameters)[1:] == ["account_id", "savings_usd"]
    check = inspect.signature(BillingBackend.check_active)  # type: ignore[arg-type]
    assert list(check.parameters)[1:] == ["account_id"]
    tier = inspect.signature(BillingBackend.current_tier)  # type: ignore[arg-type]
    assert list(tier.parameters)[1:] == ["account_id"]
    register = inspect.signature(BillingBackend.register_routes)  # type: ignore[arg-type]
    assert list(register.parameters)[1:] == ["app"]


def test_judge_rubric_provider_method_signatures_are_stable() -> None:
    rubric = inspect.signature(JudgeRubricProvider.rubric_for)  # type: ignore[arg-type]
    assert list(rubric.parameters)[1:] == ["subject_kind", "workload_id"]
    version = inspect.signature(JudgeRubricProvider.rubric_version)  # type: ignore[arg-type]
    assert list(version.parameters)[1:] == []


def test_signup_and_analytics_register_routes_signatures_are_stable() -> None:
    signup = inspect.signature(SignupBackend.register_routes)  # type: ignore[arg-type]
    assert list(signup.parameters)[1:] == ["app"]
    analytics = inspect.signature(AnalyticsExtension.register_routes)  # type: ignore[arg-type]
    assert list(analytics.parameters)[1:] == ["app"]
