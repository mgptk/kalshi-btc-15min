"""MarketState: the read-only snapshot consumed by the UI and (later) the bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class MarketState:
    ts_ms: int
    source: str
    connected: bool
    error: str | None
    fee_type: str | None
    # market
    ticker: str | None = None
    window: str = ""
    open_ms: int | None = None
    close_ms: int | None = None
    target: Decimal | None = None
    target_source: str = ""
    # index
    brti: Decimal | None = None
    brti_ts_ms: int | None = None
    avg60: Decimal | None = None
    final_avg: Decimal | None = None
    final_n: int = 0
    history: list = field(default_factory=list)      # [(ts_ms, brti, avg60)]
    # book (YES terms)
    book_ready: bool = False
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    no_bid: Decimal | None = None
    no_ask: Decimal | None = None
    bids: list = field(default_factory=list)         # [(price, size)] best first
    asks: list = field(default_factory=list)
    last_trade: dict | None = None
    # paper account
    account: dict = field(default_factory=dict)

    @property
    def seconds_left(self) -> float | None:
        if self.close_ms is None:
            return None
        return max(0.0, (self.close_ms - self.ts_ms) / 1000)

    @property
    def gap(self) -> Decimal | None:
        """Current 60 s average minus target (positive = currently Up)."""
        if self.avg60 is None or self.target is None:
            return None
        return self.avg60 - self.target
