"""Click CLI entry point for the Betting App (Kalshi edition)."""
from __future__ import annotations
import click
from datetime import datetime, timezone, timedelta


@click.group()
def cli():
    """Kalshi Betting App — AI-powered and statistical prediction-market recommendations."""
    pass


# ---------------------------------------------------------------------------
# run — fetch Kalshi markets + generate recommendations
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--mode", type=click.Choice(["agent", "compute"]), default="compute", show_default=True)
@click.option("--period", type=click.Choice(["week", "month"]), default="week", show_default=True)
@click.option("--categories", default=None,
              help="Comma-separated Kalshi categories to include, e.g. politics,crypto.")
@click.option("--min-ev", default=None, type=float,
              help="Override minimum EV threshold (default from settings).")
@click.option("--quiet", is_flag=True, help="Suppress verbose output.")
def run(mode: str, period: str, categories: str | None, min_ev: float | None, quiet: bool):
    """Fetch Kalshi markets and generate bet recommendations."""
    from src.engine.pipeline import run as pipeline_run
    from config.settings import settings

    effective_min_ev = min_ev if min_ev is not None else settings.MIN_EV_THRESHOLD

    # Pass category filter through via env override on the fetcher
    if categories:
        import os
        os.environ["KALSHI_CATEGORIES"] = categories

    recs = pipeline_run(
        mode=mode,
        period=period,
        sources=["kalshi"],
        verbose=not quiet,
        min_ev=effective_min_ev,
    )
    if recs:
        click.echo(f"\nStored {len(recs)} recommendations.")
        for rec in recs[:5]:
            click.echo(
                f"  [{rec.id}] {rec.event_name} — {rec.selection} @ {rec.odds:.2f}"
                f"  (conf={rec.confidence:.0%})"
            )
        if len(recs) > 5:
            click.echo(f"  ... and {len(recs) - 5} more. Use 'recommendations list' to see all.")
    else:
        click.echo("No recommendations generated.")


# ---------------------------------------------------------------------------
# simulate — paper-trade, auto-settle, report
# ---------------------------------------------------------------------------

@cli.group()
def simulate():
    """Paper-trade Kalshi markets for performance tracking and training."""
    pass


@simulate.command("run")
@click.option("--min-ev", default=None, type=float,
              help="Minimum EV threshold. Defaults to MIN_EV_THRESHOLD in .env.")
@click.option("--categories", default=None,
              help="Comma-separated Kalshi categories, e.g. politics,crypto.")
@click.option("--quiet", is_flag=True)
def simulate_run(min_ev: float | None, categories: str | None, quiet: bool):
    """Fetch open Kalshi markets and store paper-trade bets for every positive-EV opportunity."""
    from src.storage.db import get_session
    from src.engine.simulator import run_simulation
    from config.settings import settings

    effective_min_ev = min_ev if min_ev is not None else settings.MIN_EV_THRESHOLD
    cat_list = [c.strip() for c in categories.split(",")] if categories else None

    session = get_session()
    bets = run_simulation(session, min_ev=effective_min_ev, categories=cat_list, verbose=not quiet)
    if bets:
        click.echo(f"\nCreated {len(bets)} simulated bets.")
        for b in bets[:5]:
            click.echo(
                f"  [{b.id}] {b.ticker} ({b.side.upper()}) @ {b.entry_price_cents:.1f}¢"
                f"  EV={b.ev:.2%}  stake={b.stake_units:.2f}u"
            )
        if len(bets) > 5:
            click.echo(f"  ... and {len(bets) - 5} more.")
    else:
        click.echo("No simulated bets created (no positive-EV markets found).")


@simulate.command("settle")
@click.option("--quiet", is_flag=True)
def simulate_settle(quiet: bool):
    """Poll Kalshi and auto-settle any resolved simulated bets."""
    from src.storage.db import get_session
    from src.engine.simulator import settle_open_bets

    session = get_session()
    tally = settle_open_bets(session, verbose=not quiet)
    click.echo(
        f"Settle complete — settled={tally['settled']}"
        f"  still_open={tally['still_open']}"
        f"  errors={tally['errors']}"
    )


