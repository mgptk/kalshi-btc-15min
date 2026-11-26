import json
from decimal import Decimal as D
import threading

import pytest

from core.config import Settings
from core.persist import FileStore, GistStore, Persister, StoreError, make_store
from trading.intents import OrderIntent
from trading.paper_broker import PaperBroker
from tests.test_paper_broker import T, book, delta, trade

T2 = "KXBTC15M-NEXT"


def _book():
    return book({0.50: 100, 0.49: 200}, {0.48: 100, 0.47: 50})   # YES 50 / 52


def _traded_broker():
    """Round trip with a profit, then an open position."""
    br = PaperBroker(); br.set_ticker(T); b = _book()
    br.market_order("yes", "buy", D(10), b, 0)
    delta(b, "yes", 0.60, 50)
    br.market_order("yes", "sell", D(10), b, 1)                   # sells at 60
    br.submit(OrderIntent("yes", "buy", D("0.50"), D(4)), b, 2)
    br.on_trade(trade(0.49, 1, "no"), b, 3)                       # limit fill -> protected
    return br, b


class MemStore:
    kind, label = "mem", "memory"

    def __init__(self, state=None, fail_load=False):
        self.state, self.fail_load, self.saves = state, fail_load, 0

    def load(self):
        if self.fail_load:
            raise StoreError("offline")
        return json.loads(json.dumps(self.state)) if self.state else None

    def save(self, state):
        self.state = json.loads(json.dumps(state))
        self.saves += 1


def test_ledger_tracks_realized_and_corrections():
    br, b = _traded_broker()
    assert sum(D(e["pnl"]) for e in br.ledger) == br.realized.quantize(D("0.000001"))
    br.on_window_close(T, True, 10)                               # computed: YES won
    br.reconcile(T, False, 11)                                    # official: NO -> correction
    assert [e["event"] for e in br.ledger] == ["market", "settle", "correction", "settle"]
    assert sum(D(e["pnl"]) for e in br.ledger) == br.realized.quantize(D("0.000001"))


def test_state_round_trip_restores_account_and_exit():
    br, b = _traded_broker()
    st = json.loads(json.dumps(br.to_state()))
    br2 = PaperBroker()
    br2.load_state(st)
    assert br2.cash == br.cash and br2.realized == br.realized and br2.ledger == br.ledger
    assert br2.position.qty == D(4) and br2.position.cost == br.position.cost
    assert br2.protected and br2.stop is None and not br2.working()
    br2.set_ticker(T)                                             # same window: position kept
    assert br2.position is not None
    br2.on_book(b, 5)                                             # stop re-attached on first book
    assert br2.stop is not None and br2.stop.hwm == D("0.60")
    a = br2.snapshot(b)
    assert a["session_pnl"] == a["equity"] - (br2.cash + br2.position.cost)
    assert a["all_time_pnl"] == a["equity"] - D(1000)


def test_restored_take_profit_is_re_posted():
    br, b = _traded_broker()
    br.set_exit_mode("take_profit", b, 4)
    br2 = PaperBroker(); br2.load_state(br.to_state()); br2.set_ticker(T)
    assert not br2.working()
    br2.on_book(b, 5)
    tp = br2.working("exit")[0]
    assert tp.tag == "tp" and tp.qty == D(4)


def test_market_position_restores_without_exit():
    br = PaperBroker(); br.set_ticker(T); b = _book()
    br.market_order("yes", "buy", D(5), b, 0)
    br2 = PaperBroker(); br2.load_state(br.to_state()); br2.set_ticker(T); br2.on_book(b, 1)
    assert br2.position.qty == D(5) and br2.stop is None and not br2.protected


def test_position_from_closed_window_waits_for_official_result():
    br, _ = _traded_broker()
    cash = br.cash
    br2 = PaperBroker(); br2.load_state(br.to_state())
    br2.set_ticker(T2)                                            # app came back in a later window
    assert br2.position is None and T in br2.pending_settlement
    assert br2.unresolved_tickers() == [T]
    br2.reconcile(T, True, 9)                                     # YES won: 4 contracts pay $4
    assert br2.cash == cash + D(4) and not br2.pending_settlement


