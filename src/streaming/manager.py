"""
StreamManager — unified interface for all WebSocket feeds.

Usage:
    manager = StreamManager()
    manager.start()

    # Add/remove Kalshi tickers dynamically as markets enter/leave the window
    manager.subscribe_kalshi({"KXETH-25MAR14-T2024.99", ...})
    manager.unsubscribe_kalshi({"KXETH-25MAR14-T2024.99"})

    # Read prices (returns None if not yet received)
    spot = manager.cache.get_spot("XBTUSD")          # float USD
    ask  = manager.cache.get_yes_ask("KXETH-...")    # float cents

    manager.stop()
"""
from __future__ import annotations

import logging

from .price_cache import PriceCache
from .kraken_ws import KrakenWsClient
from .kalshi_ws import KalshiWsClient

logger = logging.getLogger(__name__)


class StreamManager:
    def __init__(self) -> None:
        self.cache = PriceCache()
        self._kraken = KrakenWsClient(self.cache)
        self._kalshi = KalshiWsClient(self.cache)
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._kraken.start()
        self._kalshi.start()
        self._running = True
        logger.info("[StreamManager] started")

    def stop(self) -> None:
        self._kraken.stop()
        self._kalshi.stop()
        self._running = False
        logger.info("[StreamManager] stopped")

    # ------------------------------------------------------------------
    # Kalshi subscription management
    # ------------------------------------------------------------------

    def subscribe_kalshi(self, tickers: set[str]) -> None:
        """Subscribe to live yes_ask for these market tickers."""
        self._kalshi.subscribe(tickers)

    def unsubscribe_kalshi(self, tickers: set[str]) -> None:
        """Unsubscribe from these market tickers."""
        self._kalshi.unsubscribe(tickers)
