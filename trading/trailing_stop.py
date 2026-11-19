"""Client-side trailing stop for one position.

Reference price is the held side's best bid. The stop:
  * tracks the high-water mark (HWM) of the reference bid since the first fill,
  * is OFF while the reference bid is >= STOP_OFF_LEVEL (HWM still updates), and re-arms below it,
  * triggers when armed and reference bid <= stop level,
  * once triggered, stays triggered (the exit keeps running even if the bid recovers).
"""
from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from core.config import STOP_OFF_LEVEL

PCT_GRID = Decimal("0.001")   # finest Kalshi price step (0.1¢)


class TrailingStop:
    ARMED, OFF, TRIGGERED = "armed", "off", "triggered"

    def __init__(self, trail_value: Decimal, trail_mode: str, initial_ref: Decimal | None):
        self.trail_value = Decimal(trail_value)
        self.trail_mode = trail_mode            # "cents" | "pct"
        self.hwm: Decimal | None = initial_ref
        self.state = self.ARMED
        if initial_ref is not None and initial_ref >= STOP_OFF_LEVEL:
            self.state = self.OFF

    def set_trail(self, value: Decimal, mode: str) -> None:
        self.trail_value, self.trail_mode = Decimal(value), mode

    @property
    def level(self) -> Decimal | None:
        if self.hwm is None:
            return None
        if self.trail_mode == "pct":
            raw = self.hwm * (Decimal(1) - self.trail_value / Decimal(100))
            return raw.quantize(PCT_GRID, rounding=ROUND_FLOOR)
        return self.hwm - self.trail_value / Decimal(100)

    def on_reference(self, bid: Decimal | None) -> bool:
        """Feed the latest reference bid. Returns True exactly when the stop triggers."""
        if self.state == self.TRIGGERED or bid is None:
            return False
        if self.hwm is None or bid > self.hwm:
            self.hwm = bid
        if bid >= STOP_OFF_LEVEL:
            self.state = self.OFF
            return False
        self.state = self.ARMED
        lvl = self.level
        if lvl is not None and bid <= lvl:
            self.state = self.TRIGGERED
            return True
        return False

    def force_trigger(self) -> None:
        self.state = self.TRIGGERED
