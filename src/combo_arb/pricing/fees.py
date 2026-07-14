"""Kalshi trading fees.

Single source of truth for the fee schedule so it is trivial to correct if the
schedule changes. Prices are dollars in [0, 1]; fees are returned in dollars.

Taker fee for an order of ``qty`` contracts at price ``P``:

    fee = ceil(taker_rate * P * (1 - P) * qty * 100) / 100      # rounded UP to the cent

with ``taker_rate = 0.07`` this yields the documented schedule, e.g.
    100 @ $0.50 -> $1.75      (max, ~1.75c/contract before rounding)
    100 @ $0.10 -> $0.63
      1 @ $0.50 -> $0.02      (aggregate rounded up to the nearest cent)

Maker fees are ~25% of the taker fee (``maker_ratio``). Rounding is applied to
the aggregate order, matching Kalshi's fee-rounding behaviour.
"""

from __future__ import annotations

import math

from combo_arb.config import FeesConfig

# Kalshi trades in cents; the tradable price band is 1c..99c.
_MIN_PRICE = 0.01
_MAX_PRICE = 0.99


def _clamp_price(price: float) -> float:
    return min(_MAX_PRICE, max(_MIN_PRICE, price))


def _ceil_cents(raw_dollars: float) -> float:
    """Round a dollar amount UP to the nearest cent.

    ``raw_dollars * 100`` is rounded to 9 decimals first so that floating-point
    representation error (e.g. 175.00000000000003) does not spuriously bump the
    ceiling to the next cent, while genuine sub-cent fractions are preserved.
    """
    return math.ceil(round(raw_dollars * 100.0, 9)) / 100.0


def taker_fee(price: float, qty: int, cfg: FeesConfig | None = None) -> float:
    """Taker fee in dollars for ``qty`` contracts at ``price`` (rounded up to the cent)."""
    cfg = cfg or FeesConfig()
    p = _clamp_price(price)
    return _ceil_cents(cfg.taker_rate * p * (1.0 - p) * qty)


def maker_fee(price: float, qty: int, cfg: FeesConfig | None = None) -> float:
    """Maker fee in dollars (~25% of taker), rounded up to the cent."""
    cfg = cfg or FeesConfig()
    p = _clamp_price(price)
    return _ceil_cents(cfg.maker_ratio * cfg.taker_rate * p * (1.0 - p) * qty)


def fee(price: float, qty: int, maker: bool = False, cfg: FeesConfig | None = None) -> float:
    return maker_fee(price, qty, cfg) if maker else taker_fee(price, qty, cfg)


def marginal_fee_per_contract(
    price: float, maker: bool = False, cfg: FeesConfig | None = None
) -> float:
    """Unrounded per-contract fee rate (taker_rate * P * (1-P)).

    Used for *threshold* estimation: the cent-ceiling in :func:`taker_fee` is an
    aggregate-order rounding, so applying it to a single contract overstates the
    marginal cost. Actual charged fees (with rounding) come from :func:`fee` at
    fill time on the real lot size.
    """
    cfg = cfg or FeesConfig()
    p = _clamp_price(price)
    rate = cfg.maker_ratio * cfg.taker_rate if maker else cfg.taker_rate
    return rate * p * (1.0 - p)
