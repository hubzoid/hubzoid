"""Invoice totals. Money is Decimal, rounded half up to the cent, like the tax office does."""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def vat(net: Decimal, rate: Decimal) -> Decimal:
    return (net * rate).quantize(CENT, rounding=ROUND_HALF_UP)


def total(net: Decimal, rate: Decimal) -> Decimal:
    return net + vat(net, rate)
