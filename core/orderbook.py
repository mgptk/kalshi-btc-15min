"""Local copy of one Kalshi market's order book.

Kalshi books are bid-only per side. With the default (no `use_yes_price`), YES levels
are YES bids in YES prices and NO levels are NO bids in NO prices, so:
    YES ask = 1 - best NO bid        NO ask = 1 - best YES bid
All prices and sizes are Decimals in dollars / contracts.
"""
from __future__ import annotations

from decimal import Decimal

ONE = Decimal("1")


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


class OrderBook:
    def __init__(self, ticker: str | None = None):
        self.ticker = ticker
        self.yes: dict[Decimal, Decimal] = {}
        self.no: dict[Decimal, Decimal] = {}
        self.ready = False
        self.last_update_ms: int | None = None

    # ---- updates -------------------------------------------------------------
    def apply_snapshot(self, msg: dict, ts_ms: int | None = None) -> None:
        self.ticker = msg.get("market_ticker", self.ticker)
        self.yes = {D(p): D(q) for p, q in (msg.get("yes_dollars_fp") or []) if D(q) > 0}
        self.no = {D(p): D(q) for p, q in (msg.get("no_dollars_fp") or []) if D(q) > 0}
        self.ready = True
        self.last_update_ms = ts_ms

    def apply_delta(self, msg: dict, ts_ms: int | None = None) -> None:
        side = self.yes if msg.get("side") == "yes" else self.no
        price = D(msg["price_dollars"])
        new = side.get(price, Decimal(0)) + D(msg["delta_fp"])
        if new > 0:
            side[price] = new
        else:
            side.pop(price, None)
        self.last_update_ms = msg.get("ts_ms", ts_ms)

    def clear(self) -> None:
        self.yes.clear()
        self.no.clear()
        self.ready = False

    # ---- queries -------------------------------------------------------------
    @property
    def best_yes_bid(self) -> Decimal | None:
        return max(self.yes) if self.yes else None

    @property
    def best_no_bid(self) -> Decimal | None:
        return max(self.no) if self.no else None

    @property
    def best_yes_ask(self) -> Decimal | None:
        b = self.best_no_bid
        return ONE - b if b is not None else None

    @property
    def best_no_ask(self) -> Decimal | None:
        b = self.best_yes_bid
        return ONE - b if b is not None else None

    def best_bid(self, side: str) -> Decimal | None:
        return self.best_yes_bid if side == "yes" else self.best_no_bid

    def best_ask(self, side: str) -> Decimal | None:
        return self.best_yes_ask if side == "yes" else self.best_no_ask

    def size_at_yes_bid(self, yes_price: Decimal) -> Decimal:
        return self.yes.get(yes_price, Decimal(0))

    def size_at_yes_ask(self, yes_price: Decimal) -> Decimal:
        """Size offered at a YES ask price (= NO bids at 1 - price)."""
        return self.no.get(ONE - yes_price, Decimal(0))

    def ladder(self, depth: int = 5, side: str = "yes") -> dict:
        """Top-of-book in `side`'s own prices: bids descending, asks ascending.

        The book is bid-only per side, so one side's asks are the other side's bids
        mirrored through 1 (YES ask = 1 - NO bid, and vice versa).
        """
        own, other = (self.yes, self.no) if side == "yes" else (self.no, self.yes)
        bids = sorted(own.items(), key=lambda kv: kv[0], reverse=True)[:depth]
        asks = sorted(((ONE - p, q) for p, q in other.items()), key=lambda kv: kv[0])[:depth]
        return {"bids": bids, "asks": asks}
