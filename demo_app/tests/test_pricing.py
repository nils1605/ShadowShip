import pytest

from demo_app import pricing


def test_subtotal():
    items = [{"price": 12.00, "quantity": 2}, {"price": 5.50, "quantity": 1}]
    assert pricing.subtotal(items) == 29.50


def test_no_code_means_no_discount():
    assert pricing.discount_for(None, 50.00) == 0.0


def test_welcome5_takes_five_off():
    assert pricing.discount_for("WELCOME5", 50.00) == 5.00


def test_welcome5_needs_minimum_subtotal():
    assert pricing.discount_for("WELCOME5", 15.00) == 0.0


def test_save10_takes_ten_percent_off():
    assert pricing.discount_for("SAVE10", 50.00) == 5.00
    assert pricing.discount_for("SAVE10", 109.00) == 10.90


def test_unknown_code_is_rejected():
    with pytest.raises(pricing.InvalidDiscountCode):
        pricing.discount_for("BOGUS", 50.00)


def test_tax_is_eight_percent():
    assert pricing.tax_on(100.00) == 8.00
    assert pricing.tax_on(24.00) == 1.92
