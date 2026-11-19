"""Market discovery for the current KXBTC15M window, via Kalshi's public REST API."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import requests

from zoneinfo import ZoneInfo

from .config import DEFAULT_PRICE_RANGES, DISPLAY_TZ, SERIES_TICKER


@dataclass
class MarketInfo:
    ticker: str
    open_ms: int
    close_ms: int
    target: Decimal | None      # floor_strike: the window's target (prior window's final average)
    title: str = ""
    price_ranges: list | None = None   # [(start, end, step)]

    @property
    def window_label(self) -> str:
        tz = ZoneInfo(DISPLAY_TZ)
        o = datetime.fromtimestamp(self.open_ms / 1000, tz)
        c = datetime.fromtimestamp(self.close_ms / 1000, tz)
        h = lambda d: f"{d.hour % 12 or 12}:{d:%M}"   # portable (no %-I on Windows)
        return f"{h(o)}–{h(c)} {c:%p} {c:%Z}"


def _iso_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def parse_market(m: dict) -> MarketInfo:
    fs = m.get("floor_strike")
    return MarketInfo(
        ticker=m["ticker"],
        open_ms=_iso_ms(m["open_time"]),
        close_ms=_iso_ms(m["close_time"]),
        target=Decimal(str(fs)) if fs not in (None, "") else None,
        title=m.get("title") or "",
        price_ranges=[
            (Decimal(str(r["start"])), Decimal(str(r["end"])), Decimal(str(r["step"])))
            for r in (m.get("price_ranges") or [])
        ] or None,
    )


def tick_at(price: Decimal, ranges=None) -> Decimal:
    """Price increment for the level at `price` (the step of the range containing it)."""
    for start, end, step in ranges or DEFAULT_PRICE_RANGES:
        if start <= price < end:
            return step
    return (ranges or DEFAULT_PRICE_RANGES)[-1][2]


class KalshiRest:
    def __init__(self, base: str, timeout: float = 10.0):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()

    def _get(self, path: str, **params) -> dict:
        r = self.s.get(f"{self.base}{path}", params=params or None, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def current_market(self, now_ms: int, series: str = SERIES_TICKER) -> MarketInfo | None:
        """The open market whose window contains now (or the soonest-closing open one)."""
        data = self._get("/markets", series_ticker=series, status="open", limit=50)
        markets = [parse_market(m) for m in data.get("markets", [])]
        live = [m for m in markets if m.close_ms > now_ms]
        if not live:
            return None
        inside = [m for m in live if m.open_ms <= now_ms]
        return min(inside or live, key=lambda m: m.close_ms)

    def market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}").get("market", {})

    def series_fee_type(self, series: str = SERIES_TICKER) -> tuple[str | None, float | None]:
        s = self._get(f"/series/{series}").get("series", {})
        return s.get("fee_type"), s.get("fee_multiplier")


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
