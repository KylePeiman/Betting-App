"""
Unit tests for the last-second price convergence strategy.
Tests PriceTracker, find_matching_bucket, find_directional_opportunity,
scan_last_second_opportunities, and kraken_pair_for_market without any
network calls.
"""
from __future__ import annotations

import time
import sys
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Minimal stubs for Market / Selection (mirrors src/fetchers/base.py)
# ---------------------------------------------------------------------------

@dataclass
class Selection:
    name: str
    odds: float
    metadata: dict = field(default_factory=dict)


@dataclass
class Market:
    id: str
    category: str
    event_name: str
    starts_at: datetime | None
    selections: list[Selection]
    source: str = "kalshi"
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers to build fake Kalshi bucket markets
# ---------------------------------------------------------------------------

def make_bucket_market(
    ticker: str,
    floor_strike: float,
    cap_strike: float,
    yes_ask: int,
    closes_in_seconds: int = 60,
    series_ticker: str = "KXBTC-25MAR-B85000",
) -> Market:
    """Create a fake price-range bucket Market object."""
    close_time = datetime.now(timezone.utc) + timedelta(seconds=closes_in_seconds)
    return Market(
        id=ticker,
        category="crypto",
        event_name="BTC hourly",
        starts_at=close_time,
        selections=[
            Selection(
                name="Yes",
                odds=round(100 / yes_ask, 4),
                metadata={"yes_ask": yes_ask, "yes_bid": yes_ask - 2, "implied_prob": yes_ask / 100},
            ),
            Selection(
                name="No",
                odds=round(100 / (100 - yes_ask), 4),
                metadata={"no_ask": 100 - yes_ask, "no_bid": 100 - yes_ask - 2, "implied_prob": (100 - yes_ask) / 100},
            ),
        ],
        metadata={
            "floor_strike": floor_strike,
            "cap_strike": cap_strike,
            "series_ticker": series_ticker,
            "mutually_exclusive": True,
            "total_markets_in_event": 10,
            "event_ticker": "KXBTC-25MAR",
        },
    )


