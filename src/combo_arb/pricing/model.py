"""Fair-combo pricing model.

Fair combo value = joint probability that every selected leg resolves in the
combo's favour, approximated as the product of each leg's marginal probability
(binary settlement: the combo pays $1 iff all legs hit).

Hooks for the two documented sources of bias:
  * ``correlation_factor`` — multiplicative lift/shrink on the product to account
    for dependence between legs (1.0 = independence assumption).
  * ``settlement_model`` — ``binary`` (product) today; ``fractional`` is a stub
    for a future last-trade / fractional-settlement payoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from combo_arb.config import AppConfig, PricingConfig
from combo_arb.models import ComboLeg, ComboRFQ, LegPrice, Side
from combo_arb.pricing.fees import marginal_fee_per_contract

_EPS = 1e-6


def _clamp_prob(p: float) -> float:
    return min(1.0 - _EPS, max(_EPS, p))


def implied_prob(leg: LegPrice, cfg: PricingConfig) -> Optional[float]:
    """Marginal probability for a leg from its book snapshot.

    ``mid`` uses (bid+ask)/2 but falls back to last trade when the book is thin
    (spread wider than ``thin_book_spread``). ``last`` uses last trade directly.
    Returns None if no usable price is available.
    """
    if cfg.prob_source == "last":
        price = leg.last_trade_price if leg.last_trade_price is not None else leg.mid
    else:  # mid
        spread = leg.spread
        if (
            spread is not None
            and spread > cfg.thin_book_spread
            and leg.last_trade_price is not None
        ):
            price = leg.last_trade_price
        else:
            price = leg.mid
    if price is None:
        return None
    return _clamp_prob(price)


def combo_implied_by_legs(
    rfq: ComboRFQ,
    leg_prices: dict[str, LegPrice],
    cfg: PricingConfig,
) -> Optional[float]:
    """Product of leg probabilities (with side handling + correlation factor).

    A YES-side leg contributes ``p``; a NO-side leg contributes ``1 - p`` (the
    combo is satisfied when the underlying resolves NO). Returns None if any leg
    price is missing.
    """
    product = 1.0
    for leg in rfq.legs:
        lp = leg_prices.get(leg.leg_ticker)
        if lp is None:
            return None
        p = implied_prob(lp, cfg)
        if p is None:
            return None
        contribution = p if leg.side == Side.YES else (1.0 - p)
        product *= contribution

    fair = product * cfg.correlation_factor
    if cfg.settlement_model == "fractional":
        # Placeholder for a future fractional/last-trade settlement payoff. Until
        # calibrated it mirrors the binary product model so behaviour is explicit.
        fair = fair
    return _clamp_prob(fair)


def leg_deltas(
    legs: list[ComboLeg],
    leg_prices: dict[str, LegPrice],
    cfg: PricingConfig,
) -> dict[str, float]:
    """Signed first-order delta of the combo w.r.t. each leg's underlying prob.

    Contribution c_i = p_i (YES leg) or (1 - p_i) (NO leg); combo = product(c_i).
    delta_i = (product of other c_j) * d(c_i)/d(p_i), scaled by correlation_factor.
    These deltas are the hedge ratios (leg contracts per combo contract).
    """
    contribs: dict[str, float] = {}
    signs: dict[str, float] = {}
    for leg in legs:
        lp = leg_prices.get(leg.leg_ticker)
        p = implied_prob(lp, cfg) if lp else None
        if p is None:
            continue
        if leg.side == Side.YES:
            contribs[leg.leg_ticker] = p
            signs[leg.leg_ticker] = 1.0
        else:
            contribs[leg.leg_ticker] = 1.0 - p
            signs[leg.leg_ticker] = -1.0

    deltas: dict[str, float] = {}
    k = cfg.correlation_factor
    for ticker in contribs:
        prod_others = 1.0
        for other, c in contribs.items():
            if other != ticker:
                prod_others *= c
        deltas[ticker] = prod_others * signs[ticker] * k
    return deltas


@dataclass
class PricingResult:
    fair_combo: float
    fees_estimate: float        # per-contract expected fees (combo trade + leg hedge)
    buffer: float               # safety buffer on top of fees
    margin_threshold: float     # fees_estimate + buffer
    arbitrage_margin: float     # directional edge net of fees (positive = tradeable)
    flagged: bool               # arbitrage_margin > buffer and > min_margin


def estimate_fees_per_contract(
    combo_price: float,
    rfq: ComboRFQ,
    leg_prices: dict[str, LegPrice],
    cfg: AppConfig,
) -> float:
    """Per-combo-contract fee estimate, from each instrument's own price and size.

    Kalshi taker fee = 0.07 * P * (1-P) per contract (unrounded marginal rate used
    here so the threshold isn't inflated by single-contract cent rounding). We
    take the combo (1 contract) and delta-hedge each leg (|delta_i| contracts per
    combo contract), so the leg fee is scaled by the hedge ratio:

        combo:  0.07 * Pc * (1-Pc)
        leg i:  |delta_i| * 0.07 * Pi * (1-Pi)
    """
    total = marginal_fee_per_contract(combo_price, maker=False, cfg=cfg.fees)
    deltas = leg_deltas(rfq.legs, leg_prices, cfg.pricing)
    for leg in rfq.legs:
        lp = leg_prices.get(leg.leg_ticker)
        price = (lp.mid if lp and lp.mid is not None else 0.5)
        hedge_ratio = abs(deltas.get(leg.leg_ticker, 0.0))
        total += hedge_ratio * marginal_fee_per_contract(price, maker=False, cfg=cfg.fees)
    return total


def price_combo(
    rfq: ComboRFQ,
    leg_prices: dict[str, LegPrice],
    cfg: AppConfig,
) -> Optional[PricingResult]:
    """Full evaluation of one combo against its legs. Returns None if unpriceable."""
    fair = combo_implied_by_legs(rfq, leg_prices, cfg.pricing)
    if fair is None:
        return None

    fees_est = estimate_fees_per_contract(rfq.quote_yes, rfq, leg_prices, cfg)
    # Buffer is opt-in (off for paper/exploration) but always enforced once live is
    # armed, so a real deployment can't accidentally trade on a zero-buffer edge.
    apply_buffer = cfg.thresholds.apply_buffer or cfg.live_trading_armed()
    buffer = (
        max(cfg.thresholds.buffer_abs, cfg.thresholds.buffer_pct * fair)
        if apply_buffer else 0.0
    )
    margin_threshold = fees_est + buffer

    # Directional edge (net of fees). Buy when the combo is quoted BELOW fair;
    # sell when quoted ABOVE fair.
    if cfg.strategy.direction == "sell_overpriced":
        arb_margin = rfq.quote_yes - fair - fees_est
    else:  # buy_underpriced (default)
        arb_margin = fair - rfq.quote_yes - fees_est
    flagged = arb_margin > buffer and arb_margin > cfg.thresholds.min_margin

    return PricingResult(
        fair_combo=fair,
        fees_estimate=fees_est,
        buffer=buffer,
        margin_threshold=margin_threshold,
        arbitrage_margin=arb_margin,
        flagged=flagged,
    )
