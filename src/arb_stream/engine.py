"""
Streaming ARB engine — uses live WebSocket price feeds to detect arbs faster
than the REST-polling scanner and records detection vs. entry price for
empirical slippage measurement.

Zero dependencies on src/engine/live_sim.py.  Isolated in src/arb_stream/.
"""
from __future__ import annotations

import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_RUNNING = True


def _handle_sigint(sig, frame):
    global _RUNNING
    _RUNNING = False
    print("\n  [ARB-STREAM] Stopping after this cycle completes...")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(log_file, msg: str, also_print: bool = True) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if also_print:
        print(line)
    log_file.write(line + "\n")
    log_file.flush()


# ---------------------------------------------------------------------------
# Market helpers
# ---------------------------------------------------------------------------

def _fetch_near_term_markets(fetcher, categories: list[str], within_minutes: int) -> list:
    """Fetch and return only markets closing within `within_minutes`."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=within_minutes)
    events = fetcher.get_events_raw(categories=categories)
    markets = []
    for event in events:
        for mkt in fetcher._parse_event(event):
            if mkt.starts_at and now < mkt.starts_at <= cutoff:
                markets.append(mkt)
    return markets


def _apply_ws_prices(market, cache) -> object:
    """
    Return a copy of *market* with yes_ask / no_ask in selection metadata
    patched from the WebSocket cache.  Falls back to the REST prices when
    the cache entry is absent or older than 10 s.
    """
    from dataclasses import replace
    from src.fetchers.base import Selection

    ws_yes = cache.get_yes_ask(market.id)
    ws_no = cache.get_no_ask(market.id)

    # Nothing fresh in cache — return market unchanged
    if (ws_yes is None or cache.yes_ask_age(market.id) >= 10) and \
       (ws_no is None or cache.no_ask_age(market.id) >= 10):
        return market

    new_selections = []
    for sel in market.selections:
        new_meta = dict(sel.metadata)
        if sel.name == "Yes" and ws_yes is not None and cache.yes_ask_age(market.id) < 10:
            new_meta["yes_ask"] = int(round(ws_yes))
        elif sel.name == "No" and ws_no is not None and cache.no_ask_age(market.id) < 10:
            new_meta["no_ask"] = int(round(ws_no))
        new_selections.append(Selection(name=sel.name, odds=sel.odds, metadata=new_meta))

    return replace(market, selections=new_selections, metadata=dict(market.metadata))


def _arb_key(opp) -> str:
    closes_iso = opp.closes_at.isoformat() if opp.closes_at else "none"
    return f"{opp.event_ticker}|{closes_iso}"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def _ws_coverage(markets: list, cache) -> dict:
    """
    For each market, check whether we have a fresh WS price (age < 10s).
    Returns counts: yes_fresh, no_fresh, both_fresh, neither (REST-only).
    """
    yes_fresh = no_fresh = both = neither = 0
    for m in markets:
        has_yes = cache.get_yes_ask(m.id) is not None and cache.yes_ask_age(m.id) < 10
        has_no  = cache.get_no_ask(m.id)  is not None and cache.no_ask_age(m.id)  < 10
        if has_yes and has_no:
            both += 1
        elif has_yes:
            yes_fresh += 1
        elif has_no:
            no_fresh += 1
        else:
            neither += 1
    return {"yes_only": yes_fresh, "no_only": no_fresh, "both": both, "rest_only": neither}


def _log_scan_results(log_file, all_opps: list, open_keys: set,
                      min_profit_cents: int, contracts: int,
                      bankroll_usd: float) -> None:
    """
    Log the full arb scan results: what was found, why each was accepted or
    skipped, and the best near-miss when nothing qualifies.
    """
    if not all_opps:
        _log(log_file, "  [SCAN] no opportunities found (sum(yes_asks) >= 100 for all groups)",
             also_print=False)
        return

    entered = skipped_not_guaranteed = skipped_profit = skipped_open = skipped_funds = 0

    for opp in all_opps:
        key = _arb_key(opp)
        tag = f"{opp.arb_type:<6} {opp.event_ticker}"
        cost = opp.total_cost_cents * contracts
        profit = opp.profit_cents  # per 1 contract
        closes = opp.closes_at.strftime("%H:%M") if opp.closes_at else "?"

        if key in open_keys:
            skipped_open += 1
            reason = "already open"
        elif not opp.guaranteed:
            skipped_not_guaranteed += 1
            reason = "not guaranteed"
        elif profit < min_profit_cents:
            skipped_profit += 1
            reason = f"profit={profit:.1f}¢ < min={min_profit_cents}¢"
        elif cost / 100.0 > bankroll_usd:
            skipped_funds += 1
            reason = f"need=${cost/100:.2f} > bankroll=${bankroll_usd:.4f}"
        else:
            entered += 1
            reason = ">>> ENTER"

        _log(log_file,
             f"  [SCAN]  {tag}  cost={opp.total_cost_cents:.1f}¢  "
             f"profit={profit:.1f}¢ ({opp.profit_pct:.2%})  "
             f"legs={len(opp.legs)}  guaranteed={opp.guaranteed}  "
             f"closes={closes}  → {reason}",
             also_print=False)

    summary_parts = [f"{len(all_opps)} opps found"]
    if entered:
        summary_parts.append(f"{entered} entered")
    if skipped_not_guaranteed:
        summary_parts.append(f"{skipped_not_guaranteed} not-guaranteed")
    if skipped_profit:
        summary_parts.append(f"{skipped_profit} below-min-profit")
    if skipped_open:
        summary_parts.append(f"{skipped_open} already-open")
    if skipped_funds:
        summary_parts.append(f"{skipped_funds} insufficient-funds")
    _log(log_file, f"  [SCAN] {' | '.join(summary_parts)}", also_print=False)


def _log_cache_sample(log_file, cache, tickers: set) -> None:
    """Dump a sample of WS prices for up to 8 subscribed tickers."""
    snap = cache.snapshot()
    yes_snap = snap.get("yes_ask", {})
    no_snap  = snap.get("no_ask", {})

    sample = sorted(tickers)[:8]
    lines = []
    for t in sample:
        ya = yes_snap.get(t)
        na = no_snap.get(t)
        ya_age = f"{cache.yes_ask_age(t):.0f}s" if ya is not None else "—"
        na_age = f"{cache.no_ask_age(t):.0f}s"  if na is not None else "—"
        ya_str = f"{ya:.0f}¢({ya_age})" if ya is not None else "—"
        na_str = f"{na:.0f}¢({na_age})" if na is not None else "—"
        lines.append(f"    {t:<40}  yes={ya_str:<12}  no={na_str}")
    _log(log_file, f"  [CACHE] sample ({len(yes_snap)} yes / {len(no_snap)} no entries total):",
         also_print=False)
    for line in lines:
        _log(log_file, line, also_print=False)


# ---------------------------------------------------------------------------
# Entry — hot path, zero IO
# ---------------------------------------------------------------------------

def _record_arb_entry(opp, db, session, open_keys, simulate: bool,
                      contracts: int, cache) -> str:
    """
    Record an arb entry with zero IO.

    Only touches in-memory state: creates the ORM object (db.add — no disk
    write until commit), updates session bankroll and open_keys.

    Returns a log message string.  The caller is responsible for calling
    _log() and db.commit() after the hot path completes.
    """
    from src.arb_stream.models import ArbStreamPosition

    if not simulate:
        raise RuntimeError("live arb-stream not yet enabled — run with --simulate")

    detected_at = datetime.now(timezone.utc)
    detection_cost = opp.total_cost_cents * contracts

    legs_json = []
    entry_cost = 0.0

    for leg in opp.legs:
        if leg.side == "yes":
            ws_ask = cache.get_yes_ask(leg.ticker)
            fresh = ws_ask is not None and cache.yes_ask_age(leg.ticker) < 10
        else:
            ws_ask = cache.get_no_ask(leg.ticker)
            fresh = ws_ask is not None and cache.no_ask_age(leg.ticker) < 10
        entry_price = int(round(ws_ask)) if fresh else leg.price_cents

        entry_ts = datetime.now(timezone.utc)
        latency_ms = int((entry_ts - detected_at).total_seconds() * 1000)

        entry_cost += entry_price * contracts
        legs_json.append({
            "ticker": leg.ticker,
            "side": leg.side,
            "detection_price_cents": leg.price_cents,
            "entry_price_cents": entry_price,
            "latency_ms": latency_ms,
            "count": contracts,
        })

    profit_cents = 100 * contracts - entry_cost
    slippage = entry_cost - detection_cost

    pos = ArbStreamPosition(
        session_id=session.id,
        detected_at=detected_at,
        arb_type=opp.arb_type,
        event_ticker=opp.event_ticker,
        cost_cents=entry_cost,
        detection_cost_cents=detection_cost,
    )
    pos.legs = legs_json
    db.add(pos)  # queued — no disk write until db.commit()

    session.current_bankroll_usd -= entry_cost / 100.0
    open_keys.add(_arb_key(opp))

    return (
        f"  [ARB-STREAM] ENTER {opp.arb_type} {opp.event_ticker}"
        f"  detect={detection_cost:.1f}¢  entry={entry_cost:.1f}¢"
        f"  slippage={slippage:+.1f}¢  profit≈{profit_cents:.1f}¢"
        f"  legs={len(opp.legs)}"
    )


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def _settle_open_positions(db, session_id: int, fetcher, log_file) -> int:
    """Poll Kalshi for each open ArbStreamPosition and settle resolved ones."""
    from src.arb_stream.models import ArbStreamPosition, ArbStreamSession

    open_positions = (
        db.query(ArbStreamPosition)
        .filter(
            ArbStreamPosition.session_id == session_id,
            ArbStreamPosition.status == "open",
        )
        .all()
    )

    settled_count = 0
    now = datetime.now(timezone.utc)
    session = db.get(ArbStreamSession, session_id)

    for pos in open_positions:
        legs = pos.legs
        if not legs:
            continue

        results: list[tuple[dict, str]] = []
        all_finalized = True

        for leg in legs:
            try:
                mkt = fetcher.get_market_status(leg["ticker"])
            except Exception as exc:
                _log(log_file, f"  [ERR] fetch {leg['ticker']}: {exc}")
                all_finalized = False
                break

            status = mkt.get("status", "")
            result = mkt.get("result") or ""
            if status not in ("finalized", "settled", "closed") or result not in ("yes", "no"):
                all_finalized = False
                break
            results.append((leg, result))

        if not all_finalized:
            continue

        # Determine outcome (uniform across binary and series):
        # Exactly one leg will have resolved to match our side → pays 100¢ * count.
        # All others resolve the other way → we lose those stakes.
        # net pnl = 100 * count - total_cost_cents
        contracts = legs[0]["count"] if legs else 1
        all_voided = all(r is None for _, r in results)

        if all_voided:
            outcome = "voided"
            pnl = 0.0
        elif any(result == leg["side"] for leg, result in results):
            outcome = "won"
            pnl = 100.0 * contracts - pos.cost_cents
        else:
            outcome = "lost"
            pnl = -pos.cost_cents

        pos.status = outcome
        pos.pnl_cents = round(pnl, 2)
        pos.settled_at = now

        if outcome == "won":
            session.current_bankroll_usd += (pos.cost_cents + pnl) / 100.0
        elif outcome == "voided":
            session.current_bankroll_usd += pos.cost_cents / 100.0

        settled_count += 1
        _log(log_file,
             f"  [ARB-STREAM] SETTLE {outcome.upper()} {pos.event_ticker}"
             f"  P&L={pnl:+.1f}¢  bankroll=${session.current_bankroll_usd:.4f}")

    return settled_count


# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------

def _make_session():
    """Create a SQLAlchemy session that includes the arb_stream tables."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from config.settings import settings
    from src.arb_stream.models import ArbStreamBase

    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
    )
    ArbStreamBase.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return SessionLocal()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_arb_stream_simulation(
    initial_bankroll_usd: float,
    simulate: bool,
    categories: list[str],
    near_term_minutes: int,
    contracts_per_arb: int,
    min_profit_cents: int,
    refresh_interval_seconds: int,
    settle_interval_seconds: int,
    logs_dir: str,
    resume_session_id: int | None,
) -> None:
    """
    Streaming arb engine main loop.

    Event-driven via WebSocket: scans for arbs immediately on each WS price
    update rather than on a fixed REST poll interval.  REST is used only to
    refresh the market list every *refresh_interval_seconds*.

    In simulate mode every entry is recorded as an ArbStreamPosition with
    detection_price_cents, entry_price_cents, and latency_ms per leg so that
    real slippage can be measured before enabling live mode.
    """
    global _RUNNING
    _RUNNING = True
    signal.signal(signal.SIGINT, _handle_sigint)

    if not simulate:
        raise RuntimeError(
            "--live mode is not yet enabled for arb-stream.  "
            "Run with --simulate until slippage data validates the strategy."
        )

    from src.arb_stream.models import ArbStreamSession
    from src.engine.arbitrage import scan_binary_arb, scan_series_arb
    from src.fetchers.kalshi import KalshiFetcher
    from src.streaming.kalshi_ws import KalshiWsClient
    from src.streaming.price_cache import PriceCache

    db = _make_session()

    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = str(Path(logs_dir) / f"arb_stream_{ts_str}.log")

    # --- Session setup -------------------------------------------------------
    if resume_session_id is not None:
        session = db.get(ArbStreamSession, resume_session_id)
        if session is None:
            raise ValueError(f"ArbStreamSession {resume_session_id} not found")
    else:
        session = ArbStreamSession(
            bankroll_usd=initial_bankroll_usd,
            current_bankroll_usd=initial_bankroll_usd,
            simulate=1 if simulate else 0,
        )
        db.add(session)
        db.commit()

    session_id = session.id

    # --- Fetcher + streaming -------------------------------------------------
    try:
        fetcher = KalshiFetcher()
    except Exception as exc:
        raise RuntimeError(f"Cannot init Kalshi fetcher: {exc}")

    cache = PriceCache()
    kalshi_ws = KalshiWsClient(cache)
    try:
        kalshi_ws.start()
    except Exception as exc:
        raise RuntimeError(f"Cannot start KalshiWsClient: {exc}")

    # Deduplication: skip arbs that are already open in this session
    open_keys: set[str] = set()
    subscribed_tickers: set[str] = set()
    markets: list = []
    last_rest = 0.0
    rest_scan_count = 0   # for periodic cache dumps

    with open(log_path, "a", encoding="utf-8") as log_file:
        _log(log_file, "=" * 65)
        _log(log_file,
             f"  ARB-STREAM  session={session_id}  {'SIM' if simulate else 'LIVE'}"
             f"  bankroll=${initial_bankroll_usd:.2f}"
             f"  contracts={contracts_per_arb}  min_profit={min_profit_cents}¢")
        _log(log_file,
             f"  categories={categories}  near_term={near_term_minutes}min"
             f"  refresh={refresh_interval_seconds}s  settle={settle_interval_seconds}s")
        _log(log_file, f"  log: {log_path}")
        _log(log_file, "=" * 65)

        try:
            while _RUNNING:
                # ============================================================
                # WAIT — block until a WS price update or timeout
                # ============================================================
                cache.update_event.wait(timeout=settle_interval_seconds)
                cache.update_event.clear()

                if not _RUNNING:
                    break

                # ============================================================
                # HOT PATH — zero IO between price update and entry
                # Patch WS prices, scan, record all qualifying arbs in memory.
                # No logging, no db.commit(), no REST calls here.
                # ============================================================
                entry_msgs: list[str] = []

                if markets:
                    patched = [_apply_ws_prices(m, cache) for m in markets]
                    all_opps = (
                        scan_series_arb(patched, min_profit_cents=-9999) +
                        scan_binary_arb(patched, min_profit_cents=-9999)
                    )

                    for opp in all_opps:
                        if not opp.guaranteed:
                            continue
                        if _arb_key(opp) in open_keys:
                            continue
                        if opp.profit_cents < min_profit_cents:
                            continue
                        if opp.total_cost_cents * contracts_per_arb / 100.0 > session.current_bankroll_usd:
                            continue

                        msg = _record_arb_entry(
                            opp, db, session, open_keys, simulate,
                            contracts_per_arb, cache,
                        )
                        entry_msgs.append(msg)
                else:
                    all_opps = []

                # ============================================================
                # COLD PATH — all IO happens here, after entries are recorded
                # ============================================================

                # 1. Log + commit entries
                for msg in entry_msgs:
                    _log(log_file, msg)
                if entry_msgs:
                    db.commit()

                # 2. REST rescan if due
                now_ts = time.time()
                if now_ts - last_rest >= refresh_interval_seconds:
                    last_rest = now_ts
                    try:
                        markets = _fetch_near_term_markets(
                            fetcher, categories, near_term_minutes
                        )
                    except Exception as exc:
                        _log(log_file, f"  [ARB-STREAM] market fetch error: {exc}")
                        markets = []

                    new_tickers = {m.id for m in markets}
                    to_sub = new_tickers - subscribed_tickers
                    to_unsub = subscribed_tickers - new_tickers
                    if to_sub:
                        kalshi_ws.subscribe(to_sub)
                        subscribed_tickers |= to_sub
                    if to_unsub:
                        kalshi_ws.unsubscribe(to_unsub)
                        subscribed_tickers -= to_unsub

                    rest_scan_count += 1
                    _log(log_file,
                         f"  [ARB-STREAM] REST scan: {len(markets)} near-term markets"
                         f"  +{len(to_sub)}/-{len(to_unsub)} WS tickers")

                    if rest_scan_count % 5 == 1 and subscribed_tickers:
                        _log_cache_sample(log_file, cache, subscribed_tickers)

                # 3. Diagnostic logging (file only, no stdout)
                if markets:
                    cov = _ws_coverage(markets, cache)
                    _log(log_file,
                         f"  [WS] coverage: {cov['both']} both / {cov['yes_only']} yes-only /"
                         f" {cov['no_only']} no-only / {cov['rest_only']} REST-only"
                         f" (of {len(markets)} markets)",
                         also_print=False)
                    _log_scan_results(log_file, all_opps, open_keys,
                                      min_profit_cents, contracts_per_arb,
                                      session.current_bankroll_usd)

                # 4. Settle open positions
                settled = _settle_open_positions(db, session_id, fetcher, log_file)
                if settled:
                    db.commit()

        finally:
            kalshi_ws.stop()
            db.refresh(session)
            session.ended_at = datetime.now(timezone.utc)
            db.commit()

            _log(log_file, "")
            _log(log_file, "=" * 65)
            _log(log_file, f"  ARB-STREAM session={session_id} STOPPED")
            _log(log_file, f"  Initial bankroll : ${initial_bankroll_usd:.4f}")
            _log(log_file, f"  Final bankroll   : ${session.current_bankroll_usd:.4f}")
            pnl = session.current_bankroll_usd - initial_bankroll_usd
            _log(log_file, f"  Unrealised P&L   : ${pnl:+.4f}")
            _log(log_file, f"  Log              : {log_path}")
            _log(log_file, "=" * 65)
