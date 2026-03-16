"""Abstract base fetcher — defines the generic market/odds interface all fetchers must implement."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Selection:
    name: str
    odds: float  # decimal odds
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Market:
    id: str
    category: str          # e.g. "sports/soccer", "politics", "esports", "entertainment"
    event_name: str
    starts_at: datetime | None
    selections: list[Selection]
    source: str            # fetcher name
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseFetcher(ABC):
    """All fetchers must implement this interface."""

    name: str = "base"

    @abstractmethod
    def get_markets(self, **kwargs) -> list[Market]:
        """Return available markets from this source."""
        ...

    @abstractmethod
    def get_odds(self, market_id: str, **kwargs) -> Market:
        """Return current odds for a specific market."""
        ...
