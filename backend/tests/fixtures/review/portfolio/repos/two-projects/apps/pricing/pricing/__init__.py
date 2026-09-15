"""synthetic fixture: price list service."""


def price(cents: int) -> str:
    return f"{cents / 100:.2f}"
