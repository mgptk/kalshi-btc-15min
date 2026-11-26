"""Broker interface. PaperBroker implements it for v0; a KalshiBroker will implement it later.

Manual UI buttons and the future bot both call the same methods with the same OrderIntent,
so swapping brokers doesn't touch strategy or UI code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from core.orderbook import OrderBook

from .intents import OrderIntent


class Broker(ABC):
    @abstractmethod
    def submit(self, intent: OrderIntent, book: OrderBook, ts_ms: int) -> tuple[bool, str]:
        """Accept or reject an intent. Returns (ok, message)."""

    @abstractmethod
    def market_order(self, side: str, action: str, qty: Decimal, book: OrderBook, ts_ms: int) -> tuple[bool, str]:
        """Buy or sell `qty` contracts of `side` immediately against the book (taker fees apply)."""

    @abstractmethod
    def cancel(self, order_id: int, ts_ms: int) -> tuple[bool, str]: ...

    @abstractmethod
    def replace(self, order_id: int, price: Decimal, qty: Decimal, book: OrderBook, ts_ms: int) -> tuple[bool, str]: ...

    @abstractmethod
    def set_trail(self, value: Decimal, mode: str, book: OrderBook | None = None, ts_ms: int = 0) -> None:
        """Set the exit distance (used by the trailing stop and the take-profit)."""

    @abstractmethod
    def set_exit_mode(self, mode: str, book: OrderBook, ts_ms: int) -> tuple[bool, str]:
        """Choose the exit strategy: "trailing" or "take_profit"."""

    @abstractmethod
    def exit_now(self, book: OrderBook, ts_ms: int) -> tuple[bool, str]: ...

    # market events -----------------------------------------------------------
    @abstractmethod
    def on_book(self, book: OrderBook, ts_ms: int) -> None: ...

    @abstractmethod
    def on_trade(self, trade: dict, book: OrderBook, ts_ms: int) -> None: ...

    @abstractmethod
    def on_window_close(self, ticker: str, yes_won: bool | None, ts_ms: int) -> None: ...

    @abstractmethod
    def set_ticker(self, ticker: str | None) -> None: ...

    @abstractmethod
    def snapshot(self, book: OrderBook | None) -> dict: ...