def make_directional_market(
    ticker: str,
    floor_strike: float,
    yes_ask: int,
    no_ask: int | None = None,
    closes_in_seconds: int = 60,
    series_ticker: str = "KXBTC15M-25MAR",
) -> Market:
    """Create a fake 15M directional Market (floor_strike only, no cap_strike)."""
    close_time = datetime.now(timezone.utc) + timedelta(seconds=closes_in_seconds)
    if no_ask is None:
        no_ask = 100 - yes_ask
    return Market(
        id=ticker,
        category="crypto",
        event_name="BTC price up in next 15 mins?",
        starts_at=close_time,
        selections=[
            Selection(
                name="Yes",
                odds=round(100 / yes_ask, 4),
                metadata={"yes_ask": yes_ask, "yes_bid": yes_ask - 2, "implied_prob": yes_ask / 100},
            ),
            Selection(
                name="No",
                odds=round(100 / no_ask, 4),
                metadata={"no_ask": no_ask, "no_bid": no_ask - 2, "implied_prob": no_ask / 100},
            ),
        ],
        metadata={
            "floor_strike": floor_strike,
            # No cap_strike — distinguishes directional from bucket markets
            "series_ticker": series_ticker,
            "event_ticker": "KXBTC15M-25MAR",
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_price_tracker_basic():
    from src.engine.last_second import PriceTracker
    tracker = PriceTracker()
    assert tracker.latest() is None
    assert tracker.observation_count() == 0

    tracker.record(85000.0)
    assert tracker.latest() == 85000.0
    assert tracker.observation_count() == 1
    print("  PASS: PriceTracker basic record/latest")


def test_price_tracker_stability_stable():
    from src.engine.last_second import PriceTracker
    tracker = PriceTracker()
    # Record prices varying by <0.3% (stable)
    for p in [85000, 85010, 85005, 85008, 85002]:
        tracker.record(p)
    assert tracker.is_stable(window_seconds=60, max_move_pct=0.003), \
        f"Expected stable; range={(max([85000,85010,85005,85008,85002]) - min([85000,85010,85005,85008,85002]))/85000:.4%}"
    print("  PASS: PriceTracker stability — stable prices")


def test_price_tracker_stability_unstable():
    from src.engine.last_second import PriceTracker
    tracker = PriceTracker()
    # Prices vary >0.3% (unstable)
    tracker.record(85000)
    tracker.record(85500)  # +0.59% move
    assert not tracker.is_stable(window_seconds=60, max_move_pct=0.003), \
        "Expected unstable"
    print("  PASS: PriceTracker stability — unstable prices")


def test_price_tracker_stability_not_enough_obs():
    from src.engine.last_second import PriceTracker
    tracker = PriceTracker()
    tracker.record(85000)  # Only 1 observation in window
    # With window=1s and only 1 obs, is_stable should return False
    assert not tracker.is_stable(window_seconds=1, max_move_pct=0.003), \
        "Expected False with only 1 observation in window"
    print("  PASS: PriceTracker stability — insufficient observations")


def test_find_matching_bucket_center():
    from src.engine.last_second import find_matching_bucket
    markets = [
        make_bucket_market("BTC-84000-85000", 84000, 85000, yes_ask=5),
        make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=82),  # target
        make_bucket_market("BTC-86000-87000", 86000, 87000, yes_ask=5),
    ]
    # spot = 85500 → center of 85000-86000 bucket
    result = find_matching_bucket(markets, spot_price=85500.0, edge_buffer_pct=0.15)
    assert result is not None, "Expected to find matching bucket"
    mkt, yes_ask = result
    assert mkt.id == "BTC-85000-86000", f"Wrong bucket: {mkt.id}"
    assert yes_ask == 82
    print("  PASS: find_matching_bucket — center of bucket")


def test_find_matching_bucket_too_close_to_edge():
    from src.engine.last_second import find_matching_bucket
    markets = [
        make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=82),
    ]
    # Bucket width = 1000; 15% buffer = 150
    # spot at 85050 → margin_from_floor = 50 < 150 → too close to floor edge
    result = find_matching_bucket(markets, spot_price=85050.0, edge_buffer_pct=0.15)
    assert result is None, f"Expected None (too close to edge), got {result}"
    print("  PASS: find_matching_bucket — rejects near-floor edge")


def test_find_matching_bucket_no_floor_cap():
    from src.engine.last_second import find_matching_bucket
    # Market without floor_strike/cap_strike in metadata
    mkt = make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=82)
    del mkt.metadata["floor_strike"]
    del mkt.metadata["cap_strike"]
    result = find_matching_bucket([mkt], spot_price=85500.0)
    assert result is None, "Expected None when no floor/cap in metadata"
    print("  PASS: find_matching_bucket — skips market without floor/cap")


def test_kraken_pair_for_market():
    from src.engine.last_second import kraken_pair_for_market
    mkt = make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=82, series_ticker="KXBTC-25MAR-B85000")
    assert kraken_pair_for_market(mkt) == "XBTUSD", f"Got: {kraken_pair_for_market(mkt)}"

    mkt2 = make_bucket_market("ETH-3000-3100", 3000, 3100, yes_ask=80, series_ticker="KXETH-25MAR-B3000")
    assert kraken_pair_for_market(mkt2) == "ETHUSD"

    # Non-crypto (no matching prefix)
    mkt3 = make_bucket_market("SPY-550-560", 550, 560, yes_ask=80, series_ticker="KXSPY-25MAR-B550")
    assert kraken_pair_for_market(mkt3) is None  # KXSPY not in mapping
    print("  PASS: kraken_pair_for_market")


