"""Click CLI entry point for the Betting App."""
from __future__ import annotations
import click
from datetime import datetime, timezone, timedelta


@click.group()
def cli():
    """Betting App — AI-powered and statistical betting recommendations."""
    pass


@cli.command("list-sports")
@click.option("--all", "show_all", is_flag=True, help="Include inactive sports.")
def list_sports(show_all: bool):
    """List available sport keys from TheOddsAPI (costs 1 API request)."""
    from src.fetchers.odds_api import OddsAPIFetcher
    fetcher = OddsAPIFetcher()
    try:
        sports = fetcher.list_sports(active_only=not show_all)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    click.echo(f"{'Key':<45} {'Group':<20} {'Title'}")
    click.echo("-" * 90)
    for s in sorted(sports, key=lambda x: (x.get("group", ""), x.get("key", ""))):
        click.echo(f"{s['key']:<45} {s.get('group',''):<20} {s.get('title','')}")


@cli.command()
@click.option("--mode", type=click.Choice(["agent", "compute"]), default="compute", show_default=True)
@click.option("--period", type=click.Choice(["week", "month"]), default="week", show_default=True)
@click.option("--sources", multiple=True, help="Data sources to use (odds_api, betfair, sportsdata).")
@click.option("--quiet", is_flag=True, help="Suppress verbose output.")
def run(mode: str, period: str, sources: tuple, quiet: bool):
    """Fetch markets and generate bet recommendations."""
    from src.engine.pipeline import run as pipeline_run
    src_list = list(sources) if sources else None
    recs = pipeline_run(mode=mode, period=period, sources=src_list, verbose=not quiet)
    if recs:
        click.echo(f"\nStored {len(recs)} recommendations.")
        for rec in recs[:5]:
            click.echo(f"  [{rec.id}] {rec.event_name} — {rec.selection} @ {rec.odds:.2f}  (conf={rec.confidence:.0%})")
        if len(recs) > 5:
            click.echo(f"  ... and {len(recs) - 5} more. Use 'recommendations list' to see all.")
    else:
        click.echo("No recommendations generated.")


@cli.command()
@click.option("--from", "from_date", default=None, help="Start date (YYYY-MM-DD). Defaults to 30 days ago.")
@click.option("--to", "to_date", default=None, help="End date (YYYY-MM-DD). Defaults to today.")
def evaluate(from_date: str | None, to_date: str | None):
    """Evaluate historical bet performance."""
    from src.storage.db import get_session
    from src.evaluator.performance import evaluate as eval_fn, print_report

    now = datetime.now(timezone.utc)
    period_end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if to_date else now
    period_start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if from_date else now - timedelta(days=30)

    session = get_session()
    report = eval_fn(session, period_start, period_end)
    print_report(report)


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
    """Settle a recommendation with its actual result."""
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

    outcome = Outcome(
        recommendation_id=rec_id,
        result=result,
        actual_odds=actual_odds,
    )
    rec.status = "settled"
    session.add(outcome)
    session.commit()
    click.echo(f"Recommendation {rec_id} settled as '{result}'.")


if __name__ == "__main__":
    cli()
