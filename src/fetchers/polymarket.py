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
_GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
_CLOB_URL = "https://clob.polymarket.com/book"

# Maps Polymarket tag strings → internal category names matching Kalshi convention.
_TAG_TO_CATEGORY: dict[str, str] = {
    # Crypto
    "crypto": "Crypto",
    "cryptocurrency": "Crypto",
    "bitcoin": "Crypto",
    "ethereum": "Crypto",
    "defi": "Crypto",
    "solana": "Crypto",
    "xrp": "Crypto",
    # Economics / Financials
    "economics": "Economics",
    "economy": "Economics",
    "finance": "Financials",
    "financials": "Financials",
    "stocks": "Financials",
    "markets": "Financials",
    "business": "Financials",
    "ipos": "Financials",
    # Politics / World
    "politics": "Politics",
    "elections": "Politics",
    "election": "Politics",
    "political": "Politics",
    "government": "Politics",
    "world": "World",
    "geopolitics": "World",
    # Sports
    "sports": "Sports",
    "soccer": "Sports",
    "football": "Sports",
    "basketball": "Sports",
    "nba": "Sports",
    "nfl": "Sports",
    "nhl": "Sports",
    "mlb": "Sports",
    "golf": "Sports",
    "tennis": "Sports",
    "mma": "Sports",
    "ufc": "Sports",
    "baseball": "Sports",
    "hockey": "Sports",
    "fifa": "Sports",
    "olympics": "Sports",
    # Science / Tech
    "science": "Science and Technology",
    "technology": "Science and Technology",
    "tech": "Science and Technology",
    "ai": "Science and Technology",
    # Other
    "climate": "Climate and Weather",
    "weather": "Climate and Weather",
    "health": "Health",
    "entertainment": "Entertainment",
    "pop-culture": "Entertainment",
    "music": "Entertainment",
}