@simulate.command("list")
@click.option("--status", default=None, type=click.Choice(["open", "settled", "expired"]))
@click.option("--limit", default=30, show_default=True)
def simulate_list(status: str | None, limit: int):
    """List simulated bets."""
    from src.storage.db import get_session
    from src.storage.models import SimulatedBet

    session = get_session()
    q = session.query(SimulatedBet).order_by(SimulatedBet.created_at.desc())
    if status:
        q = q.filter(SimulatedBet.status == status)
    bets = q.limit(limit).all()

    if not bets:
        click.echo("No simulated bets found.")
        return

    click.echo(
        f"{'ID':>4}  {'Ticker':<35}  {'Side':>4}  "
        f"{'Entry¢':>7}  {'EV':>6}  {'Closes':>11}  {'Result':>7}  {'P&L':>7}"
    )
    click.echo("-" * 100)
    for b in bets:
        closes_str = b.closes_at.strftime("%Y-%m-%d") if b.closes_at else "—"
        result_str = b.result or b.status
        pnl_str = f"{b.pnl_units:+.4f}" if b.pnl_units is not None else "—"
        click.echo(
            f"{b.id:>4}  {b.ticker[:35]:<35}  {b.side.upper():>4}  "
            f"{b.entry_price_cents:>7.1f}  {b.ev:>6.2%}  "
            f"{closes_str:>11}  {result_str:>7}  {pnl_str:>7}"
        )


@cli.command("live")
@click.option("--simulate", "mode", flag_value="simulate",
              help="Paper trade only — no real orders placed.")
@click.option("--live", "mode", flag_value="live",
              help="Place real orders on Kalshi.")
@click.option("--bankroll", default=None, type=float,
              help="Starting bankroll in USD. Defaults to Kalshi account balance for --live, $5.00 for --simulate.")
@click.option("--interval", default=60, type=int, show_default=True,
              help="Seconds between full market scans.")
@click.option("--settle-interval", default=5, type=int, show_default=True,
              help="Seconds between settlement polls while waiting for a scan.")
@click.option("--categories", default="Crypto,Economics,Financials",
              show_default=True, help="Comma-separated Kalshi categories to trade.")
@click.option("--near-term", default=60, type=int, show_default=True,
              help="Only trade events closing within this many minutes from now.")
@click.option("--min-arb-profit", default=1.0, type=float, show_default=True,
              help="Minimum arb profit in cents to enter.")
@click.option("--max-position", default=0.10, type=float, show_default=True,
              help="Max fraction of bankroll per single position.")
@click.option("--max-deploy", default=0.80, type=float, show_default=True,
              help="Max fraction of liquid bankroll to deploy per cycle (keeps a reserve).")
@click.option("--logs-dir", default="logs", show_default=True,
              help="Directory for log files.")
@click.option("--resume", default=None, type=int,
              help="Resume an existing session by ID.")
def live_cmd(
    mode: str | None,
    bankroll: float,
    interval: int,
    settle_interval: int,
    categories: str,
    near_term: int,
    min_arb_profit: float,
    max_position: float,
    max_deploy: float,
    logs_dir: str,
    resume: int | None,
):
    """Run the arb scanner. Use --simulate for paper trading or --live for real orders."""
    if mode is None:
        raise click.UsageError("Specify --simulate (paper trade) or --live (real orders).")

    from src.storage.db import get_session
    from src.engine.live_sim import run_live_simulation

    use_live_orders = mode == "live"
    cat_list = [c.strip() for c in categories.split(",")]
    db = get_session()

    if bankroll is None:
        if use_live_orders:
            from src.fetchers.kalshi import KalshiFetcher
            try:
                balance_cents = KalshiFetcher().get_balance()
                bankroll = balance_cents / 100
                click.echo(f"Detected Kalshi balance: ${bankroll:.2f}")
            except Exception as exc:
                raise click.UsageError(f"Could not fetch Kalshi balance: {exc}. Pass --bankroll manually.")
        else:
            bankroll = 5.00

    if use_live_orders:
        click.echo("WARNING: --live mode enabled. Real orders will be placed on Kalshi.")
        click.confirm("Continue?", abort=True)

    try:
        run_live_simulation(
            db=db,
            initial_bankroll_usd=bankroll,
            interval_seconds=interval,
            settle_interval_seconds=settle_interval,
            categories=cat_list,
            near_term_minutes=near_term,
            min_arb_profit_cents=min_arb_profit,
            max_position_pct=max_position,
            max_deploy_pct=max_deploy,
            logs_dir=logs_dir,
            resume_session_id=resume,
            use_live_orders=use_live_orders,
        )
    except RuntimeError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@simulate.command("sessions")