def test_scan_last_second_opportunity_found():
    from src.engine.last_second import PriceTracker, scan_last_second_opportunities

    # Build a crypto bucket market closing in 60 seconds
    markets = [
        make_bucket_market("BTC-84000-85000", 84000, 85000, yes_ask=5, closes_in_seconds=60),
        make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=82, closes_in_seconds=60),
        make_bucket_market("BTC-86000-87000", 86000, 87000, yes_ask=5, closes_in_seconds=60),
    ]

    tracker = PriceTracker()
    # Record stable prices in the 85000-86000 bucket center
    for p in [85500, 85505, 85498, 85502, 85501]:
        tracker.record(p)
        time.sleep(0.01)

    trackers = {"XBTUSD": tracker}
    now = datetime.now(timezone.utc)
    entries = scan_last_second_opportunities(
        markets, trackers, now,
        entry_window_seconds=75,
        min_yes_cents=70, max_yes_cents=95,
        edge_buffer_pct=0.15,
        stability_window_s=15, stability_threshold_pct=0.003,
    )
    assert len(entries) == 1, f"Expected 1 entry, got {len(entries)}"
    e = entries[0]
    assert e["market"].id == "BTC-85000-86000"
    assert e["yes_ask_cents"] == 82
    assert e["kraken_pair"] == "XBTUSD"
    print("  PASS: scan_last_second_opportunities — opportunity found")


def test_scan_last_second_no_opportunity_unstable():
    from src.engine.last_second import PriceTracker, scan_last_second_opportunities

    markets = [
        make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=82, closes_in_seconds=60),
    ]
    tracker = PriceTracker()
    # Unstable prices
    tracker.record(85000)
    tracker.record(85600)  # +0.7% move → unstable
    trackers = {"XBTUSD": tracker}

    now = datetime.now(timezone.utc)
    entries = scan_last_second_opportunities(markets, trackers, now)
    assert len(entries) == 0, f"Expected 0 entries (unstable price), got {len(entries)}"
    print("  PASS: scan_last_second_opportunities — no entry on unstable price")


def test_scan_last_second_no_opportunity_too_late():
    from src.engine.last_second import PriceTracker, scan_last_second_opportunities

    # Market already expired (negative closes_in)
    markets = [
        make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=82, closes_in_seconds=-5),
    ]
    tracker = PriceTracker()
    for p in [85500, 85501, 85502]:
        tracker.record(p)
    trackers = {"XBTUSD": tracker}

    now = datetime.now(timezone.utc)
    entries = scan_last_second_opportunities(markets, trackers, now)
    assert len(entries) == 0, f"Expected 0 entries (market expired), got {len(entries)}"
    print("  PASS: scan_last_second_opportunities — no entry for expired market")


def test_scan_last_second_yes_ask_too_low():
    from src.engine.last_second import PriceTracker, scan_last_second_opportunities

    # YES ask at 60 → below min_yes_cents=70
    markets = [
        make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=60, closes_in_seconds=60),
    ]
    tracker = PriceTracker()
    for p in [85500, 85501, 85502]:
        tracker.record(p)
    trackers = {"XBTUSD": tracker}

    now = datetime.now(timezone.utc)
    entries = scan_last_second_opportunities(markets, trackers, now, min_yes_cents=70)
    assert len(entries) == 0, f"Expected 0 entries (yes_ask too low), got {len(entries)}"
    print("  PASS: scan_last_second_opportunities — no entry when YES ask too low")


def test_scan_last_second_yes_ask_too_high():
    from src.engine.last_second import PriceTracker, scan_last_second_opportunities

    # YES ask at 97 → above max_yes_cents=95
    markets = [
        make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=97, closes_in_seconds=60),
    ]
    tracker = PriceTracker()
    for p in [85500, 85501, 85502]:
        tracker.record(p)
    trackers = {"XBTUSD": tracker}

    now = datetime.now(timezone.utc)
    entries = scan_last_second_opportunities(markets, trackers, now, max_yes_cents=95)
    assert len(entries) == 0, f"Expected 0 entries (yes_ask too high), got {len(entries)}"
    print("  PASS: scan_last_second_opportunities — no entry when YES ask too high")


def test_scan_last_second_market_too_far_out():
    from src.engine.last_second import PriceTracker, scan_last_second_opportunities

    # Market closes in 200s, but window is only 75s
    markets = [
        make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=82, closes_in_seconds=200),
    ]
    tracker = PriceTracker()
    for p in [85500, 85501, 85502]:
        tracker.record(p)
    trackers = {"XBTUSD": tracker}

    now = datetime.now(timezone.utc)
    entries = scan_last_second_opportunities(markets, trackers, now, entry_window_seconds=75)
    assert len(entries) == 0, f"Expected 0 entries (too far from close), got {len(entries)}"
    print("  PASS: scan_last_second_opportunities — no entry when too far from close")


