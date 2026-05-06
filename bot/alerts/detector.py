"""
Detektor 4 typów alertów - SERCE BOTA.

Logika jest celowo "pure" (bez I/O, bez bazy danych) żeby była łatwa
do przetestowania. Cooldown i deduplikację robi orchestrator (main loop)
korzystając z bazy danych - tu skupiamy się tylko na: "czy warunek
alertu jest spełniony?".

4 typy alertów (przypomnienie ze specyfikacji):
  A - "Ask topnieje": suma asków na 0.998/0.999 < 30000 shares
      Specjalnie: spadek o >5000 shares vs poprzednia suma -> ignoruj cooldown
  B - "Duży market buy": trade BUY o size >= 5000 na 0.998/0.999
  C - "Nowy sell order": wzrost size na asku 0.998/0.999 o >= 5000
  D - "Duży limit buy": wzrost size na bidzie 0.998/0.999 o >= 19000
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..polymarket.models import (
    SIDE_NO,
    SIDE_YES,
    OrderBook,
    Trade,
)


AlertType = Literal["A", "B", "C", "D"]


@dataclass
class Alert:
    """Wykryty alert - bez kontekstu cooldownu (orchestrator filtruje)."""

    alert_type: AlertType
    token_id: str
    side: str                       # 'YES' / 'NO'
    price: float                    # 0.998 albo 0.999 (cena której dotyczy)
    payload: dict                   # dane specyficzne dla typu (do formattera)
    bypass_cooldown: bool = False   # alert A z burst-drop -> True


@dataclass
class DetectorThresholds:
    """Progi alertów (z config.yaml)."""

    ask_melting_threshold: int = 30000
    ask_melting_burst_drop: int = 5000
    market_buy_min_size: int = 5000
    new_sell_order_min_size: int = 5000
    big_limit_buy_min_size: int = 19000


@dataclass
class Detector:
    """
    Stateful detektor: trzyma w pamięci poprzednie sumy asków per token,
    żeby móc wykrywać "burst drop" dla alertu A.
    """

    thresholds: DetectorThresholds
    monitored_prices: tuple[float, ...] = (0.998, 0.999)

    # Wewnętrzny stan - poprzednia suma asków na monitored_prices per token
    _last_ask_sum: dict[str, float] = field(default_factory=dict)

    # -------------------------------------------------------------------------
    # Klasyfikacja czy rynek jest "monitorowany" (blisko 99.9¢)
    # -------------------------------------------------------------------------

    def is_token_monitored(self, book: OrderBook) -> bool:
        """
        Token jest "monitorowany" jeśli któryś z monitored_prices występuje
        po stronie ASK (czyli ktoś chce go SPRZEDAĆ za 99.8¢ lub 99.9¢).
        Zgodnie z ustaleniem: liczy się tylko strona sell (najlepszy ask).
        """
        prices_set = {round(p, 6) for p in self.monitored_prices}
        return any(round(lvl.price, 6) in prices_set for lvl in book.asks)

    # -------------------------------------------------------------------------
    # Zdarzenie: zmiana order booka (book snapshot lub price_change)
    # -------------------------------------------------------------------------

    def check_book_change(
        self,
        token_id: str,
        side: str,
        old_book: OrderBook | None,
        new_book: OrderBook,
    ) -> list[Alert]:
        """
        Sprawdza alerty A, C, D dla zmiany order booka.

        Args:
            token_id: identyfikator tokena
            side: 'YES' albo 'NO' - która strona rynku
            old_book: poprzedni stan (None jeśli pierwszy snapshot)
            new_book: nowy stan
        """
        if not self.is_token_monitored(new_book):
            # Token "oddalił się" od 99.9¢ - czyścimy cache i nic nie alertujemy
            self._last_ask_sum.pop(token_id, None)
            return []

        alerts: list[Alert] = []
        prices = list(self.monitored_prices)

        # ---------- Alert A: Ask topnieje ----------
        new_ask_sum = new_book.total_ask_size_at_prices(prices)
        prev_sum = self._last_ask_sum.get(token_id)

        if new_ask_sum < self.thresholds.ask_melting_threshold:
            burst = (
                prev_sum is not None
                and (prev_sum - new_ask_sum) > self.thresholds.ask_melting_burst_drop
            )
            alerts.append(Alert(
                alert_type="A",
                token_id=token_id,
                side=side,
                price=self._dominant_monitored_ask_price(new_book, prices),
                payload={
                    "ask_sum": new_ask_sum,
                    "previous_sum": prev_sum,
                    "threshold": self.thresholds.ask_melting_threshold,
                },
                bypass_cooldown=burst,
            ))

        # Aktualizuj zapamiętaną sumę dopiero PO sprawdzeniu (żeby burst-drop
        # mógł porównać z poprzednią wartością)
        self._last_ask_sum[token_id] = new_ask_sum

        # ---------- Alerty C i D: nowe duże ordery na monitored prices ----------
        if old_book is not None:
            # Alert C: nowy sell order
            for price in prices:
                old = old_book.size_at_ask_price(price) if old_book else 0
                new = new_book.size_at_ask_price(price)
                delta = new - old
                if delta >= self.thresholds.new_sell_order_min_size:
                    alerts.append(Alert(
                        alert_type="C",
                        token_id=token_id,
                        side=side,
                        price=price,
                        payload={
                            "old_size": old, "new_size": new, "delta": delta,
                        },
                    ))

            # Alert D: nowy limit buy
            for price in prices:
                old = old_book.size_at_bid_price(price) if old_book else 0
                new = new_book.size_at_bid_price(price)
                delta = new - old
                if delta >= self.thresholds.big_limit_buy_min_size:
                    alerts.append(Alert(
                        alert_type="D",
                        token_id=token_id,
                        side=side,
                        price=price,
                        payload={
                            "old_size": old, "new_size": new, "delta": delta,
                        },
                    ))

        return alerts

    # -------------------------------------------------------------------------
    # Zdarzenie: trade
    # -------------------------------------------------------------------------

    def check_trade(self, trade: Trade, side: str) -> list[Alert]:
        """Sprawdza alert B (duży market buy)."""
        prices_set = {round(p, 6) for p in self.monitored_prices}
        if round(trade.price, 6) not in prices_set:
            return []
        if trade.side != "BUY":
            return []
        if trade.size < self.thresholds.market_buy_min_size:
            return []
        return [Alert(
            alert_type="B",
            token_id=trade.token_id,
            side=side,
            price=trade.price,
            payload={"size": trade.size, "price": trade.price},
        )]

    # -------------------------------------------------------------------------
    # Pomocnicze
    # -------------------------------------------------------------------------

    def _dominant_monitored_ask_price(
        self, book: OrderBook, prices: list[float]
    ) -> float:
        """Cena z monitored_prices, na której najwięcej asków (do payloadu)."""
        best_price, best_size = prices[0], -1.0
        for p in prices:
            s = book.size_at_ask_price(p)
            if s > best_size:
                best_price, best_size = p, s
        return best_price

    def reset_token_state(self, token_id: str) -> None:
        """Wywołać gdy odsubskrybujemy token (czyszczenie pamięci)."""
        self._last_ask_sum.pop(token_id, None)


# -----------------------------------------------------------------------------
# Helper - mapowanie token_id na 'YES'/'NO' na podstawie metadanych rynku
# -----------------------------------------------------------------------------


def resolve_side(token_id: str, token_yes_id: str | None,
                 token_no_id: str | None) -> str:
    if token_id == token_yes_id:
        return SIDE_YES
    if token_id == token_no_id:
        return SIDE_NO
    return "?"