@click.option("--limit", default=10, show_default=True)
def simulate_sessions(limit: int):
    """List live simulation sessions."""
    from src.storage.db import get_session
    from src.storage.models import SimSession

    db = get_session()
    sessions = db.query(SimSession).order_by(SimSession.created_at.desc()).limit(limit).all()
    if not sessions:
        click.echo("No sessions found.")
        return

    click.echo(f"{'ID':>4}  {'Started':>19}  {'Status':>8}  {'Initial$':>9}  {'Liquid$':>8}  {'Trades':>6}  {'W/L/V':>9}  Log")
    click.echo("-" * 110)
    for s in sessions:
        started = s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else "—"
        click.echo(
            f"{s.id:>4}  {started:>19}  {s.status:>8}  "
            f"{s.initial_bankroll_cents/100:>9.2f}  {s.current_bankroll_cents/100:>8.4f}  "
            f"{s.total_trades:>6}  {s.won}/{s.lost}/{s.voided}  {s.log_path}"
        )


@simulate.command("report")
def simulate_report():
    """Show aggregate performance stats for all simulated bets."""
    from src.storage.db import get_session
    from src.engine.simulator import simulation_report, print_simulation_report

    session = get_session()
    stats = simulation_report(session)
    print_simulation_report(stats)


# ---------------------------------------------------------------------------
# evaluate — historical performance of stored recommendations
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--from", "from_date", default=None, help="Start date (YYYY-MM-DD). Defaults to 30 days ago.")
@click.option("--to", "to_date", default=None, help="End date (YYYY-MM-DD). Defaults to today.")
def evaluate(from_date: str | None, to_date: str | None):
    """Evaluate historical recommendation performance."""
    from src.storage.db import get_session
    from src.evaluator.performance import evaluate as eval_fn, print_report

    now = datetime.now(timezone.utc)
    period_end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if to_date else now
    period_start = (
        datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if from_date else now - timedelta(days=30)
    )

    session = get_session()
    report = eval_fn(session, period_start, period_end)
    print_report(report)


# ---------------------------------------------------------------------------
# recommendations — manage stored recommendations
# ---------------------------------------------------------------------------

@cli.group()
def recommendations():
    """Manage recommendations."""
    pass


@recommendations.command("list")
@click.option("--limit", default=20, show_default=True)
@click.option("--status", default=None, type=click.Choice(["pending", "settled"]))
@click.option("--mode", default=None, type=click.Choice(["agent", "compute"]))
def list_recs(limit: int, status: str | None, mode: str | None):
    """List stored recommendations."""
    from src.storage.db import get_session
    from src.storage.models import Recommendation

    session = get_session()
    q = session.query(Recommendation).order_by(Recommendation.created_at.desc())
    if status:
        q = q.filter(Recommendation.status == status)
    if mode:
        q = q.filter(Recommendation.mode == mode)
    recs = q.limit(limit).all()

    if not recs:
        click.echo("No recommendations found.")
        return

    click.echo(f"{'ID':>4}  {'Date':>10}  {'Mode':>7}  {'Event':<35}  {'Selection':<20}  {'Odds':>6}  {'Conf':>5}  {'Status':>8}")
    click.echo("-" * 105)
    for rec in recs:
        date_str = rec.created_at.strftime("%Y-%m-%d") if rec.created_at else "—"
        click.echo(
            f"{rec.id:>4}  {date_str:>10}  {rec.mode:>7}  {rec.event_name[:35]:<35}  "
            f"{rec.selection[:20]:<20}  {rec.odds:>6.2f}  {rec.confidence:>5.0%}  {rec.status:>8}"
        )


