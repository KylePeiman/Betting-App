"""Betfair Exchange fetcher — broadest market variety."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

from .base import BaseFetcher, Market, Selection
from config.settings import settings


class BetfairFetcher(BaseFetcher):
    """
    Fetches markets and odds from Betfair Exchange.
    Requires betfairlightweight and a UK/EU account with API access.
    Covers: sports, politics, reality TV, financials, esports, and more.
    """

    name = "betfair"

    def __init__(self):
        self._client = None
        self._setup()

    def _setup(self):
        try:
            import betfairlightweight
            self._client = betfairlightweight.APIClient(
                username=settings.BETFAIR_USERNAME,
                password=settings.BETFAIR_PASSWORD,
                app_key=settings.BETFAIR_APP_KEY,
                certs=settings.BETFAIR_CERTS_PATH or None,
            )
            self._client.login()
        except ImportError:
            print("Warning: betfairlightweight not installed. BetfairFetcher unavailable.")
        except Exception as exc:
            print(f"Warning: Betfair login failed: {exc}. BetfairFetcher unavailable.")

    def get_markets(self, event_type_ids: list[str] | None = None, **kwargs) -> list[Market]:
        if self._client is None:
            return []

        try:
            from betfairlightweight.filters import market_filter
        except ImportError:
            return []

        try:
            mf = market_filter(event_type_ids=event_type_ids) if event_type_ids else market_filter()
            catalogues = self._client.betting.list_market_catalogue(
                filter=mf,
                market_projection=["EVENT_TYPE", "MARKET_NAME", "MARKET_START_TIME", "RUNNER_DESCRIPTION"],
                max_results=50,
            )
        except Exception as exc:
            print(f"Betfair list_market_catalogue error: {exc}")
            return []

        markets = []
        for cat in catalogues:
            runners = cat.runners or []
            selections = [Selection(name=r.runner_name, odds=0.0) for r in runners]
            markets.append(Market(
                id=cat.market_id,
                category=f"betfair/{(cat.event_type.name if cat.event_type else 'unknown').lower()}",
                event_name=cat.market_name,
                starts_at=cat.market_start_time,
                selections=selections,
                source=self.name,
            ))
        return markets

    def get_odds(self, market_id: str, **kwargs) -> Market:
        if self._client is None:
            raise RuntimeError("Betfair client not initialized.")

        from betfairlightweight.filters import price_projection, price_data
        books = self._client.betting.list_market_book(
            market_ids=[market_id],
            price_projection=price_projection(price_data=price_data(["EX_BEST_OFFERS"])),
        )
        if not books:
            raise ValueError(f"No market book returned for {market_id}")

        book = books[0]
        selections = []
        for runner in book.runners:
            best_back = runner.ex.available_to_back
            odds = best_back[0].price if best_back else 0.0
            selections.append(Selection(name=str(runner.selection_id), odds=odds))

        return Market(
            id=market_id,
            category="betfair/unknown",
            event_name=market_id,
            starts_at=None,
            selections=selections,
            source=self.name,
        )
