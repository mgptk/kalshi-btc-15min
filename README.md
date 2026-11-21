# BTC 15m — Kalshi paper-trading terminal (v0)

A phone-friendly Streamlit terminal for the current Kalshi **KXBTC15M** (BTC Up/Down, 15 min) window:
live order book, CF Benchmarks **BRTI** + trailing 60 s average, the window target, and **paper trading**
against the real book with post-only entries and automatic trailing-stop exits.

Full design: project doc `specs/v0-manual-paper-terminal.md`.

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1) Try it with no keys — offline simulator
FEED_SOURCE=sim streamlit run app.py                     # Windows PowerShell: $env:FEED_SOURCE="sim"; streamlit run app.py

# 2) Live data
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then paste your key ID + private key
python check_connection.py                                   # verifies auth, BRTI and the order book
streamlit run app.py
```

With credentials present the app uses live Kalshi data; without them it falls back to the simulator
(the top line shows **LIVE** or **SIM**). `FEED_SOURCE` forces either.

## Deploy to Streamlit Community Cloud (free)

1. Push this folder to GitHub (a private repo is fine). `.streamlit/secrets.toml` is gitignored — check it isn't committed.
2. On share.streamlit.io → **Create app** → pick the repo, branch, `app.py`. Python 3.11+.
3. **Advanced settings → Secrets**: paste the contents of your local `secrets.toml`.
4. After deploy, open app **Settings → Sharing** and make it **private** (only invited emails can view) —
   the free tier allows one private app, and the order buttons shouldn't be public.

The app sleeps after 12 h without visits; opening it wakes it (paper account resets on restart).

## Using the terminal

- **Header**: time left · gap between the current 60 s average and the target (green = currently Up) ·
  YES/NO bid/ask · target · BRTI · in the final minute, the running settlement average and tick count (n/60).
- **Ladder**: top 5 YES bids and YES asks (asks are derived from NO bids).
- **Ticket**: pick YES (Up) or NO (Down), price in cents, contracts. **Join bid** / **Bid + 1¢** fill the price
  from the live book. Orders are **post-only** — a price at or through the ask is rejected, so entries never pay
  taker fees. **Replace** cancels the working entry and re-places it (queue position resets).
- **Exit Strategy** (expander): choose **Trailing stop** or **Take profit** with the buttons, then set the
  distance in ¢ or % (default 5¢). The same distance drives whichever strategy is selected.

### Paper-trading rules (summary)

| Rule | Behaviour |
|---|---|
| Bankroll | $1,000; resting buys reserve cash |
| Fills | Conservative queue model: you're behind the size already showing at your price; only trades at your price advance the queue; a trade through your price fills you fully; cancellations don't help you |
| Exit strategy | Trailing stop (default) or Take profit, chosen in the Exit Strategy section. Switching with a position open applies to it immediately (unless an exit is already working) |
| Trailing stop | Created on first fill, covers the whole position and grows with fills. Tracks the high of your side's best bid; stop = high − trail |
| Take profit | Rests a post-only sell for the whole position at average entry + distance (¢, or % of the average), rounded up to the price grid. Re-placed when fills are added or the distance changes. If the bid is already past the target it posts at bid + 1 tick. No stop loss in this mode |
| Stop off | While your side's bid is ≥ 90¢ (re-arms below 90¢) |
| On trigger | Cancels any unfilled entry, then posts a fee-free sell at best bid + 1 tick and re-posts every time the bid moves, until filled or the window closes |
| Exit now | Starts the fee-free chase immediately (either strategy; cancels a resting take-profit) |
| Close | Working orders cancelled; open position settles $1/$0 from the final-minute average vs target, then reconciled with Kalshi's official result |
| Fees | Checked from the series `fee_type` at startup (currently `quadratic` = makers pay nothing) |
| One side at a time | Can't buy NO while holding YES (or vice-versa) |

Ticks: Kalshi uses 0.1¢ steps below 10¢ and above 90¢ and 1¢ in between; the exit chase follows that grid.
The ticket places entries in whole cents.

## Code layout

```
app.py                  Streamlit UI only (reads FeedService.snapshot(), calls its actions)
check_connection.py     live credential/feed check
core/
  feed.py               FeedService: one background thread + one Kalshi WebSocket, rollover, settlement
  orderbook.py          local book from snapshot + deltas
  index.py              BRTI, trailing 60 s avg, final-minute settlement avg
  market.py             REST discovery of the current KXBTC15M market, price grid
  auth.py               RSA-PSS request signing
  sim.py                offline simulator speaking Kalshi's message shapes
  state.py              MarketState snapshot (what the UI and the future bot read)
trading/
  broker.py             Broker interface (PaperBroker now, KalshiBroker later)
  paper_broker.py       orders, queue fills, exits, settlement, P&L
  trailing_stop.py      stop state machine
tests/                  pytest suite for fills, stops, settlement, parsing, signing
```

Run tests: `python -m pytest -q`

## Hooking up the bot later

The bot should read `FeedService.snapshot()` (or register a callback in `FeedService.listeners`)
and submit `OrderIntent`s through the same broker interface the buttons use. Moving to real money means
adding a `KalshiBroker` that implements `trading/broker.py` and running the feed on an always-on host
(a client-side trailing stop only protects you while the process is running).
