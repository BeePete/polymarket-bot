"""
Modele danych dla Polymarket - czyste dataclasses.

Polymarket API zwraca trochę "bałaganiarskie" odpowiedzi (czasem string,
czasem liczba, czasem JSON-w-stringu). Te klasy normalizują dane do
spójnej, typowanej formy używanej w reszcie bota.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# Strona rynku: której tokenowi przyglądamy się jako "blisko 99.9¢"
SIDE_YES = "YES"
SIDE_NO = "NO"


@dataclass
class Market:
    """Pojedynczy rynek (np. 'Bitcoin above $70,000 on May 6')."""

    condition_id: str           # unikalny identyfikator rynku
    question: str               # treść pytania
    slug: str
    token_yes_id: str | None    # id tokena YES (do subskrypcji WS)
    token_no_id: str | None     # id tokena NO
    end_date: str | None        # ISO datetime kiedy się zamyka
    accepting_orders: bool = True
    closed: bool = False
    active: bool = True

    @property
    def all_token_ids(self) -> list[str]:
        return [t for t in (self.token_yes_id, self.token_no_id) if t]


@dataclass
class Event:
    """Event = grupa rynków (np. 'Bitcoin above ___ on May 6')."""

    id: str
    slug: str
    title: str
    end_date: str | None
    closed: bool
    active: bool
    markets: list[Market] = field(default_factory=list)

    @property
    def end_datetime(self) -> datetime | None:
        if not self.end_date:
            return None
        try:
            # Polymarket zwraca daty w ISO 8601, czasem z 'Z', czasem z offsetem
            cleaned = self.end_date.replace("Z", "+00:00")
            return datetime.fromisoformat(cleaned)
        except (ValueError, TypeError):
            return None

    def hours_to_close(self, now: datetime | None = None) -> float | None:
        end = self.end_datetime
        if not end:
            return None
        now = now or datetime.now(timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        delta = end - now
        return delta.total_seconds() / 3600.0


# -----------------------------------------------------------------------------
# Parsery odpowiedzi Gamma API
# -----------------------------------------------------------------------------


def _parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return default


def _parse_clob_token_ids(raw: Any) -> tuple[str | None, str | None]:
    """
    Polymarket zwraca clobTokenIds bardzo nieregularnie:
      - czasem to lista: ["123", "456"]
      - czasem to string z JSON-em: "[\"123\", \"456\"]"
      - czasem null
    Pierwszy element to YES, drugi NO (zgodnie z `outcomes: ["Yes", "No"]`).
    """
    if not raw:
        return None, None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(raw, list) or len(raw) < 2:
        return None, None
    return str(raw[0]), str(raw[1])


def parse_market(raw: dict[str, Any]) -> Market:
    """Konwertuje surowy obiekt market z Gamma API do dataclassy Market."""
    token_yes, token_no = _parse_clob_token_ids(raw.get("clobTokenIds"))
    return Market(
        condition_id=raw.get("conditionId") or "",
        question=raw.get("question") or "",
        slug=raw.get("slug") or "",
        token_yes_id=token_yes,
        token_no_id=token_no,
        end_date=raw.get("endDate") or raw.get("endDateIso"),
        accepting_orders=_parse_bool(raw.get("acceptingOrders"), default=True),
        closed=_parse_bool(raw.get("closed"), default=False),
        active=_parse_bool(raw.get("active"), default=True),
    )


def parse_event(raw: dict[str, Any]) -> Event:
    """Konwertuje surowy obiekt event z Gamma API do dataclassy Event."""
    markets_raw = raw.get("markets") or []
    return Event(
        id=str(raw.get("id") or ""),
        slug=raw.get("slug") or "",
        title=raw.get("title") or "",
        end_date=raw.get("endDate"),
        closed=_parse_bool(raw.get("closed"), default=False),
        active=_parse_bool(raw.get("active"), default=True),
        markets=[parse_market(m) for m in markets_raw],
    )


# -----------------------------------------------------------------------------
# Reprezentacja order booka (z WebSocketa)
# -----------------------------------------------------------------------------


@dataclass
class OrderBookLevel:
    """Pojedynczy poziom cenowy: cena + suma shares na nim."""

    price: float
    size: float


@dataclass
class OrderBook:
    """
    Order book dla pojedynczego tokena.
    bids - posortowane malejąco po cenie (najlepszy bid = bids[0])
    asks - posortowane rosnąco po cenie (najlepszy ask = asks[0])
    """

    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp_ms: int = 0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def size_at_ask_price(self, price: float) -> float:
        """Suma shares wystawionych na sprzedaż na konkretnym poziomie ceny."""
        return sum(lvl.size for lvl in self.asks if abs(lvl.price - price) < 1e-9)

    def size_at_bid_price(self, price: float) -> float:
        return sum(lvl.size for lvl in self.bids if abs(lvl.price - price) < 1e-9)

    def total_ask_size_at_prices(self, prices: list[float]) -> float:
        prices_set = {round(p, 6) for p in prices}
        return sum(lvl.size for lvl in self.asks if round(lvl.price, 6) in prices_set)

    def total_bid_size_at_prices(self, prices: list[float]) -> float:
        prices_set = {round(p, 6) for p in prices}
        return sum(lvl.size for lvl in self.bids if round(lvl.price, 6) in prices_set)


def order_book_from_ws_book(payload: dict[str, Any]) -> OrderBook:
    """Konstruuje OrderBook z wiadomości typu 'book' (snapshot z WebSocketa)."""
    bids = [
        OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
        for b in payload.get("bids", [])
    ]
    asks = [
        OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
        for a in payload.get("asks", [])
    ]
    bids.sort(key=lambda lvl: -lvl.price)  # najlepszy (najwyższy) bid pierwszy
    asks.sort(key=lambda lvl: lvl.price)   # najlepszy (najniższy) ask pierwszy
    return OrderBook(
        token_id=str(payload.get("asset_id", "")),
        bids=bids,
        asks=asks,
        timestamp_ms=int(payload.get("timestamp", 0) or 0),
    )


def apply_price_change(book: OrderBook, change: dict[str, Any]) -> OrderBook:
    """
    Aplikuje pojedynczą zmianę z wiadomości price_change na order book.
    side='BUY' = bid (kupno), side='SELL' = ask (sprzedaż).
    size=0 oznacza usunięcie poziomu.
    Zwraca NOWY obiekt OrderBook (immutable update).
    """
    price = float(change["price"])
    size = float(change["size"])
    side = (change.get("side") or "").upper()

    if side == "BUY":
        levels = [lvl for lvl in book.bids if abs(lvl.price - price) >= 1e-9]
        if size > 0:
            levels.append(OrderBookLevel(price=price, size=size))
        levels.sort(key=lambda lvl: -lvl.price)
        return OrderBook(
            token_id=book.token_id, bids=levels, asks=book.asks,
            timestamp_ms=book.timestamp_ms,
        )

    if side == "SELL":
        levels = [lvl for lvl in book.asks if abs(lvl.price - price) >= 1e-9]
        if size > 0:
            levels.append(OrderBookLevel(price=price, size=size))
        levels.sort(key=lambda lvl: lvl.price)
        return OrderBook(
            token_id=book.token_id, bids=book.bids, asks=levels,
            timestamp_ms=book.timestamp_ms,
        )

    # Nieznany side - zwróć bez zmian
    return book


@dataclass
class Trade:
    """Wykonany trade - z wiadomości last_trade_price."""

    token_id: str
    price: float
    size: float
    side: str           # 'BUY' / 'SELL'
    timestamp_ms: int


def trade_from_ws(payload: dict[str, Any]) -> Trade:
    return Trade(
        token_id=str(payload.get("asset_id", "")),
        price=float(payload.get("price", 0)),
        size=float(payload.get("size", 0)),
        side=str(payload.get("side", "")).upper(),
        timestamp_ms=int(payload.get("timestamp", 0) or 0),
    )
