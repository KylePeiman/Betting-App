"""Unit tests for cross-platform arb scanner and Polymarket fetcher helpers."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.fetchers.base import Market, Selection
from src.engine.cross_arb import (
    _normalise,
    _text_score,
    _expiry_score,
    _category_score,
    _get_price,
    match_markets,
    scan_cross_arb,
    MatchedPair,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _market(
    id: str,
    event_name: str,
    category: str = "Crypto",
    starts_at: datetime | None = None,
    yes_ask: int = 55,
    no_ask: int = 50,
    source: str = "kalshi",
) -> Market:
    return Market(
        id=id,
        category=category,
        event_name=event_name,
        starts_at=starts_at or datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc),
        selections=[
            Selection(name="yes", odds=round(100 / yes_ask, 4)),
            Selection(name="no", odds=round(100 / no_ask, 4)),
        ],
        source=source,
        metadata={"yes_ask": yes_ask, "no_ask": no_ask},
    )


# ---------------------------------------------------------------------------
# _normalise
# ---------------------------------------------------------------------------

class TestNormalise:
    def test_strips_filler_words(self):
        result = _normalise("Will the BTC price be above $50k by Friday?")
        assert "will" not in result
        assert "the" not in result
        assert "by" not in result
        assert "btc" in result
        assert "price" in result

    def test_lowercases(self):
        assert _normalise("BITCOIN") == "bitcoin"

    def test_strips_punctuation(self):
        result = _normalise("BTC/USD above $50,000?")
        assert "," not in result
        assert "?" not in result
        assert "/" not in result


# ---------------------------------------------------------------------------
# _text_score
# ---------------------------------------------------------------------------

class TestTextScore:
    def test_identical(self):
        assert _text_score("Bitcoin above 50000 Friday", "Bitcoin above 50000 Friday") == 1.0

    def test_close_match(self):
        score = _text_score(
            "Will Bitcoin be above $50k on Friday?",
            "Bitcoin above $50,000 by Friday",
        )
        assert score >= 0.50, f"Expected >= 0.50, got {score}"

    def test_unrelated(self):
        score = _text_score("Bitcoin price above 50k", "US presidential election winner 2026")
        assert score < 0.30, f"Expected < 0.30, got {score}"


# ---------------------------------------------------------------------------
# _expiry_score
# ---------------------------------------------------------------------------

class TestExpiryScore:
    def test_same_hour(self):
        base = datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc)
        a = _market("a", "x", starts_at=base)
        b = _market("b", "x", starts_at=base + timedelta(minutes=10))
        score = _expiry_score(a, b)
        assert score >= 0.99

    def test_24h_apart(self):
        base = datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc)
        a = _market("a", "x", starts_at=base)
        b = _market("b", "x", starts_at=base + timedelta(hours=24))
        score = _expiry_score(a, b)
        assert 0.45 <= score <= 0.55, f"Expected ~0.5, got {score}"

    def test_48h_apart(self):
        base = datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc)
        a = _market("a", "x", starts_at=base)
        b = _market("b", "x", starts_at=base + timedelta(hours=48))
        score = _expiry_score(a, b)
        assert score == 0.0

    def test_missing_expiry(self):
        a = _market("a", "x")
        b = _market("b", "x", starts_at=None)
        b.starts_at = None
        score = _expiry_score(a, b)
        assert score == 0.5  # neutral when unknown


# ---------------------------------------------------------------------------
# _category_score
# ---------------------------------------------------------------------------

class TestCategoryScore:
    def test_exact_match(self):
        a = _market("a", "x", category="Crypto")
        b = _market("b", "x", category="Crypto")
        assert _category_score(a, b) == 1.0

    def test_related(self):
        a = _market("a", "x", category="Economics")
        b = _market("b", "x", category="Financials")
        assert _category_score(a, b) == 0.5

    def test_unrelated(self):
        a = _market("a", "x", category="Crypto")
        b = _market("b", "x", category="Sports")
        assert _category_score(a, b) == 0.0


# ---------------------------------------------------------------------------
# _get_price
# ---------------------------------------------------------------------------

class TestGetPrice:
    def test_from_metadata(self):
        m = _market("x", "test", yes_ask=62, no_ask=41)
        assert _get_price(m, "yes") == 62
        assert _get_price(m, "no") == 41

    def test_fallback_to_odds(self):
        m = _market("x", "test", yes_ask=50, no_ask=55)
        m.metadata = {}  # clear metadata
        price = _get_price(m, "yes")
        assert price is not None
        assert 45 <= price <= 55


# ---------------------------------------------------------------------------
# match_markets
# ---------------------------------------------------------------------------

class TestMatchMarkets:
    def test_obvious_match(self):
        now = datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc)
        km = _market("k1", "Will Bitcoin close above $90k on March 13?",
                     category="Crypto", starts_at=now, source="kalshi")
        pm = _market("p1", "Bitcoin above $90,000 by March 13 close?",
                     category="Crypto", starts_at=now + timedelta(minutes=5), source="polymarket")
        pairs = match_markets([km], [pm], min_score=0.5)
        assert len(pairs) == 1
        assert pairs[0].kalshi_market.id == "k1"
        assert pairs[0].poly_market.id == "p1"
        assert pairs[0].match_score >= 0.5

    def test_no_match_below_threshold(self):
        km = _market("k1", "Will BTC close above $90k?", source="kalshi")
        pm = _market("p1", "US election winner 2026", category="Politics", source="polymarket")
        pairs = match_markets([km], [pm], min_score=0.85)
        assert len(pairs) == 0

    def test_one_to_one_constraint(self):
        now = datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc)
        km1 = _market("k1", "Bitcoin above 90k March 13", category="Crypto", starts_at=now, source="kalshi")
        km2 = _market("k2", "Bitcoin above 90k March 13", category="Crypto", starts_at=now, source="kalshi")
        pm1 = _market("p1", "Bitcoin above 90k March 13", category="Crypto", starts_at=now, source="polymarket")
        pairs = match_markets([km1, km2], [pm1], min_score=0.5)
        # p1 can only match one Kalshi market
        poly_ids = [p.poly_market.id for p in pairs]
        assert poly_ids.count("p1") <= 1


# ---------------------------------------------------------------------------
# scan_cross_arb
# ---------------------------------------------------------------------------

class TestScanCrossArb:
    def _make_pair(self, k_yes, k_no, p_yes, p_no, score=0.92) -> MatchedPair:
        now = datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc)
        km = _market("k1", "BTC above 90k", yes_ask=k_yes, no_ask=k_no, source="kalshi")
        pm = _market("p1", "BTC above 90k", yes_ask=p_yes, no_ask=p_no, source="polymarket")
        return MatchedPair(
            kalshi_market=km,
            poly_market=pm,
            match_score=score,
            match_reason="test",
        )

    def test_detects_kalshi_yes_arb(self):
        # k_yes=52 + p_no=44 = 96¢ → 4¢ profit
        pair = self._make_pair(k_yes=52, k_no=51, p_yes=55, p_no=44)
        opps = scan_cross_arb([pair], min_profit_cents=1.0)
        kalshi_yes_opps = [o for o in opps if o.direction == "kalshi_yes"]
        assert len(kalshi_yes_opps) == 1
        assert abs(kalshi_yes_opps[0].profit_cents - 4.0) < 0.5

    def test_detects_poly_yes_arb(self):
        # p_yes=48 + k_no=46 = 94¢ → 6¢ profit
        pair = self._make_pair(k_yes=60, k_no=46, p_yes=48, p_no=55)
        opps = scan_cross_arb([pair], min_profit_cents=1.0)
        poly_yes_opps = [o for o in opps if o.direction == "poly_yes"]
        assert len(poly_yes_opps) == 1
        assert abs(poly_yes_opps[0].profit_cents - 6.0) < 0.5

    def test_skips_below_min_profit(self):
        # k_yes=50 + p_no=50 = 100 → 0 profit
        pair = self._make_pair(k_yes=50, k_no=50, p_yes=50, p_no=50)
        opps = scan_cross_arb([pair], min_profit_cents=1.0)
        assert len(opps) == 0

    def test_skips_high_risk(self):
        pair = self._make_pair(k_yes=52, k_no=51, p_yes=55, p_no=44, score=0.75)
        # score 0.75 < 0.80 threshold → should be filtered by match_markets but
        # scan_cross_arb also skips if score < 0.80
        opps = scan_cross_arb([pair], min_profit_cents=1.0)
        assert len(opps) == 0

    def test_sorted_by_profit_pct_desc(self):
        pair1 = self._make_pair(k_yes=52, k_no=51, p_yes=55, p_no=44)  # ~4¢ profit
        pair2 = self._make_pair(k_yes=50, k_no=51, p_yes=55, p_no=46)  # ~4¢ from both
        opps = scan_cross_arb([pair1, pair2], min_profit_cents=1.0)
        if len(opps) >= 2:
            assert opps[0].profit_pct >= opps[1].profit_pct


# ---------------------------------------------------------------------------
# PolymarketFetcher._fetch_book price parsing
# ---------------------------------------------------------------------------

class TestPolymarketFetcherBookParsing:
    def test_price_conversion(self):
        """CLOB prices are 0.0–1.0; we multiply × 100 → integer cents."""
        from src.fetchers.polymarket import PolymarketFetcher

        fetcher = PolymarketFetcher()
        mock_book = {
            "asks": [{"price": "0.55", "size": "100"}, {"price": "0.57", "size": "50"}],
            "bids": [{"price": "0.52", "size": "80"}],
        }
        with patch.object(fetcher._client, "get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = mock_book
            mock_get.return_value = mock_resp

            result = fetcher._fetch_book("token123")

        # best ask = min(0.55, 0.57) = 0.55 → 55 cents
        assert result["best_ask"] == 55
        # best bid = max(0.52) = 0.52 → 52 cents
        assert result["best_bid"] == 52

    def test_empty_book_returns_none(self):
        from src.fetchers.polymarket import PolymarketFetcher

        fetcher = PolymarketFetcher()
        mock_book = {"asks": [], "bids": []}
        with patch.object(fetcher._client, "get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = mock_book
            mock_get.return_value = mock_resp

            result = fetcher._fetch_book("token123")

        assert result.get("best_ask") is None
        assert result.get("best_bid") is None

    def test_http_error_returns_empty(self):
        from src.fetchers.polymarket import PolymarketFetcher
        import httpx

        fetcher = PolymarketFetcher()
        with patch.object(fetcher._client, "get") as mock_get:
            mock_get.side_effect = httpx.ConnectError("connection refused")
            result = fetcher._fetch_book("token123")

        assert result == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