@recommendations.command("show")
@click.argument("rec_id", type=int)
def show_rec(rec_id: int):
    """Show full details of a recommendation."""
    from src.storage.db import get_session
    from src.storage.models import Recommendation

    session = get_session()
    rec = session.get(Recommendation, rec_id)
    if rec is None:
        click.echo(f"Recommendation {rec_id} not found.", err=True)
        raise SystemExit(1)

    click.echo(f"ID:         {rec.id}")
    click.echo(f"Created:    {rec.created_at}")
    click.echo(f"Mode:       {rec.mode}")
    click.echo(f"Period:     {rec.period}")
    click.echo(f"Source:     {rec.source}")
    click.echo(f"Category:   {rec.category}")
    click.echo(f"Event:      {rec.event_name}")
    click.echo(f"Selection:  {rec.selection}")
    click.echo(f"Odds:       {rec.odds:.3f}")
    click.echo(f"Stake:      {rec.stake_units} units")
    click.echo(f"Confidence: {rec.confidence:.0%}")
    click.echo(f"Status:     {rec.status}")
    click.echo(f"Rationale:\n  {rec.rationale}")
    if rec.outcome:
        click.echo(f"Outcome:    {rec.outcome.result} (settled {rec.outcome.settled_at})")


@recommendations.command("settle")
@click.argument("rec_id", type=int)
@click.option("--result", type=click.Choice(["win", "loss", "void"]), required=True)
@click.option("--actual-odds", default=None, type=float, help="Actual closing/settlement odds.")
def settle_rec(rec_id: int, result: str, actual_odds: float | None):
    """Manually settle a recommendation with its actual result."""
    from src.storage.db import get_session
    from src.storage.models import Recommendation, Outcome

    session = get_session()
    rec = session.get(Recommendation, rec_id)
    if rec is None:
        click.echo(f"Recommendation {rec_id} not found.", err=True)
        raise SystemExit(1)

    if rec.outcome:
        click.echo(f"Recommendation {rec_id} is already settled as '{rec.outcome.result}'.", err=True)
        raise SystemExit(1)

    outcome = Outcome(recommendation_id=rec_id, result=result, actual_odds=actual_odds)
    rec.status = "settled"
    session.add(outcome)
    session.commit()
    click.echo(f"Recommendation {rec_id} settled as '{result}'.")


# ---------------------------------------------------------------------------
# arb — scan and simulate micro-arbitrage opportunities on Kalshi
# ---------------------------------------------------------------------------

@cli.group()
def arb():
    """Scan and simulate micro-arbitrage opportunities on Kalshi."""
    pass


@arb.command("scan")
@click.option("--categories", default="Crypto,Economics,Financials,Companies",
              show_default=True, help="Comma-separated Kalshi categories to scan.")
@click.option("--min-profit", default=1.0, type=float, show_default=True,
              help="Minimum profit in cents per contract to report.")
@click.option("--type", "arb_type", default="all",
              type=click.Choice(["all", "binary", "series"]), show_default=True)
def arb_scan(categories: str, min_profit: float, arb_type: str):
    """Scan live Kalshi markets for arbitrage opportunities."""
    from src.fetchers.kalshi import KalshiFetcher
    from src.engine.arbitrage import scan_binary_arb, scan_series_arb
    from config.settings import settings

    cat_list = [c.strip() for c in categories.split(",")]
    try:
        fetcher = KalshiFetcher()
        fetcher.category_filter = cat_list
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"Fetching markets for: {', '.join(cat_list)}...")
    markets = fetcher.get_markets()
    click.echo(f"Fetched {len(markets)} markets.")

    all_opps = []
    if arb_type in ("all", "binary"):
        binary = scan_binary_arb(markets, min_profit_cents=min_profit)
        all_opps.extend(binary)
        click.echo(f"\nBinary arbs (guaranteed): {len(binary)}")
        for o in binary[:20]:
            legs_str = "  +  ".join(f"{l.ticker}({l.side}@{l.price_cents}¢)" for l in o.legs)
            click.echo(f"  profit={o.profit_cents:.0f}¢ ({o.profit_pct:.2%})  {legs_str}")

    if arb_type in ("all", "series"):
        series = scan_series_arb(markets, min_profit_cents=min_profit)
        click.echo(f"\nSeries arbs (mutually exclusive legs): {len(series)}")
        for o in series[:20]:
            g = "GUARANTEED" if o.guaranteed else "not exhaustive"
            click.echo(
                f"  {o.event_ticker:<40} legs={len(o.legs):>2}  "
                f"cost={o.total_cost_cents:.0f}¢  profit={o.profit_cents:.0f}¢ ({o.profit_pct:.2%})  [{g}]"
            )


