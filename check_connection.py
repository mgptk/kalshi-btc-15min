"""Quick live check of your Kalshi credentials and feeds (run before deploying).

    python check_connection.py

Reads KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY (or KALSHI_PRIVATE_KEY_PATH) from
.streamlit/secrets.toml or the environment, opens the WebSocket for ~15 s and reports
what arrived. Nothing is traded.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import tomllib
from collections import Counter

from core.auth import KalshiSigner
from core.config import PROD_REST, PROD_WS
from core.market import KalshiRest, now_ms
from core.ws import ws_connect


def creds() -> tuple[str, str]:
    sec = {}
    if os.path.exists(".streamlit/secrets.toml"):
        with open(".streamlit/secrets.toml", "rb") as fh:
            sec = tomllib.load(fh)
    get = lambda k: sec.get(k) or os.environ.get(k)
    key_id, pem = get("KALSHI_API_KEY_ID"), get("KALSHI_PRIVATE_KEY")
    if not pem and get("KALSHI_PRIVATE_KEY_PATH"):
        pem = open(get("KALSHI_PRIVATE_KEY_PATH"), encoding="utf-8").read()
    if not (key_id and pem):
        sys.exit("Missing KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY — see README.")
    return key_id, pem


async def main() -> None:
    key_id, pem = creds()
    rest = KalshiRest(PROD_REST)
    print("Series fee type:", rest.series_fee_type())
    m = rest.current_market(now_ms())
    print(f"Current market: {m.ticker}  window {m.window_label}  target {m.target}")
    headers = KalshiSigner(key_id, pem).sign("GET", "/trade-api/ws/v2")
    counts: Counter = Counter()
    first: dict = {}
    async with ws_connect(PROD_WS, headers, open_timeout=15) as ws:
        print("WebSocket connected ✔")
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                  "params": {"channels": ["cfbenchmarks_value"], "index_ids": ["BRTI"]}}))
        await ws.send(json.dumps({"id": 2, "cmd": "subscribe",
                                  "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": [m.ticker]}}))
        end = time.time() + 15
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=end - time.time())
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            t = msg.get("type")
            counts[t] += 1
            first.setdefault(t, msg)
            if t == "error":
                print("Error from Kalshi:", msg)
    print("Messages in 15 s:", dict(counts))
    for t in ("cfbenchmarks_value", "orderbook_snapshot", "trade"):
        if t in first:
            print(f"\nSample {t}:\n{json.dumps(first[t])[:600]}")
    ok = counts["cfbenchmarks_value"] > 0 and counts["orderbook_snapshot"] > 0
    print("\nRESULT:", "all feeds OK ✔" if ok else "something is missing ✘ (see above)")


if __name__ == "__main__":
    asyncio.run(main())
