"""Pricing rules for the demo shop. Pure functions, easy to unit test."""

from __future__ import annotations

TAX_RATE = 0.08

# code -> (kind, amount, minimum subtotal)
DISCOUNT_CODES = {
    "WELCOME5": ("fixed", 5.00, 20.00),
    "SAVE10": ("percent", 10, 0.00),
}


class InvalidDiscountCode(ValueError):
    pass


def money(amount: float) -> float:
    return round(amount + 1e-9, 2)


def subtotal(items: list[dict]) -> float:
    """Sum of price * quantity over cart items."""
    return money(sum(item["price"] * item["quantity"] for item in items))


def discount_for(code: str | None, amount: float) -> float:
    """Discount for a code applied to `amount`. Unknown codes raise InvalidDiscountCode."""
    if not code:
        return 0.0
    if code not in DISCOUNT_CODES:
        raise InvalidDiscountCode(code)
    kind, value, minimum = DISCOUNT_CODES[code]
    if amount < minimum:
        return 0.0
    if kind == "percent":
        return money(amount * value / 100)
    return money(min(value, amount))


def tax_on(amount: float) -> float:
    return money(amount * TAX_RATE)
