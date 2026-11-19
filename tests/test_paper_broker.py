from decimal import Decimal as D

import pytest

from core.orderbook import OrderBook
from trading.intents import OrderIntent
from trading.paper_broker import PaperBroker
from trading.trailing_stop import TrailingStop

T = "KXBTC15M-TEST"


def book(yes, no):
    b = OrderBook(T)
    b.apply_snapshot({
        "market_ticker": T,
        "yes_dollars_fp": [[f"{p:.4f}", f"{q:.2f}"] for p, q in yes.items()],
        "no_dollars_fp": [[f"{p:.4f}", f"{q:.2f}"] for p, q in no.items()],
    })
    return b


def trade(yes_px, qty, taker):
    return {"market_ticker": T, "yes_price_dollars": f"{yes_px:.4f}", "count_fp": f"{qty:.2f}",
            "taker_side": taker, "taker_outcome_side": taker}


def delta(b, side, px, d):
    b.apply_delta({"side": side, "price_dollars": f"{px:.4f}", "delta_fp": f"{d:.2f}"})


@pytest.fixture
def setup():
    br = PaperBroker()
    br.set_ticker(T)
    # YES bids 50/49, NO bids 48/47  -> YES ask 52, NO ask 50
    b = book({0.50: 100, 0.49: 200}, {0.48: 100, 0.47: 50})
    return br, b


def test_book_derivations(setup):
    _, b = setup
    assert b.best_yes_bid == D("0.50") and b.best_yes_ask == D("0.52")
    assert b.best_no_bid == D("0.48") and b.best_no_ask == D("0.50")


def test_post_only_rejects_crossing(setup):
    br, b = setup
    ok, msg = br.submit(OrderIntent("yes", "buy", D("0.52"), D(10)), b, 0)
    assert not ok and "Post-only" in msg
    ok, _ = br.submit(OrderIntent("yes", "buy", D("0.51"), D(10)), b, 0)
    assert ok
    ok, _ = br.submit(OrderIntent("no", "buy", D("0.50"), D(10)), b, 0)
    assert not ok  # crosses NO ask 50 — and an opposite-side entry is working anyway


def test_reserve_and_buying_power(setup):
    br, b = setup
    ok, _ = br.submit(OrderIntent("yes", "buy", D("0.50"), D(1000)), b, 0)
    assert ok and br.reserved == D("500.00") and br.available == D("500.00")
    ok, msg = br.submit(OrderIntent("yes", "buy", D("0.49"), D(2000)), b, 0)
    assert not ok and "buying power" in msg


def test_queue_model(setup):
    br, b = setup
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(30)), b, 0)   # 100 ahead
    o = br.working()[0]
    assert o.queue_ahead == D(100)
    br.on_trade(trade(0.50, 60, "yes"), b, 1)    # taker bought YES: doesn't touch YES bids
    assert o.queue_ahead == D(100)
    br.on_trade(trade(0.50, 60, "no"), b, 2)     # hits YES bids at 50
    assert o.queue_ahead == D(40) and o.filled == 0
    br.on_trade(trade(0.50, 50, "no"), b, 3)     # 40 to queue, 10 to us
    assert o.queue_ahead == 0 and o.filled == D(10)
    br.on_trade(trade(0.49, 1, "no"), b, 4)      # traded through our level -> full fill
    assert o.filled == D(30) and o.status == "filled"
    assert br.position.qty == D(30) and br.cash == D(1000) - D("15.00")


def test_cancellation_does_not_advance_queue(setup):
    br, b = setup
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(10)), b, 0)
    o = br.working()[0]
    delta(b, "yes", 0.50, -90)                   # size vanishes without a trade
    br.on_book(b, 1)
    assert o.queue_ahead == D(100)


def test_no_side_entry_fills_on_taker_yes(setup):
    br, b = setup
    br.submit(OrderIntent("no", "buy", D("0.48"), D(10)), b, 0)    # rests at YES ask 52
    o = br.working()[0]
    assert o.book_side == "ask" and o.yes_level == D("0.52") and o.queue_ahead == D(100)
    br.on_trade(trade(0.53, 5, "yes"), b, 1)     # taker paid more than our level -> through
    assert o.filled == D(10)


def _filled_yes(br, b, qty=10, px="0.50"):
    br.submit(OrderIntent("yes", "buy", D(px), D(qty)), b, 0)
    br.on_trade(trade(float(px) - 0.01, 1, "no"), b, 1)
    assert br.position and br.position.qty == D(qty)


def test_stop_trails_triggers_and_chases(setup):
    br, b = setup
    _filled_yes(br, b)
    st = br.stop
    assert st.hwm == D("0.50") and st.level == D("0.45")
    delta(b, "yes", 0.55, 10); br.on_book(b, 2)          # bid up to 55
    assert st.hwm == D("0.55") and st.level == D("0.50")
    delta(b, "yes", 0.55, -10); br.on_book(b, 3)          # back to 50 -> trigger (50 <= 50)
    assert st.state == TrailingStop.TRIGGERED
    ex = br.working("exit")[0]
    assert ex.action == "sell" and ex.side == "yes" and ex.price == D("0.51") and ex.qty == D(10)
    assert ex.queue_ahead == 0                             # 51 improves the 52 ask
    # bid drops -> exit re-prices to new bid + 1
    delta(b, "yes", 0.50, -100); br.on_book(b, 4)
    ex2 = br.working("exit")[0]
    assert ex2.id != ex.id and ex2.price == D("0.50")
    # bid recovers above 90 later: exit keeps running
    delta(b, "yes", 0.91, 5); br.on_book(b, 5)
    assert br.stop.state == TrailingStop.TRIGGERED and br.working("exit")[0].price == D("0.911")  # 0.1¢ ticks above 90¢


