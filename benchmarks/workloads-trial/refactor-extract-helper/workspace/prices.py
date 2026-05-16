"""Price formatting helpers for the storefront.

Two functions render prices for human readers. They share most of their
logic (fixed-point quantization, currency-symbol placement, thousands
separators) but the formatting code was copied between them — a
candidate for an extracted helper.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def format_price_usd(amount: Decimal) -> str:
    """Render `amount` as a USD string ('$1,234.56')."""
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, _, frac = f"{quantized:f}".partition(".")
    sign = ""
    if whole.startswith("-"):
        sign = "-"
        whole = whole[1:]
    parts: list[str] = []
    while len(whole) > 3:
        parts.insert(0, whole[-3:])
        whole = whole[:-3]
    parts.insert(0, whole)
    grouped = ",".join(parts)
    return f"{sign}${grouped}.{frac}"


def format_price_eur(amount: Decimal) -> str:
    """Render `amount` as a EUR string ('€1,234.56')."""
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, _, frac = f"{quantized:f}".partition(".")
    sign = ""
    if whole.startswith("-"):
        sign = "-"
        whole = whole[1:]
    parts: list[str] = []
    while len(whole) > 3:
        parts.insert(0, whole[-3:])
        whole = whole[:-3]
    parts.insert(0, whole)
    grouped = ",".join(parts)
    return f"{sign}€{grouped}.{frac}"
