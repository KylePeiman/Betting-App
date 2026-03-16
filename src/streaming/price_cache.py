"""Thread-safe shared cache for streaming prices."""
from __future__ import annotations

import threading
import time


class PriceCache:
    """
    Thread-safe store for the latest prices from both WebSocket feeds.

    Spot prices: keyed by Kraken pair (e.g. "XBTUSD")
    Yes-ask:     keyed by Kalshi market ticker (e.g. "KXETH-25MAR14-T2024.99"),
                 stored as float cents (1–99).

    update_event is set whenever any price changes. The main loop waits on it
    (with a timeout) instead of sleeping a fixed interval, so the scanner fires
    within milliseconds of any price update rather than on a fixed 5-second poll.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._spot: dict[str, tuple[float, float]] = {}       # pair → (price, ts)
        self._yes_ask: dict[str, tuple[float, float]] = {}    # ticker → (cents, ts)
        self._no_ask: dict[str, tuple[float, float]] = {}     # ticker → (cents, ts)
        self.update_event = threading.Event()                  # signalled on any price change
        self._triggered_pairs: set[str] = set()               # Kraken pairs updated since last pop
        self._triggered_tickers: set[str] = set()             # Kalshi tickers updated since last pop

    # ------------------------------------------------------------------
    # Spot prices (Kraken)
    # ------------------------------------------------------------------

    def set_spot(self, pair: str, price: float) -> None:
        with self._lock:
            self._spot[pair] = (price, time.time())
            self._triggered_pairs.add(pair)
        self.update_event.set()

    def get_spot(self, pair: str) -> float | None:
        with self._lock:
            entry = self._spot.get(pair)
            return entry[0] if entry else None

    def spot_age(self, pair: str) -> float:
        """Seconds since last spot update, or infinity if never seen."""
        with self._lock:
            entry = self._spot.get(pair)
            return time.time() - entry[1] if entry else float("inf")

    # ------------------------------------------------------------------
    # Yes-ask prices (Kalshi orderbook)
    # ------------------------------------------------------------------

    def set_yes_ask(self, ticker: str, cents: float) -> None:
        with self._lock:
            self._yes_ask[ticker] = (cents, time.time())
            self._triggered_tickers.add(ticker)
        self.update_event.set()

    def get_yes_ask(self, ticker: str) -> float | None:
        with self._lock:
            entry = self._yes_ask.get(ticker)
            return entry[0] if entry else None

    def yes_ask_age(self, ticker: str) -> float:
        """Seconds since last yes_ask update, or infinity if never seen."""
        with self._lock:
            entry = self._yes_ask.get(ticker)
            return time.time() - entry[1] if entry else float("inf")

    def set_no_ask(self, ticker: str, cents: float) -> None:
        with self._lock:
            self._no_ask[ticker] = (cents, time.time())
            self._triggered_tickers.add(ticker)
        self.update_event.set()

    def get_no_ask(self, ticker: str) -> float | None:
        with self._lock:
            entry = self._no_ask.get(ticker)
            return entry[0] if entry else None

    def no_ask_age(self, ticker: str) -> float:
        """Seconds since last no_ask update, or infinity if never seen."""
        with self._lock:
            entry = self._no_ask.get(ticker)
            return time.time() - entry[1] if entry else float("inf")

    # ------------------------------------------------------------------
    # Triggered set (for per-ticker reactive scanning)
    # ------------------------------------------------------------------

    def pop_triggered(self) -> tuple[set[str], set[str]]:
        """
        Return and clear the sets of Kraken pairs and Kalshi tickers that have
        received at least one update since the last call.

        Used by the main loop to restrict the scanner to only the markets
        that are relevant to what just changed, rather than scanning everything.
        """
        with self._lock:
            pairs = self._triggered_pairs.copy()
            tickers = self._triggered_tickers.copy()
            self._triggered_pairs.clear()
            self._triggered_tickers.clear()
        return pairs, tickers

    # ------------------------------------------------------------------
    # Snapshot (for debugging)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "spot": {k: v[0] for k, v in self._spot.items()},
                "yes_ask": {k: v[0] for k, v in self._yes_ask.items()},
                "no_ask": {k: v[0] for k, v in self._no_ask.items()},
            }
