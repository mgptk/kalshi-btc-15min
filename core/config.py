"""Runtime configuration.

Credentials are read from Streamlit secrets when running under Streamlit, otherwise
from environment variables. Nothing secret is ever hard-coded or committed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

PROD_REST = "https://api.elections.kalshi.com/trade-api/v2"
PROD_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"

SERIES_TICKER = "KXBTC15M"
INDEX_ID = "BRTI"

TICK = Decimal("0.01")          # entry price grid used by the ticket (whole cents)
# Kalshi "tapered_deci_cent" price levels: 0.1¢ steps below 10¢ and above 90¢, 1¢ in between.
# Overwritten with the market's own `price_ranges` at runtime.
DEFAULT_PRICE_RANGES = [
    (Decimal("0.0000"), Decimal("0.1000"), Decimal("0.0010")),
    (Decimal("0.1000"), Decimal("0.9000"), Decimal("0.0100")),
    (Decimal("0.9000"), Decimal("1.0000"), Decimal("0.0010")),
]
STOP_OFF_LEVEL = Decimal("0.90")  # trailing stop is switched off at/above this reference bid
STARTING_CASH = Decimal("1000")
DEFAULT_TRAIL_VALUE = Decimal("5")
DEFAULT_TRAIL_MODE = "cents"     # "cents" or "pct"
DISPLAY_TZ = os.environ.get("DISPLAY_TZ", "America/New_York")


@dataclass
class Settings:
    source: str                  # "kalshi" or "sim"
    api_key_id: str | None
    private_key_pem: str | None
    rest_base: str = PROD_REST
    ws_url: str = PROD_WS


def _get(name: str) -> str | None:
    """Look up a setting in st.secrets first, then the environment."""
    try:
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.environ.get(name)


def load_settings() -> Settings:
    key_id = _get("KALSHI_API_KEY_ID")
    pem = _get("KALSHI_PRIVATE_KEY")
    if pem is None:
        path = _get("KALSHI_PRIVATE_KEY_PATH")
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                pem = fh.read()
    source = (_get("FEED_SOURCE") or "").lower()
    if source not in ("kalshi", "sim"):
        source = "kalshi" if (key_id and pem) else "sim"
    return Settings(source=source, api_key_id=key_id, private_key_pem=pem)
