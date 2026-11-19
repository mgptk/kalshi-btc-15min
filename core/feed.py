"""FeedService: one background thread holding one Kalshi WebSocket.

Maintains the current KXBTC15M market, its order book and trades, BRTI + averages, and
drives the broker on every market event. The UI (and later the bot) only:
  * read `snapshot()` -> MarketState
  * call the thread-safe action methods (place / cancel / replace / set_trail / exit_now)
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import traceback
from decimal import Decimal
from typing import Callable

from .config import INDEX_ID, SERIES_TICKER, Settings
from .index import IndexState
from .market import KalshiRest, MarketInfo, now_ms, parse_market
from .orderbook import OrderBook
from .state import MarketState

log = logging.getLogger("btc15m.feed")

CLOSE_GRACE_MS = 2500          # wait for the close tick before settling
RESULT_POLL_S = 15
RESULT_POLL_MAX_S = 15 * 60


class FeedService:
    def __init__(self, settings: Settings, broker):
        self.settings = settings
        self.broker = broker
        self.lock = threading.RLock()
        self.book = OrderBook()
        self.index = IndexState()
        self.market: MarketInfo | None = None
        self.target_source = ""
        self.connected = False
        self.error: str | None = None
        self.fee_type: str | None = None
        self.last_trade: dict | None = None
        self.listeners: list[Callable[[str], None]] = []
        self._thread: threading.Thread | None = None
        self._cmd_id = 0
        self._sids: dict[str, int] = {}
        self._seq: dict[int, int] = {}
        self._conn = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rest: KalshiRest | None = None
        self._sim = None
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "FeedService":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run_thread, name="btc15m-feed", daemon=True)
            self._thread.start()
        return self

    def _run_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        if self.settings.source == "sim":
            from .sim import SimExchange
            self._sim = SimExchange()
            self.fee_type = "quadratic (sim)"
        else:
            self._rest = KalshiRest(self.settings.rest_base)
            try:
                ft, mult = await asyncio.to_thread(self._rest.series_fee_type, SERIES_TICKER)
                with self.lock:
                    self.fee_type = ft
                    self.broker.set_fee_model(ft, mult)
            except Exception as e:  # non-fatal
                self._set_error(f"Fee check failed: {e}")
        backoff = 1.0
        while True:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("feed session error: %s", traceback.format_exc())
                self._set_error(f"{type(e).__name__}: {e}")
            with self.lock:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _connect(self):
        if self._sim is not None:
            from .sim import SimConnection
            return SimConnection(self._sim)
        from .auth import KalshiSigner
        from .ws import ws_connect
        signer = KalshiSigner(self.settings.api_key_id, self.settings.private_key_pem)
        headers = signer.sign("GET", "/trade-api/ws/v2")
        return ws_connect(
            self.settings.ws_url, headers,
            ping_interval=20, ping_timeout=20, max_size=None, open_timeout=15,
        )

    async def _session(self) -> None:
        async with self._connect() as conn:
            self._conn = conn
            self._sids.clear()
            self._seq.clear()
            with self.lock:
                self.connected = True
                self.error = None
            await self._send("subscribe", {"channels": ["cfbenchmarks_value"], "index_ids": [INDEX_ID]})
            await self._roll_market()
            clock = asyncio.create_task(self._clock())
            try:
                async for raw in conn:
                    self._on_raw(raw)
                    if clock.done():
                        clock.result()  # surface clock errors
            finally:
                clock.cancel()

    # ------------------------------------------------------------------ commands
    async def _send(self, cmd: str, params: dict) -> int:
        self._cmd_id += 1
        await self._conn.send(json.dumps({"id": self._cmd_id, "cmd": cmd, "params": params}))
        return self._cmd_id

    async def _subscribe_market(self, ticker: str) -> None:
        old = [self._sids.pop(c) for c in ("orderbook_delta", "trade") if c in self._sids]
        if old:
            await self._send("unsubscribe", {"sids": old})
        with self.lock:
            self.book = OrderBook(ticker)
        await self._send("subscribe", {"channels": ["orderbook_delta", "trade"], "market_tickers": [ticker]})

    async def _discover(self) -> MarketInfo | None:
        t = now_ms()
        if self._sim is not None:
            return parse_market(self._sim.current_market(t))
        return await asyncio.to_thread(self._rest.current_market, t)

    async def _roll_market(self) -> None:
        mkt = None
        for attempt in range(40):
            mkt = await self._discover()
            if mkt and mkt.close_ms > now_ms():
                break
            await asyncio.sleep(min(1 + attempt, 5))
        if not mkt:
            raise RuntimeError("No open KXBTC15M market found")
        old = self.market
        if old and old.ticker != mkt.ticker:
            self._close_window(old)
        target, src = mkt.target, "market"
        if target is None:
            prev = self.index.settlement_for(mkt.open_ms)
            target, src = (prev[0], "prior window avg") if prev else (None, "")
        with self.lock:
            mkt.target = target
            self.market = mkt
            self.target_source = src
            self.broker.set_ticker(mkt.ticker, mkt.price_ranges)
        await self._subscribe_market(mkt.ticker)
        self._notify("market")

    async def _clock(self) -> None:
        """Watches the close time; settles and rolls to the next window."""
        while True:
            await asyncio.sleep(0.25)
            mkt = self.market
            if not mkt:
                continue
            if mkt.target is None and self._rest is not None and int(time.time()) % 5 == 0:
                await self._refresh_target(mkt)
            if now_ms() < mkt.close_ms + CLOSE_GRACE_MS:
                continue
            await self._roll_market()

    def _close_window(self, mkt: MarketInfo) -> None:
        """Settle the finished window (computed result now, official result reconciled later)."""
        final = self.index.settlement_for(mkt.close_ms)
        yes_won = None
        if final and mkt.target is not None and final[1] >= 55:
            yes_won = final[0] >= mkt.target
        with self.lock:
            self.broker.on_window_close(mkt.ticker, yes_won, now_ms())
        self._spawn(self._poll_result(mkt.ticker))

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _refresh_target(self, mkt: MarketInfo) -> None:
        try:
            m = await asyncio.to_thread(self._rest.market, mkt.ticker)
            fs = m.get("floor_strike")
            if fs not in (None, ""):
                with self.lock:
                    mkt.target, self.target_source = Decimal(str(fs)), "market"
        except Exception:
            pass

    async def _poll_result(self, ticker: str) -> None:
        waited = 0
        while waited < RESULT_POLL_MAX_S:
            await asyncio.sleep(RESULT_POLL_S if self._sim is None else 2)
            waited += RESULT_POLL_S
            try:
                if self._sim is not None:
                    res = self._sim.result(ticker)
                    res = None if res is None else ("yes" if res else "no")
                else:
                    res = (await asyncio.to_thread(self._rest.market, ticker)).get("result")
            except Exception:
                continue
            if res in ("yes", "no"):
                with self.lock:
                    self.broker.reconcile(ticker, res == "yes", now_ms())
                return

    # ------------------------------------------------------------------ message handling
    def _on_raw(self, raw) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        typ = m.get("type")
        msg = m.get("msg") or {}
        ts = m.get("sending_ts_ms") or now_ms()
        if typ == "subscribed":
            self._sids[msg.get("channel")] = msg.get("sid")
            return
        if typ == "error":
            self._set_error(f"WS error {msg.get('code')}: {msg.get('msg')}")
            return
        if typ in ("orderbook_snapshot", "orderbook_delta"):
            self._on_book_msg(typ, m, msg, ts)
        elif typ == "trade":
            if self.market and msg.get("market_ticker") == self.market.ticker:
                with self.lock:
                    self.last_trade = msg
                    self.broker.on_trade(msg, self.book, now_ms())
                self._notify("trade")
        elif typ == "cfbenchmarks_value":
            if msg.get("index_id", INDEX_ID) == INDEX_ID:
                with self.lock:
                    self.index.apply(msg)
                self._notify("index")

    def _on_book_msg(self, typ: str, m: dict, msg: dict, ts: int) -> None:
        if not self.market or msg.get("market_ticker") != self.market.ticker:
            return
        sid, seq = m.get("sid"), m.get("seq")
        if typ == "orderbook_delta" and sid in self._seq and seq is not None and seq != self._seq[sid] + 1:
            self._set_error(f"Order book sequence gap ({self._seq[sid]}→{seq}); resubscribing")
            self._seq.pop(sid, None)
            self._spawn(self._subscribe_market(self.market.ticker))
            return
        if seq is not None:
            self._seq[sid] = seq
        with self.lock:
            if typ == "orderbook_snapshot":
                self.book.apply_snapshot(msg, ts)
            elif self.book.ready:
                self.book.apply_delta(msg, ts)
            else:
                return
            self.broker.on_book(self.book, now_ms())
        self._notify("book")

    def _set_error(self, text: str) -> None:
        with self.lock:
            self.error = text

    def _notify(self, kind: str) -> None:
        for cb in list(self.listeners):
            try:
                cb(kind)
            except Exception:
                pass

    # ------------------------------------------------------------------ thread-safe API
    def snapshot(self) -> MarketState:
        with self.lock:
            b, ix, mk = self.book, self.index, self.market
            lad = b.ladder(5) if b.ready else {"bids": [], "asks": []}
            st = MarketState(
                ts_ms=now_ms(), source=self.settings.source, connected=self.connected,
                error=self.error, fee_type=self.fee_type,
                ticker=mk.ticker if mk else None, window=mk.window_label if mk else "",
                open_ms=mk.open_ms if mk else None, close_ms=mk.close_ms if mk else None,
                target=mk.target if mk else None, target_source=self.target_source,
                brti=ix.value, brti_ts_ms=ix.value_ts_ms, avg60=ix.avg60,
                final_avg=ix.final_avg, final_n=ix.final_n,
                history=[h for h in ix.history if mk is None or h[0] >= mk.open_ms - 60_000],
                book_ready=b.ready, yes_bid=b.best_yes_bid, yes_ask=b.best_yes_ask,
                no_bid=b.best_no_bid, no_ask=b.best_no_ask,
                bids=lad["bids"], asks=lad["asks"], last_trade=dict(self.last_trade) if self.last_trade else None,
                account=self.broker.snapshot(b if b.ready else None),
            )
        return st

    def place(self, side: str, price: Decimal, qty: Decimal) -> tuple[bool, str]:
        from trading.intents import OrderIntent
        with self.lock:
            return self.broker.submit(OrderIntent(side, "buy", Decimal(price), Decimal(qty)), self.book, now_ms())

    def cancel(self, order_id: int) -> tuple[bool, str]:
        with self.lock:
            return self.broker.cancel(order_id, now_ms())

    def replace(self, order_id: int, price: Decimal, qty: Decimal) -> tuple[bool, str]:
        with self.lock:
            return self.broker.replace(order_id, Decimal(price), Decimal(qty), self.book, now_ms())

    def set_trail(self, value: Decimal, mode: str) -> None:
        with self.lock:
            self.broker.set_trail(Decimal(value), mode)

    def exit_now(self) -> tuple[bool, str]:
        with self.lock:
            return self.broker.exit_now(self.book, now_ms())
