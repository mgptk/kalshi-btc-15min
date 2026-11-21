"""Order and fill types shared by the UI, the paper broker and (later) the bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]
Purpose = Literal["entry", "exit"]

ONE = Decimal("1")


@dataclass
class OrderIntent:
    """What a human (button) or the bot asks for. Brokers decide whether to accept it."""
    side: Side            # outcome being bought (entry) or sold (exit)
    action: Action
    price: Decimal        # in that side's own price terms (NO orders are priced in NO cents)
    qty: Decimal
    purpose: Purpose = "entry"
    post_only: bool = True


@dataclass
class Order:
    id: int
    ticker: str
    side: Side
    action: Action
    price: Decimal
    qty: Decimal
    purpose: Purpose
    created_ms: int
    filled: Decimal = Decimal(0)
    queue_ahead: Decimal = Decimal(0)
    status: str = "working"           # working | filled | cancelled
    reserved: Decimal = Decimal(0)    # cash held for a resting buy
    tag: str = ""                     # exits: "tp" (take-profit) or "chase" (fee-free exit chase)

    @property
    def remaining(self) -> Decimal:
        return self.qty - self.filled

    @property
    def book_side(self) -> str:
        """Where this order rests in YES terms: 'bid' (buy YES / sell NO) or 'ask'."""
        return "bid" if (self.side == "yes") == (self.action == "buy") else "ask"

    @property
    def yes_level(self) -> Decimal:
        """This order's price expressed as a YES price."""
        return self.price if self.side == "yes" else ONE - self.price


@dataclass
class Fill:
    order_id: int
    ticker: str
    side: Side
    action: Action
    price: Decimal
    qty: Decimal
    fee: Decimal
    ts_ms: int


@dataclass
class Position:
    ticker: str
    side: Side
    qty: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)        # total $ paid for the open quantity (incl. fees)

    @property
    def avg_price(self) -> Decimal | None:
        return (self.cost / self.qty) if self.qty > 0 else None


@dataclass
class LogEntry:
    ts_ms: int
    ticker: str
    event: str
    detail: str = ""
    pnl: Decimal | None = None
    extra: dict = field(default_factory=dict)
