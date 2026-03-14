"""
Live simulation engine — continuously paper-trades near-term Kalshi crypto
markets (events expiring within a configurable time window).

Strategy:
  1. Discover 15M crypto markets early, add to watchlist.
  2. Arbs and EV bets enter immediately each scan (timing irrelevant).
  3. Agent bets (Claude) fire only within `entry_window_seconds` of close
     so Claude has the maximum price information before committing.
  4. Repeat every tick (settle_interval_seconds).

Usage:
    python -m src.cli simulate live --bankroll 5.00
    python -m src.cli simulate live --bankroll 5.00 --entry-window 30
"""
from __future__ import annotations

import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

_RUNNING = True


def _handle_sigint(sig, frame):
    global _RUNNING
    _RUNNING = False
    print("\n  [SIM] Stopping after this cycle completes...")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(log_file, msg: str, also_print: bool = True):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if also_print:
        print(line)
    log_file.write(line + "\n")
    log_file.flush()


# ---------------------------------------------------------------------------
# Near-term market fetch
# ---------------------------------------------------------------------------

def _event_close_time(event: dict) -> datetime | None:
    raw = event.get("close_time") or event.get("expiration_time")
    if not raw:
        for m in event.get("markets") or []:
            raw = m.get("close_time") or m.get("expiration_time")
            if raw:
                break
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.rstrip("Z")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fetch_near_term_markets(fetcher, categories: list[str], within_minutes: int):
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=within_minutes)
    events = fetcher.get_events_raw(categories=categories)
    near_markets = []
    seen_events: list[str] = []
    for event in events:
        close_time = _event_close_time(event)
        if close_time is None or close_time <= now or close_time > cutoff:
            continue
        parsed = fetcher._parse_event(event)
        if parsed:
            near_markets.extend(parsed)
            seen_events.append(
                f"{event.get('event_ticker','?')} closes {close_time.strftime('%H:%M UTC')}"
            )
    return near_markets, seen_events


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def _settle_open_positions(db: Session, session_id: int, fetcher, log_file) -> int:
    from src.storage.models import SimPosition, SimSession

    open_positions = (
        db.query(SimPosition)
        .filter(SimPosition.session_id == session_id, SimPosition.status == "open")
        .all()
    )
    settled_count = 0
    now = datetime.now(timezone.utc)
    sim = db.get(SimSession, session_id)

    for pos in open_positions:
        try:
            check_ticker = pos.ticker
            if pos.arb_type == "series" and pos.legs:
                check_ticker = pos.legs[0]["ticker"]
            elif pos.arb_type == "cross" and pos.legs:
                # Use Kalshi leg as canonical oracle; find it by source field
                kalshi_leg = next(
                    (l for l in pos.legs if l.get("source") == "kalshi"),
                    pos.legs[0],
                )
                check_ticker = kalshi_leg.get("ticker", pos.ticker.replace("CROSS_", "", 1))
            mkt = fetcher.get_market_status(check_ticker)
        except Exception as exc:
            _log(log_file, f"  [ERR] fetch {pos.ticker}: {exc}")
            continue

        mkt_status = mkt.get("status", "")
        mkt_result = mkt.get("result") or ""  # normalize None → ""
        if mkt_status not in ("finalized", "settled", "closed") or mkt_result not in ("yes", "no"):
            continue

        if pos.arb_type == "binary":
            outcome = "won"
            pnl = pos.contracts * 100.0 - pos.cost_cents
        elif pos.arb_type == "series":
            if mkt_result == pos.side:
                outcome = "won"
                pnl = pos.contracts * 100.0 - pos.cost_cents
            else:
                outcome = "lost"
                pnl = -pos.cost_cents
        else:
            # Covers "cross" (cross-platform arb) and directional bets.
            # For cross arb: uses Kalshi leg as canonical oracle for paper-trade
            # settlement (ticker=CROSS_<kalshi_id>, side=kalshi_leg side).
            if mkt_result == pos.side:
                outcome = "won"
                pnl = pos.contracts * 100.0 - pos.cost_cents
            else:
                outcome = "lost"
                pnl = -pos.cost_cents

        pos.status = outcome
        pos.result = mkt_result
        pos.pnl_cents = round(pnl, 2)
        pos.settled_at = now

        if outcome == "won":
            sim.current_bankroll_cents += pos.cost_cents + pnl
            sim.won += 1
        elif outcome == "voided":
            sim.current_bankroll_cents += pos.cost_cents
            sim.voided += 1
        else:
            sim.lost += 1

        label = "WON " if outcome == "won" else "LOST"
        _log(log_file,
            f"  SETTLE {label} {pos.ticker} ({pos.side.upper()}) "
            f"| P&L={pnl:+.0f}c  bankroll=${sim.current_bankroll_cents/100:.4f}"
        )
        settled_count += 1

    db.commit()
    return settled_count


