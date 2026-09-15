"""synthetic fixture."""

from pricing import price


def check_price():
    assert price(1250) == "12.50"