def test_exit_fill_realizes_pnl(setup):
    br, b = setup
    _filled_yes(br, b)
    br.exit_now(b, 2)
    ex = br.working("exit")[0]
    assert ex.price == D("0.51")
    br.on_trade(trade(0.52, 5, "yes"), b, 3)                # taker lifts above us -> full fill
    assert br.position is None and br.stop is None
    assert br.realized == D("0.10") and br.cash == D("1000.10")


def test_stop_off_at_90_and_rearms():
    st = TrailingStop(D(5), "cents", D("0.80"))
    assert not st.on_reference(D("0.92")) and st.state == TrailingStop.OFF and st.hwm == D("0.92")
    # dropping from 92 straight to 86: below 90 re-arms, and 86 <= stop 87 -> triggers at once
    assert st.on_reference(D("0.86")) and st.state == TrailingStop.TRIGGERED
    st2 = TrailingStop(D(5), "cents", D("0.80"))
    st2.on_reference(D("0.92")); st2.on_reference(D("0.90"))
    assert st2.state == TrailingStop.OFF
    assert not st2.on_reference(D("0.89")) and st2.state == TrailingStop.ARMED  # re-armed, 89 > 87
    assert st2.on_reference(D("0.87")) and st2.state == TrailingStop.TRIGGERED


def test_percent_trail():
    st = TrailingStop(D(10), "pct", D("0.55"))
    assert st.level == D("0.495")                              # floored to the 0.1¢ grid
    assert st.on_reference(D("0.49"))


def test_stop_grows_with_fills_and_trigger_cancels_entry(setup):
    br, b = setup
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(30)), b, 0)
    o = br.working()[0]
    br.on_trade(trade(0.50, 110, "no"), b, 1)                   # 10 filled
    assert br.position.qty == D(10)
    br.on_trade(trade(0.50, 5, "no"), b, 2)                     # 5 more
    assert br.position.qty == D(15)
    delta(b, "yes", 0.50, -100); br.on_book(b, 3)               # bid 49 -> no trigger (stop 45)
    delta(b, "yes", 0.49, -200); br.on_book(b, 4)               # book empty on YES side
    delta(b, "yes", 0.44, 50); br.on_book(b, 5)                 # bid 44 -> trigger
    assert o.status == "cancelled" and br.reserved == 0
    ex = br.working("exit")[0]
    assert ex.qty == D(15) and ex.price == D("0.45")


def test_settlement_and_reconcile(setup):
    br, b = setup
    _filled_yes(br, b)
    br.on_window_close(T, True, 10)                             # computed: YES won
    assert br.cash == D("1005.00") and br.realized == D("5.00")
    br.reconcile(T, False, 20)                                  # official disagrees
    assert br.cash == D("995.00") and br.realized == D("-5.00")


def test_window_close_cancels_orders_and_pending(setup):
    br, b = setup
    _filled_yes(br, b)
    br.submit(OrderIntent("yes", "buy", D("0.49"), D(5)), b, 2)
    br.on_window_close(T, None, 10)
    assert not br.working() and T in br.pending_settlement
    br.reconcile(T, True, 20)
    assert br.cash == D("1005.00") and not br.pending_settlement


def test_opposite_side_blocked_while_holding(setup):
    br, b = setup
    _filled_yes(br, b)
    ok, msg = br.submit(OrderIntent("no", "buy", D("0.45"), D(5)), b, 2)
    assert not ok and "close it" in msg


def test_replace_resets_queue(setup):
    br, b = setup
    br.submit(OrderIntent("yes", "buy", D("0.49"), D(10)), b, 0)
    o = br.working()[0]
    ok, _ = br.replace(o.id, D("0.51"), D(10), b, 1)
    assert ok and o.status == "cancelled"
    n = br.working()[0]
    assert n.price == D("0.51") and n.queue_ahead == 0


def test_maker_fee_model():
    br = PaperBroker()
    br.set_fee_model("quadratic", 1)
    assert br.maker_fee(D("0.50"), D(100)) == 0
    br.set_fee_model("quadratic_with_maker_fees", 1)
    assert br.maker_fee(D("0.50"), D(100)) == D("0.44")


def test_exit_chase_uses_subcent_ticks_below_10c():
    br = PaperBroker()
    br.set_ticker(T)
    b = book({0.12: 50}, {0.86: 50})                 # YES 12 / 14
    br.submit(OrderIntent("yes", "buy", D("0.12"), D(10)), b, 0)
    br.on_trade(trade(0.11, 1, "no"), b, 1)
    delta(b, "yes", 0.12, -50)
    b.apply_delta({"side": "yes", "price_dollars": "0.0500", "delta_fp": "20.00"})
    br.on_book(b, 2)                                   # bid 5¢ -> trigger, exit at 5.1¢ not 6¢
    assert br.working("exit")[0].price == D("0.051")


def test_tick_at():
    from core.market import tick_at
    assert tick_at(D("0.05")) == D("0.001") and tick_at(D("0.50")) == D("0.01")
    assert tick_at(D("0.095")) == D("0.001") and tick_at(D("0.10")) == D("0.01") and tick_at(D("0.93")) == D("0.001")
