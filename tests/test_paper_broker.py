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


# ---------------------------------------------------------------- take profit
def _tp_broker():
    br = PaperBroker(exit_mode="take_profit")
    br.set_ticker(T)
    b = book({0.50: 100, 0.49: 200}, {0.48: 100, 0.47: 50})   # YES 50 / 52
    return br, b


def test_take_profit_rests_at_avg_plus_distance_and_tracks_fills():
    br, b = _tp_broker()
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(30)), b, 0)
    br.on_trade(trade(0.50, 110, "no"), b, 1)                   # 10 filled (100 ahead)
    assert br.stop is None
    tp = br.working("exit")[0]
    assert tp.tag == "tp" and tp.action == "sell" and tp.price == D("0.55") and tp.qty == D(10)
    assert tp.queue_ahead == 0                                  # 55 is above the 52 ask: nobody there
    br.on_trade(trade(0.50, 5, "no"), b, 2)                     # 5 more -> TP grows to 15
    tp2 = br.working("exit")[0]
    assert tp2.qty == D(15) and tp2.price == D("0.55") and tp.status == "cancelled"


def test_take_profit_fill_realizes_profit():
    br, b = _tp_broker()
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(10)), b, 0)
    br.on_trade(trade(0.49, 1, "no"), b, 1)
    br.on_book(b, 2)                                            # book moves don't touch the TP
    br.on_trade(trade(0.56, 4, "yes"), b, 3)                    # taker paid through 55 -> filled
    assert br.position is None and br.realized == D("0.50") and br.cash == D("1000.50")


def test_take_profit_pct_and_rounding_up():
    br, b = _tp_broker()
    br.set_trail(D(10), "pct", b, 0)
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(10)), b, 0)
    br.on_trade(trade(0.49, 1, "no"), b, 1)
    assert br.working("exit")[0].price == D("0.55")             # 50 * 1.10
    br.set_trail(D("7.5"), "pct", b, 2)                         # 53.75 -> rounds up to 54
    assert br.working("exit")[0].price == D("0.54")


def test_take_profit_already_past_target_stays_maker():
    br, b = _tp_broker()
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(10)), b, 0)
    br.on_trade(trade(0.49, 1, "no"), b, 1)
    delta(b, "no", 0.48, -100); delta(b, "no", 0.47, -50)       # clear asks
    delta(b, "yes", 0.60, 20)                                    # bid jumps to 60 (> 55 target)
    br.set_trail(D(6), "cents", b, 2)                            # target 56 <= bid 60 -> bid + 1 tick
    assert br.working("exit")[0].price == D("0.61")


def test_exit_now_in_take_profit_mode_cancels_tp_and_chases():
    br, b = _tp_broker()
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(10)), b, 0)
    br.on_trade(trade(0.49, 1, "no"), b, 1)
    tp = br.working("exit")[0]
    ok, _ = br.exit_now(b, 2)
    assert ok and br.exiting and tp.status == "cancelled"
    ex = br.working("exit")[0]
    assert ex.tag == "chase" and ex.price == D("0.51")
    delta(b, "yes", 0.50, -100); br.on_book(b, 3)               # bid 49 -> chase re-prices to 50
    assert br.working("exit")[0].price == D("0.50")


def test_switch_modes_with_open_position():
    br = PaperBroker()
    br.set_ticker(T)
    b = book({0.50: 100, 0.49: 200}, {0.48: 100, 0.47: 50})
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(10)), b, 0)
    br.on_trade(trade(0.49, 1, "no"), b, 1)
    assert br.stop is not None and not br.working("exit")
    ok, msg = br.set_exit_mode("take_profit", b, 2)
    assert ok and br.stop is None and br.working("exit")[0].price == D("0.55")
    ok, _ = br.set_exit_mode("trailing", b, 3)
    assert ok and not br.working("exit") and br.stop.hwm == D("0.50") and br.stop.level == D("0.45")
    assert br.set_exit_mode("bogus", b, 4)[0] is False


def test_take_profit_mode_has_no_stop_loss():
    br, b = _tp_broker()
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(10)), b, 0)
    br.on_trade(trade(0.49, 1, "no"), b, 1)
    delta(b, "yes", 0.50, -100); delta(b, "yes", 0.49, -200)
    delta(b, "yes", 0.20, 10); br.on_book(b, 2)                  # bid collapses to 20
    assert not br.exiting and br.working("exit")[0].tag == "tp"


# ---------------------------------------------------------------- market orders
def test_taker_fee_formula():
    br = PaperBroker()
    assert br.taker_fee(D("0.50"), D(100)) == D("1.75")          # 0.07 * 100 * .5 * .5
    assert br.taker_fee(D("0.52"), D(1)) == D("0.02")            # 0.017472 rounds up
    br.set_fee_model("quadratic", 0.5)
    assert br.taker_fee(D("0.50"), D(100)) == D("0.88")


