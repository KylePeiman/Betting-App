"""
Kalshi WebSocket client — authenticated, orderbook feed.

Streams real-time orderbook snapshots/deltas for subscribed market tickers
and writes the best YES ask price (cents) to PriceCache.

Kalshi WS docs: https://trading-api.readme.io/reference/websocket
Auth: RSA-PSS SHA-256 — same headers as REST API.
      Sign string: {timestamp_ms}GET/trade-api/ws/v2
      Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .price_cache import PriceCache

logger = logging.getLogger(__name__)

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"   # used in signing
RECONNECT_DELAY_S = 5


def _make_auth_headers(private_key) -> dict:
    """Build RSA-PSS auth headers for the WebSocket handshake (same as REST)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from config.settings import settings

    ts = str(int(time.time() * 1000))
    message = (ts + "GET" + WS_PATH).encode()
    sig_bytes = private_key.sign(
        message,
        asym_padding.PSS(
            mgf=asym_padding.MGF1(hashes.SHA256()),
            salt_length=asym_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": settings.KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig_bytes).decode(),
    }


class KalshiWsClient:
    """
    Runs in a background thread. Connects to Kalshi WS, subscribes to
    orderbook_delta for the given tickers, and writes the best YES ask
    (in cents) to PriceCache.

    Tickers can be updated at runtime via subscribe() / unsubscribe().
    """

    def __init__(self, cache: "PriceCache") -> None:
        self._cache = cache
        self._lock = threading.Lock()
        self._subscribed: set[str] = set()
        self._pending_sub: set[str] = set()      # tickers to add on next reconnect or via ws
        self._pending_unsub: set[str] = set()    # tickers to remove
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._private_key = None
        self._msg_id = 0
        self._ws_queue: asyncio.Queue | None = None  # send queue for live ws commands
        self._loop: asyncio.AbstractEventLoop | None = None

    def _load_key(self) -> None:
        from config.settings import settings
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        if not settings.KALSHI_PRIVATE_KEY_PATH:
            raise ValueError("KALSHI_PRIVATE_KEY_PATH not set")
        with open(settings.KALSHI_PRIVATE_KEY_PATH, "rb") as fh:
            self._private_key = load_pem_private_key(fh.read(), password=None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._load_key()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="kalshi-ws", daemon=True
        )
        self._thread.start()
        logger.info("[KalshiWS] started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[KalshiWS] stopped")

    def subscribe(self, tickers: set[str]) -> None:
        """Subscribe to orderbook_delta for the given tickers."""
        new = tickers - self._subscribed
        if not new:
            return
        with self._lock:
            self._pending_sub |= new
        # If there's a live ws loop, enqueue the command immediately
        if self._loop and self._ws_queue:
            self._loop.call_soon_threadsafe(
                self._ws_queue.put_nowait,
                ("subscribe", list(new)),
            )
        logger.info("[KalshiWS] queued subscribe: %s", sorted(new))

    def unsubscribe(self, tickers: set[str]) -> None:
        """Unsubscribe from orderbook_delta for the given tickers."""
        with self._lock:
            self._pending_unsub |= tickers & self._subscribed
        if self._loop and self._ws_queue:
            self._loop.call_soon_threadsafe(
                self._ws_queue.put_nowait,
                ("unsubscribe", list(tickers)),
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect_loop())
        finally:
            self._loop = None
            loop.close()

    async def _connect_loop(self) -> None:
        import websockets

        self._ws_queue = asyncio.Queue()

        while not self._stop_event.is_set():
            try:
                headers = _make_auth_headers(self._private_key)
                async with websockets.connect(WS_URL, additional_headers=headers, ping_interval=20) as ws:
                    logger.info("[KalshiWS] connected")

                    # Re-subscribe to previously subscribed tickers after reconnect
                    with self._lock:
                        all_tickers = self._subscribed | self._pending_sub
                        self._pending_sub.clear()
                    if all_tickers:
                        await self._send_subscribe(ws, list(all_tickers))

                    recv_task = asyncio.ensure_future(self._recv_loop(ws))
                    send_task = asyncio.ensure_future(self._send_loop(ws))

                    done, pending = await asyncio.wait(
                        [recv_task, send_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    for t in done:
                        exc = t.exception()
                        if exc:
                            raise exc

            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("[KalshiWS] disconnected (%s), reconnecting in %ds", exc, RECONNECT_DELAY_S)
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            if self._stop_event.is_set():
                break
            self._handle(raw)

    async def _send_loop(self, ws) -> None:
        while not self._stop_event.is_set():
            cmd, tickers = await self._ws_queue.get()
            if cmd == "subscribe":
                await self._send_subscribe(ws, tickers)
            elif cmd == "unsubscribe":
                await self._send_unsubscribe(ws, tickers)

    async def _send_subscribe(self, ws, tickers: list[str]) -> None:
        if not tickers:
            return
        msg = json.dumps({
            "id": self._next_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        })
        await ws.send(msg)
        with self._lock:
            self._subscribed |= set(tickers)
        logger.info("[KalshiWS] subscribed to %d tickers", len(tickers))

    async def _send_unsubscribe(self, ws, tickers: list[str]) -> None:
        if not tickers:
            return
        msg = json.dumps({
            "id": self._next_id(),
            "cmd": "unsubscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        })
        await ws.send(msg)
        with self._lock:
            self._subscribed -= set(tickers)
        logger.info("[KalshiWS] unsubscribed from %d tickers", len(tickers))

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        if msg_type not in ("orderbook_snapshot", "orderbook_delta"):
            return

        ticker = msg.get("market_ticker")
        if not ticker:
            return

        if msg_type == "orderbook_snapshot":
            self._process_snapshot(ticker, msg)
        else:
            self._process_delta(ticker, msg)

    def _process_snapshot(self, ticker: str, msg: dict) -> None:
        """
        Full orderbook snapshot. Format:
          {"type": "orderbook_snapshot", "market_ticker": "...", "seq": N,
           "yes": [[price_cents, quantity], ...],
           "no":  [[price_cents, quantity], ...]}

        YES asks are contracts offered for sale at a given price.
        The best (lowest) yes ask is the minimum price in the yes list.
        NO asks work the same way on the no side.
        """
        self._update_yes_ask(ticker, msg.get("yes", []))
        self._update_no_ask(ticker, msg.get("no", []))

    def _process_delta(self, ticker: str, msg: dict) -> None:
        """
        Orderbook delta update. Same structure as snapshot but partial.
        Quantity=0 means the level was removed.

        Kalshi sends the full updated side on each delta so we can
        recompute the best ask directly from the delta message.
        """
        yes_levels = msg.get("yes", [])
        if yes_levels:
            self._update_yes_ask(ticker, yes_levels)
        no_levels = msg.get("no", [])
        if no_levels:
            self._update_no_ask(ticker, no_levels)

    def _best_ask(self, levels: list) -> float | None:
        """Return the minimum non-zero price from an orderbook side, or None."""
        best = None
        for entry in levels:
            try:
                price, qty = entry[0], entry[1]
                if qty > 0 and (best is None or price < best):
                    best = price
            except (IndexError, TypeError):
                continue
        return float(best) if best is not None else None

    def _update_yes_ask(self, ticker: str, levels: list) -> None:
        best = self._best_ask(levels)
        if best is not None:
            self._cache.set_yes_ask(ticker, best)

    def _update_no_ask(self, ticker: str, levels: list) -> None:
        best = self._best_ask(levels)
        if best is not None:
            self._cache.set_no_ask(ticker, best)
