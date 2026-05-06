"""
Klient CLOB WebSocket - real-time order book i trades.

Architektura:
  - jedno trwałe połączenie WS do `wss://ws-subscriptions-clob.polymarket.com/ws/market`
  - wysyłamy PING co 10 sekund (wymagane przez serwer, inaczej rozłącza)
  - auto-reconnect z exponential backoff przy zerwaniu połączenia
  - subskrybujemy listę asset_ids; można je dynamicznie dodawać/usuwać
  - po reconnect ponownie subskrybujemy wszystkie znane tokeny

Wzorzec producer/consumer:
  - WebSocketManager (tu) jest producentem: wkłada zdarzenia do asyncio.Queue
  - Consumer (główna pętla bota) wyjmuje z kolejki i przekazuje do detektora
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import websockets
from loguru import logger
from websockets.exceptions import ConnectionClosed, WebSocketException

from .models import (
    OrderBook,
    Trade,
    apply_price_change,
    order_book_from_ws_book,
    trade_from_ws,
)


# -----------------------------------------------------------------------------
# Typy zdarzeń wkładanych do kolejki
# -----------------------------------------------------------------------------


@dataclass
class BookSnapshotEvent:
    """Pełny snapshot order booka dla tokena (event 'book')."""

    token_id: str
    book: OrderBook


@dataclass
class PriceChangeEvent:
    """Delta zmiany order booka. NEW_book już zawiera zaaplikowaną zmianę."""

    token_id: str
    new_book: OrderBook
    raw_change: dict[str, Any]


@dataclass
class TradeEvent:
    """Wykonany trade (event 'last_trade_price')."""

    trade: Trade


@dataclass
class TickSizeChangeEvent:
    """Zmiana minimalnego ticka cenowego (rzadkie, ale logujemy)."""

    token_id: str
    old_tick_size: float
    new_tick_size: float


WSEvent = (
    BookSnapshotEvent | PriceChangeEvent | TradeEvent | TickSizeChangeEvent
)


# -----------------------------------------------------------------------------
# Manager WebSocketa
# -----------------------------------------------------------------------------


class CLOBWebSocketManager:
    """
    Trwale trzyma połączenie z CLOB WS, parsuje wiadomości i wkłada
    znormalizowane zdarzenia do `self.events` (asyncio.Queue).
    """

    PING_INTERVAL_SECONDS = 10

    def __init__(
        self,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        events_queue_maxsize: int = 1000,
    ):
        self.ws_url = ws_url
        self.events: asyncio.Queue[WSEvent] = asyncio.Queue(maxsize=events_queue_maxsize)

        # Aktualnie zasubskrybowane token_id (ten sam set re-subscribe po reconnect)
        self._subscribed: set[str] = set()
        # Lokalny "stan" order booków - aktualizowany na bieżąco z book/price_change
        self._books: dict[str, OrderBook] = {}

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_attempt = 0
        self._send_lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # Publiczne API
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        """Uruchamia główną pętlę połączenia (blokująca - puścić jako task)."""
        self._running = True
        await self._connection_loop()

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def subscribe(self, token_ids: list[str]) -> None:
        """Dodaje tokeny do subskrypcji (i wysyła operation: subscribe na żywo)."""
        new = [t for t in token_ids if t and t not in self._subscribed]
        if not new:
            return
        self._subscribed.update(new)
        if self._ws and not self._ws.closed:
            await self._send_subscription_update(new, operation="subscribe")
            logger.info(f"WS: zasubskrybowano nowe tokeny ({len(new)}): {new[:3]}...")

    async def unsubscribe(self, token_ids: list[str]) -> None:
        existing = [t for t in token_ids if t in self._subscribed]
        if not existing:
            return
        for t in existing:
            self._subscribed.discard(t)
            self._books.pop(t, None)
        if self._ws and not self._ws.closed:
            await self._send_subscription_update(existing, operation="unsubscribe")
            logger.info(f"WS: odsubskrybowano tokeny ({len(existing)})")

    def get_book(self, token_id: str) -> OrderBook | None:
        """Aktualny lokalny stan order booka dla tokena."""
        return self._books.get(token_id)

    # -------------------------------------------------------------------------
    # Pętla połączenia z reconnectem
    # -------------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        while self._running:
            try:
                await self._run_once()
                self._reconnect_attempt = 0  # udane połączenie -> reset backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"WS: błąd połączenia: {exc!r}")

            if not self._running:
                break

            # Exponential backoff: 1s, 2s, 4s, 8s, ..., max 60s
            self._reconnect_attempt += 1
            wait = min(60, 2 ** min(self._reconnect_attempt, 6))
            logger.info(f"WS: ponawiam połączenie za {wait}s "
                        f"(próba {self._reconnect_attempt})")
            await asyncio.sleep(wait)

    async def _run_once(self) -> None:
        """Pojedyncza sesja połączenia: connect → subscribe → odbieraj."""
        logger.info(f"WS: łączenie z {self.ws_url}")

        async with websockets.connect(
            self.ws_url,
            ping_interval=None,   # robimy własny PING tekstowy (nie WS-frame)
            close_timeout=5,
            max_size=10 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            logger.success("WS: połączono")

            # Subskrybuj wszystko co miało być (przy starcie LUB po reconnect)
            if self._subscribed:
                await self._send_initial_subscription(list(self._subscribed))

            ping_task = asyncio.create_task(self._ping_loop())
            try:
                async for raw in ws:
                    await self._handle_raw(raw)
            finally:
                ping_task.cancel()
                with _suppress_cancelled():
                    await ping_task
                self._ws = None

    async def _ping_loop(self) -> None:
        """Wysyła PING co 10s; serwer wymaga tego, inaczej zerwie."""
        try:
            while True:
                await asyncio.sleep(self.PING_INTERVAL_SECONDS)
                async with self._send_lock:
                    if self._ws and not self._ws.closed:
                        await self._ws.send("PING")
        except (ConnectionClosed, WebSocketException, asyncio.CancelledError):
            pass

    # -------------------------------------------------------------------------
    # Wysyłanie wiadomości
    # -------------------------------------------------------------------------

    async def _send_initial_subscription(self, token_ids: list[str]) -> None:
        payload = {
            "assets_ids": token_ids,
            "type": "market",
            "initial_dump": True,
            "level": 2,
        }
        async with self._send_lock:
            assert self._ws
            await self._ws.send(json.dumps(payload))
        logger.info(f"WS: wysłano subskrypcję dla {len(token_ids)} tokenów")

    async def _send_subscription_update(
        self, token_ids: list[str], operation: str
    ) -> None:
        payload = {
            "operation": operation,
            "assets_ids": token_ids,
            "level": 2,
        }
        async with self._send_lock:
            assert self._ws
            await self._ws.send(json.dumps(payload))

    # -------------------------------------------------------------------------
    # Odbieranie i parsowanie
    # -------------------------------------------------------------------------

    async def _handle_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")

        # PONG od serwera - ignorujemy
        if raw.strip() in ("PONG", "pong"):
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"WS: nie-JSON wiadomość: {raw[:200]!r}")
            return

        # Czasem przychodzi tablica (batch zdarzeń), czasem pojedynczy obiekt
        if isinstance(data, list):
            for item in data:
                await self._handle_event(item)
        elif isinstance(data, dict):
            await self._handle_event(data)

    async def _handle_event(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("event_type")

        if event_type == "book":
            book = order_book_from_ws_book(msg)
            self._books[book.token_id] = book
            await self._enqueue(BookSnapshotEvent(token_id=book.token_id, book=book))
            return

        if event_type == "price_change":
            # Wiadomość zawiera listę zmian; każda ma własny asset_id
            for change in msg.get("price_changes", []):
                token_id = str(change.get("asset_id", ""))
                if not token_id:
                    continue
                old_book = self._books.get(token_id)
                if not old_book:
                    # Brak snapshotu - czekamy na 'book' przed deltami
                    continue
                new_book = apply_price_change(old_book, change)
                self._books[token_id] = new_book
                await self._enqueue(PriceChangeEvent(
                    token_id=token_id, new_book=new_book, raw_change=change,
                ))
            return

        if event_type == "last_trade_price":
            trade = trade_from_ws(msg)
            await self._enqueue(TradeEvent(trade=trade))
            return

        if event_type == "tick_size_change":
            await self._enqueue(TickSizeChangeEvent(
                token_id=str(msg.get("asset_id", "")),
                old_tick_size=float(msg.get("old_tick_size", 0) or 0),
                new_tick_size=float(msg.get("new_tick_size", 0) or 0),
            ))
            return

        # Inne zdarzenia (best_bid_ask, new_market, market_resolved) - logujemy
        if event_type:
            logger.debug(f"WS: nieobsługiwane zdarzenie '{event_type}'")

    async def _enqueue(self, event: WSEvent) -> None:
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            # Detektor nie nadąża - drop najstarszego, żeby nowy się zmieścił
            try:
                self.events.get_nowait()
            except asyncio.QueueEmpty:
                pass
            await self.events.put(event)
            logger.warning("WS: kolejka zdarzeń pełna - odrzucono najstarsze")


# -----------------------------------------------------------------------------
# Helper
# -----------------------------------------------------------------------------


from contextlib import contextmanager


@contextmanager
def _suppress_cancelled():
    try:
        yield
    except asyncio.CancelledError:
        pass
