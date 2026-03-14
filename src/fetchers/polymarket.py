"""Polymarket fetcher — binary prediction markets via Gamma + CLOB APIs.

No authentication is required for read-only market data.

Two-pass design to avoid hammering the CLOB order-book endpoint:
  1. Fetch all market metadata from Gamma API (no CLOB calls yet).
  2. Filter candidates by category.
  3. Fetch CLOB book only for filtered markets (with rate-limiting sleep).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import BaseFetcher, Market, Selection


_GAMMA_URL = "https://gamma-api.polymarket.com/markets"
_CLOB_URL = "https://clob.polymarket.com/book"

# Maps Polymarket tag strings → internal category names matching Kalshi convention.
_TAG_TO_CATEGORY: dict[str, str] = {
    "crypto": "Crypto",
    "cryptocurrency": "Crypto",
    "bitcoin": "Crypto",
    "ethereum": "Crypto",
    "defi": "Crypto",
    "economics": "Economics",
    "economy": "Economics",
    "finance": "Financials",
    "financials": "Financials",
    "stocks": "Financials",
    "markets": "Financials",
    "politics": "Politics",
    "elections": "Politics",
    "election": "Politics",
    "sports": "Sports",
    "soccer": "Sports",
    "football": "Sports",
    "basketball": "Sports",
    "science": "Science and Technology",
    "technology": "Science and Technology",
    "tech": "Science and Technology",
    "climate": "Climate and Weather",
    "weather": "Climate and Weather",
    "health": "Health",
    "entertainment": "Entertainment",
    "world": "World",
}


def _map_category(tags: list[str]) -> str:
    """Map a list of Polymarket tag strings to the best-matching internal category."""
    for tag in tags:
        cat = _TAG_TO_CATEGORY.get(tag.lower())
        if cat:
            return cat
    return "Other"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Handle both "Z" suffix and "+00:00"
        cleaned = s.rstrip("Z")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


class PolymarketFetcher(BaseFetcher):
    """
    Fetches binary prediction markets from Polymarket.

    Uses two public endpoints:
      - Gamma API  (market metadata, no auth)
      - CLOB API   (per-token order book for best bid/ask, no auth)

    All prices are normalised to integer cents (0–100) so they are compatible
    with the existing arb scanner (scan_binary_arb, scan_cross_arb).
    """

    name = "polymarket"

    def __init__(self, category_filter: list[str] | None = None):
        self.category_filter: list[str] = category_filter or []
        self._client = httpx.Client(timeout=15.0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_gamma_page(self, offset: int, limit: int = 100) -> list[dict]:
        resp = self._client.get(
            _GAMMA_URL,
            params={"closed": "false", "limit": limit, "offset": offset},
        )
        resp.raise_for_status()
        data = resp.json()
        # Gamma API returns a list directly or wrapped in {"data": [...]}
        if isinstance(data, list):
            return data
        return data.get("data") or data.get("markets") or []

    def _fetch_all_gamma(self) -> list[dict]:
        """Paginate through Gamma API until an incomplete page is returned."""
        all_markets: list[dict] = []
        offset = 0
        limit = 100
        while True:
            page = self._fetch_gamma_page(offset, limit)
            all_markets.extend(page)
            if len(page) < limit:
                break
            offset += limit
        return all_markets

    def _fetch_book(self, token_id: str) -> dict[str, float]:
        """
        Fetch best ask for a single CLOB token.
        Returns {"best_ask": float_cents, "best_bid": float_cents} where prices
        are already converted to integer cents (multiply raw 0–1 price × 100).
        Returns empty dict on failure.
        """
        try:
            resp = self._client.get(_CLOB_URL, params={"token_id": token_id})
            resp.raise_for_status()
            book = resp.json()
            asks = book.get("asks") or []
            bids = book.get("bids") or []
            best_ask = min((float(a["price"]) for a in asks if a.get("price")), default=None)
            best_bid = max((float(b["price"]) for b in bids if b.get("price")), default=None)
            return {
                "best_ask": round(best_ask * 100) if best_ask is not None else None,
                "best_bid": round(best_bid * 100) if best_bid is not None else None,
            }
        except Exception:
            return {}

    def _parse_tokens(self, raw: dict) -> tuple[str | None, str | None]:
        """Extract yes_token_id and no_token_id from a Gamma market dict."""
        tokens = raw.get("tokens") or raw.get("clobTokenIds") or []
        yes_id = no_id = None
        for tok in tokens:
            if isinstance(tok, dict):
                outcome = tok.get("outcome", "").lower()
                tid = tok.get("token_id") or tok.get("tokenId") or tok.get("id")
                if outcome == "yes":
                    yes_id = str(tid) if tid else None
                elif outcome == "no":
                    no_id = str(tid) if tid else None
        # Fallback: some markets provide clobTokenIds as a plain list [yes_id, no_id]
        if yes_id is None and no_id is None and isinstance(tokens, list) and len(tokens) == 2:
            if isinstance(tokens[0], str):
                yes_id, no_id = tokens[0], tokens[1]
        return yes_id, no_id

    def _raw_to_market(self, raw: dict, yes_book: dict, no_book: dict) -> Market | None:
        """Convert a Gamma market dict + CLOB books into a Market object."""
        yes_ask = yes_book.get("best_ask")
        no_ask = no_book.get("best_ask")
        yes_bid = yes_book.get("best_bid")
        no_bid = no_book.get("best_bid")

        if yes_ask is None or no_ask is None:
            return None

        # Decimal odds from ask price in cents
        yes_odds = round(100.0 / yes_ask, 4) if yes_ask and yes_ask > 0 else 0.0
        no_odds = round(100.0 / no_ask, 4) if no_ask and no_ask > 0 else 0.0

        yes_sel = Selection(
            name="yes",
            odds=yes_odds,
            metadata={"yes_ask": int(yes_ask), "yes_bid": yes_bid},
        )
        no_sel = Selection(
            name="no",
            odds=no_odds,
            metadata={"no_ask": int(no_ask), "no_bid": no_bid},
        )

        condition_id = raw.get("condition_id") or raw.get("conditionId") or raw.get("id", "")
        tags = raw.get("tags") or []
        if isinstance(tags, list) and tags and isinstance(tags[0], dict):
            tags = [t.get("label") or t.get("name") or "" for t in tags]

        yes_token_id, no_token_id = self._parse_tokens(raw)
        volume_24hr = raw.get("volume24hr") or raw.get("volumeNum") or 0.0

        return Market(
            id=str(condition_id),
            category=_map_category(tags),
            event_name=raw.get("question") or raw.get("title") or "",
            starts_at=_parse_iso(raw.get("end_date_iso") or raw.get("endDateIso") or raw.get("endDate")),
            selections=[yes_sel, no_sel],
            source="polymarket",
            metadata={
                "condition_id": str(condition_id),
                "yes_token_id": yes_token_id,
                "no_token_id": no_token_id,
                "yes_ask": int(yes_ask),
                "no_ask": int(no_ask),
                "yes_bid": yes_bid,
                "no_bid": no_bid,
                "volume_24hr": float(volume_24hr),
            },
        )

    # ------------------------------------------------------------------
    # BaseFetcher interface
    # ------------------------------------------------------------------

    def get_markets(self, **kwargs) -> list[Market]:
        """
        Fetch active binary Polymarket markets and return them as Market objects.

        Two-pass approach:
          1. Fetch all metadata from Gamma API (no CLOB calls).
          2. Filter by category if category_filter is set.
          3. Fetch CLOB books only for filtered candidates (with 50ms rate-limit sleep).
        """
        raw_markets = self._fetch_all_gamma()

        # Filter: active, accepting orders, binary only
        candidates: list[dict] = []
        for raw in raw_markets:
            if not raw.get("active", True):
                continue
            if raw.get("accepting_orders") is False or raw.get("acceptingOrders") is False:
                continue
            yes_id, no_id = self._parse_tokens(raw)
            if not yes_id or not no_id:
                continue
            candidates.append(raw)

        # Category filter (applied before CLOB calls to save quota)
        if self.category_filter:
            filtered = []
            for raw in candidates:
                tags = raw.get("tags") or []
                if isinstance(tags, list) and tags and isinstance(tags[0], dict):
                    tags = [t.get("label") or t.get("name") or "" for t in tags]
                cat = _map_category(tags)
                if any(cat.lower() == f.lower() for f in self.category_filter):
                    filtered.append(raw)
            candidates = filtered

        markets: list[Market] = []
        for raw in candidates:
            yes_id, no_id = self._parse_tokens(raw)
            time.sleep(0.05)  # rate-limit CLOB calls
            yes_book = self._fetch_book(yes_id) if yes_id else {}
            no_book = self._fetch_book(no_id) if no_id else {}
            mkt = self._raw_to_market(raw, yes_book, no_book)
            if mkt:
                markets.append(mkt)

        return markets

    def get_odds(self, market_id: str, **kwargs) -> Market:
        """Return a single market refreshed with latest CLOB prices."""
        # Find the market in a fresh batch (or fetch by condition_id directly if possible)
        markets = self.get_markets()
        for m in markets:
            if m.id == market_id:
                return m
        raise ValueError(f"Polymarket market {market_id!r} not found or no longer active")
