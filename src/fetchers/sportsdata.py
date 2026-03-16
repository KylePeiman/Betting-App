"""SportsDataIO fetcher — stats enrichment."""
from __future__ import annotations
import httpx
from typing import Any

from .base import BaseFetcher, Market, Selection
from config.settings import settings


class SportsDataFetcher(BaseFetcher):
    """
    SportsDataIO — primarily for stats enrichment.
    Returns minimal market objects; mainly useful for historical data and team stats.
    """

    name = "sportsdata"
    BASE_URL = "https://api.sportsdata.io/v3"

    def __init__(self):
        self.api_key = settings.SPORTSDATA_API_KEY

    def _get(self, sport: str, path: str) -> Any:
        url = f"{self.BASE_URL}/{sport}{path}"
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, params={"key": self.api_key})
            resp.raise_for_status()
            return resp.json()

    def get_markets(self, sport: str = "nfl", **kwargs) -> list[Market]:
        """Returns upcoming games as markets (no odds — use for stats context)."""
        try:
            games = self._get(sport, "/scores/json/UpcomingGames")
        except Exception as exc:
            print(f"SportsDataIO error: {exc}")
            return []

        markets = []
        for game in games[:20]:
            markets.append(Market(
                id=str(game.get("GameKey", game.get("GameId", ""))),
                category=f"sports/{sport}",
                event_name=f"{game.get('AwayTeam', '')} vs {game.get('HomeTeam', '')}",
                starts_at=None,
                selections=[
                    Selection(name=game.get("AwayTeam", "Away"), odds=0.0),
                    Selection(name=game.get("HomeTeam", "Home"), odds=0.0),
                ],
                source=self.name,
                metadata=game,
            ))
        return markets

    def get_odds(self, market_id: str, **kwargs) -> Market:
        raise NotImplementedError("SportsDataIO is used for stats enrichment only — no live odds.")
