"""Streamlit dashboard for the Kalshi Last-Second Sniper."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from collections import defaultdict

import streamlit as st
import pandas as pd

st.set_page_config(page_title="Kalshi Sniper Dashboard", page_icon="⚡", layout="wide")

# ── ticker helpers ─────────────────────────────────────────────────────────────

_SYMBOLS: dict[str, str] = {
    "BTCD": "BTC Daily",
    "BTCW": "BTC Weekly",
    "SOLE": "SOL",
    "ETHE": "ETH",
    "XRPE": "XRP",
    "DOGE": "DOGE",
    "AVAX": "AVAX",
    "LINK": "LINK",
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_TYPE_LABELS: dict[str, str] = {
    "LAST_SECOND": "Last-Second",
    "PREDICTION":  "Prediction",
    # legacy arb types (old sessions)
    "BINARY":      "Binary Arb",
    "SERIES":      "Series Arb",
    "CROSS":       "Cross-Arb",
    "DIRECTIONAL": "Directional",
}


def _fmt_type(raw: str) -> str:
    return _TYPE_LABELS.get(raw.upper(), raw.title())


def _parse_kalshi_date(s: str) -> str:
    """'26MAR1316' → 'Mar 13 4:00 PM'"""
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})(\d{2})", s)
    if not m:
        return s
    yy, mon, day, hr = m.groups()
    month_num = _MONTHS.get(mon, 1)
    try:
        dt = datetime(2000 + int(yy), month_num, int(day), int(hr), 0)
        return dt.strftime("%b %-d %-I:%M %p")
    except Exception:
        return s


def _describe_event(ticker: str, event_name: str | None = None) -> str:
    if event_name:
        return event_name[:60] + ("…" if len(event_name) > 60 else "")
    raw = ticker.removeprefix("CROSS_")
    m = re.match(r"KX([A-Z]+)-(\d{2}[A-Z]{3}\d{4})", raw)
    if not m:
        return raw
    sym, date_str = m.groups()
    label = _SYMBOLS.get(sym, sym)
    return f"{label} · {_parse_kalshi_date(date_str)}"


def _series_event_ticker(ticker: str) -> str:
    return re.sub(r"-B\d+$", "", ticker)


# ── session helpers ─────────────────────────────────────────────────────────────

def _is_live_session(session) -> bool:
    return "LIVE" in (session.log_path or "").upper()


def _read_log_header(session, chars: int = 600) -> str:
    try:
        with open(session.log_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(chars)
    except Exception:
        return ""


def _session_strategy(session) -> str:
    header = _read_log_header(session)
    parts = []
    if "+LAST-SEC" in header or any(p.arb_type == "last_second" for p in session.positions):
        parts.append("Last-Sec")
    if "+PREDICTION" in header or any(p.arb_type is None and p.ev and p.ev > 0 for p in session.positions):
        parts.append("Prediction")
    # legacy
    if not parts:
        if any(p.arb_type in ("series", "binary", "cross") for p in session.positions):
            parts.append("Arb (legacy)")
    return " · ".join(parts) if parts else "—"


# ── DB ─────────────────────────────────────────────────────────────────────────

@st.cache_resource
def _get_db():
    from src.storage.db import get_session
    return get_session()


def _refresh():
    _get_db().expire_all()


def stop_all_running_sessions() -> int:
    from src.storage.models import SimSession
    db = _get_db()
    running = db.query(SimSession).filter(SimSession.status == "running").all()
    for s in running:
        s.status = "stopped"
        s.stopped_at = datetime.now(timezone.utc)
        for p in s.positions:
            if p.status == "open":
                p.status = "voided"
    db.commit()
    return len(running)


# ── live API balance ───────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def _kalshi_balance() -> float | None:
    try:
        from src.fetchers.kalshi import KalshiFetcher
        return KalshiFetcher().get_balance() / 100
    except Exception:
        return None


# ── data loaders ───────────────────────────────────────────────────────────────

def load_sessions() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (sim_df, live_df)."""
    from src.storage.models import SimSession
    db = _get_db()
    rows = db.query(SimSession).order_by(SimSession.created_at.desc()).all()
    sim_rows, live_rows = [], []
    for s in rows:
        locked = s.locked_cents()
        total = s.total_value_cents()
        initial = s.initial_bankroll_cents
        pnl_cents = total - initial
        pnl_pct = pnl_cents / initial * 100 if initial else 0.0
        is_live = _is_live_session(s)
        record = {
            "ID": s.id,
            "Started": s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else "—",
            "Status": s.status,
            "Strategy": _session_strategy(s),
            "Balance ($)": round(total / 100, 4),
            "P&L ($)": round(pnl_cents / 100, 4),
            "Gain/Loss %": round(pnl_pct, 2),
            "Trades": s.total_trades,
            "W": s.won,
            "L": s.lost,
            "V": s.voided,
        }
        (live_rows if is_live else sim_rows).append(record)
    return pd.DataFrame(sim_rows), pd.DataFrame(live_rows)


