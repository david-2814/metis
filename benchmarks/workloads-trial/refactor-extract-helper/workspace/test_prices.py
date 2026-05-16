"""Tests for prices.format_price_usd / prices.format_price_eur.

Both formatters must continue to pass after any refactor. The shared
helper that consolidates the duplicated logic is private (a name with
a leading underscore is fine); these tests don't import it directly.
"""

from decimal import Decimal

from prices import format_price_eur, format_price_usd


def test_usd_basic():
    assert format_price_usd(Decimal("9.99")) == "$9.99"


def test_usd_thousands_separator():
    assert format_price_usd(Decimal("1234567.89")) == "$1,234,567.89"


def test_usd_rounding_half_up():
    assert format_price_usd(Decimal("0.005")) == "$0.01"


def test_usd_negative_sign_outside_currency():
    assert format_price_usd(Decimal("-42.50")) == "-$42.50"


def test_eur_basic():
    assert format_price_eur(Decimal("9.99")) == "€9.99"


def test_eur_thousands_separator():
    assert format_price_eur(Decimal("1234567.89")) == "€1,234,567.89"


def test_eur_negative_sign_outside_currency():
    assert format_price_eur(Decimal("-42.50")) == "-€42.50"