def _map_category(tags: list) -> str:
    """
    Map a list of Polymarket tags to the best-matching internal category.
    Tags may be plain strings or dicts with 'label' and/or 'slug' keys.
    """
    for tag in tags:
        if isinstance(tag, dict):
            candidates = [tag.get("slug") or "", tag.get("label") or ""]
        else:
            candidates = [str(tag)]
        for candidate in candidates:
            cat = _TAG_TO_CATEGORY.get(candidate.lower())
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

    def _fetch_markets_page(
        self,
        offset: int,
        limit: int = 100,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
    ) -> list[dict]:
        """Fetch one page from the /markets endpoint, sorted by volume24hr descending."""
        params: dict = {
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
        }
        if end_date_min:
            params["end_date_min"] = end_date_min
        if end_date_max:
            params["end_date_max"] = end_date_max
        resp = self._client.get(_GAMMA_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else (data.get("data") or data.get("markets") or [])

    def _fetch_event_tags(self, max_events: int = 500) -> dict[str, list]:
        """
        Fetch the top events (by volume) from the /events endpoint and return a
        mapping of condition_id → tags list.  Used to annotate /markets results
        with proper category tags (events endpoint has tags; markets endpoint does not).
        """
        tag_map: dict[str, list] = {}
        limit = 100
        for offset in range(0, max_events, limit):
            try:
                resp = self._client.get(
                    _GAMMA_EVENTS_URL,
                    params={"closed": "false", "active": "true",
                            "order": "volume24hr", "limit": limit, "offset": offset},
                )
                resp.raise_for_status()
                events = resp.json() if isinstance(resp.json(), list) else []
                for ev in events:
                    ev_tags = ev.get("tags") or []
                    for mkt in (ev.get("markets") or []):
                        cid = str(mkt.get("conditionId") or mkt.get("id") or "")
                        if cid and ev_tags:
                            tag_map[cid] = ev_tags
                if len(events) < limit:
                    break
            except Exception:
                break
        return tag_map

    def _fetch_all_gamma(
        self,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
        max_pages: int = 5,
    ) -> list[dict]:
        """
        Fetch active markets from the /markets endpoint (full data: volume, prices,
        acceptingOrders, clobTokenIds) sorted by volume24hr descending.

        end_date_min / end_date_max: ISO-8601 strings to scope by settlement date.
        max_pages: hard cap (default 5 = 500 markets).  Most liquid markets appear
          first, so the first few pages cover the actionable cross-arb universe.

        Tags are fetched separately from the /events endpoint and stamped onto
        markets so _map_category works correctly.
        """
        # Fetch tags from events endpoint (best-effort; don't fail if unavailable)
        tag_map = self._fetch_event_tags(max_events=500)

        all_markets: list[dict] = []
        offset = 0
        limit = 100
        pages_fetched = 0
        while pages_fetched < max_pages:
            page = self._fetch_markets_page(
                offset, limit,
                end_date_min=end_date_min,
                end_date_max=end_date_max,
            )
            for mkt in page:
                cid = str(mkt.get("conditionId") or mkt.get("id") or "")
                if not mkt.get("tags") and cid in tag_map:
                    mkt["tags"] = tag_map[cid]
                all_markets.append(mkt)
            pages_fetched += 1
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
        import json as _json

        tokens = raw.get("tokens") or raw.get("clobTokenIds") or []
        # clobTokenIds is sometimes returned as a JSON string — parse it
        if isinstance(tokens, str):
            try:
                tokens = _json.loads(tokens)
            except Exception:
                tokens = []

        yes_id = no_id = None
        for tok in tokens:
            if isinstance(tok, dict):
                outcome = tok.get("outcome", "").lower()
                tid = tok.get("token_id") or tok.get("tokenId") or tok.get("id")
                if outcome == "yes":
                    yes_id = str(tid) if tid else None
                elif outcome == "no":
                    no_id = str(tid) if tid else None
        # Fallback: clobTokenIds as a plain list [yes_id, no_id] (order: yes first)
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

        # Fall back to inline Gamma prices when CLOB call returned nothing.
        # bestAsk/bestBid are 0–1 floats for the YES token.
        # outcomePrices is a JSON string ["yes_mid", "no_mid"] (midpoints, not asks).
        if yes_ask is None:
            raw_ask = raw.get("bestAsk")
            if raw_ask is not None:
                yes_ask = round(float(raw_ask) * 100)
        if no_ask is None:
            import json as _json
            op = raw.get("outcomePrices")
            if op:
                try:
                    prices = _json.loads(op) if isinstance(op, str) else op
                    no_ask = round(float(prices[1]) * 100)
                except Exception:
                    pass
        if yes_bid is None:
            raw_bid = raw.get("bestBid")
            if raw_bid is not None:
                yes_bid = round(float(raw_bid) * 100)

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
        # Tags are pre-stamped from the events endpoint (dicts with label+slug).
        # _map_category handles both dict and string forms.
        tags = raw.get("tags") or []

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

    def get_markets(
        self,
        within_days: int | None = None,
        min_volume_24h: float = 0.0,
        **kwargs,
    ) -> list[Market]:
        """
        Fetch active binary Polymarket markets and return them as Market objects.

        within_days:    only fetch markets settling within this many days from now.
                        Uses end_date_max API filter — much faster than full pagination.
        min_volume_24h: skip markets with 24-hour volume below this threshold (USD).
                        Use ~100 for cross-arb scanning to drop illiquid long-shots.

        Two-pass approach:
          1. Fetch metadata from Gamma /events API (proper tags, optional date filter).
          2. Filter by category, volume, and binary token availability.
          3. Fetch CLOB books only for candidates (50ms rate-limit sleep per market).
        """
        end_date_min = end_date_max = None
        if within_days is not None:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            end_date_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_date_max = (now + timedelta(days=within_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_markets = self._fetch_all_gamma(end_date_min=end_date_min, end_date_max=end_date_max)

        # Filter: active, accepting orders, binary only, optional volume floor
        candidates: list[dict] = []
        for raw in raw_markets:
            if not raw.get("active", True):
                continue
            if raw.get("accepting_orders") is False or raw.get("acceptingOrders") is False:
                continue
            yes_id, no_id = self._parse_tokens(raw)
            if not yes_id or not no_id:
                continue
            if min_volume_24h > 0:
                vol = raw.get("volume24hr") or raw.get("volumeNum") or 0.0
                if float(vol) < min_volume_24h:
                    continue
            candidates.append(raw)

        # Category filter (applied before CLOB calls to save quota)
        if self.category_filter:
            filtered = []
            for raw in candidates:
                cat = _map_category(raw.get("tags") or [])
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

    # ------------------------------------------------------------------
    # Live order placement (requires POLYMARKET_PRIVATE_KEY + py-clob-client)
    # ------------------------------------------------------------------

    def _get_clob_client(self):
        """Return a configured ClobClient. Raises RuntimeError if credentials are missing."""
        try:
            from py_clob_client.client import ClobClient  # type: ignore
        except ImportError:
            raise RuntimeError(
                "py-clob-client is not installed. Run: pip install py-clob-client"
            )
        from config.settings import settings
        key = settings.POLYMARKET_PRIVATE_KEY
        if not key:
            raise RuntimeError(
                "POLYMARKET_PRIVATE_KEY is not set. Add your Ethereum private key to .env."
            )
        return ClobClient(
            host="https://clob.polymarket.com",
            chain_id=settings.POLYMARKET_CHAIN_ID,
            private_key=key,
            signature_type=0,  # EOA
        )

    def place_order(self, token_id: str, side: str, price_cents: int, count: int) -> dict:
        """
        Place a limit buy order on Polymarket CLOB.

        Args:
            token_id:    YES or NO token ID from market metadata.
            side:        "buy" (always buying a leg in an arb).
            price_cents: Price in integer cents (1–99).
            count:       Number of shares to buy.

        Returns dict with at least {"order_id": str, "status": str}.
        Raises RuntimeError on failure.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore

        client = self._get_clob_client()
        price = price_cents / 100.0
        order_args = OrderArgs(token_id=token_id, price=price, size=float(count), side="BUY")
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        # py-clob-client raises on failure; normalise response
        order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id", "")
        status = resp.get("status", "placed")
        return {"order_id": order_id, "status": status, "raw": resp}

    def cancel_order(self, order_id: str) -> None:
        """Cancel an open Polymarket order by ID."""
        client = self._get_clob_client()
        client.cancel(order_id=order_id)