def load_open_positions(session_id: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    from src.storage.models import SimPosition, SimSession
    db = _get_db()
    q = (
        db.query(SimPosition)
        .join(SimSession, SimPosition.session_id == SimSession.id)
        .filter(SimPosition.status == "open", SimSession.status == "running")
    )
    if session_id:
        q = q.filter(SimPosition.session_id == session_id)
    positions = q.order_by(SimPosition.created_at.desc()).all()

    # Group legacy series legs; everything else is one row
    series_groups: dict[str, list] = defaultdict(list)
    singles: list = []
    for p in positions:
        if p.arb_type == "series":
            series_groups[f"{p.session_id}|{_series_event_ticker(p.ticker)}"].append(p)
        else:
            singles.append(p)

    sim_rows, live_rows = [], []

    for key, legs in series_groups.items():
        rep = legs[0]
        row = {
            "Session": rep.session_id,
            "Type": "Series Arb",
            "Event": _describe_event(_series_event_ticker(rep.ticker)),
            "Contracts": rep.contracts,
            "Cost ($)": round(sum(l.cost_cents for l in legs) / 100, 4),
            "Entered": rep.created_at.strftime("%m-%d %H:%M") if rep.created_at else "—",
        }
        (live_rows if rep.live else sim_rows).append(row)

    for p in singles:
        arb_type = p.arb_type or "prediction"
        event_name = None
        if arb_type == "cross" and p.legs:
            kalshi_leg = next((l for l in p.legs if l.get("source") == "kalshi"), p.legs[0])
            event_name = kalshi_leg.get("event_name")
        row = {
            "Session": p.session_id,
            "Type": _fmt_type(arb_type),
            "Event": _describe_event(p.ticker.removeprefix("CROSS_"), event_name),
            "Contracts": p.contracts,
            "Cost ($)": round(p.cost_cents / 100, 4),
            "Entered": p.created_at.strftime("%m-%d %H:%M") if p.created_at else "—",
        }
        (live_rows if p.live else sim_rows).append(row)

    return pd.DataFrame(sim_rows), pd.DataFrame(live_rows)


def load_history(session_id: int | None = None, limit: int = 500) -> tuple[list[dict], list[dict]]:
    from src.storage.models import SimPosition
    db = _get_db()
    q = db.query(SimPosition).filter(SimPosition.status != "open")
    if session_id:
        q = q.filter(SimPosition.session_id == session_id)
    positions = q.order_by(SimPosition.settled_at.desc()).limit(limit).all()

    # Group legacy series legs
    series_groups: dict[str, list] = defaultdict(list)
    singles: list = []
    for p in positions:
        if p.arb_type == "series":
            series_groups[f"{p.session_id}|{_series_event_ticker(p.ticker)}"].append(p)
        else:
            singles.append(p)

    sim_arbs: list[dict] = []
    live_arbs: list[dict] = []

    for key, legs in series_groups.items():
        rep = legs[0]
        total_cost = sum(l.cost_cents for l in legs) / 100
        total_pnl = sum((l.pnl_cents or 0) for l in legs) / 100
        statuses = {l.status for l in legs}
        net_result = "VOIDED" if statuses == {"voided"} else ("WON" if total_pnl > 0 else "LOST")
        arb = {
            "result": net_result,
            "arb_type_label": "Series Arb",
            "event": _describe_event(_series_event_ticker(rep.ticker)),
            "contracts": rep.contracts,
            "cost": round(total_cost, 4),
            "pnl": round(total_pnl, 4),
            "roi": round(total_pnl / total_cost * 100 if total_cost else 0, 2),
            "settled": rep.settled_at.strftime("%m-%d %H:%M") if rep.settled_at else "—",
            "legs": [{"Ticker": l.ticker, "Side": l.side.upper(),
                      "Entry ¢": round(l.entry_price_cents, 1), "Contracts": l.contracts,
                      "Result": l.status.upper(), "P&L ($)": round((l.pnl_cents or 0) / 100, 4)}
                     for l in sorted(legs, key=lambda l: l.ticker)],
            "session": rep.session_id,
            "is_live": bool(rep.live),
        }
        (live_arbs if rep.live else sim_arbs).append(arb)

    for p in singles:
        arb_type = p.arb_type or "prediction"
        event_name = None
        if arb_type == "cross" and p.legs:
            kalshi_leg = next((l for l in p.legs if l.get("source") == "kalshi"), p.legs[0])
            event_name = kalshi_leg.get("event_name")
        cost = p.cost_cents / 100
        pnl = (p.pnl_cents or 0) / 100
        raw_legs = p.legs or [{"ticker": p.ticker, "side": p.side, "price_cents": p.entry_price_cents}]
        arb = {
            "result": p.status.upper(),
            "arb_type_label": _fmt_type(arb_type),
            "event": _describe_event(p.ticker.removeprefix("CROSS_"), event_name),
            "contracts": p.contracts,
            "cost": round(cost, 4),
            "pnl": round(pnl, 4),
            "roi": round(pnl / cost * 100 if cost else 0, 2),
            "settled": p.settled_at.strftime("%m-%d %H:%M") if p.settled_at else "—",
            "legs": [{"Ticker": l.get("ticker", p.ticker), "Side": l.get("side", p.side).upper(),
                      "Entry ¢": round(l.get("price_cents", p.entry_price_cents), 1),
                      "Contracts": p.contracts, "Result": p.status.upper(),
                      "P&L ($)": round(pnl, 4)} for l in raw_legs],
            "session": p.session_id,
            "is_live": bool(p.live),
        }
        (live_arbs if p.live else sim_arbs).append(arb)

    sim_arbs.sort(key=lambda x: x["settled"], reverse=True)
    live_arbs.sort(key=lambda x: x["settled"], reverse=True)
    return sim_arbs, live_arbs


# ── rendering helpers ──────────────────────────────────────────────────────────

_RESULT_COLORS = {"WON": ":green", "LOST": ":red", "VOIDED": ":orange"}
_RESULT_ICONS  = {"WON": "✅", "LOST": "❌", "VOIDED": "⚫"}


def _is_live_session_by_id(sid: int, live_df: pd.DataFrame) -> bool:
    return not live_df.empty and sid in live_df["ID"].values


def _render_sessions(df: pd.DataFrame, label: str):
    if df.empty:
        st.info(f"No {label.lower()} sessions.")
        return
    running = df[df["Status"] == "running"]
    target = running.iloc[0] if not running.empty else df.iloc[0]

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Session", f"#{int(target['ID'])}", target["Status"].upper())
    m2.metric("Strategy", target["Strategy"])
    m3.metric("Balance", f"${target['Balance ($)']:,.4f}")
    m4.metric("P&L", f"${target['P&L ($)']:+,.4f}", f"{target['Gain/Loss %']:+.2f}%",
              help="Gain/Loss % relative to starting bankroll.")
    m5.metric("W / L / V", f"{int(target['W'])} / {int(target['L'])} / {int(target['V'])}")

    st.dataframe(df, use_container_width=True, hide_index=True)


def _render_open_positions(df: pd.DataFrame):
    if df.empty:
        st.info("No open positions.")
        return
    st.caption(f"{len(df)} positions · ${df['Cost ($)'].sum():,.4f} locked")
    st.dataframe(df, use_container_width=True, hide_index=True)


def _render_history(arbs: list[dict]):
    if not arbs:
        st.info("No settled trades yet.")
        return

    wins  = sum(1 for a in arbs if a["result"] == "WON")
    losses = sum(1 for a in arbs if a["result"] == "LOST")
    voids  = sum(1 for a in arbs if a["result"] == "VOIDED")
    total_pnl = sum(a["pnl"] for a in arbs)
    win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0.0

    h1, h2, h3, h4, h5 = st.columns(5)
    h1.metric("Trades", wins + losses + voids)
    h2.metric("Wins", wins)
    h3.metric("Losses", losses)
    h4.metric("Win Rate", f"{win_rate:.1f}%")
    h5.metric("Total P&L", f"${total_pnl:+.4f}")

    # Strategy breakdown (only show if more than one type)
    type_counts: dict[str, int] = defaultdict(int)
    type_pnl: dict[str, float] = defaultdict(float)
    for a in arbs:
        lbl = a["arb_type_label"]
        type_counts[lbl] += 1
        type_pnl[lbl] += a["pnl"]

    if len(type_counts) > 1:
        bd = pd.DataFrame({
            "Strategy": list(type_counts.keys()),
            "Trades":   list(type_counts.values()),
            "P&L ($)":  [round(type_pnl[k], 4) for k in type_counts],
        }).sort_values("Trades", ascending=False)
        with st.expander("Strategy breakdown"):
            c1, c2 = st.columns(2)
            c1.caption("Trades by strategy")
            c1.bar_chart(bd.set_index("Strategy")["Trades"], height=160)
            c2.caption("P&L ($) by strategy")
            c2.bar_chart(bd.set_index("Strategy")["P&L ($)"], height=160)

    # Cumulative P&L
    cumulative, running_total = [], 0.0
    for v in [a["pnl"] for a in reversed(arbs)]:
        running_total += v
        cumulative.append(running_total)
    st.line_chart(pd.Series(cumulative, name="Cumulative P&L ($)"), height=180)

    st.divider()

    for arb in arbs:
        icon  = _RESULT_ICONS.get(arb["result"], "")
        color = _RESULT_COLORS.get(arb["result"], "")
        header = (
            f"{icon} **{arb['event']}** &nbsp;|&nbsp; "
            f"`{arb['arb_type_label']}` &nbsp;|&nbsp; "
            f"P&L: **{color}[${arb['pnl']:+.4f}]** "
            f"({arb['roi']:+.2f}%) &nbsp;|&nbsp; "
            f"Cost: ${arb['cost']:.4f} &nbsp;|&nbsp; {arb['settled']}"
        )
        with st.expander(header):
            if arb["legs"]:
                st.dataframe(pd.DataFrame(arb["legs"]), use_container_width=True, hide_index=True)
            else:
                st.caption("No leg detail available.")


# ── sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Controls")
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        _refresh()
        st.rerun()

    st.divider()
    st.subheader("▶ New Simulation")
    sim_bankroll = st.number_input("Bankroll ($)", min_value=1.0, value=5.0, step=1.0)
    sim_last_sec = st.checkbox("Last-second strategy", value=True,
                               help="Buy YES on the bucket containing stable Kraken spot price ~75s before close.")
    sim_predict  = st.checkbox("Prediction trades (Claude + NewsAPI)", value=False,
                               help="Scan headlines each cycle and let Claude approve directional trades. Requires NEWS_API_KEY.")

    if st.button("🚀 Start Simulation", use_container_width=True, type="primary"):
        import subprocess, sys, os
        cmd = [sys.executable, "-m", "src.cli", "--simulate", "--bankroll", str(sim_bankroll)]
        cmd.append("--last-second" if sim_last_sec else "--no-last-second")
        if sim_predict:
            cmd.append("--prediction")
        try:
            subprocess.Popen(
                cmd,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            st.success("Simulation started in a new terminal window.")
        except Exception as e:
            st.error(f"Failed to start: {e}")

    st.divider()
    sim_sessions_df, live_sessions_df = load_sessions()
    all_sessions = pd.concat([sim_sessions_df, live_sessions_df], ignore_index=True)

    session_filter: int | None = None
    if not all_sessions.empty:
        running_count = (all_sessions["Status"] == "running").sum()
        if running_count:
            st.warning(f"{running_count} session(s) still marked running.")
            if st.button("🛑 Stop All Running Sessions", use_container_width=True, type="primary"):
                n = stop_all_running_sessions()
                st.cache_data.clear()
                _refresh()
                st.success(f"Stopped {n} session(s).")
                st.rerun()

        opts = ["All sessions"] + [
            f"Session {int(row['ID'])} ({'Live' if _is_live_session_by_id(int(row['ID']), live_sessions_df) else 'Sim'} · {row['Status']})"
            for _, row in all_sessions.sort_values("ID", ascending=False).iterrows()
        ]
        sel = st.selectbox("Filter by session", opts)
        if sel != "All sessions":
            session_filter = int(sel.split()[1])

    st.divider()
    auto_refresh = st.checkbox("Auto-refresh (30s)", value=False)


if auto_refresh:
    import time
    time.sleep(30)
    st.cache_data.clear()
    _refresh()
    st.rerun()

# ── header ─────────────────────────────────────────────────────────────────────

st.title("⚡ Kalshi Sniper Dashboard")
st.caption(f"Last loaded: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

# ── Kalshi balance ─────────────────────────────────────────────────────────────

st.subheader("💰 Account Balance")
col_bal, col_spacer = st.columns([1, 3])
with col_bal:
    bal = _kalshi_balance()
    st.metric("Kalshi", f"${bal:,.2f}" if bal is not None else "N/A")

st.divider()

# ── sessions ───────────────────────────────────────────────────────────────────

st.subheader("📊 Sessions")
tab_sim, tab_live = st.tabs(["Simulated", "Live"])
with tab_sim:
    _render_sessions(sim_sessions_df, "simulated")
with tab_live:
    _render_sessions(live_sessions_df, "live")

st.divider()

# ── open positions ─────────────────────────────────────────────────────────────

st.subheader("🔓 Open Positions")
st.caption("Positions from active (running) sessions only.")
sim_open, live_open = load_open_positions(session_filter)
tab_sim, tab_live = st.tabs(["Simulated", "Live"])
with tab_sim:
    _render_open_positions(sim_open)
with tab_live:
    _render_open_positions(live_open)

st.divider()

# ── historical trades ──────────────────────────────────────────────────────────

st.subheader("📜 Trade History")
st.caption("Click any row to see leg detail.")
sim_arbs, live_arbs = load_history(session_filter)
tab_sim, tab_live = st.tabs(["Simulated", "Live"])
with tab_sim:
    _render_history(sim_arbs)
with tab_live:
    _render_history(live_arbs)
