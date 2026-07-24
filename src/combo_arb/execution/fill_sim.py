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
        # Combo trades at the quoted combo YES price (we buy or sell it there).
        return order.price

    lp = leg_prices.get(order.instrument)
    if lp is None:
        return order.price

    is_yes = order.side.value == "yes"
    if cfg.execution.fill_model == "mid" and lp.mid is not None:
        # NO mid = 1 - YES mid.
        return lp.mid if is_yes else (1.0 - lp.mid)

    # taker_cross / depth_prob: cross the spread on the order's side.
    #   buy YES -> yes_ask         sell YES -> yes_bid
    #   buy NO  -> 1 - yes_bid     sell NO  -> 1 - yes_ask
    if is_yes:
        ref = lp.best_ask if order.action == "buy" else lp.best_bid
    else:
        if order.action == "buy":
            ref = (1.0 - lp.best_bid) if lp.best_bid is not None else None
        else:
            ref = (1.0 - lp.best_ask) if lp.best_ask is not None else None
    if ref is None:
        ref = lp.mid if lp.mid is not None else order.price
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
        instrument_type=order.instrument_type,
        side=order.side,
        action=order.action,
        price=price,
        qty=order.qty,
        fee=fee,
    )