def test_market_buy_walks_asks_pays_taker_fee_and_has_no_exit(setup):
    br, b = setup                                                 # YES asks 52 x100, 53 x50
    ok, msg = br.market_order("yes", "buy", D(120), b, 0)
    assert ok, msg
    pos = br.position
    # 100 @ 52 (fee 1.75) + 20 @ 53 (fee 0.07*20*.53*.47 = 0.34874 -> 0.35)
    assert pos.qty == D(120) and pos.cost == D("52.00") + D("10.60") + D("1.75") + D("0.35")
    assert br.cash == D(1000) - pos.cost
    assert [(f.price, f.qty, f.fee) for f in br.fills] == [(D("0.52"), D(100), D("1.75")),
                                                          (D("0.53"), D(20), D("0.35"))]
    assert br.stop is None and not br.working() and not br.protected
    delta(b, "yes", 0.50, -100); delta(b, "yes", 0.49, -200); delta(b, "yes", 0.10, 5)
    br.on_book(b, 1)                                              # bid collapses: nothing triggers
    assert not br.exiting and not br.working()


def test_market_buy_take_profit_mode_still_unprotected():
    br, b = _tp_broker()
    assert br.market_order("yes", "buy", D(10), b, 0)[0]
    assert not br.working("exit") and br.stop is None
    br.set_trail(D(6), "cents", b, 1)                             # changing distance doesn't attach
    assert not br.working("exit")


def test_market_buy_partial_when_book_thin(setup):
    br, b = setup
    ok, msg = br.market_order("no", "buy", D(500), b, 0)          # NO asks: 50 x100, 51 x200
    assert ok and "200 unfilled" in msg and br.position.qty == D(300)


def test_market_buy_rejections(setup):
    br, b = setup
    assert not br.market_order("yes", "buy", D("1.5"), b, 0)[0]
    assert br.market_order("yes", "buy", D(10), b, 0)[0]
    br2 = PaperBroker(starting_cash=D(10)); br2.set_ticker(T)
    ok, msg = br2.market_order("yes", "buy", D(100), b, 0)
    assert not ok and "buying power" in msg
    ok, msg = br.market_order("no", "buy", D(1), b, 0)            # holding YES
    assert not ok and "hold YES" in msg
    empty = book({0.50: 10}, {})
    br3 = PaperBroker(); br3.set_ticker(T)
    ok, msg = br3.market_order("yes", "buy", D(1), empty, 0)
    assert not ok and "No YES asks" in msg


def test_market_sell_hits_bids_and_realizes_pnl(setup):
    br, b = setup
    br.market_order("yes", "buy", D(10), b, 0)                    # 10 @ 52, fee 0.18
    cost = br.position.cost
    assert cost == D("5.38")
    ok, msg = br.market_order("yes", "sell", D(4), b, 1)          # 4 @ 50 bid, fee 0.07
    assert ok, msg
    assert br.position.qty == D(6)
    pnl = D("2.00") - D("0.07") - cost * D(4) / D(10)
    assert br.realized == pnl
    ok, _ = br.market_order("yes", "sell", D(6), b, 2)
    assert ok and br.position is None and not br.protected


def test_market_sell_rejections(setup):
    br, b = setup
    ok, msg = br.market_order("yes", "sell", D(1), b, 0)
    assert not ok and "No YES position" in msg
    br.market_order("yes", "buy", D(10), b, 0)
    ok, msg = br.market_order("no", "sell", D(1), b, 1)
    assert not ok and "No NO position" in msg
    ok, msg = br.market_order("yes", "sell", D(11), b, 1)
    assert not ok and "only hold 10" in msg


def test_attach_exit_to_market_position_afterwards(setup):
    br, b = setup
    br.market_order("yes", "buy", D(10), b, 0)
    assert br.exit_mode == "trailing" and br.stop is None
    ok, msg = br.set_exit_mode("trailing", b, 1)                  # same mode: attaches the stop
    assert ok and br.protected and br.stop.hwm == D("0.50") and br.stop.level == D("0.45")
    ok, _ = br.set_exit_mode("take_profit", b, 2)
    tp = br.working("exit")[0]
    assert tp.tag == "tp" and tp.qty == D(10) and tp.price == br.tp_target()


def test_market_buy_into_protected_position_resizes_take_profit():
    br, b = _tp_broker()
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(10)), b, 0)
    br.on_trade(trade(0.49, 1, "no"), b, 1)                       # limit fill -> TP for 10
    assert br.working("exit")[0].qty == D(10)
    br.market_order("yes", "buy", D(5), b, 2)
    tp = br.working("exit")[0]
    assert tp.qty == D(15) and tp.price == br.tp_target()


def test_market_sell_resizes_or_cancels_exit_orders():
    br, b = _tp_broker()
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(10)), b, 0)
    br.on_trade(trade(0.49, 1, "no"), b, 1)
    br.market_order("yes", "sell", D(4), b, 2)
    assert br.working("exit")[0].qty == D(6)
    br.market_order("yes", "sell", D(6), b, 3)
    assert br.position is None and not br.working()


def test_market_sell_during_chase_reprices_chase(setup):
    br, b = setup
    _filled_yes(br, b)
    br.exit_now(b, 2)
    assert br.working("exit")[0].qty == D(10)
    br.market_order("yes", "sell", D(3), b, 3)
    ex = br.working("exit")
    assert len(ex) == 1 and ex[0].qty == D(7) and ex[0].tag == "chase"


def test_limit_fill_after_market_buy_attaches_exit(setup):
    br, b = setup
    br.market_order("yes", "buy", D(5), b, 0)
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(5)), b, 1)
    br.on_trade(trade(0.49, 1, "no"), b, 2)
    assert br.position.qty == D(10) and br.protected and br.stop is not None
