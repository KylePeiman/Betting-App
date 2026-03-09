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
    Free tier: ~500 requests/month. Each get_markets() call costs 1 request per sport key.
    """

    name = "odds_api"
    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(self):
        self.api_key = settings.ODDS_API_KEY
        self.regions = settings.ODDS_API_REGIONS
        self.markets_param = settings.ODDS_API_MARKETS
        self.sport_keys = settings.ODDS_API_SPORT_KEYS

    def _get(self, path: str, params: dict | None = None) -> tuple[Any, dict]:
        """Returns (response_json, response_headers)."""
        if params is None:
            params = {}
        params["apiKey"] = self.api_key
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{self.BASE_URL}{path}", params=params)
            resp.raise_for_status()
            return resp.json(), dict(resp.headers)

    def list_sports(self, active_only: bool = True) -> list[dict]:
        """
        Return all sports from TheOddsAPI. Costs 1 request.
        Use this to discover valid sport keys; don't call in automated pipelines.
        """
        data, _ = self._get("/sports", {"all": "false" if active_only else "true"})
        return data

    def get_markets(self, sport_keys: list[str] | None = None, **kwargs) -> list[Market]:
        """
        Fetch odds for the given sport keys (one API request per key).
        Defaults to settings.ODDS_API_SPORT_KEYS — keep this list short to preserve quota.
        """
        if sport_keys is None:
            sport_keys = self.sport_keys

        markets: list[Market] = []

        for sport_key in sport_keys:
            try:
                events, headers = self._get(f"/sports/{sport_key}/odds", params={
                    "regions": self.regions,
                    "markets": self.markets_param,
                    "oddsFormat": "decimal",
                })
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 422:
                    # Sport key not found or not active on this plan — skip silently
                    continue
                if status == 429:
                    remaining = exc.response.headers.get("x-requests-remaining", "?")
                    print(f"  Rate limited fetching '{sport_key}' (remaining quota: {remaining}). Stopping.")
                    break
                print(f"  HTTP {status} fetching '{sport_key}': {exc}")
                continue
            except httpx.RequestError as exc:
                print(f"  Network error fetching '{sport_key}': {exc}")
                continue

            remaining = headers.get("x-requests-remaining", "?")
            used = headers.get("x-requests-used", "?")

            if not events:
                continue

            for event in events:
                selections = self._parse_selections(event)
                if not selections:
                    continue

                commence = event.get("commence_time")
                starts_at = (
                    datetime.fromisoformat(commence.rstrip("Z")).replace(tzinfo=timezone.utc)
                    if commence else None
                )
                home = event.get("home_team", "")
                away = event.get("away_team", "")
                sport_group = sport_key.split("_")[0]

                markets.append(Market(
                    id=event["id"],
                    category=f"sports/{sport_group}",
                    event_name=f"{home} vs {away}" if home and away else event.get("id", ""),
                    starts_at=starts_at,
                    selections=selections,
                    source=self.name,
                    metadata={"sport_key": sport_key},
                ))

            print(f"  [{sport_key}] {len(events)} events  |  quota used={used} remaining={remaining}")

        return markets

    def _parse_selections(self, event: dict) -> list[Selection]:
        """Extract selections from the first bookmaker that has h2h markets."""
        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                return [
                    Selection(
                        name=o["name"],
                        odds=float(o["price"]),
                        metadata={"bookmaker": bookmaker["key"]},
                    )
                    for o in market.get("outcomes", [])
                ]
        return []

    def get_odds(self, market_id: str, **kwargs) -> Market:
        raise NotImplementedError(
            "TheOddsAPI identifies events by sport key, not a standalone market ID. "
            "Use get_markets(sport_keys=[...]) instead."
        )
