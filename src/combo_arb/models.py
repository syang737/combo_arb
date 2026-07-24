"""Core data models for the combo arbitrage engine.

Prices are represented as floats in [0, 1] (probability / price per $1 payout).
The Kalshi REST API expresses prices as integer cents (1..99); the client layer
is responsible for converting cents <-> dollars at the boundary.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def _now() -> float:
    return time.time()


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class InstrumentType(str, Enum):
    LEG = "leg"
    COMBO = "combo"


class OrderStatus(str, Enum):
    NEW = "new"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class SignalAction(str, Enum):
    """What the scanner recommends for a flagged combo."""

    HEDGE_VIA_LEGS = "hedge_via_legs"  # capture edge, hedge exposure through the legs
    SIGNAL_ONLY = "signal_only"        # edge exists but hedge not operationally possible
    SKIP = "skip"                      # below threshold / risk-blocked


class LegPrice(BaseModel):
    """Top-of-book snapshot for a single underlying leg market."""

    leg_ticker: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    last_trade_price: Optional[float] = None
    timestamp: float = Field(default_factory=_now)

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return self.last_trade_price

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


class ComboLeg(BaseModel):
    """One selected leg within a multivariate-event combo."""

    leg_ticker: str
    side: Side = Side.YES
    ratio: int = 1


class ComboRFQ(BaseModel):
    """A combo (multivariate event) request-for-quote / displayed combo quote."""

    rfq_id: str
    mve_collection_ticker: str
    market_ticker: Optional[str] = None    # the tradeable combo market (priced directly)
    legs: list[ComboLeg]
    quote_yes: Optional[float] = None      # combo YES price read from the combo market
    quote_no: Optional[float] = None       # NO side (not assumed tradeable by this engine)
    size: int = 1                          # contracts requested / available
    quote_time: float = Field(default_factory=_now)
    requester_id: Optional[str] = None


class ArbSignal(BaseModel):
    """A flagged arbitrage candidate emitted by the scanner."""

    rfq_id: str
    mve_collection_ticker: str
    market_ticker: Optional[str] = None    # the tradeable combo market (live order target)
    legs: list[ComboLeg]
    leg_prices: dict[str, LegPrice]        # keyed by leg_ticker, snapshot used for pricing
    combo_quote_yes: float
    fair_combo: float
    fees_estimate: float
    margin_threshold: float
    arbitrage_margin: float                # combo_quote_yes - fair_combo - fees_estimate
    size: int
    action: SignalAction
    timestamp: float = Field(default_factory=_now)


class ComboEvaluation(BaseModel):
    """Per-combo scan result (recorded for EVERY combo, flagged or not).

    Lets you watch how close the market gets to a tradeable edge and calibrate the
    buffer, even when nothing clears the threshold.
    """

    rfq_id: str
    mve_collection_ticker: str
    direction: str                         # buy_underpriced | sell_overpriced
    combo_quote_yes: float
    fair_combo: float
    fees_estimate: float
    buffer: float
    arbitrage_margin: float                # directional edge net of fees
    flagged: bool
    timestamp: float = Field(default_factory=_now)

    @property
    def gap_to_flag(self) -> float:
        """How far the edge is from flagging. >= 0 means it flagged."""
        return self.arbitrage_margin - self.buffer

    @property
    def value_gap(self) -> float:
        """Signed quote-vs-fair gap before fees (fair - quote for buy view)."""
        return self.fair_combo - self.combo_quote_yes


class Order(BaseModel):
    instrument: str                        # leg_ticker or mve_collection_ticker
    instrument_type: InstrumentType
    side: Side
    action: str                            # "buy" | "sell"
    price: float                           # limit / expected fill price in [0, 1]
    qty: int
    mode: str = "paper"                    # paper | live
    status: OrderStatus = OrderStatus.NEW
    order_id: Optional[str] = None         # local id (assigned by persistence)
    kalshi_order_id: Optional[str] = None
    signal_ref: Optional[str] = None       # rfq_id of the originating signal
    timestamp: float = Field(default_factory=_now)


class Fill(BaseModel):
    order_id: str
    instrument: str
    instrument_type: InstrumentType = InstrumentType.LEG
    side: Side
    action: str
    price: float
    qty: int
    fee: float
    timestamp: float = Field(default_factory=_now)


class Position(BaseModel):
    instrument: str
    instrument_type: InstrumentType
    net_qty: int = 0                       # signed: +long / -short (in YES terms)
    avg_price: float = 0.0
    updated_ts: float = Field(default_factory=_now)


class PnL(BaseModel):
    realized: float = 0.0
    unrealized: float = 0.0
    equity: float = 0.0
    timestamp: float = Field(default_factory=_now)
