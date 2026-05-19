"""Extension Protocols for metis-pro overlays.

The paid-tier Metis features (billing, signup, hosted analytics endpoints,
curated rubric library) live in a private ``metis-pro`` repo that injects
implementations at boot. OSS deployments use the noop defaults defined below;
Pro deployments swap them for real implementations via the gateway / server
composition root.

Two Protocols (``SignupBackend``, ``AnalyticsExtension``) reference
``starlette.applications.Starlette`` so the overlay can mount HTTP routes onto
the gateway / server's ASGI app. The Starlette import is ``TYPE_CHECKING``-only
— ``metis-core`` does NOT take a runtime dependency on Starlette. The reference
is purely structural; only ``metis-gateway`` / ``metis-server`` (which both
already depend on Starlette) ever pass a real Starlette app into a Protocol
method.

Invariant: every Protocol here has a Noop default that lets the OSS substrate
boot and serve traffic without metis-pro. The "OSS standalone-usable" rule is
enforced via the contract tests in ``tests/test_extensions.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover — type-only import
    from starlette.applications import Starlette


# ----------------------------------------------------------------------------
# Protocols — implemented by metis-pro overlays.
# ----------------------------------------------------------------------------


@runtime_checkable
class BillingBackend(Protocol):
    """Optional billing surface for the Pro / Enterprise tiers.

    The OSS noop default records nothing, reports every account active,
    returns ``"free"`` as the tier, and mounts no routes. ``metis-pro``
    provides a Stripe-backed implementation. The gateway calls into the
    Protocol at request entry (``check_active`` / ``current_tier`` for
    tier-axis quota composition), at turn-completion time (``record_usage``
    driven by the savings counterfactual from ``analytics-api.md §4.7``),
    and once at boot (``register_routes`` to mount ``/account/billing/*``
    + ``/webhooks/stripe`` onto the gateway's Starlette app).
    """

    async def record_usage(self, account_id: str, savings_usd: Decimal) -> None:
        """Record metered savings for the enterprise %-of-savings line item.

        Noop semantics: drop the call. Free-tier deployments do not bill.
        """
        ...

    async def check_active(self, account_id: str) -> bool:
        """Return True iff the account is in good standing (subscription active,
        payment current). Noop semantics: always True.
        """
        ...

    async def current_tier(self, account_id: str) -> str:
        """Return one of ``"free"`` / ``"pro"`` / ``"enterprise"``. Noop returns
        ``"free"`` so OSS-only deployments always see free-tier quota rules.
        """
        ...

    def register_routes(self, app: Starlette) -> None:
        """Mount billing routes onto the gateway's Starlette app at boot. Called
        exactly once during ``build_app`` after the OSS routes are in place. The
        noop mounts nothing; ``metis-pro`` mounts ``/account/billing/*`` and
        ``/webhooks/stripe``.
        """
        ...


@runtime_checkable
class SignupBackend(Protocol):
    """Optional self-serve signup overlay.

    Implementations mount ``/signup``, ``/signup/verify``, and ``/account/keys``
    handlers onto the gateway's Starlette app at boot time. Called exactly
    once during ``build_app``; never per-request.
    """

    def register_routes(self, app: Starlette) -> None: ...


@runtime_checkable
class AnalyticsExtension(Protocol):
    """Optional per-user / per-team analytics route overlay.

    The rollup SQL itself lives in ``metis_core.analytics`` and stays OSS — this
    Protocol only governs which HTTP routes expose those rollups. The OSS noop
    mounts nothing; ``metis-pro`` mounts ``/analytics/by_user`` /
    ``/analytics/by_team`` (per ``analytics-api.md §4.1 / §4.9``). Called once
    at boot; never per-request.
    """

    def register_routes(self, app: Starlette) -> None: ...


@runtime_checkable
class JudgeRubricProvider(Protocol):
    """Optional curated rubric library for ``LLMJudge`` / ``HybridJudge``.

    The judge classes themselves stay in OSS (``metis_core.eval``); this
    Protocol governs the curated prompt library that ``metis-pro`` keeps
    private. The OSS noop returns ``None`` for every lookup, which causes the
    judge to fall back to its built-in rubric. ``metis-pro`` provides a library
    that returns workload-specific prompts and a stable version stamp.
    """

    def rubric_for(self, subject_kind: str, workload_id: str | None) -> str | None:
        """Return the rubric prompt for ``(subject_kind, workload_id)`` or
        ``None`` to fall back to the judge's built-in template.
        """
        ...

    def rubric_version(self) -> str:
        """Stable id for the rubric library, stamped on every ``eval.completed``
        verdict so dashboards can distinguish rubric vintages. Noop returns
        ``"noop-1.0"``.
        """
        ...


# ----------------------------------------------------------------------------
# Noop default implementations.
# Wired by GatewayConfig / ServerConfig as the factory default. OSS-only
# deployments use these unchanged; metis-pro overlays substitute real impls
# via ``metis_pro.setup.build_pro_config``.
# ----------------------------------------------------------------------------


class NoopBillingBackend:
    """Free-tier billing: records nothing, reports every account active, tier='free',
    mounts no routes.
    """

    async def record_usage(self, account_id: str, savings_usd: Decimal) -> None:
        return None

    async def check_active(self, account_id: str) -> bool:
        return True

    async def current_tier(self, account_id: str) -> str:
        return "free"

    def register_routes(self, app: Starlette) -> None:
        return None


class NoopSignupBackend:
    """Mounts no signup routes. OSS-only deployments don't expose /signup."""

    def register_routes(self, app: Starlette) -> None:
        return None


class NoopAnalyticsExtension:
    """Mounts no per-user / per-team routes. OSS exposes per-key analytics only."""

    def register_routes(self, app: Starlette) -> None:
        return None


class NoopJudgeRubricProvider:
    """Returns no curated rubric; judges use their built-in templates."""

    def rubric_for(self, subject_kind: str, workload_id: str | None) -> str | None:
        return None

    def rubric_version(self) -> str:
        return "noop-1.0"


__all__ = [
    "AnalyticsExtension",
    "BillingBackend",
    "JudgeRubricProvider",
    "NoopAnalyticsExtension",
    "NoopBillingBackend",
    "NoopJudgeRubricProvider",
    "NoopSignupBackend",
    "SignupBackend",
]
