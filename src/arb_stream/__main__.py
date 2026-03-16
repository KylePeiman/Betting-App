"""
Standalone CLI for the streaming ARB engine.

Usage:
    python -m src.arb_stream --simulate
    python -m src.arb_stream --simulate --near-term 30 --refresh-interval 60
    python -m src.arb_stream --simulate --resume 3
    python -m src.arb_stream --live        # raises error — not yet enabled
"""
from __future__ import annotations

import sys
import click


@click.command()
@click.option("--simulate", "mode", flag_value="simulate",
              help="Paper trade — no real orders placed.")
@click.option("--live", "mode", flag_value="live",
              help="Place real orders (not yet enabled).")
@click.option("--bankroll", default=5.0, show_default=True, type=float,
              help="Starting bankroll in USD.")
@click.option("--categories", default="Crypto,Economics,Financials", show_default=True,
              help="Comma-separated Kalshi categories to scan.")
@click.option("--near-term", default=60, show_default=True, type=int,
              help="Minutes before close to include in the scan window.")
@click.option("--contracts", default=1, show_default=True, type=int,
              help="Contracts per arb leg.")
@click.option("--min-profit", default=1, show_default=True, type=int,
              help="Minimum profit in cents to enter an arb.")
@click.option("--refresh-interval", default=120, show_default=True, type=int,
              help="Seconds between REST market rescans.")
@click.option("--settle-interval", default=30, show_default=True, type=int,
              help="Seconds to wait between settlement polls (also WS timeout).")
@click.option("--logs-dir", default="logs/arb_stream", show_default=True,
              help="Directory for log files.")
@click.option("--resume", default=None, type=int,
              help="Resume an existing ArbStreamSession by ID.")
def main(mode, bankroll, categories, near_term, contracts, min_profit,
         refresh_interval, settle_interval, logs_dir, resume):
    """Streaming ARB scanner — event-driven via WebSocket price feeds."""
    if mode is None:
        click.echo("ERROR: specify --simulate or --live", err=True)
        sys.exit(1)

    simulate = mode == "simulate"
    cats = [c.strip() for c in categories.split(",") if c.strip()]

    try:
        from src.arb_stream.engine import run_arb_stream_simulation

        run_arb_stream_simulation(
            initial_bankroll_usd=bankroll,
            simulate=simulate,
            categories=cats,
            near_term_minutes=near_term,
            contracts_per_arb=contracts,
            min_profit_cents=min_profit,
            refresh_interval_seconds=refresh_interval,
            settle_interval_seconds=settle_interval,
            logs_dir=logs_dir,
            resume_session_id=resume,
        )
    except RuntimeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