# ---------------------------------------------------------------------------
# Tests: find_directional_opportunity
# ---------------------------------------------------------------------------

def test_find_directional_opportunity_yes():
    from src.engine.last_second import find_directional_opportunity
    mkt = make_directional_market("BTC15M-85000", floor_strike=85000.0, yes_ask=78, no_ask=25)
    # Spot 1% above floor → YES signal
    result = find_directional_opportunity(mkt, spot_price=85850.0, margin_pct=0.003)
    assert result is not None, "Expected YES opportunity"
    assert result["side"] == "yes"
    assert result["yes_ask_cents"] == 78
    print("  PASS: find_directional_opportunity — YES when spot above floor")


def test_find_directional_opportunity_no():
    from src.engine.last_second import find_directional_opportunity
    mkt = make_directional_market("BTC15M-85000", floor_strike=85000.0, yes_ask=75, no_ask=28)
    # Spot 1% below floor → NO signal; no_ask=28 within default MAX_NO_CENTS=40
    result = find_directional_opportunity(mkt, spot_price=84150.0, margin_pct=0.003)
    assert result is not None, "Expected NO opportunity"
    assert result["side"] == "no"
    assert result["no_ask_cents"] == 28
    print("  PASS: find_directional_opportunity — NO when spot below floor")


def test_find_directional_opportunity_too_close():
    from src.engine.last_second import find_directional_opportunity
    mkt = make_directional_market("BTC15M-85000", floor_strike=85000.0, yes_ask=52, no_ask=50)
    # Spot only 0.1% above floor — inside margin band → None
    result = find_directional_opportunity(mkt, spot_price=85085.0, margin_pct=0.003)
    assert result is None, f"Expected None (too close to floor), got {result}"
    print("  PASS: find_directional_opportunity — None when too close to floor")


def test_find_directional_opportunity_skips_bucket_market():
    from src.engine.last_second import find_directional_opportunity
    # Bucket market (has cap_strike) → should be skipped by directional function
    mkt = make_bucket_market("BTC-85000-86000", 85000, 86000, yes_ask=82)
    result = find_directional_opportunity(mkt, spot_price=85500.0)
    assert result is None, "Expected None for bucket market (has cap_strike)"
    print("  PASS: find_directional_opportunity — skips bucket market with cap_strike")


def test_find_directional_opportunity_yes_ask_out_of_range():
    from src.engine.last_second import find_directional_opportunity
    mkt = make_directional_market("BTC15M-85000", floor_strike=85000.0, yes_ask=60, no_ask=42)
    # yes_ask=60 is below min_yes_cents=70
    result = find_directional_opportunity(
        mkt, spot_price=85850.0, margin_pct=0.003,
        min_yes_cents=70, max_yes_cents=95,
    )
    assert result is None, f"Expected None (yes_ask out of range), got {result}"
    print("  PASS: find_directional_opportunity — None when yes_ask below min")


# ---------------------------------------------------------------------------
# Tests: scan with directional markets
# ---------------------------------------------------------------------------

def test_scan_directional_yes_opportunity():
    from src.engine.last_second import PriceTracker, scan_last_second_opportunities

    mkt = make_directional_market(
        "BTC15M-85000", floor_strike=85000.0, yes_ask=80, no_ask=22,
        closes_in_seconds=60, series_ticker="KXBTC15M-25MAR",
    )

    tracker = PriceTracker()
    # Stable prices 1% above floor
    for p in [85850, 85855, 85848, 85852, 85851]:
        tracker.record(p)
        time.sleep(0.01)

    trackers = {"XBTUSD": tracker}
    now = datetime.now(timezone.utc)
    entries = scan_last_second_opportunities(
        [mkt], trackers, now,
        entry_window_seconds=75,
        min_yes_cents=70, max_yes_cents=95,
        min_no_cents=3, max_no_cents=40,
        edge_buffer_pct=0.15,
        stability_window_s=15, stability_threshold_pct=0.003,
    )
    yes_entries = [e for e in entries if e["side"] == "yes"]
    assert len(yes_entries) == 1, f"Expected 1 directional YES entry, got {entries}"
    e = yes_entries[0]
    assert e["market"].id == "BTC15M-85000"
    assert e["yes_ask_cents"] == 80
    print("  PASS: scan_last_second_opportunities — directional YES found")


