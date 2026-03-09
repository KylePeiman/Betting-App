"""Mock fetcher — generates synthetic market data for testing without consuming API quota."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
import random

from .base import BaseFetcher, Market, Selection


# Realistic synthetic events with slightly imbalanced odds to trigger EV detection
_MOCK_EVENTS = [
    {
        "id": "mock_nba_001",
        "category": "sports/basketball",
        "event_name": "Boston Celtics vs Miami Heat",
        "selections": [
            {"name": "Boston Celtics", "odds": 1.72},
            {"name": "Miami Heat", "odds": 2.18},
        ],
    },
    {
        "id": "mock_nba_002",
        "category": "sports/basketball",
        "event_name": "LA Lakers vs Golden State Warriors",
        "selections": [
            {"name": "LA Lakers", "odds": 2.05},
            {"name": "Golden State Warriors", "odds": 1.83},
        ],
    },
    {
        "id": "mock_nhl_001",
        "category": "sports/icehockey",
        "event_name": "Toronto Maple Leafs vs Montreal Canadiens",
        "selections": [
            {"name": "Toronto Maple Leafs", "odds": 1.65},
            {"name": "Montreal Canadiens", "odds": 2.40},
        ],
    },
    {
        "id": "mock_soccer_001",
        "category": "sports/soccer",
        "event_name": "Arsenal vs Chelsea",
        "selections": [
            {"name": "Arsenal", "odds": 2.10},
            {"name": "Draw", "odds": 3.40},
            {"name": "Chelsea", "odds": 3.50},
        ],
    },
    {
        "id": "mock_soccer_002",
        "category": "sports/soccer",
        "event_name": "Manchester City vs Liverpool",
        "selections": [
            {"name": "Manchester City", "odds": 1.95},
            {"name": "Draw", "odds": 3.60},
            {"name": "Liverpool", "odds": 3.80},
        ],
    },
    {
        "id": "mock_mma_001",
        "category": "sports/mma",
        "event_name": "UFC 310: Fighter A vs Fighter B",
        "selections": [
            {"name": "Fighter A", "odds": 1.55},
            {"name": "Fighter B", "odds": 2.55},
        ],
    },
    {
        "id": "mock_tennis_001",
        "category": "sports/tennis",
        "event_name": "Alcaraz vs Sinner — ATP Final",
        "selections": [
            {"name": "Carlos Alcaraz", "odds": 1.88},
            {"name": "Jannik Sinner", "odds": 1.98},
        ],
    },
    {
        "id": "mock_politics_001",
        "category": "politics",
        "event_name": "UK General Election — Next PM",
        "selections": [
            {"name": "Labour", "odds": 1.30},
            {"name": "Conservative", "odds": 4.50},
            {"name": "Other", "odds": 12.00},
        ],
    },
]


class MockFetcher(BaseFetcher):
    """
    Returns synthetic market data with realistic odds.
    Use for development/testing — does not consume any API quota.
    Run with: --sources mock
    """

    name = "mock"

    def get_markets(self, **kwargs) -> list[Market]:
        now = datetime.now(timezone.utc)
        markets = []
        for i, event in enumerate(_MOCK_EVENTS):
            selections = [
                Selection(name=s["name"], odds=s["odds"], metadata={"bookmaker": "mock_book"})
                for s in event["selections"]
            ]
            markets.append(Market(
                id=event["id"],
                category=event["category"],
                event_name=event["event_name"],
                starts_at=now + timedelta(days=random.randint(1, 7)),
                selections=selections,
                source=self.name,
            ))
        return markets

    def get_odds(self, market_id: str, **kwargs) -> Market:
        for event in _MOCK_EVENTS:
            if event["id"] == market_id:
                selections = [
                    Selection(name=s["name"], odds=s["odds"])
                    for s in event["selections"]
                ]
                return Market(
                    id=market_id,
                    category=event["category"],
                    event_name=event["event_name"],
                    starts_at=None,
                    selections=selections,
                    source=self.name,
                )
        raise ValueError(f"Mock market '{market_id}' not found.")