def test_unreconciled_computed_settlement_survives_restart():
    br, _ = _traded_broker()
    br.on_window_close(T, True, 10)                               # computed only
    br2 = PaperBroker(); br2.load_state(json.loads(json.dumps(br.to_state())))
    assert br2.unresolved_tickers() == [T]
    br2.reconcile(T, False, 11)                                   # official disagrees -> corrected
    assert br2.realized == br.realized - D(4)


def test_reset_account():
    br, b = _traded_broker()
    assert not br.reset_account(0)[0]                             # position open
    br.market_order("yes", "sell", D(4), b, 1)
    ok, _ = br.reset_account(2)
    assert ok and br.cash == D(1000) and br.realized == 0 and not br.ledger


def test_bad_version_rejected():
    with pytest.raises(ValueError):
        PaperBroker().load_state({"version": 99})


def test_persister_restores_and_saves_only_on_change():
    br, _ = _traded_broker()
    store = MemStore(br.to_state())
    br2, lock = PaperBroker(), threading.RLock()
    p = Persister(br2, store, lock)
    assert p.load() and p.restored and br2.cash == br.cash
    assert not p.save_if_changed() and store.saves == 0           # nothing new yet
    br2.set_ticker(T); b = _book()
    br2.market_order("yes", "sell", D(4), b, 1)
    assert p.save_if_changed() and store.saves == 1
    assert store.state["position"] is None and len(store.state["ledger"]) == 2


def test_persister_never_saves_after_failed_load():
    store = MemStore({"version": 1}, fail_load=True)
    br = PaperBroker(); br.set_ticker(T)
    p = Persister(br, store, threading.RLock())
    assert not p.load(attempts=2, delay=0) and p.error
    br.market_order("yes", "buy", D(1), _book(), 0)
    assert not p.save_if_changed() and store.saves == 0 and store.state == {"version": 1}


def test_persister_refuses_to_overwrite_unreadable_state():
    store = MemStore({"version": 99})
    p = Persister(PaperBroker(), store, threading.RLock())
    assert not p.load() and "couldn't be read" in p.error and not p.save_if_changed()


def test_file_store_round_trip(tmp_path):
    fs = FileStore(str(tmp_path / "sub" / "state.json"))
    assert fs.load() is None
    fs.save({"version": 1, "x": "1"})
    assert fs.load() == {"version": 1, "x": "1"}
    (tmp_path / "sub" / "state.json").write_text("{nope")
    with pytest.raises(StoreError):
        fs.load()


class FakeResp:
    def __init__(self, code, body=None, text=""):
        self.status_code, self._body, self.text = code, body, text or json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, files):
        self.headers, self.files, self.calls = {}, files, []

    def get(self, url, timeout):
        self.calls.append(("GET", url))
        return FakeResp(200, {"files": self.files})

    def patch(self, url, json, timeout):
        self.calls.append(("PATCH", url))
        for k, v in json["files"].items():
            self.files[k] = {"content": v["content"]}
        return FakeResp(200, {})


def test_gist_store_load_and_save():
    sess = FakeSession({})
    g = GistStore("abc123", "tok", "paper_state_live.json", session=sess)
    assert g.load() is None                                       # file not in the gist yet
    g.save({"version": 1})
    assert g.load() == {"version": 1}
    assert sess.headers["Authorization"] == "Bearer tok"
    assert ("PATCH", "https://api.github.com/gists/abc123") in sess.calls


def test_gist_store_http_error():
    sess = FakeSession({})
    sess.get = lambda url, timeout: FakeResp(404, {"message": "Not Found"})
    with pytest.raises(StoreError):
        GistStore("x", "t", "f.json", session=sess).load()


def test_make_store_selection():
    base = dict(api_key_id=None, private_key_pem=None)
    assert isinstance(make_store(Settings("sim", **base)), FileStore)
    assert make_store(Settings("sim", **base)).path.endswith("paper_state_sim.json")
    assert make_store(Settings("kalshi", **base)).path.endswith("paper_state_live.json")
    g = make_store(Settings("kalshi", **base, gist_id="g", github_token="t"))
    assert isinstance(g, GistStore) and g.filename == "paper_state_live.json"
    assert make_store(Settings("kalshi", **base, persist="off")) is None
