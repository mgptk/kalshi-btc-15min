"""Paper broker: post-only resting orders, conservative queue-position fills,
market (taker) orders that walk the book and pay taker fees,
automatic trailing-stop / take-profit exits with a fee-free chase, and settlement at close.

All methods are called by FeedService while it holds its lock, so no locking here.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from core.config import (
    DEFAULT_PRICE_RANGES,
    DEFAULT_TRAIL_MODE,
    DEFAULT_TRAIL_VALUE,
    STARTING_CASH,
    TICK,
)
from core.market import tick_at
from core.orderbook import OrderBook

from .broker import Broker
from .intents import ONE, Fill, LogEntry, Order, OrderIntent, Position
from .trailing_stop import TrailingStop

MIN_PRICE = TICK
MAX_PRICE = ONE - TICK
MAKER_RATE = Decimal("0.0175")
TAKER_RATE = Decimal("0.07")
CENT = Decimal("0.01")


def _cents(x: Decimal | None) -> str:
    if x is None:
        return "—"
    v = x * 100
    return f"{v:.0f}¢" if v == v.to_integral_value() else f"{v.normalize():f}¢"


def _qty(x: Decimal) -> str:
    return f"{x.normalize():f}"


class PaperBroker(Broker):
    def __init__(
        self,
        starting_cash: Decimal = STARTING_CASH,
        trail_value: Decimal = DEFAULT_TRAIL_VALUE,
        trail_mode: str = DEFAULT_TRAIL_MODE,
        exit_mode: str = "trailing",
    ):
        self.starting_cash = Decimal(starting_cash)
        self.cash = Decimal(starting_cash)
        self.trail_value = Decimal(trail_value)
        self.trail_mode = trail_mode
        self.exit_mode = exit_mode               # "trailing" | "take_profit"
        self.exiting = False                     # fee-free exit chase running for the open position
        self.protected = False                   # open position has an automatic exit (stop / take-profit)
        self.maker_fee_multiplier = Decimal(0)   # 0 => no maker fees (standard "quadratic" series)
        self.taker_fee_multiplier = Decimal(1)   # series fee_multiplier applied to the taker rate
        self.ticker: str | None = None
        self.price_ranges = None                 # market's price grid; None -> tapered default
        self.orders: dict[int, Order] = {}
        self.position: Position | None = None
        self.stop: TrailingStop | None = None
        self.realized = Decimal(0)
        self.fills: list[Fill] = []
        self.log: list[LogEntry] = []
        self._next_id = 1
        # settlement bookkeeping, keyed by ticker
        self.pending_settlement: dict[str, Position] = {}
        self.settled: dict[str, dict] = {}

    # ------------------------------------------------------------------ helpers
    def _log(self, ts: int, event: str, detail: str = "", pnl: Decimal | None = None, ticker: str | None = None):
        self.log.append(LogEntry(ts, ticker or self.ticker or "", event, detail, pnl))
        if len(self.log) > 500:
            del self.log[:100]

    def set_fee_model(self, fee_type: str | None, multiplier: float | None) -> None:
        if fee_type and "maker" in fee_type:
            self.maker_fee_multiplier = Decimal(str(multiplier or 1))
        else:
            self.maker_fee_multiplier = Decimal(0)
        self.taker_fee_multiplier = Decimal(str(multiplier)) if multiplier is not None else Decimal(1)

    def maker_fee(self, price: Decimal, qty: Decimal) -> Decimal:
        if self.maker_fee_multiplier == 0:
            return Decimal(0)
        raw = MAKER_RATE * self.maker_fee_multiplier * qty * price * (ONE - price)
        return raw.quantize(CENT, rounding=ROUND_CEILING)

    def taker_fee(self, price: Decimal, qty: Decimal) -> Decimal:
        """Kalshi taker fee: 7% × multiplier × C × P × (1 − P), rounded up to the cent."""
        raw = TAKER_RATE * self.taker_fee_multiplier * qty * price * (ONE - price)
        return raw.quantize(CENT, rounding=ROUND_CEILING)

    @property
    def reserved(self) -> Decimal:
        return sum((o.reserved for o in self.working()), Decimal(0))

    @property
    def available(self) -> Decimal:
        return self.cash - self.reserved

    def working(self, purpose: str | None = None) -> list[Order]:
        return [
            o for o in self.orders.values()
            if o.status == "working" and (purpose is None or o.purpose == purpose)
        ]

    @staticmethod
    def _queue_ahead(order: Order, book: OrderBook) -> Decimal:
        if order.book_side == "bid":
            return book.size_at_yes_bid(order.yes_level)
        return book.size_at_yes_ask(order.yes_level)

    @staticmethod
    def _is_post_only_ok(order: Order, book: OrderBook) -> bool:
        if order.book_side == "bid":
            ask = book.best_yes_ask
            return ask is None or order.yes_level < ask
        bid = book.best_yes_bid
        return bid is None or order.yes_level > bid

    def _new_order(self, side, action, price, qty, purpose, ts, book) -> Order:
        o = Order(self._next_id, self.ticker, side, action, price, qty, purpose, ts)
        self._next_id += 1
        o.queue_ahead = self._queue_ahead(o, book)
        if action == "buy":
            o.reserved = price * qty + self.maker_fee(price, qty)
        self.orders[o.id] = o
        return o

    def _cancel_order(self, o: Order, ts: int, reason: str, quiet: bool = False) -> None:
        if o.status != "working":
            return
        o.status = "cancelled"
        o.reserved = Decimal(0)
        if not quiet:
            self._log(ts, "cancel", f"#{o.id} {o.action} {o.side.upper()} {_cents(o.price)} — {reason}")

    # ------------------------------------------------------------------ user actions
    def submit(self, intent: OrderIntent, book: OrderBook, ts_ms: int) -> tuple[bool, str]:
        if intent.purpose != "entry" or intent.action != "buy":
            return False, "Only buy entries can be placed by hand; exits are handled by the trailing stop."
        if not self.ticker or not book.ready:
            return False, "No live market/order book yet."
        price, qty = Decimal(intent.price), Decimal(intent.qty)
        if qty <= 0 or qty != qty.to_integral_value():
            return False, "Quantity must be a whole number of contracts."
        if not (MIN_PRICE <= price <= MAX_PRICE) or (price % TICK) != 0:
            return False, "Price must be between 1¢ and 99¢ in 1¢ steps."
        opp = "no" if intent.side == "yes" else "yes"
        if self.position and self.position.side == opp and self.position.qty > 0:
            return False, f"You hold {opp.upper()} — close it before buying {intent.side.upper()}."
        if any(o.side == opp for o in self.working("entry")):
            return False, f"Cancel your working {opp.upper()} entry first."
        if self.exiting:
            return False, "An exit is working on your position — wait for it to finish."
        probe = Order(0, self.ticker, intent.side, "buy", price, qty, "entry", ts_ms)
        if intent.post_only and not self._is_post_only_ok(probe, book):
            return False, (
                f"Post-only: {intent.side.upper()} buy at {_cents(price)} would cross the ask "
                f"({_cents(book.best_ask(intent.side))}). Price it below the ask."
            )
        need = price * qty + self.maker_fee(price, qty)
        if need > self.available:
            return False, f"Not enough buying power (${self.available:.2f} available, ${need:.2f} needed)."
        o = self._new_order(intent.side, "buy", price, qty, "entry", ts_ms, book)
        self._log(ts_ms, "place", f"#{o.id} buy {_qty(qty)} {o.side.upper()} @ {_cents(price)} (queue ahead {_qty(o.queue_ahead)})")
        return True, f"Placed #{o.id}: buy {_qty(qty)} {o.side.upper()} @ {_cents(price)}"

    def cancel(self, order_id: int, ts_ms: int) -> tuple[bool, str]:
        o = self.orders.get(order_id)
        if not o or o.status != "working":
            return False, "Order is not working."
        if o.purpose == "exit":
            return False, "Exit orders are managed by the trailing stop."
        self._cancel_order(o, ts_ms, "user")
        return True, f"Cancelled #{o.id}"

    def replace(self, order_id: int, price: Decimal, qty: Decimal, book: OrderBook, ts_ms: int) -> tuple[bool, str]:
        o = self.orders.get(order_id)
        if not o or o.status != "working" or o.purpose != "entry":
            return False, "Only working entries can be modified."
        new_qty = Decimal(qty)
        if new_qty <= o.filled:
            return False, f"New quantity must exceed what's already filled ({_qty(o.filled)})."
        # cancel, then place the unfilled remainder as a new order (queue position resets)
        self._cancel_order(o, ts_ms, "replaced", quiet=True)
        ok, msg = self.submit(OrderIntent(o.side, "buy", Decimal(price), new_qty - o.filled), book, ts_ms)
        if not ok:
            o.status, o.reserved = "working", (o.price * o.remaining + self.maker_fee(o.price, o.remaining))
            return False, msg
        return True, f"Replaced #{o.id} → {msg}"

    def set_trail(self, value: Decimal, mode: str, book: OrderBook | None = None, ts_ms: int = 0) -> None:
        self.trail_value, self.trail_mode = Decimal(value), mode
        if self.stop and self.stop.state != TrailingStop.TRIGGERED:
            self.stop.set_trail(self.trail_value, mode)
        if book is not None and self.exit_mode == "take_profit":
            self._sync_tp(book, ts_ms)

    def set_exit_mode(self, mode: str, book: OrderBook, ts_ms: int) -> tuple[bool, str]:
        if mode not in ("trailing", "take_profit"):
            return False, f"Unknown exit strategy: {mode}"
        label = "Trailing stop" if mode == "trailing" else "Take profit"
        pos = self.position
        has_pos = bool(pos and pos.qty > 0)
        attach = has_pos and not self.protected and not self.exiting
        if mode == self.exit_mode and not attach:
            return True, f"Exit strategy: {label}"
        self.exit_mode = mode
        note = ""
        if has_pos:
            if self.exiting:
                note = " (applies to your next position — an exit is already working)"
            elif mode == "take_profit":
                self.protected = True
                self.stop = None
                self._sync_tp(book, ts_ms)
                note = " — take-profit order placed for the open position"
            else:
                self.protected = True
                for o in self.working("exit"):
                    self._cancel_order(o, ts_ms, "switched to trailing stop", quiet=True)
                self.stop = TrailingStop(self.trail_value, self.trail_mode, book.best_bid(pos.side) or pos.avg_price)
                note = " — stop now trails the open position from the current bid"
        self._log(ts_ms, "exit mode", f"{label}{note}")
        return True, f"Exit strategy: {label}{note}"

    def exit_now(self, book: OrderBook, ts_ms: int) -> tuple[bool, str]:
        if not self.position or self.position.qty <= 0:
            return False, "No open position."
        if self.exiting:
            return False, "Exit already working."
        if self.stop is not None:
            self.stop.force_trigger()
        self._start_exit(book, ts_ms, "manual exit")
        return True, "Exit started (fee-free chase)."

    def market_order(self, side: str, action: str, qty: Decimal, book: OrderBook, ts_ms: int) -> tuple[bool, str]:
        """Take liquidity now: walk the opposite side of the book for `qty` contracts.

        Fills level by level at the resting prices and pays the taker fee on each level.
        Whatever the book can't fill is cancelled (immediate-or-cancel). A market buy never
        attaches a stop or take-profit by itself; one can be added from the Exit Strategy
        controls. If the position already has an exit, it keeps covering the whole position.
        """
        if side not in ("yes", "no") or action not in ("buy", "sell"):
            return False, "Unknown market order."
        if not self.ticker or not book.ready:
            return False, "No live market/order book yet."
        qty = Decimal(qty)
        if qty <= 0 or qty != qty.to_integral_value():
            return False, "Quantity must be a whole number of contracts."
        pos = self.position
        holding = pos is not None and pos.qty > 0 and pos.ticker == self.ticker
        if action == "buy":
            opp = "no" if side == "yes" else "yes"
            if holding and pos.side == opp:
                return False, f"You hold {opp.upper()} — close it before buying {side.upper()}."
            if any(o.side == opp for o in self.working("entry")):
                return False, f"Cancel your working {opp.upper()} entry first."
            if self.exiting:
                return False, "An exit is working on your position — wait for it to finish."
            levels = book.ladder(depth=10_000, side=side)["asks"]
        else:
            if not holding or pos.side != side:
                return False, f"No {side.upper()} position to sell."
            if qty > pos.qty:
                return False, f"You only hold {_qty(pos.qty)} {side.upper()}."
            levels = book.ladder(depth=10_000, side=side)["bids"]

        plan: list[tuple[Decimal, Decimal, Decimal]] = []   # (price, qty, fee)
        left = qty
        for px, size in levels:
            if left <= 0:
                break
            take = min(left, size)
            plan.append((px, take, self.taker_fee(px, take)))
            left -= take
        if not plan:
            where = "asks" if action == "buy" else "bids"
            return False, f"No {side.upper()} {where} in the book."
        filled = qty - left
        notional = sum((px * n for px, n, _ in plan), Decimal(0))
        fees = sum((f for _, _, f in plan), Decimal(0))
        if action == "buy" and notional + fees > self.available:
            return False, (f"Not enough buying power (${self.available:.2f} available, "
                           f"${notional + fees:.2f} needed).")

        o = Order(self._next_id, self.ticker, side, action, plan[-1][0], filled,
                  "entry" if action == "buy" else "exit", ts_ms, filled=filled, status="filled", tag="market")
        self._next_id += 1
        self.orders[o.id] = o
        pnl = Decimal(0)
        for px, n, fee in plan:
            if action == "buy":
                self._book_buy(o, px, n, fee, ts_ms)
            else:
                pnl += self._book_sell(o, px, n, fee, ts_ms)

        vwap = notional / filled
        short = f"; {_qty(left)} unfilled (book exhausted)" if left > 0 else ""
        verb = "bought" if action == "buy" else "sold"
        detail = (f"#{o.id} {verb} {_qty(filled)} {side.upper()} @ avg {_cents(vwap.quantize(Decimal('0.0001')))} "
                  f"(worst {_cents(plan[-1][0])}, taker fee ${fees:.2f}){short}")
        if action == "buy":
            if self.protected and self.exit_mode == "take_profit":
                self._sync_tp(book, ts_ms)
            self._log(ts_ms, "market", f"{detail} → position {_qty(self.position.qty)}"
                      + ("" if self.protected else " · no stop/TP"))
        else:
            self._log(ts_ms, "market", detail, pnl=pnl)
            if self.position is None or self.position.qty <= 0:
                for x in self.working("exit"):
                    self._cancel_order(x, ts_ms, "position closed by market sell", quiet=True)
                self._go_flat()
                self._log(ts_ms, "flat", "Position closed")
            elif self.exiting:
                self._chase(book, ts_ms)
            elif self.protected and self.exit_mode == "take_profit":
                self._sync_tp(book, ts_ms)
        msg = f"Market {action}: {verb} {_qty(filled)} {side.upper()} @ avg {_cents(vwap.quantize(Decimal('0.0001')))}, fee ${fees:.2f}"
        if action == "sell":
            msg += f", P&L {'+' if pnl >= 0 else '−'}${abs(pnl):.2f}"
        return True, msg + short

    # ------------------------------------------------------------------ take profit
    def _grid_ceil(self, price: Decimal) -> Decimal:
        step = tick_at(price, self.price_ranges)
        return (price / step).to_integral_value(rounding=ROUND_CEILING) * step

    @property
    def _max_price(self) -> Decimal:
        return ONE - (self.price_ranges or DEFAULT_PRICE_RANGES)[-1][2]

    def tp_target(self) -> Decimal | None:
        """Take-profit price: average entry (incl. fees) + distance, rounded up to the price grid."""
        pos = self.position
        if not pos or pos.qty <= 0:
            return None
        avg = pos.avg_price
        if self.trail_mode == "pct":
            raw = avg * (ONE + self.trail_value / Decimal(100))
        else:
            raw = avg + self.trail_value / Decimal(100)
        return min(self._grid_ceil(raw), self._max_price)

    def _sync_tp(self, book: OrderBook, ts: int) -> None:
        """Keep one resting post-only take-profit sell for the whole position at the target."""
        pos = self.position
        if not pos or pos.qty <= 0 or self.exiting or self.exit_mode != "take_profit" or not self.protected:
            return
        target = self.tp_target()
        px = target
        bid = book.best_bid(pos.side)
        if bid is not None and px <= bid:          # already past the target: stay maker at bid + 1 tick
            px = min(bid + tick_at(bid, self.price_ranges), self._max_price)
        exits = self.working("exit")
        if exits and exits[0].tag == "tp" and exits[0].price == px and exits[0].remaining == pos.qty:
            return
        for o in exits:
            self._cancel_order(o, ts, "take-profit update", quiet=True)
        o = self._new_order(pos.side, "sell", px, pos.qty, "exit", ts, book)
        o.tag = "tp"
        self._log(ts, "take profit", f"#{o.id} sell {_qty(o.qty)} {o.side.upper()} @ {_cents(px)} "
                                     f"(avg {_cents(pos.avg_price)}, queue ahead {_qty(o.queue_ahead)})")

    # ------------------------------------------------------------------ market events
    def set_ticker(self, ticker: str | None, price_ranges=None) -> None:
        self.ticker = ticker
        self.price_ranges = price_ranges

    def on_book(self, book: OrderBook, ts_ms: int) -> None:
        if not self.position or self.position.qty <= 0 or self.position.ticker != book.ticker:
            return
        if self.exiting:
            self._chase(book, ts_ms)
            return
        if self.exit_mode != "trailing" or self.stop is None:
            return   # take-profit rests in the book; it is re-priced only on fills or setting changes
        ref = book.best_bid(self.position.side)
        if self.stop.on_reference(ref):
            self._log(
                ts_ms, "stop",
                f"Triggered: {self.position.side.upper()} bid {_cents(ref)} ≤ stop {_cents(self.stop.level)} (HWM {_cents(self.stop.hwm)})",
            )
            self._start_exit(book, ts_ms, "stop")

    def on_trade(self, trade: dict, book: OrderBook, ts_ms: int) -> None:
        ticker = trade.get("market_ticker")
        taker = trade.get("taker_outcome_side") or trade.get("taker_side")
        try:
            t = Decimal(str(trade["yes_price_dollars"]))
            vol = Decimal(str(trade["count_fp"]))
        except (KeyError, ArithmeticError, ValueError):
            return
        for o in list(self.working()):
            if o.ticker != ticker or o.remaining <= 0:
                continue
            L = o.yes_level
            if o.book_side == "bid" and taker == "no":
                through, at = t < L, t == L
            elif o.book_side == "ask" and taker == "yes":
                through, at = t > L, t == L
            else:
                continue
            if through:
                self._fill(o, o.remaining, book, ts_ms)
            elif at:
                if o.queue_ahead >= vol:
                    o.queue_ahead -= vol
                else:
                    fillable = vol - o.queue_ahead
                    o.queue_ahead = Decimal(0)
                    self._fill(o, min(fillable, o.remaining), book, ts_ms)

    def on_window_close(self, ticker: str, yes_won: bool | None, ts_ms: int) -> None:
        for o in self.working():
            if o.ticker == ticker:
                self._cancel_order(o, ts_ms, "window closed")
        if self.position and self.position.ticker == ticker and self.position.qty > 0:
            pos = self.position
            self._go_flat()
            if yes_won is None:
                self.pending_settlement[ticker] = pos
                self._log(ts_ms, "close", f"Window closed holding {_qty(pos.qty)} {pos.side.upper()} — awaiting result", ticker=ticker)
            else:
                self._settle(ticker, pos, yes_won, ts_ms, "computed")
        elif self.position and self.position.ticker == ticker:
            self._go_flat()

    def reconcile(self, ticker: str, official_yes_won: bool, ts_ms: int) -> None:
        """Apply Kalshi's official result: settle pending positions or correct a computed one."""
        if ticker in self.pending_settlement:
            self._settle(ticker, self.pending_settlement.pop(ticker), official_yes_won, ts_ms, "official")
            return
        rec = self.settled.get(ticker)
        if not rec or rec["source"] == "official":
            return
        if rec["yes_won"] != official_yes_won:
            self.cash -= rec["payout"]
            self.realized -= rec["pnl"]
            self._log(ts_ms, "correction", "Computed result disagreed with Kalshi's official result — re-settling", ticker=ticker)
            self._settle(ticker, rec["position"], official_yes_won, ts_ms, "official")
        else:
            rec["source"] = "official"

    # ------------------------------------------------------------------ internals
    def _go_flat(self) -> None:
        self.position, self.stop, self.exiting, self.protected = None, None, False, False

    def _book_buy(self, o: Order, price: Decimal, qty: Decimal, fee: Decimal, ts: int) -> Decimal:
        """Record a buy fill: cash out, position up. Returns the cost incl. fee."""
        cost = price * qty + fee
        self.fills.append(Fill(o.id, o.ticker, o.side, "buy", price, qty, fee, ts))
        self.cash -= cost
        if self.position is None or self.position.ticker != o.ticker:
            self.position = Position(o.ticker, o.side)
        self.position.qty += qty
        self.position.cost += cost
        return cost

    def _book_sell(self, o: Order, price: Decimal, qty: Decimal, fee: Decimal, ts: int) -> Decimal:
        """Record a sell fill against the position at average cost. Returns realized P&L."""
        pos = self.position
        self.fills.append(Fill(o.id, o.ticker, o.side, "sell", price, qty, fee, ts))
        proceeds = price * qty - fee
        basis = pos.cost * (qty / pos.qty) if pos and pos.qty > 0 else Decimal(0)
        pnl = proceeds - basis
        self.cash += proceeds
        self.realized += pnl
        if pos:
            pos.qty -= qty
            pos.cost -= basis
        return pnl

    def _fill(self, o: Order, qty: Decimal, book: OrderBook, ts: int) -> None:
        if qty <= 0:
            return
        fee = self.maker_fee(o.price, qty)
        o.filled += qty
        if o.action == "buy":
            cost = self._book_buy(o, o.price, qty, fee, ts)
            o.reserved = max(Decimal(0), o.reserved - cost)
            self.protected = True          # limit entries always get the selected exit strategy
            self._log(ts, "fill", f"#{o.id} bought {_qty(qty)} {o.side.upper()} @ {_cents(o.price)} → position {_qty(self.position.qty)}")
            if self.exit_mode == "take_profit":
                self._sync_tp(book, ts)
            elif self.stop is None:
                self.stop = TrailingStop(self.trail_value, self.trail_mode, book.best_bid(o.side) or o.price)
        else:
            pnl = self._book_sell(o, o.price, qty, fee, ts)
            pos = self.position
            self._log(ts, "fill", f"#{o.id} sold {_qty(qty)} {o.side.upper()} @ {_cents(o.price)}", pnl=pnl)
            if pos and pos.qty <= 0:
                self._go_flat()
                self._log(ts, "flat", "Position closed")
        if o.remaining <= 0:
            o.status = "filled"
            o.reserved = Decimal(0)

    def _start_exit(self, book: OrderBook, ts: int, reason: str) -> None:
        self.exiting = True
        for o in self.working("entry"):
            if o.ticker == self.position.ticker:
                self._cancel_order(o, ts, f"{reason} — cancel entry remainder")
        self._chase(book, ts)

    def _chase(self, book: OrderBook, ts: int) -> None:
        pos = self.position
        if not pos or pos.qty <= 0:
            return
        bid = book.best_bid(pos.side)
        if bid is None:
            return
        top = (self.price_ranges or DEFAULT_PRICE_RANGES)[-1]
        target = min(bid + tick_at(bid, self.price_ranges), ONE - top[2])
        exits = self.working("exit")
        cur = exits[0] if exits else None
        if cur and cur.tag == "chase" and cur.price == target and cur.remaining == pos.qty:
            return
        for o in exits:
            self._cancel_order(o, ts, "re-price", quiet=True)
        o = self._new_order(pos.side, "sell", target, pos.qty, "exit", ts, book)
        o.tag = "chase"
        self._log(ts, "exit", f"#{o.id} sell {_qty(o.qty)} {o.side.upper()} @ {_cents(target)} (bid {_cents(bid)}, queue ahead {_qty(o.queue_ahead)})")

    def _settle(self, ticker: str, pos: Position, yes_won: bool, ts: int, source: str) -> None:
        won = (pos.side == "yes") == yes_won
        payout = pos.qty if won else Decimal(0)
        pnl = payout - pos.cost
        self.cash += payout
        self.realized += pnl
        self.settled[ticker] = {"position": pos, "yes_won": yes_won, "payout": payout, "pnl": pnl, "source": source}
        self._log(
            ts, "settle",
            f"{'YES' if yes_won else 'NO'} won ({source}) — {_qty(pos.qty)} {pos.side.upper()} paid ${payout:.2f}",
            pnl=pnl, ticker=ticker,
        )

    # ------------------------------------------------------------------ reporting
    def snapshot(self, book: OrderBook | None) -> dict:
        pos = self.position
        mark = unreal = None
        if pos and pos.qty > 0 and book is not None:
            bid = book.best_bid(pos.side)
            if bid is not None:
                mark = bid * pos.qty
                unreal = mark - pos.cost
        equity = self.cash + (mark or Decimal(0))
        stop = self.stop
        return {
            "cash": self.cash,
            "reserved": self.reserved,
            "available": self.available,
            "realized": self.realized,
            "unrealized": unreal,
            "equity": equity,
            "session_pnl": equity - self.starting_cash,
            "position": None if not pos else {
                "ticker": pos.ticker, "side": pos.side, "qty": pos.qty,
                "avg": pos.avg_price, "cost": pos.cost, "mark": mark,
            },
            "stop": None if not stop else {
                "state": stop.state, "hwm": stop.hwm, "level": stop.level,
                "trail_value": stop.trail_value, "trail_mode": stop.trail_mode,
            },
            "trail_value": self.trail_value,
            "trail_mode": self.trail_mode,
            "exit_mode": self.exit_mode,
            "exiting": self.exiting,
            "protected": self.protected,
            "tp_target": self.tp_target() if self.exit_mode == "take_profit" else None,
            "orders": [vars(o).copy() for o in self.working()],
            "log": [vars(e).copy() for e in reversed(self.log[-40:])],
            "pending": list(self.pending_settlement),
            "maker_fees": self.maker_fee_multiplier > 0,
        }
