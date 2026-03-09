"""On-demand performance evaluation — ROI, hit rate, CLV, units P&L."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from src.storage.models import Recommendation, Outcome, EvaluationReport


@dataclass
class ReportResult:
    period_start: datetime
    period_end: datetime
    roi: float
    hit_rate: float
    clv_avg: float | None
    units_profit: float
    total_bets: int
    wins: int
    losses: int
    voids: int
    mode_breakdown: dict = field(default_factory=dict)
    category_breakdown: dict = field(default_factory=dict)


def evaluate(
    session: Session,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> ReportResult:
    """
    Evaluate performance for all settled recommendations in the given period.
    Defaults to the last 30 days.
    """
    now = datetime.now(timezone.utc)
    if period_end is None:
        period_end = now
    if period_start is None:
        period_start = now - timedelta(days=30)

    # Query settled recommendations with outcomes
    recs = (
        session.query(Recommendation)
        .filter(
            Recommendation.created_at >= period_start,
            Recommendation.created_at <= period_end,
            Recommendation.status == "settled",
        )
        .all()
    )

    total_bets = len(recs)
    wins = losses = voids = 0
    total_staked = 0.0
    total_returned = 0.0
    mode_stats: dict[str, dict] = {}
    category_stats: dict[str, dict] = {}

    for rec in recs:
        outcome = rec.outcome
        if outcome is None:
            continue

        staked = rec.stake_units
        total_staked += staked

        mode_stats.setdefault(rec.mode, {"wins": 0, "losses": 0, "staked": 0.0, "returned": 0.0})
        category_stats.setdefault(rec.category, {"wins": 0, "losses": 0, "staked": 0.0, "returned": 0.0})

        mode_stats[rec.mode]["staked"] += staked
        category_stats[rec.category]["staked"] += staked

        if outcome.result == "win":
            wins += 1
            returned = staked * rec.odds
            total_returned += returned
            mode_stats[rec.mode]["wins"] += 1
            mode_stats[rec.mode]["returned"] += returned
            category_stats[rec.category]["wins"] += 1
            category_stats[rec.category]["returned"] += returned
        elif outcome.result == "loss":
            losses += 1
            mode_stats[rec.mode]["losses"] += 1
            category_stats[rec.category]["losses"] += 1
        elif outcome.result == "void":
            voids += 1
            total_returned += staked  # stake returned
            mode_stats[rec.mode]["returned"] += staked
            category_stats[rec.category]["returned"] += staked

    roi = (total_returned - total_staked) / total_staked if total_staked > 0 else 0.0
    hit_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0
    units_profit = total_returned - total_staked

    # Mode breakdown ROI
    mode_breakdown = {}
    for mode, stats in mode_stats.items():
        s = stats["staked"]
        r = stats["returned"]
        mode_breakdown[mode] = {
            "roi": (r - s) / s if s > 0 else 0.0,
            "wins": stats["wins"],
            "losses": stats["losses"],
        }

    category_breakdown = {}
    for cat, stats in category_stats.items():
        s = stats["staked"]
        r = stats["returned"]
        category_breakdown[cat] = {
            "roi": (r - s) / s if s > 0 else 0.0,
            "wins": stats["wins"],
            "losses": stats["losses"],
        }

    # Persist report
    report_orm = EvaluationReport(
        period_start=period_start,
        period_end=period_end,
        roi=roi,
        hit_rate=hit_rate,
        units_profit=units_profit,
        total_bets=total_bets,
    )
    report_orm.mode_breakdown = mode_breakdown
    session.add(report_orm)
    session.commit()

    return ReportResult(
        period_start=period_start,
        period_end=period_end,
        roi=roi,
        hit_rate=hit_rate,
        clv_avg=None,
        units_profit=units_profit,
        total_bets=total_bets,
        wins=wins,
        losses=losses,
        voids=voids,
        mode_breakdown=mode_breakdown,
        category_breakdown=category_breakdown,
    )


def print_report(report: ReportResult) -> None:
    """Pretty-print an evaluation report to stdout."""
    sep = "=" * 60
    print(sep)
    print("  BETTING APP — PERFORMANCE REPORT")
    print(sep)
    print(f"  Period:       {report.period_start.date()} -> {report.period_end.date()}")
    print(f"  Total Bets:   {report.total_bets}")
    print(f"  Wins:         {report.wins}")
    print(f"  Losses:       {report.losses}")
    print(f"  Voids:        {report.voids}")
    print(f"  Hit Rate:     {report.hit_rate:.1%}")
    print(f"  ROI:          {report.roi:+.2%}")
    print(f"  Units P&L:    {report.units_profit:+.2f}")
    if report.mode_breakdown:
        print()
        print("  -- By Mode --")
        for mode, stats in report.mode_breakdown.items():
            print(f"    {mode:12s}  ROI={stats['roi']:+.2%}  W={stats['wins']} L={stats['losses']}")
    if report.category_breakdown:
        print()
        print("  -- By Category --")
        for cat, stats in report.category_breakdown.items():
            print(f"    {cat:30s}  ROI={stats['roi']:+.2%}  W={stats['wins']} L={stats['losses']}")
    print(sep)