def test_scan_directional_no_opportunity():
    from src.engine.last_second import PriceTracker, scan_last_second_opportunities

    mkt = make_directional_market(
        "BTC15M-85000", floor_strike=85000.0, yes_ask=22, no_ask=80,
        closes_in_seconds=60, series_ticker="KXBTC15M-25MAR",
    )

    tracker = PriceTracker()
    # Stable prices 1% below floor
    for p in [84150, 84148, 84155, 84151, 84149]:
        tracker.record(p)
        time.sleep(0.01)

    trackers = {"XBTUSD": tracker}
    now = datetime.now(timezone.utc)
    entries = scan_last_second_opportunities(
        [mkt], trackers, now,
        entry_window_seconds=75,
        min_yes_cents=70, max_yes_cents=95,
        min_no_cents=3, max_no_cents=90,
        edge_buffer_pct=0.15,
        stability_window_s=15, stability_threshold_pct=0.003,
    )
    no_entries = [e for e in entries if e["side"] == "no"]
    assert len(no_entries) == 1, f"Expected 1 directional NO entry, got {entries}"
    e = no_entries[0]
    assert e["market"].id == "BTC15M-85000"
    assert e["no_ask_cents"] == 80
    print("  PASS: scan_last_second_opportunities — directional NO found")


def test_scan_directional_too_close_no_opportunity():
    from src.engine.last_second import PriceTracker, scan_last_second_opportunities

    mkt = make_directional_market(
        "BTC15M-85000", floor_strike=85000.0, yes_ask=52, no_ask=50,
        closes_in_seconds=60, series_ticker="KXBTC15M-25MAR",
    )

    tracker = PriceTracker()
    # Stable prices right at floor — within margin band
    for p in [85010, 85012, 85008, 85011, 85009]:
        tracker.record(p)
        time.sleep(0.01)

    trackers = {"XBTUSD": tracker}
    now = datetime.now(timezone.utc)
    entries = scan_last_second_opportunities(
        [mkt], trackers, now,
        entry_window_seconds=75,
        stability_window_s=15, stability_threshold_pct=0.003,
    )
    assert len(entries) == 0, f"Expected 0 entries (too close to floor), got {entries}"
    print("  PASS: scan_last_second_opportunities — no directional entry when too close to floor")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_price_tracker_basic,
        test_price_tracker_stability_stable,
        test_price_tracker_stability_unstable,
        test_price_tracker_stability_not_enough_obs,
        test_find_matching_bucket_center,
        test_find_matching_bucket_too_close_to_edge,
        test_find_matching_bucket_no_floor_cap,
        test_kraken_pair_for_market,
        test_scan_last_second_opportunity_found,
        test_scan_last_second_no_opportunity_unstable,
        test_scan_last_second_no_opportunity_too_late,
        test_scan_last_second_yes_ask_too_low,
        test_scan_last_second_yes_ask_too_high,
        test_scan_last_second_market_too_far_out,
        # Directional (15M) market tests
        test_find_directional_opportunity_yes,
        test_find_directional_opportunity_no,
        test_find_directional_opportunity_too_close,
        test_find_directional_opportunity_skips_bucket_market,
        test_find_directional_opportunity_yes_ask_out_of_range,
        test_scan_directional_yes_opportunity,
        test_scan_directional_no_opportunity,
        test_scan_directional_too_close_no_opportunity,
    ]

    passed = 0
    failed = 0
    print(f"\nRunning {len(tests)} tests...\n")
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            print(f"  FAIL: {test_fn.__name__}: {exc}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    else:
        print("All tests passed.")
