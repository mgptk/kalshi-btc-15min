"""BTC 15-min paper-trading terminal (v0) — Streamlit UI.

The UI only reads FeedService.snapshot() and calls its action methods. All market data,
fills and stop logic run in FeedService's background thread (shared across browser tabs).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
import streamlit as st

from core.config import DISPLAY_TZ, load_settings
from core.feed import FeedService
from trading.paper_broker import PaperBroker

st.set_page_config(page_title="BTC 15m Terminal", page_icon="📈", layout="centered",
                   initial_sidebar_state="collapsed")

TZ = ZoneInfo(DISPLAY_TZ)
UP, DOWN, MUTED = "#16a34a", "#dc2626", "#6b7280"

st.markdown("""
<style>
/* Streamlit's header is fixed and overlays the page: keep it slim and transparent,
   then start the content below it so the first row of cards isn't clipped. */
header[data-testid="stHeader"] {height: 2.6rem; min-height: 2.6rem; background: transparent;}
div[data-testid="stAppViewBlockContainer"], .block-container {padding-top: 3.1rem; padding-bottom: 3rem; max-width: 560px;}
div[data-testid="stVerticalBlock"] {gap: 0.55rem;}
.stButton > button, div[data-testid="stButton"] button {width: 100%; min-height: 2.8rem; font-weight: 600;}
/* keep st.columns side by side on phones instead of stacking */
div[data-testid="stHorizontalBlock"] {flex-wrap: nowrap !important; gap: 0.5rem;}
div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {min-width: 0 !important; width: auto !important; flex: 1 1 0 !important;}
.hdr {display:grid; grid-template-columns: repeat(3, 1fr); gap:6px; margin-bottom:2px;}
.card {background: rgba(127,127,127,0.08); border-radius: 10px; padding: 7px 9px;}
.card .k {font-size: 0.68rem; color: #8b8f97; text-transform: uppercase; letter-spacing: .03em;}
.card .v {font-size: 1.05rem; font-weight: 700; font-variant-numeric: tabular-nums; line-height:1.25;}
.card .s {font-size: 0.72rem; color: #8b8f97;}
.big .v {font-size: 1.5rem;}
.topline {display:flex; margin-top:4px; justify-content:space-between; font-size:0.78rem; color:#8b8f97; margin-bottom:4px;}
.dot {display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px;}
table.lad {width:100%; border-collapse:collapse; font-variant-numeric: tabular-nums; font-size:0.95rem;}
table.lad, table.lad tr, table.lad td {border: none !important; background: transparent !important;}
table.lad td {padding: 3px 6px;}
table.lad td.bq {text-align:right; color:#8b8f97;} table.lad td.bp {text-align:right; color:#16a34a; font-weight:700;}
table.lad td.ap {text-align:left; color:#dc2626; font-weight:700;} table.lad td.aq {text-align:left; color:#8b8f97;}
table.lad tr:first-child td {border-bottom: 1px solid rgba(127,127,127,.25);}
.chip {display:inline-block; padding:1px 8px; border-radius:999px; font-size:0.75rem; font-weight:700;}
.pos {font-variant-numeric: tabular-nums;}
</style>
""", unsafe_allow_html=True)


@st.cache_resource(show_spinner="Connecting to market data…")
def get_feed() -> FeedService:
    """One FeedService per server process, shared by every tab/session."""
    return FeedService(load_settings(), PaperBroker()).start()


feed = get_feed()


# ---------------------------------------------------------------- formatting helpers
def c(x: Decimal | None) -> str:
    if x is None:
        return "—"
    v = x * 100
    return f"{v:.0f}¢" if v == v.to_integral_value() else f"{v:.1f}¢"


def usd(x: Decimal | None, dp: int = 2, sign: bool = False) -> str:
    if x is None:
        return "—"
    s = f"{abs(x):,.{dp}f}"
    pre = ("+" if x >= 0 else "−") if sign else ("−" if x < 0 else "")
    return f"{pre}${s}"


def q(x) -> str:
    return f"{Decimal(x).normalize():f}"


def color(x: Decimal | None) -> str:
    return MUTED if x is None or x == 0 else (UP if x > 0 else DOWN)


def card(k: str, v: str, s: str = "", vcolor: str | None = None, cls: str = "") -> str:
    style = f' style="color:{vcolor}"' if vcolor else ""
    return f'<div class="card {cls}"><div class="k">{k}</div><div class="v"{style}>{v}</div><div class="s">{s}</div></div>'


def flash(ok: bool, msg: str) -> None:
    st.toast(("✅ " if ok else "⚠️ ") + msg)


# ---------------------------------------------------------------- live panel (header, ladder, position)
@st.fragment(run_every=0.5)
def live_panel() -> None:
    s = feed.snapshot()
    a = s.account

    status_color = UP if s.connected and not s.error else ("#f59e0b" if s.connected else DOWN)
    src = "SIM" if s.source == "sim" else "LIVE"
    st.markdown(
        f'<div class="topline"><span><span class="dot" style="background:{status_color}"></span>'
        f'{src} · {s.window or "—"}</span><span>{s.ticker or "finding market…"}</span></div>',
        unsafe_allow_html=True,
    )
    if s.error:
        st.caption(f"⚠️ {s.error}")
    if a.get("maker_fees"):
        st.warning("This series currently charges maker fees — paper fills include them.")

    view = (st.session_state.get("side") or "YES").lower()
    secs = s.seconds_left
    left = "—" if secs is None else f"{int(secs // 60)}:{int(secs % 60):02d}"
    gap = s.gap
    bps = (gap / s.target * 10000) if gap is not None and s.target else None
    final = f"{usd(s.final_avg)}" if s.final_avg is not None else "—"
    final_sub = f"settlement avg · {s.final_n}/60" if s.final_avg is not None else "settles on final-minute avg"
    cells = [
        card("Time left", left, "final minute" if secs is not None and secs <= 60 else "", cls="big",
             vcolor=DOWN if secs is not None and secs <= 60 else None),
        card("Gap (60s avg − target)", usd(gap, sign=True), "—" if bps is None else f"{bps:+.1f} bps",
             vcolor=color(gap), cls="big"),
        card(f"{view.upper()} bid / ask",
             f"{c(s.no_bid)} / {c(s.no_ask)}" if view == "no" else f"{c(s.yes_bid)} / {c(s.yes_ask)}",
             f"YES {c(s.yes_bid)} / {c(s.yes_ask)}" if view == "no" else f"NO {c(s.no_bid)} / {c(s.no_ask)}",
             cls="big"),
        card("Target", usd(s.target), s.target_source),
        card("60s avg", usd(s.avg60), f"BRTI {usd(s.brti)}"),
        card("Final avg", final, final_sub),
    ]
    st.markdown(f'<div class="hdr">{"".join(cells)}</div>', unsafe_allow_html=True)

    # ladder — shown in the side the ticket is set to
    bids, asks = s.ladder(view)
    rows = []
    for i in range(5):
        b = bids[i] if i < len(bids) else None
        k = asks[i] if i < len(asks) else None
        rows.append(
            f'<tr><td class="bq">{q(b[1]) if b else ""}</td><td class="bp">{c(b[0]) if b else ""}</td>'
            f'<td class="ap">{c(k[0]) if k else ""}</td><td class="aq">{q(k[1]) if k else ""}</td></tr>'
        )
    lt = s.last_trade
    if lt:
        px = Decimal(str(lt["yes_price_dollars"]))
        if view == "no":
            px = Decimal(1) - px
        taker = "YES" if (lt.get("taker_outcome_side") or lt.get("taker_side")) == "yes" else "NO"
        last = f'last: {q(Decimal(str(lt["count_fp"])))} @ {c(px)} ({taker} taker)'
    else:
        last = ""
    st.markdown(
        f'<div class="card"><div class="topline"><span>{view.upper()} bids</span><span>{last}</span>'
        f'<span>{view.upper()} asks</span></div>'
        f'<table class="lad">{"".join(rows)}</table></div>' if s.book_ready else
        '<div class="card"><div class="s">Waiting for order book…</div></div>',
        unsafe_allow_html=True,
    )

    # position + stop
    pos, stop = a["position"], a["stop"]
    if pos:
        unreal = a["unrealized"]
        chip_bg = {"armed": UP, "off": "#f59e0b", "triggered": DOWN}.get(stop["state"] if stop else "", MUTED)
        trail = (f'{q(stop["trail_value"])}{"¢" if stop["trail_mode"] == "cents" else "%"}') if stop else ""
        st.markdown(
            f'<div class="card pos"><div class="topline"><span><b>{pos["side"].upper()}</b> '
            f'{q(pos["qty"])} @ {c(pos["avg"])}</span>'
            f'<span>uPnL <b style="color:{color(unreal)}">{usd(unreal, sign=True)}</b></span></div>'
            + (f'<span class="chip" style="background:{chip_bg};color:white">STOP {stop["state"].upper()}</span> '
               f'&nbsp;HWM {c(stop["hwm"])} · stop {c(stop["level"])} · trail {trail}' if stop else "")
            + '</div>',
            unsafe_allow_html=True,
        )
        if not stop or stop["state"] != "triggered":
            if st.button("Exit now (fee-free chase)", key="exit_now", width="stretch"):
                flash(*feed.exit_now())

    for o in a["orders"]:
        label = (f'{"EXIT" if o["purpose"] == "exit" else "ENTRY"} #{o["id"]}: {o["action"]} '
                 f'{q(o["qty"])} {o["side"].upper()} @ {c(o["price"])} · filled {q(o["filled"])} · '
                 f'queue ahead {q(o["queue_ahead"])}')
        if o["purpose"] == "entry":
            col1, col2 = st.columns([3, 1], vertical_alignment="center")
            col1.markdown(f'<div class="s pos">{label}</div>', unsafe_allow_html=True)
            if col2.button("Cancel", key=f"cx{o['id']}", width="stretch"):
                flash(*feed.cancel(o["id"]))
        else:
            st.markdown(f'<div class="s pos">{label}</div>', unsafe_allow_html=True)

    st.markdown(
        f'<div class="topline pos"><span>Equity <b>{usd(a["equity"])}</b></span>'
        f'<span>Avail {usd(a["available"])}</span>'
        f'<span>Session <b style="color:{color(a["session_pnl"])}">{usd(a["session_pnl"], sign=True)}</b></span></div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------- order ticket
def _set_px(which: str) -> None:
    s = feed.snapshot()
    side = st.session_state.get("side", "YES").lower()
    bid, ask = (s.yes_bid, s.yes_ask) if side == "yes" else (s.no_bid, s.no_ask)
    if bid is None:
        return
    px = bid if which == "bid" else bid + Decimal("0.01")
    if ask is not None and px >= ask:
        px = ask - Decimal("0.01")
    st.session_state["px"] = max(1, min(99, int(px * 100)))


@st.fragment
def ticket() -> None:
    st.session_state.setdefault("px", 50)
    st.session_state.setdefault("qty", 10)
    st.segmented_control("Side", ["YES", "NO"], key="side", default="YES", label_visibility="collapsed",
                         width="stretch")
    c1, c2 = st.columns(2)
    c1.number_input("Price (¢)", min_value=1, max_value=99, step=1, key="px")
    c2.number_input("Contracts", min_value=1, max_value=100_000, step=1, key="qty")
    b1, b2 = st.columns(2)
    b1.button("Join bid", on_click=_set_px, args=("bid",), key="join", width="stretch")
    b2.button("Bid + 1¢", on_click=_set_px, args=("improve",), key="improve", width="stretch")
    side = (st.session_state.get("side") or "YES").lower()
    px = Decimal(st.session_state["px"]) / 100
    qty = Decimal(st.session_state["qty"])
    if st.button(f"Place: buy {qty} {side.upper()} @ {st.session_state['px']}¢ (post-only)", type="primary", width="stretch",
                 key="place"):
        flash(*feed.place(side, px, qty))
    if st.button(f"Replace working entry with {qty} @ {st.session_state['px']}¢", key="replace", width="stretch"):
        entries = [o for o in feed.snapshot().account["orders"] if o["purpose"] == "entry"]
        if entries:
            flash(*feed.replace(entries[0]["id"], px, qty))
        else:
            flash(False, "No working entry to replace.")


# ---------------------------------------------------------------- trailing-stop setting
def _apply_trail() -> None:
    mode = "cents" if st.session_state["trail_mode"] == "¢" else "pct"
    feed.set_trail(Decimal(str(st.session_state["trail_val"])), mode)


@st.fragment
def trail_control() -> None:
    a = feed.snapshot().account
    st.session_state.setdefault("trail_val", float(a["trail_value"]))
    st.session_state.setdefault("trail_mode", "¢" if a["trail_mode"] == "cents" else "%")
    c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
    pct = st.session_state["trail_mode"] == "%"
    c1.number_input("Trailing stop distance", min_value=0.5, max_value=99.0 if pct else 98.0,
                    step=0.5 if pct else 1.0, format="%.1f", key="trail_val", on_change=_apply_trail)
    c2.segmented_control("Unit", ["¢", "%"], key="trail_mode", on_change=_apply_trail,
                         label_visibility="collapsed", width="stretch")
    st.caption("Applies to new positions and live to an untriggered stop. Off while bid ≥ 90¢.")


# ---------------------------------------------------------------- chart
@st.fragment(run_every=1.5)
def chart() -> None:
    s = feed.snapshot()
    if len(s.history) < 2:
        st.caption("Chart starts after a few BRTI ticks…")
        return
    xs = [datetime.fromtimestamp(t / 1000, TZ) for t, _, _ in s.history]
    fig = go.Figure()
    fig.add_scatter(x=xs, y=[float(v) for _, v, _ in s.history], name="BRTI", mode="lines",
                    line=dict(width=1.2, color="#9ca3af"))
    fig.add_scatter(x=xs, y=[float(a) for _, _, a in s.history], name="60s avg", mode="lines",
                    line=dict(width=2.2, color="#2563eb"))
    if s.target is not None:
        fig.add_hline(y=float(s.target), line=dict(color="#f59e0b", dash="dash", width=1.5),
                      annotation_text="target", annotation_position="top left")
    if s.close_ms:
        fig.add_vrect(x0=datetime.fromtimestamp((s.close_ms - 60_000) / 1000, TZ),
                      x1=datetime.fromtimestamp(s.close_ms / 1000, TZ),
                      fillcolor="#dc2626", opacity=0.07, line_width=0)
        fig.update_xaxes(range=[datetime.fromtimestamp((s.open_ms - 60_000) / 1000, TZ),
                                datetime.fromtimestamp(s.close_ms / 1000, TZ)])
    fig.update_layout(height=250, margin=dict(l=0, r=0, t=8, b=0), showlegend=False,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      yaxis=dict(tickformat=",.0f", gridcolor="rgba(127,127,127,.15)"),
                      xaxis=dict(gridcolor="rgba(127,127,127,.1)"))
    st.plotly_chart(fig, config={"staticPlot": True}, key="chart")


# ---------------------------------------------------------------- activity log
@st.fragment(run_every=2)
def activity() -> None:
    a = feed.snapshot().account
    st.markdown(
        f'<div class="topline pos"><span>Cash {usd(a["cash"])}</span><span>Reserved {usd(a["reserved"])}</span>'
        f'<span>Realized <b style="color:{color(a["realized"])}">{usd(a["realized"], sign=True)}</b></span></div>',
        unsafe_allow_html=True,
    )
    if a["pending"]:
        st.caption("Awaiting official result: " + ", ".join(a["pending"]))
    lines = []
    for e in a["log"][:25]:
        t = datetime.fromtimestamp(e["ts_ms"] / 1000, TZ).strftime("%H:%M:%S")
        pnl = f' · {usd(e["pnl"], sign=True)}' if e["pnl"] is not None else ""
        lines.append(f"`{t}` **{e['event']}** {e['detail']}{pnl}")
    st.markdown("  \n".join(lines) if lines else "_No activity yet._")


# ---------------------------------------------------------------- layout
live_panel()
ticket()
with st.expander("Trailing stop", expanded=False):
    trail_control()
chart()
with st.expander("Activity & account", expanded=False):
    activity()
