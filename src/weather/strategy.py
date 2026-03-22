"""Weather strategy loop — scan NWS-edge markets, enter positions, settle.

Continuously scans Kalshi weather markets for NWS forecast edge,
enters positions when edge exceeds a threshold, and settles them
when the market resolves.  Supports both paper-trade (simulated)
and live order placement.

Usage (via CLI or direct call)::

    run_weather_strategy(live=False, bankroll_cents=500)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import IO, Any

from src.fetchers.kalshi import KalshiFetcher
from src.storage.db import get_session
from src.storage.models import SimPosition, SimSession
from src.weather.scanner import scan_weather_markets


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(f: IO[str], msg: str) -> None:
    """Write a timestamped line to both stdout and the log file.

    Args:
        f: Open log file handle.
        msg: Message to log.
    """
    line = f"[{datetime.utcnow().strftime('%H:%M:%S')}] {msg}"
    print(line)
    f.write(line + "\n")
    f.flush()


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def _settle_positions(
    open_positions: list[SimPosition],
    fetcher: KalshiFetcher,
    session: SimSession,
    db: Any,
    log_file: IO[str],
) -> list[SimPosition]:
    """Settle resolved positions and return those still open.

    For each open position, polls the Kalshi market status.  If the
    market has resolved to ``"yes"`` or ``"no"``, the position is
    settled and the session bankroll / win-loss counters are updated.

    Args:
        open_positions: List of ``SimPosition`` objects with
            ``status == "open"``.
        fetcher: Authenticated ``KalshiFetcher`` for market lookups.
        session: The parent ``SimSession`` whose bankroll to update.
        db: SQLAlchemy session for persistence.
        log_file: Open log file handle.

    Returns:
        A list of positions that remain open (unresolved).
    """
    still_open: list[SimPosition] = []
    now = datetime.now(timezone.utc)

    for pos in open_positions:
        try:
            mkt = fetcher.get_market_status(pos.ticker)
        except Exception as exc:
            _log(log_file, f"  [ERR] fetch {pos.ticker}: {exc}")
            still_open.append(pos)
            continue

        result = mkt.get("result") or ""
        if result not in ("yes", "no"):
            still_open.append(pos)
            continue

        # Determine outcome based on position side vs market result.
        if result == "yes":
            won = pos.side == "yes"
        else:
            won = pos.side == "no"

        if won:
            pnl_cents = (
                (100 - pos.entry_price_cents) * pos.contracts
            )
            pos.status = "won"
            pos.pnl_cents = pnl_cents
            session.current_bankroll_cents += pos.cost_cents + pnl_cents
            session.won += 1
        else:
            pnl_cents = -pos.cost_cents
            pos.status = "lost"
            pos.pnl_cents = pnl_cents
            session.lost += 1

        pos.result = result
        pos.settled_at = now

        label = "WON " if won else "LOST"
        _log(
            log_file,
            f"  SETTLE {label} {pos.ticker} ({pos.side.upper()}) "
            f"| P&L={pnl_cents:+.0f}c  "
            f"bankroll=${session.current_bankroll_cents / 100:.4f}",
        )
        db.commit()

    return still_open


# ---------------------------------------------------------------------------
# Position entry
# ---------------------------------------------------------------------------

def _enter_position(
    opp: dict[str, Any],
    session: SimSession,
    fetcher: KalshiFetcher,
    live: bool,
    db: Any,
    log_file: IO[str],
) -> None:
    """Open a new weather position from a scanner opportunity.

    Calculates contract count (2.5 % of bankroll per position),
    creates a ``SimPosition``, deducts cost from the session
    bankroll, and optionally places a real Kalshi order.

    Args:
        opp: Opportunity dict from ``scan_weather_markets`` with
            keys ``market``, ``side``, ``ask_cents``.
        session: Active ``SimSession``.
        fetcher: Authenticated ``KalshiFetcher`` for live orders.
        live: If ``True``, place a real Kalshi order.
        db: SQLAlchemy session.
        log_file: Open log file handle.
    """
    ask_cents: int = opp["ask_cents"]
    if ask_cents <= 0:
        _log(log_file, f"  SKIP {opp['market'].id}: ask_cents={ask_cents}")
        return

    contracts = max(
        1,
        int(session.current_bankroll_cents * 0.025 / ask_cents),
    )
    cost_cents = contracts * ask_cents

    if cost_cents > session.current_bankroll_cents:
        _log(
            log_file,
            f"  SKIP {opp['market'].id}: cost={cost_cents}c "
            f"> bankroll={session.current_bankroll_cents:.0f}c",
        )
        return

    ticker = opp["market"].id
    side = opp["side"]

    position = SimPosition(
        session_id=session.id,
        ticker=ticker,
        side=side,
        entry_price_cents=ask_cents,
        cost_cents=cost_cents,
        contracts=contracts,
        arb_type="weather",
        status="open",
        live=int(live),
    )
    db.add(position)

    session.current_bankroll_cents -= cost_cents
    session.total_trades += 1
    db.commit()

    # Place real order if running live.
    if live:
        try:
            order = fetcher.place_order(
                ticker=ticker,
                side=side,
                price_cents=ask_cents,
                count=contracts,
            )
            order_id = order.get("order_id") or order.get("id", "")
            filled = order.get("fill_count") or order.get(
                "filled_count", 0
            )
            status = order.get("status", "")

            if status in ("executed", "filled") and not filled:
                filled = contracts

            if filled > 0:
                position.order_ids = json.dumps([order_id])
                _log(
                    log_file,
                    f"  [LIVE] filled {ticker} ({side}) "
                    f"x{filled}/{contracts} @ {ask_cents}c "
                    f"order={order_id}",
                )
            else:
                _log(
                    log_file,
                    f"  [LIVE] no fill {ticker} ({side}) "
                    f"x{contracts} @ {ask_cents}c — reverting",
                )
                # Revert bankroll and mark position voided.
                session.current_bankroll_cents += cost_cents
                session.total_trades -= 1
                position.status = "voided"
                session.voided += 1
                db.commit()
                return

            db.commit()
        except Exception as exc:
            _log(
                log_file,
                f"  [LIVE] order FAILED {ticker} ({side}): {exc} "
                f"— reverting",
            )
            session.current_bankroll_cents += cost_cents
            session.total_trades -= 1
            position.status = "voided"
            session.voided += 1
            db.commit()
            return

    _log(
        log_file,
        f"  ENTER {ticker} side={side} ask={ask_cents}c "
        f"x{contracts} cost={cost_cents}c "
        f"bankroll=${session.current_bankroll_cents / 100:.4f}",
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_weather_strategy(
    live: bool,
    bankroll_cents: float,
    interval_seconds: int = 300,
    min_edge: float = 0.05,
    session_id: int | None = None,
) -> None:
    """Run the weather-edge strategy in a continuous loop.

    Scans Kalshi weather markets for NWS forecast edge, enters
    positions when edge exceeds ``min_edge``, and settles them
    when the underlying market resolves.

    Args:
        live: If ``True``, place real Kalshi orders; otherwise
            paper-trade only.
        bankroll_cents: Starting bankroll in cents (ignored when
            resuming via ``session_id``).
        interval_seconds: Seconds between market scans.  Settlement
            checks run every 30 s regardless.
        min_edge: Minimum absolute probability edge to enter a
            position.
        session_id: If provided, resume an existing ``SimSession``
            instead of creating a new one.

    Raises:
        ValueError: If ``session_id`` refers to a session that is
            not in ``"running"`` status.
    """
    db = get_session()
    fetcher = KalshiFetcher()

    # ---- Session setup ---------------------------------------------------
    if session_id is not None:
        session = db.get(SimSession, session_id)
        if session is None:
            raise ValueError(
                f"SimSession {session_id} not found"
            )
        if session.status != "running":
            raise ValueError(
                f"SimSession {session_id} status is "
                f"'{session.status}', expected 'running'"
            )
        log_path = session.log_path
    else:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        log_path = f"logs/weather_{timestamp}.log"
        os.makedirs("logs", exist_ok=True)

        session = SimSession(
            initial_bankroll_cents=bankroll_cents,
            current_bankroll_cents=bankroll_cents,
            status="running",
            log_path=log_path,
        )
        db.add(session)
        db.commit()

    # Load any already-open positions from a resumed session.
    open_positions: list[SimPosition] = (
        db.query(SimPosition)
        .filter(
            SimPosition.session_id == session.id,
            SimPosition.status == "open",
        )
        .all()
    )
    entered_tickers: set[str] = {
        p.ticker for p in open_positions
    }

    last_scan_at: float = 0.0
    mode_label = "LIVE" if live else "SIM"

    log_file = open(log_path, "a")
    try:
        _log(
            log_file,
            f"Weather strategy started [{mode_label}] "
            f"session={session.id} "
            f"bankroll=${session.current_bankroll_cents / 100:.2f} "
            f"interval={interval_seconds}s "
            f"min_edge={min_edge}",
        )

        while True:
            # -- Settle resolved positions ---------------------------------
            open_positions = _settle_positions(
                open_positions, fetcher, session, db, log_file,
            )

            # -- Scan for new opportunities --------------------------------
            now = time.time()
            if now - last_scan_at >= interval_seconds:
                _log(log_file, "Scanning weather markets...")
                try:
                    opportunities = scan_weather_markets(
                        fetcher, min_edge,
                    )
                except Exception as exc:
                    _log(
                        log_file,
                        f"  [ERR] scan failed: {exc}",
                    )
                    opportunities = []

                for opp in opportunities:
                    market_id = opp["market"].id
                    if market_id not in entered_tickers:
                        _enter_position(
                            opp, session, fetcher, live,
                            db, log_file,
                        )
                        entered_tickers.add(market_id)
                        # Refresh open positions list after entry.
                        new_pos = (
                            db.query(SimPosition)
                            .filter(
                                SimPosition.session_id
                                == session.id,
                                SimPosition.ticker == market_id,
                                SimPosition.status == "open",
                            )
                            .first()
                        )
                        if new_pos is not None:
                            open_positions.append(new_pos)

                last_scan_at = now
                _log(
                    log_file,
                    f"  Open positions: {len(open_positions)} | "
                    f"Bankroll: "
                    f"${session.current_bankroll_cents / 100:.4f}",
                )

            time.sleep(30)

    except KeyboardInterrupt:
        _log(log_file, "Shutting down (KeyboardInterrupt)...")
    finally:
        session.status = "stopped"
        session.stopped_at = datetime.utcnow()
        db.commit()

        total_pnl = (
            session.current_bankroll_cents
            - session.initial_bankroll_cents
        )
        _log(
            log_file,
            f"Session {session.id} stopped. "
            f"Bankroll: ${session.current_bankroll_cents / 100:.4f} "
            f"| P&L: {total_pnl:+.0f}c "
            f"| Trades: {session.total_trades} "
            f"| W={session.won} L={session.lost} "
            f"V={session.voided}",
        )
        log_file.close()