# ---------------------------------------------------------------------------
# Live order placement
# ---------------------------------------------------------------------------

def _place_live_legs(fetcher, legs: list[dict], count: int, log_file) -> list[str] | None:
    """
    Place limit buy orders for each leg. Returns list of order_ids if all legs
    filled, or None if any leg failed (already-placed legs are cancelled).
    legs: [{"ticker": str, "side": str, "price_cents": int}]
    """
    import time
    placed: list[tuple[str, str]] = []  # (order_id, ticker)

    for leg in legs:
        try:
            order = fetcher.place_order(
                ticker=leg["ticker"],
                side=leg["side"],
                price_cents=int(leg["price_cents"]),
                count=count,
            )
        except Exception as exc:
            _log(log_file, f"  [LIVE] order FAILED {leg['ticker']} ({leg['side']}): {exc}")
            # Cancel any already-placed orders
            for oid, tkr in placed:
                try:
                    fetcher.cancel_order(oid)
                    _log(log_file, f"  [LIVE] cancelled {oid} ({tkr})")
                except Exception:
                    pass
            return None

        order_id = order.get("order_id") or order.get("id", "")
        status = order.get("status", "")
        filled = order.get("fill_count") or order.get("filled_count", 0)

        # If not immediately filled, wait briefly and re-check
        if status not in ("filled", "executed") and filled < count:
            time.sleep(2)
            try:
                order = fetcher.get_order(order_id)
                status = order.get("status", "")
                filled = order.get("fill_count") or order.get("filled_count", 0)
            except Exception:
                pass

        if status in ("filled", "executed") or filled >= count:
            _log(log_file, f"  [LIVE] filled  {leg['ticker']} ({leg['side']}) x{count} @ {leg['price_cents']}¢  order={order_id}")
            placed.append((order_id, leg["ticker"]))
        else:
            _log(log_file, f"  [LIVE] NOT filled {leg['ticker']} status={status} filled={filled}/{count} — cancelling all")
            # Cancel this order
            try:
                fetcher.cancel_order(order_id)
            except Exception:
                pass
            # Cancel already-placed orders
            for oid, tkr in placed:
                try:
                    fetcher.cancel_order(oid)
                    _log(log_file, f"  [LIVE] cancelled {oid} ({tkr})")
                except Exception:
                    pass
            return None

    return [oid for oid, _ in placed]


# ---------------------------------------------------------------------------
# Position entry helpers
# ---------------------------------------------------------------------------

