"""TheOddsAPI fetcher — sports + non-sports markets."""
from __future__ import annotations
import httpx
from datetime import datetime, timezone
from typing import Any

from .base import BaseFetcher, Market, Selection
from config.settings import settings


class OddsAPIFetcher(BaseFetcher):
    """
    Fetches markets and odds from TheOddsAPI (https://the-odds-api.com).
    Covers sports, and on higher tiers: politics, entertainment, esports.
    """

    name = "odds_api"
    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(self):
        self.api_key = settings.ODDS_API_KEY
        self.regions = settings.ODDS_API_REGIONS
        self.markets_param = settings.ODDS_API_MARKETS

    def _get(self, path: str, params: dict | None = None) -> Any:
        if params is None:
            params = {}
        params["apiKey"] = self.api_key
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{self.BASE_URL}{path}", params=params)
            resp.raise_for_status()
            return resp.json()

    def _list_sports(self) -> list[dict]:
        return self._get("/sports")

    def get_markets(self, sport_key: str = "upcoming", **kwargs) -> list[Market]:
        """
        Returns available events/markets for a given sport key.
        Use sport_key='upcoming' to get next events across all sports.
        """
        sports = self._list_sports()
        markets: list[Market] = []

        for sport in sports:
            try:
                events = self._get(f"/sports/{sport['key']}/odds", params={
                    "regions": self.regions,
                    "markets": self.markets_param,
                    "oddsFormat": "decimal",
                })
            except httpx.HTTPStatusError:
                continue

            for event in events:
                selections = []
                for bookmaker in event.get("bookmakers", []):
                    for market in bookmaker.get("markets", []):
                        for outcome in market.get("outcomes", []):
                            selections.append(Selection(
                                name=outcome["name"],
                                odds=float(outcome["price"]),
                                metadata={"bookmaker": bookmaker["key"], "market_type": market["key"]},
                            ))
                        break  # first bookmaker only for deduplication
                    break

                commence = event.get("commence_time")
                starts_at = datetime.fromisoformat(commence.rstrip("Z")).replace(tzinfo=timezone.utc) if commence else None

                markets.append(Market(
                    id=event["id"],
                    category=f"sports/{sport['group'].lower()}",
                    event_name=f"{event.get('home_team', '')} vs {event.get('away_team', '')}",
                    starts_at=starts_at,
                    selections=selections,
                    source=self.name,
                    metadata={"sport_key": sport["key"], "sport_title": sport["title"]},
                ))

        return markets

    def get_odds(self, market_id: str, **kwargs) -> Market:
        raise NotImplementedError("Use get_markets() to retrieve odds by event ID from TheOddsAPI.")
