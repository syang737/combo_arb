"""Paper-mode fill simulation.

Given an order and the current leg snapshot, decide the fill price and fee.
Fill models (config ``execution.fill_model``):
  * ``taker_cross`` — cross the spread (buys pay ask, sells hit bid); the combo
    sale fills at the quoted combo YES (we are the maker to the RFQ requester).
  * ``mid``         — fill at mid.
  * ``depth_prob``  — same price as taker_cross; a hook for depth-based partial
    fills (top-of-book depth is not in the snapshot, so it fills fully today).

Fees: leg orders are takers; the combo sale is treated as a maker fill.
"""

from __future__ import annotations

import uuid

from combo_arb.config import AppConfig
from combo_arb.models import Fill, InstrumentType, LegPrice, Order, OrderStatus
from combo_arb.pricing.fees import fee as compute_fee


def _fill_price(order: Order, leg_prices: dict[str, LegPrice], cfg: AppConfig) -> float:
    if order.instrument_type == InstrumentType.COMBO:
        # We sell the overpriced combo YES to the requester at the quoted price.
        return order.price

    lp = leg_prices.get(order.instrument)
    model = cfg.execution.fill_model
    if lp is None:
        return order.price

    if model == "mid" and lp.mid is not None:
        return lp.mid
    # taker_cross / depth_prob: cross the spread.
    if order.action == "buy":
        ref = lp.best_ask if order.side.value == "yes" else lp.best_bid
    else:
        ref = lp.best_bid if order.side.value == "yes" else lp.best_ask
    if ref is None:
        ref = lp.mid if lp.mid is not None else order.price
    # For a NO-side buy the quoted YES ask must be converted to a NO price.
    if order.side.value == "no" and order.action == "buy" and lp.best_ask is not None:
        ref = 1.0 - lp.best_ask
    return ref


def simulate_fill(order: Order, leg_prices: dict[str, LegPrice], cfg: AppConfig) -> Fill:
    if order.order_id is None:
        order.order_id = uuid.uuid4().hex
    price = _fill_price(order, leg_prices, cfg)
    is_maker = order.instrument_type == InstrumentType.COMBO
    fee = compute_fee(price, order.qty, maker=is_maker, cfg=cfg.fees)
    order.status = OrderStatus.FILLED
    return Fill(
        order_id=order.order_id,
        instrument=order.instrument,
        side=order.side,
        action=order.action,
        price=price,
        qty=order.qty,
        fee=fee,
    )
