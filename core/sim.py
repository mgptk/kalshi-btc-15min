"""Offline simulator that speaks Kalshi's WebSocket message shapes.

Lets you run and test the terminal with no API keys (FEED_SOURCE=sim). BRTI is a random walk;
the KXBTC15M book is quoted around a simple fair probability; trades print randomly.
Windows align with real quarter hours so rollover and settlement behave like production.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections import deque
from datetime import datetime, timezone

QUARTER_MS = 15 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fmt(p: float) -> str:
    return f"{p:.4f}"


class SimExchange:
    def __init__(self, seed: int | None = None, start_price: float = 90_000.0):
        self.rng = random.Random(seed)
        self.price = start_price
        self.sigma = 9e-5                          # per-second log vol (~50% annualized)
        self.ticks: deque = deque(maxlen=3600)    # (ts_ms, price) at whole seconds
        self.finals: dict[int, float] = {}        # quarter close ms -> final 60 s average
        now = _now_ms() // 1000 * 1000
        for i in range(120, 0, -1):               # warm-up history
            self._step_price()
            self.ticks.append((now - i * 1000, self.price))

    # ---- price process ----------------------------------------------------
    def _step_price(self) -> None:
        self.price *= math.exp(self.rng.gauss(0, self.sigma))

    def tick(self, ts_ms: int) -> None:
        self._step_price()
        self.ticks.append((ts_ms, self.price))
        if ts_ms % QUARTER_MS == 0:
            avg, n = self.quarter_avg(ts_ms, ts_ms)
            if n:
                self.finals[ts_ms] = avg

    def avg60(self, ts_ms: int) -> tuple[float, int]:
        w = [p for t, p in self.ticks if ts_ms - 60_000 <= t < ts_ms]
        return (sum(w) / len(w), len(w)) if w else (self.price, 0)

    def quarter_avg(self, ts_ms: int, close_ms: int) -> tuple[float, int]:
        w = [p for t, p in self.ticks if close_ms - 60_000 < t <= min(ts_ms, close_ms)]
        return (sum(w) / len(w), len(w)) if w else (self.price, 0)

    # ---- markets ----------------------------------------------------------
    @staticmethod
    def window(now_ms: int) -> tuple[int, int]:
        o = now_ms // QUARTER_MS * QUARTER_MS
        return o, o + QUARTER_MS

    def ticker_for(self, close_ms: int) -> str:
        c = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
        return f"KXBTC15M-SIM{c:%y%b%d%H%M}".upper()

    def target_for(self, open_ms: int) -> float:
        if open_ms in self.finals:
            return self.finals[open_ms]
        avg, _ = self.avg60(open_ms)
        return avg

    def current_market(self, now_ms: int) -> dict:
        o, c = self.window(now_ms)
        iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "ticker": self.ticker_for(c), "open_time": iso(o), "close_time": iso(c),
            "floor_strike": round(self.target_for(o), 2), "title": "SIM BTC up in next 15 mins?",
        }

    def result(self, ticker: str) -> bool | None:
        for close_ms, avg in self.finals.items():
            if self.ticker_for(close_ms) == ticker:
                return avg >= self.target_for(close_ms - QUARTER_MS)
        return None

    def fair_yes(self, now_ms: int) -> float:
        o, c = self.window(now_ms)
        target = self.target_for(o)
        avg, _ = self.avg60(now_ms)
        secs = max(1.0, (c - now_ms) / 1000)
        sd = self.price * self.sigma * math.sqrt(secs) + 1e-9
        return 0.5 * (1 + math.erf((avg - target) / (sd * math.sqrt(2))))


class SimConnection:
    """Mimics a websockets connection: `await send(str)` and `async for raw in conn`."""

    def __init__(self, ex: SimExchange):
        self.ex = ex
        self.q: asyncio.Queue[str] = asyncio.Queue()
        self.sid = 0
        self.subs: dict[int, dict] = {}            # sid -> {"channel", "ticker"}
        self.seq: dict[int, int] = {}
        self.yes: dict[float, float] = {}
        self.no: dict[float, float] = {}
        self.book_ticker: str | None = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        return await self.q.get()

    # ---- commands --------------------------------------------------------
    async def send(self, raw: str) -> None:
        cmd = json.loads(raw)
        p = cmd.get("params", {})
        if cmd["cmd"] == "subscribe":
            for ch in p.get("channels", []):
                self.sid += 1
                tick = (p.get("market_tickers") or [None])[0]
                self.subs[self.sid] = {"channel": ch, "ticker": tick}
                self.seq[self.sid] = 0
                self._emit({"id": cmd["id"], "type": "subscribed", "msg": {"channel": ch, "sid": self.sid}})
                if ch == "orderbook_delta":
                    self.book_ticker = tick
                    self._rebuild_book(_now_ms())
                    self._emit_snapshot(self.sid)
        elif cmd["cmd"] == "unsubscribe":
            for sid in p.get("sids", []):
                self.subs.pop(sid, None)
                self._emit({"sid": sid, "type": "unsubscribed"})

    # ---- emitters ----------------------------------------------------------
    def _emit(self, obj: dict) -> None:
        self.q.put_nowait(json.dumps(obj))

    def _sids(self, channel: str) -> list[int]:
        return [s for s, v in self.subs.items() if v["channel"] == channel]

    def _next_seq(self, sid: int) -> int:
        self.seq[sid] += 1
        return self.seq[sid]

    def _emit_snapshot(self, sid: int) -> None:
        self._emit({
            "type": "orderbook_snapshot", "sid": sid, "seq": self._next_seq(sid),
            "msg": {
                "market_ticker": self.book_ticker,
                "yes_dollars_fp": [[_fmt(p), f"{q:.2f}"] for p, q in sorted(self.yes.items())],
                "no_dollars_fp": [[_fmt(p), f"{q:.2f}"] for p, q in sorted(self.no.items())],
            },
        })

    def _emit_delta(self, side: str, price: float, delta: float, ts: int) -> None:
        for sid in self._sids("orderbook_delta"):
            self._emit({
                "type": "orderbook_delta", "sid": sid, "seq": self._next_seq(sid),
                "msg": {"market_ticker": self.book_ticker, "price_dollars": _fmt(price),
                        "delta_fp": f"{delta:.2f}", "side": side, "ts_ms": ts},
            })

    # ---- book model ----------------------------------------------------------
    def _target_book(self, now_ms: int) -> tuple[dict, dict]:
        r = self.ex.rng
        fair = min(0.985, max(0.015, self.ex.fair_yes(now_ms)))
        half = r.choice([0.01, 0.01, 0.02])
        bid = max(0.01, round(fair - half, 2))
        ask = min(0.99, max(bid + 0.01, round(fair + half, 2)))
        yes, no = {}, {}
        for i in range(8):
            if bid - i * 0.01 >= 0.01:
                yes[round(bid - i * 0.01, 2)] = float(r.randint(20, 400) * (1 + i // 3))
            if ask + i * 0.01 <= 0.99:
                no[round(1 - (ask + i * 0.01), 2)] = float(r.randint(20, 400) * (1 + i // 3))
        return yes, no

    def _rebuild_book(self, now_ms: int) -> None:
        self.yes, self.no = self._target_book(now_ms)

    def _drift_book(self, now_ms: int) -> None:
        ty, tn = self._target_book(now_ms)
        for side, cur, tgt in (("yes", self.yes, ty), ("no", self.no, tn)):
            for p in set(cur) | set(tgt):
                old, new = cur.get(p, 0.0), tgt.get(p, 0.0)
                if p in cur and p in tgt and self.ex.rng.random() < 0.7:
                    continue                      # keep most resting size stable
                if new != old:
                    if new > 0:
                        cur[p] = new
                    else:
                        cur.pop(p, None)
                    self._emit_delta(side, p, new - old, now_ms)

    def _random_trade(self, now_ms: int) -> None:
        if not self.yes or not self.no or not self._sids("trade"):
            return
        r = self.ex.rng
        taker_yes = r.random() < self.ex.fair_yes(now_ms)
        if taker_yes:                                  # lifts YES ask = hits best NO bid
            nb = max(self.no)
            yes_px, side_book, lvl = 1 - nb, "no", nb
        else:                                          # hits best YES bid
            yb = max(self.yes)
            yes_px, side_book, lvl = yb, "yes", yb
        book = self.no if side_book == "no" else self.yes
        size = min(book[lvl], float(r.randint(1, 60)))
        for sid in self._sids("trade"):
            self._emit({
                "type": "trade", "sid": sid, "seq": self._next_seq(sid),
                "msg": {"market_ticker": self.book_ticker, "yes_price_dollars": _fmt(yes_px),
                        "no_price_dollars": _fmt(1 - yes_px), "count_fp": f"{size:.2f}",
                        "taker_side": "yes" if taker_yes else "no",
                        "taker_outcome_side": "yes" if taker_yes else "no", "ts_ms": now_ms},
            })
        book[lvl] -= size
        if book[lvl] <= 0:
            book.pop(lvl)
        self._emit_delta(side_book, lvl, -size, now_ms)

    def _emit_index(self, ts: int) -> None:
        sids = self._sids("cfbenchmarks_value")
        if not sids:
            return
        avg, n = self.ex.avg60(ts)
        _, c = self.ex.window(ts - 1)                 # close tick belongs to the closing window
        msg = {
            "index_id": "BRTI", "received_at": ts + 40,
            "data": json.dumps({"type": "value", "id": "BRTI", "time": ts, "value": f"{self.ex.price:.2f}"}),
            "avg_60s_data": {"value": f"{avg:.8f}", "window_size": n,
                              "window_start_ts_ms": ts - 60_000, "window_end_ts_exclusive": ts},
        }
        if c - 60_000 < ts <= c:
            qa, qn = self.ex.quarter_avg(ts, c)
            msg["last_60s_windowed_average_15min"] = {
                "value": f"{qa:.8f}", "window_size": qn,
                "window_start_ts_ms": c - 60_000, "window_end_ts_exclusive": c + 1,
            }
        for sid in sids:
            self._emit({"type": "cfbenchmarks_value", "sid": sid, "seq": self._next_seq(sid), "msg": msg})

    async def _run(self) -> None:
        last_sec = _now_ms() // 1000
        while True:
            await asyncio.sleep(0.2)
            now = _now_ms()
            sec = now // 1000
            while last_sec < sec:                        # one BRTI tick per whole second
                last_sec += 1
                self.ex.tick(last_sec * 1000)
                self._emit_index(last_sec * 1000)
            if self.book_ticker and self._sids("orderbook_delta"):
                if self.ex.rng.random() < 0.5:
                    self._drift_book(now)
                if self.ex.rng.random() < 0.35:
                    self._random_trade(now)