@arb.command("simulate")
@click.option("--categories", default="Crypto,Economics,Financials,Companies", show_default=True)
@click.option("--min-profit", default=1.0, type=float, show_default=True)
@click.option("--type", "arb_type", default="all",
              type=click.Choice(["all", "binary", "series"]), show_default=True)
@click.option("--quiet", is_flag=True)
def arb_simulate(categories: str, min_profit: float, arb_type: str, quiet: bool):
    """Record current arbitrage opportunities as simulated trades."""
    from src.fetchers.kalshi import KalshiFetcher
    from src.engine.arbitrage import scan_binary_arb, scan_series_arb, opportunities_to_sim
    from src.storage.db import get_session

    cat_list = [c.strip() for c in categories.split(",")]
    try:
        fetcher = KalshiFetcher()
        fetcher.category_filter = cat_list
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if not quiet:
        click.echo(f"Fetching markets...")
    markets = fetcher.get_markets()

    opps = []
    if arb_type in ("all", "binary"):
        opps.extend(scan_binary_arb(markets, min_profit_cents=min_profit))
    if arb_type in ("all", "series"):
        opps.extend(scan_series_arb(markets, min_profit_cents=min_profit))

    if not opps:
        click.echo("No arbitrage opportunities found.")
        return

    session = get_session()
    rows = opportunities_to_sim(opps)
    for row in rows:
        session.add(row)
    session.commit()

    click.echo(f"Recorded {len(rows)} arb simulations.")
    for row in rows[:8]:
        g = "guaranteed" if row.guaranteed else "unguaranteed"
        click.echo(
            f"  [{row.id}] {row.arb_type:<6} {row.event_ticker:<40} "
            f"profit={row.profit_cents:.0f}¢ ({row.profit_pct:.2%}) [{g}]"
        )
    if len(rows) > 8:
        click.echo(f"  ... and {len(rows) - 8} more.")


@arb.command("settle")
@click.option("--quiet", is_flag=True)
def arb_settle(quiet: bool):
    """Auto-settle resolved arb simulations by polling Kalshi."""
    from src.storage.db import get_session
    from src.engine.arbitrage import settle_arb_simulations

    session = get_session()
    tally = settle_arb_simulations(session, verbose=not quiet)
    click.echo(
        f"Settle complete — settled={tally['settled']}"
        f"  still_open={tally['still_open']}"
        f"  errors={tally['errors']}"
    )


@arb.command("list")
@click.option("--status", default=None, type=click.Choice(["open", "won", "lost", "voided"]))
@click.option("--limit", default=30, show_default=True)
def arb_list(status: str | None, limit: int):
    """List recorded arb simulations."""
    from src.storage.db import get_session
    from src.storage.models import ArbSimulation

    session = get_session()
    q = session.query(ArbSimulation).order_by(ArbSimulation.created_at.desc())
    if status:
        q = q.filter(ArbSimulation.status == status)
    sims = q.limit(limit).all()

    if not sims:
        click.echo("No arb simulations found.")
        return

    click.echo(
        f"{'ID':>4}  {'Type':<6}  {'Event':<38}  {'Legs':>4}  "
        f"{'Cost¢':>5}  {'Profit¢':>7}  {'ROI':>6}  {'G':>1}  {'Status':>7}  {'Closes':>10}"
    )
    click.echo("-" * 105)
    for s in sims:
        closes_str = s.closes_at.strftime("%Y-%m-%d") if s.closes_at else "—"
        g = "Y" if s.guaranteed else "N"
        click.echo(
            f"{s.id:>4}  {s.arb_type:<6}  {s.event_ticker[:38]:<38}  {len(s.legs):>4}  "
            f"{s.total_cost_cents:>5.0f}  {s.profit_cents:>7.0f}  {s.profit_pct:>6.2%}  "
            f"{g:>1}  {s.status:>7}  {closes_str:>10}"
        )


@arb.command("report")
def arb_report_cmd():
    """Show aggregate P&L for all arb simulations."""
    from src.storage.db import get_session
    from src.engine.arbitrage import arb_report, print_arb_report

    session = get_session()
    stats = arb_report(session)
    print_arb_report(stats)


if __name__ == "__main__":
    cli()
