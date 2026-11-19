"""BRTI state from Kalshi's `cfbenchmarks_value` channel."""
from __future__ import annotations

import json
from collections import deque
from decimal import Decimal


def _dec(x) -> Decimal | None:
    if x is None or x == "":
        return None
    return Decimal(str(x))


class IndexState:
    def __init__(self, history_seconds: int = 20 * 60):
        self.value: Decimal | None = None          # latest BRTI
        self.value_ts_ms: int | None = None        # CF calculation time
        self.avg60: Decimal | None = None          # trailing 60 s average
        self.final_avg: Decimal | None = None      # running settlement average (final minute only)
        self.final_n: int = 0                      # ticks in the settlement window so far (of 60)
        self.final_window_end_ms: int | None = None
        self.history: deque = deque(maxlen=history_seconds)  # (ts_ms, value, avg60)
        # completed quarter-hour settlement averages: close_ts_ms -> (avg, n)
        self.closed_quarters: dict[int, tuple[Decimal, int]] = {}

    def apply(self, msg: dict) -> None:
        raw = msg.get("data")
        frame = json.loads(raw) if isinstance(raw, str) else (raw or {})
        val = _dec(frame.get("value"))
        ts = frame.get("time") or msg.get("received_at")
        if val is None:
            return
        self.value, self.value_ts_ms = val, int(ts) if ts is not None else None

        avg = msg.get("avg_60s_data") or {}
        self.avg60 = _dec(avg.get("value")) or val

        q = msg.get("last_60s_windowed_average_15min")
        if q:
            self.final_avg = _dec(q.get("value"))
            self.final_n = int(q.get("window_size") or 0)
            end = q.get("window_end_ts_exclusive")
            self.final_window_end_ms = int(end) if end is not None else None
            if self.final_window_end_ms is not None and self.final_avg is not None:
                close_ms = _quarter_close_ms(self.final_window_end_ms)
                self.closed_quarters[close_ms] = (self.final_avg, self.final_n)
                if len(self.closed_quarters) > 8:
                    self.closed_quarters.pop(min(self.closed_quarters))
        else:
            self.final_avg, self.final_n, self.final_window_end_ms = None, 0, None

        if self.value_ts_ms is not None:
            self.history.append((self.value_ts_ms, val, self.avg60))

    def settlement_for(self, close_ms: int) -> tuple[Decimal, int] | None:
        return self.closed_quarters.get(close_ms)


def _quarter_close_ms(ts_ms: int) -> int:
    """Snap a timestamp near a quarter-hour boundary (within the final minute) to that boundary."""
    q = 15 * 60 * 1000
    return ((ts_ms + q // 2) // q) * q