def _enter_binary_arb(db, sim, open_keys, opp, max_position_pct, log_file, fetcher=None, live: bool = False):
    from src.storage.models import SimPosition

    ticker = opp.legs[0].ticker
    if ticker in open_keys:
        return
    bankroll = sim.current_bankroll_cents
    cost_per_set = opp.total_cost_cents
    sets = max(1, int(bankroll * min(0.20, max_position_pct * 2) // cost_per_set))
    total_cost = sets * cost_per_set
    if total_cost > bankroll:
        return

    leg_dicts = [{"ticker": l.ticker, "side": l.side, "price_cents": l.price_cents} for l in opp.legs]

    order_ids = None
    if live and fetcher is not None:
        order_ids = _place_live_legs(fetcher, leg_dicts, sets, log_file)
        if order_ids is None:
            _log(log_file, f"  [LIVE] binary-arb skipped (fill failed) {ticker}")
            return

    import json
    pos = SimPosition(
        session_id=sim.id, ticker=ticker, side="yes_no",
        entry_price_cents=cost_per_set, cost_cents=total_cost,
        contracts=sets, ev=opp.profit_pct, arb_type="binary",
        live=1 if live else 0,
        order_ids=json.dumps(order_ids) if order_ids else None,
    )
    pos.legs = leg_dicts
    db.add(pos)
    sim.current_bankroll_cents -= total_cost
    sim.total_trades += 1
    open_keys.add(ticker)
    mode_tag = "[LIVE]" if live else ""
    _log(log_file,
        f"  BUY  binary-arb {mode_tag} {ticker} | {sets} pair(s) @ {cost_per_set:.0f}c"
        f" | profit={opp.profit_cents * sets:.0f}c guaranteed"
        f"  | bankroll=${sim.current_bankroll_cents/100:.4f}"
    )


def _enter_series_arb(db, sim, open_keys, opp, log_file, fetcher=None, live: bool = False):
    import json
    from src.storage.models import SimPosition

    event_key = opp.event_ticker
    if event_key in open_keys:
        return
    bankroll = sim.current_bankroll_cents
    cost_per_set = opp.total_cost_cents
    sets = max(1, int(bankroll * 0.15 // cost_per_set))
    total_cost = sets * cost_per_set
    if total_cost > bankroll:
        return

    leg_dicts = [{"ticker": l.ticker, "side": l.side, "price_cents": l.price_cents} for l in opp.legs]

    # Place real orders if live mode — one order per leg
    order_ids_by_leg: list[list[str] | None] = [None] * len(opp.legs)
    if live and fetcher is not None:
        all_ids = _place_live_legs(fetcher, leg_dicts, sets, log_file)
        if all_ids is None:
            _log(log_file, f"  [LIVE] series-arb skipped (fill failed) {event_key}")
            return
        # Each leg got one order; distribute IDs
        for i in range(len(opp.legs)):
            order_ids_by_leg[i] = [all_ids[i]] if i < len(all_ids) else None

    for i, leg in enumerate(opp.legs):
        leg_dict = leg_dicts[i]
        pos = SimPosition(
            session_id=sim.id, ticker=leg.ticker, side=leg.side,
            entry_price_cents=leg.price_cents, cost_cents=leg.price_cents * sets,
            contracts=sets, ev=opp.profit_pct, arb_type="series",
            live=1 if live else 0,
            order_ids=json.dumps(order_ids_by_leg[i]) if order_ids_by_leg[i] else None,
        )
        pos.legs = [leg_dict]
        db.add(pos)
    sim.current_bankroll_cents -= total_cost
    sim.total_trades += 1
    open_keys.add(event_key)
    arb_label = "GUARANTEED" if opp.guaranteed else f"PARTIAL ~{opp.total_cost_cents:.0f}c coverage"
    mode_tag = "[LIVE] " if live else ""
    _log(log_file,
        f"  BUY  series-arb {mode_tag}{event_key} | {len(opp.legs)} legs x {sets} set(s)"
        f" @ {cost_per_set:.0f}c | profit={opp.profit_cents * sets:.0f}c [{arb_label}]"
        f"  | bankroll=${sim.current_bankroll_cents/100:.4f}"
    )


def _enter_agent_bet(db, sim, open_keys, market, advice, max_position_pct, log_file):
    from src.storage.models import SimPosition

    side = advice["action"]
    ticker = market.id
    pos_key = f"{ticker}_{side}"
    if pos_key in open_keys:
        return
    sel = next((s for s in market.selections if s.name.lower() == side), None)
    if sel is None:
        return
    bankroll = sim.current_bankroll_cents
    alloc = min(advice["confidence"] * 0.10, max_position_pct)
    price_cents = round(100.0 / sel.odds, 1)
    contracts = max(1, int(bankroll * alloc // price_cents))
    total_cost = contracts * price_cents
    if total_cost > bankroll or total_cost < 1:
        return
    pos = SimPosition(
        session_id=sim.id, ticker=ticker, side=side,
        entry_price_cents=price_cents, cost_cents=total_cost,
        contracts=contracts, ev=advice["confidence"], arb_type=None,
    )
    db.add(pos)
    sim.current_bankroll_cents -= total_cost
    sim.total_trades += 1
    open_keys.add(pos_key)
    _log(log_file,
        f"  BUY  agent-bet   {ticker} {side.upper()} @ {price_cents:.1f}c"
        f" | conf={advice['confidence']:.0%}  alloc={alloc:.1%}"
        f" | {contracts}x cost={total_cost:.0f}c"
        f"  | bankroll=${sim.current_bankroll_cents/100:.4f}"
        f"\n            Claude: {advice['rationale']}"
    )


def _enter_last_second_bet(
    db, sim, open_keys, entry: dict, contracts: int, log_file,
    fetcher=None, live: bool = False
):
    """Enter a last-second YES bet on a single Kalshi bucket."""
    import json
    from src.storage.models import SimPosition

    mkt = entry["market"]
    ticker = mkt.id
    if ticker in open_keys:
        return

    yes_ask = entry["yes_ask_cents"]
    total_cost = yes_ask * contracts
    bankroll = sim.current_bankroll_cents
    if total_cost > bankroll or total_cost < 1:
        return

    order_ids = None
    if live and fetcher is not None:
        leg_dicts = [{"ticker": ticker, "side": "yes", "price_cents": yes_ask}]
        order_ids = _place_live_legs(fetcher, leg_dicts, contracts, log_file)
        if order_ids is None:
            _log(log_file, f"  [LIVE] last-second skipped (fill failed) {ticker}")
            return

    pos = SimPosition(
        session_id=sim.id, ticker=ticker, side="yes",
        entry_price_cents=yes_ask, cost_cents=total_cost,
        contracts=contracts, ev=0.0, arb_type="last_second",
        live=1 if live else 0,
        order_ids=json.dumps(order_ids) if order_ids else None,
    )
    pos.legs = [{"ticker": ticker, "side": "yes", "price_cents": yes_ask}]
    db.add(pos)
    sim.current_bankroll_cents -= total_cost
    sim.total_trades += 1
    open_keys.add(ticker)

    mode_tag = "[LIVE] " if live else ""
    _log(log_file,
        f"  BUY  last-second {mode_tag}{ticker} YES @ {yes_ask}¢ x{contracts}"
        f"  | spot={entry['spot_price']:.4f} ({entry['kraken_pair']})"
        f"  | closes_in={entry['seconds_to_close']:.0f}s"
        f"  | bankroll=${sim.current_bankroll_cents/100:.4f}"
    )


def _enter_ev_bet(db, sim, open_keys, rec, max_position_pct, log_file):
    from src.storage.models import SimPosition

    ticker = rec.market.id
    side = rec.selection.name.lower()
    pos_key = f"{ticker}_{side}"
    if pos_key in open_keys:
        return
    bankroll = sim.current_bankroll_cents
    price_cents = round(100.0 / rec.selection.odds, 1)
    contracts = max(1, int(bankroll * min(rec.kelly_fraction, max_position_pct) // price_cents))
    total_cost = contracts * price_cents
    if total_cost > bankroll or total_cost < 1:
        return
    pos = SimPosition(
        session_id=sim.id, ticker=ticker, side=side,
        entry_price_cents=price_cents, cost_cents=total_cost,
        contracts=contracts, ev=rec.ev, arb_type=None,
    )
    db.add(pos)
    sim.current_bankroll_cents -= total_cost
    sim.total_trades += 1
    open_keys.add(pos_key)
    _log(log_file,
        f"  BUY  ev-bet      {ticker} {side.upper()} @ {price_cents:.1f}c"
        f" | EV={rec.ev:.2%} kelly={rec.kelly_fraction:.3%}"
        f" | {contracts}x cost={total_cost:.0f}c"
        f"  | bankroll=${sim.current_bankroll_cents/100:.4f}"
    )


# ---------------------------------------------------------------------------
# Cross-platform arb entry helper
# ---------------------------------------------------------------------------

def _enter_cross_arb(
    db, sim, open_keys, opp, log_file,
    live: bool = False,
    min_profit_cents: float = 3.0,
):
    """
    Enter a cross-platform arb position (sim only for now).

    Settlement note: uses the Kalshi leg as the canonical oracle for paper-trade
    P&L purposes.  Live Polymarket order placement is out of scope (requires USDC
    on Polygon); if use_live_orders=True this helper logs a warning and skips.
    """
    import json
    from src.storage.models import SimPosition

    if opp.profit_cents < min_profit_cents:
        return
    if opp.settlement_risk == "high":
        return

    if live:
        _log(log_file,
             f"  [CROSS-ARB] Skipping live order — Polymarket live placement not supported: "
             f"{opp.kalshi_market.event_name[:50]}")
        return

    key = f"CROSS_{opp.kalshi_market.id}"
    if key in open_keys:
        return

    bankroll = sim.current_bankroll_cents
    cost_per_set = opp.total_cost_cents
    sets = max(1, int(bankroll * 0.10 // cost_per_set))
    total_cost = sets * cost_per_set
    if total_cost > bankroll:
        return

    # Enrich leg dicts with ticker so settlement can look up Kalshi result
    kalshi_leg = dict(opp.kalshi_leg, ticker=opp.kalshi_market.id)
    poly_leg = dict(opp.poly_leg, ticker=opp.poly_market.id)
    leg_dicts = [kalshi_leg, poly_leg]
    pos = SimPosition(
        session_id=sim.id,
        ticker=key,
        side=opp.kalshi_leg["side"],
        entry_price_cents=cost_per_set,
        cost_cents=total_cost,
        contracts=sets,
        ev=opp.profit_pct,
        arb_type="cross",
        live=0,
        order_ids=None,
    )
    pos.legs = leg_dicts
    db.add(pos)
    sim.current_bankroll_cents -= total_cost
    sim.total_trades += 1
    open_keys.add(key)

    _log(log_file,
        f"  BUY  cross-arb  {key} | {sets} set(s) @ {cost_per_set:.0f}c"
        f" | profit={opp.profit_cents * sets:.1f}c ({opp.profit_pct:.2%})"
        f" | risk={opp.settlement_risk} score={opp.match_score:.2f}"
        f"  | bankroll=${sim.current_bankroll_cents/100:.4f}"
        f"\n            K: {opp.kalshi_market.event_name[:50]}"
        f"\n            P: {opp.poly_market.event_name[:50]}"
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _wait_interruptible(seconds: int) -> bool:
    for _ in range(seconds):
        if not _RUNNING:
            return False
        time.sleep(1)
    return True


def run_live_simulation(
    db: Session,
    initial_bankroll_usd: float = 5.00,
    interval_seconds: int = 60,
    settle_interval_seconds: int = 5,
    categories: list[str] | None = None,
    min_arb_profit_cents: float = 1.0,
    min_leg_cost_cents: float = 101.0,
    max_position_pct: float = 0.20,
    max_deploy_pct: float = 0.80,
    near_term_minutes: int = 60,
    logs_dir: str = "logs",
    resume_session_id: int | None = None,
    use_live_orders: bool = False,
    use_last_second: bool = False,
    ls_entry_window: int = 75,
    ls_min_yes_cents: int = 70,
    ls_max_yes_cents: int = 95,
    ls_edge_buffer_pct: float = 0.15,
    ls_stability_window_s: int = 15,
    ls_stability_threshold_pct: float = 0.003,
    use_polymarket: bool = False,
    # kept for CLI compat, unused
    min_ev: float = 0.005,
    entry_window_seconds: int = 45,
    use_agent: bool = False,
) -> None:
    """
    Arb-only (+ optional last-second + optional cross-platform) live simulation.
    Every scan cycle:
      1. Settle any resolved positions.
      2. Fetch near-term Kalshi markets.
      3. Enter guaranteed binary arbs and series arbs.
         - Guaranteed (exhaustive coverage): always entered.
         - Partial coverage: entered only when total leg cost >= min_leg_cost_cents
           (default 101.0 effectively disables partial arbs).
      4. If use_polymarket=True, also each scan cycle (section B2):
         - Fetch Polymarket markets for matched categories.
         - Run match_markets() + scan_cross_arb().
         - Enter cross-arb positions (sim only — live Polymarket placement not supported).
      5. If use_last_second=True, also on EVERY TICK:
         - Update Kraken spot prices for tracked pairs.
         - Find crypto bucket markets closing within ls_entry_window seconds.
         - Buy YES on the bucket containing the stable spot price.
    """
    global _RUNNING
    _RUNNING = True
    signal.signal(signal.SIGINT, _handle_sigint)

    from src.fetchers.kalshi import KalshiFetcher
    from src.engine.arbitrage import scan_binary_arb, scan_series_arb
    from src.storage.models import SimSession, SimPosition

    # Last-second strategy state
    _ls_trackers: dict = {}          # kraken_pair → PriceTracker
    _ls_markets_cache: list = []     # near-term markets from last full scan
    _ls_entered_tickers: set = set() # tickers already entered this close-time cycle

    target_categories = categories or ["Crypto", "Economics", "Financials"]
    Path(logs_dir).mkdir(exist_ok=True)
    initial_cents = round(initial_bankroll_usd * 100, 2)

    if resume_session_id:
        sim = db.get(SimSession, resume_session_id)
        if sim is None:
            raise ValueError(f"Session {resume_session_id} not found")
        log_path = sim.log_path
    else:
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = str(Path(logs_dir) / f"sim_{ts_str}.log")
        sim = SimSession(
            initial_bankroll_cents=initial_cents,
            current_bankroll_cents=initial_cents,
            status="running",
            log_path=log_path,
        )
        db.add(sim)
        db.commit()

    session_id = sim.id

    try:
        fetcher = KalshiFetcher()
    except Exception as exc:
        raise RuntimeError(f"Cannot init Kalshi fetcher: {exc}")

    poly_fetcher = None
    if use_polymarket:
        from src.fetchers.polymarket import PolymarketFetcher
        try:
            poly_fetcher = PolymarketFetcher(category_filter=target_categories)
        except Exception as exc:
            raise RuntimeError(f"Cannot init Polymarket fetcher: {exc}")

    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with open(log_path, "a", encoding="utf-8") as log_file:
        mode_parts = ["LIVE" if use_live_orders else "SIM"]
        if use_polymarket:
            mode_parts.append("+CROSS-ARB")
        if use_last_second:
            mode_parts.append("+LAST-SEC")
        mode_label = "ARB-ONLY " + " ".join(mode_parts)
        _log(log_file, "=" * 65)
        _log(log_file,
            f"  SESSION {session_id}  [{mode_label}]  bankroll=${initial_bankroll_usd:.2f}"
            f"  |  scan={interval_seconds}s  tick={settle_interval_seconds}s"
        )
        _log(log_file, f"  categories={target_categories}  |  near_term={near_term_minutes}min")
        _log(log_file, f"  min_arb_profit={min_arb_profit_cents:.0f}c  |  min_leg_cost={min_leg_cost_cents:.0f}c  |  max_pos={max_position_pct:.0%}  |  max_deploy={max_deploy_pct:.0%}")
        if use_last_second:
            _log(log_file,
                f"  [LAST-SECOND] entry_window={ls_entry_window}s"
                f"  yes_ask=[{ls_min_yes_cents},{ls_max_yes_cents}]¢"
                f"  edge_buf={ls_edge_buffer_pct:.0%}"
                f"  stability={ls_stability_window_s}s/<{ls_stability_threshold_pct:.1%}"
            )
        _log(log_file, "=" * 65)

        last_scan_at = 0.0
        tick = 0

        while _RUNNING:
            tick += 1
            now = datetime.now(timezone.utc)
            now_ts = now.timestamp()

            # ----------------------------------------------------------------
            # A. Settle open positions (every tick)
            # ----------------------------------------------------------------
            newly_settled = _settle_open_positions(db, session_id, fetcher, log_file)
            if newly_settled:
                db.refresh(sim)
                locked_now = sum(
                    p.cost_cents for p in db.query(SimPosition).filter(
                        SimPosition.session_id == session_id,
                        SimPosition.status == "open",
                    ).all()
                )
                _log(log_file,
                    f"  SETTLED {newly_settled}"
                    f"  liquid=${sim.current_bankroll_cents/100:.4f}"
                    f"  locked=${locked_now/100:.4f}"
                    f"  total=${(sim.current_bankroll_cents + locked_now)/100:.4f}"
                )

            # ----------------------------------------------------------------
            # A2. Last-second scanner (every tick when enabled)
            # ----------------------------------------------------------------
            if use_last_second and _ls_markets_cache:
                from src.engine.last_second import (
                    scan_last_second_opportunities,
                    update_price_trackers,
                    kraken_pair_for_market,
                    _PREFIX_TO_KRAKEN,
                )
                # Determine which pairs we need to track
                pairs_needed: set[str] = set()
                for mkt in _ls_markets_cache:
                    pair = kraken_pair_for_market(mkt)
                    if pair:
                        pairs_needed.add(pair)

                if pairs_needed:
                    update_price_trackers(_ls_trackers, pairs_needed)

                ls_entries = scan_last_second_opportunities(
                    _ls_markets_cache, _ls_trackers, now,
                    entry_window_seconds=ls_entry_window,
                    min_yes_cents=ls_min_yes_cents,
                    max_yes_cents=ls_max_yes_cents,
                    edge_buffer_pct=ls_edge_buffer_pct,
                    stability_window_s=ls_stability_window_s,
                    stability_threshold_pct=ls_stability_threshold_pct,
                )

                for entry in ls_entries:
                    ticker = entry["market"].id
                    if ticker in _ls_entered_tickers:
                        continue
                    db.refresh(sim)
                    # Size: 1 contract per opportunity (small, directional bet)
                    contracts = max(1, int(sim.current_bankroll_cents * 0.05 // entry["yes_ask_cents"]))
                    contracts = min(contracts, 5)  # cap at 5 contracts
                    # Use open_keys across all position types
                    ls_open_keys: set[str] = {
                        p.ticker for p in db.query(SimPosition).filter(
                            SimPosition.session_id == session_id,
                            SimPosition.status == "open",
                        ).all()
                    }
                    _enter_last_second_bet(
                        db, sim, ls_open_keys, entry, contracts, log_file,
                        fetcher=fetcher, live=use_live_orders,
                    )
                    if ticker in ls_open_keys or contracts >= 1:
                        _ls_entered_tickers.add(ticker)
                    db.commit()

                # Prune _ls_entered_tickers for markets that have closed
                cutoff_dt = now
                expired = {
                    t for t in _ls_entered_tickers
                    if not any(m.id == t and m.starts_at and m.starts_at > cutoff_dt
                               for m in _ls_markets_cache)
                }
                _ls_entered_tickers -= expired

            # ----------------------------------------------------------------
            # B. Full market scan + arb entry (every interval_seconds)
            # ----------------------------------------------------------------
            if now_ts - last_scan_at >= interval_seconds:
                last_scan_at = now_ts
                _log(log_file, "")
                _log(log_file,
                    f"-- SCAN  tick={tick}  {now.strftime('%H:%M:%S UTC')} --------------------"
                )
                db.refresh(sim)
                liquid = sim.current_bankroll_cents
                open_positions = db.query(SimPosition).filter(
                    SimPosition.session_id == session_id,
                    SimPosition.status == "open",
                ).all()
                locked_now = sum(p.cost_cents for p in open_positions)
                # total = liquid + locked (liquid already excludes deployed funds)
                _log(log_file,
                    f"  liquid=${liquid/100:.4f}  locked=${locked_now/100:.4f}"
                    f"  total=${(liquid + locked_now)/100:.4f}  |  open={len(open_positions)}"
                )
                try:
                    markets, seen_events = _fetch_near_term_markets(
                        fetcher, target_categories, near_term_minutes
                    )
                    _ls_markets_cache[:] = markets  # update last-second cache
                except Exception as exc:
                    _log(log_file, f"  ERROR fetching markets: {exc}")
                    markets, seen_events = [], []

                if markets:
                    _log(log_file, f"  {len(markets)} markets across {len(seen_events)} event(s):")
                    for ev_info in seen_events[:8]:
                        _log(log_file, f"    - {ev_info}")
                    if len(seen_events) > 8:
                        _log(log_file, f"    ... and {len(seen_events) - 8} more")
                else:
                    _log(log_file, f"  No near-term markets found in {target_categories}.")

                spendable = liquid * max_deploy_pct
                open_keys: set[str] = {p.ticker for p in open_positions}
                open_event_keys: set[str] = {p.ticker.rsplit("-", 1)[0] for p in open_positions}
                cycle_spent = 0.0
                arbs_entered = 0

                binary_opps = scan_binary_arb(markets, min_profit_cents=min_arb_profit_cents)
                all_series = scan_series_arb(markets, min_profit_cents=min_arb_profit_cents)
                series_opps = [
                    o for o in all_series
                    if o.guaranteed or o.total_cost_cents >= min_leg_cost_cents
                ]
                n_guaranteed = sum(1 for o in series_opps if o.guaranteed)
                n_partial = len(series_opps) - n_guaranteed

                if binary_opps:
                    _log(log_file, f"  Binary arbs found: {len(binary_opps)}")
                if series_opps:
                    _log(log_file,
                        f"  Series arbs found: {len(series_opps)}"
                        f"  (guaranteed={n_guaranteed}  partial={n_partial})"
                    )
                if not binary_opps and not series_opps:
                    _log(log_file, "  No arb opportunities this scan.")

                for opp in binary_opps:
                    if not _RUNNING or cycle_spent >= spendable:
                        break
                    before = sim.current_bankroll_cents
                    _enter_binary_arb(db, sim, open_keys, opp, max_position_pct, log_file,
                                      fetcher=fetcher, live=use_live_orders)
                    spent = before - sim.current_bankroll_cents
                    cycle_spent += spent
                    if spent > 0:
                        arbs_entered += 1

                for opp in series_opps:
                    if not _RUNNING or cycle_spent >= spendable:
                        break
                    event_key = opp.event_ticker
                    if event_key in open_event_keys:
                        continue
                    before = sim.current_bankroll_cents
                    _enter_series_arb(db, sim, open_keys, opp, log_file,
                                      fetcher=fetcher, live=use_live_orders)
                    spent = before - sim.current_bankroll_cents
                    cycle_spent += spent
                    if spent > 0:
                        arbs_entered += 1
                        open_event_keys.add(event_key)

                if arbs_entered:
                    _log(log_file, f"  Entered {arbs_entered} arb position(s) this scan.")

                # --------------------------------------------------------
                # B2. Cross-platform arb scan (Polymarket, sim only)
                # --------------------------------------------------------
                if use_polymarket and poly_fetcher is not None and markets:
                    from src.engine.cross_arb import match_markets, scan_cross_arb
                    try:
                        poly_markets = poly_fetcher.get_markets()
                        cross_pairs = match_markets(markets, poly_markets)
                        cross_opps = scan_cross_arb(
                            cross_pairs,
                            min_profit_cents=max(min_arb_profit_cents, 3.0),
                        )
                        if cross_opps:
                            _log(log_file, f"  Cross-arb opportunities: {len(cross_opps)}")
                        cross_open_keys: set[str] = {
                            p.ticker for p in db.query(SimPosition).filter(
                                SimPosition.session_id == session_id,
                                SimPosition.status == "open",
                            ).all()
                        }
                        for opp in cross_opps:
                            if not _RUNNING or cycle_spent >= spendable:
                                break
                            before = sim.current_bankroll_cents
                            _enter_cross_arb(
                                db, sim, cross_open_keys, opp, log_file,
                                live=use_live_orders,
                            )
                            spent = before - sim.current_bankroll_cents
                            cycle_spent += spent
                    except Exception as exc:
                        _log(log_file, f"  [CROSS-ARB] fetch/scan error: {exc}")

                db.commit()

            # ----------------------------------------------------------------
            # C. Stop if bankrupt with no open positions
            # ----------------------------------------------------------------
            db.refresh(sim)
            if sim.current_bankroll_cents <= 0:
                open_count = db.query(SimPosition).filter(
                    SimPosition.session_id == session_id,
                    SimPosition.status == "open",
                ).count()
                if open_count == 0:
                    _log(log_file, "  Bankroll depleted and no open positions -- stopping.")
                    break

            if _RUNNING:
                _wait_interruptible(settle_interval_seconds)

        # Shutdown
        db.refresh(sim)
        sim.status = "stopped"
        sim.stopped_at = datetime.now(timezone.utc)
        db.commit()

        locked = sum(
            p.cost_cents for p in db.query(SimPosition).filter(
                SimPosition.session_id == session_id,
                SimPosition.status == "open",
            ).all()
        )
        total_value = sim.current_bankroll_cents + locked
        pnl = total_value - sim.initial_bankroll_cents

        _log(log_file, "")
        _log(log_file, "=" * 65)
        _log(log_file, f"  SESSION {session_id} STOPPED  ticks={tick}")
        _log(log_file, f"  Initial bankroll : ${sim.initial_bankroll_cents/100:.4f}")
        _log(log_file, f"  Liquid           : ${sim.current_bankroll_cents/100:.4f}")
        _log(log_file, f"  Locked (open)    : ${locked/100:.4f}")
        _log(log_file, f"  Total value      : ${total_value/100:.4f}")
        _log(log_file, f"  Unrealised P&L   : ${pnl/100:+.4f}")
        _log(log_file, f"  Trades opened    : {sim.total_trades}")
        _log(log_file, f"  W / L / V        : {sim.won} / {sim.lost} / {sim.voided}")
        _log(log_file, f"  Log file         : {log_path}")
        _log(log_file, "=" * 65)
