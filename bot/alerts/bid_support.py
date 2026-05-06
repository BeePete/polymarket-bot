"""
Filtr "bid support" - wycisza alerty na podrynkach które nie mają
wsparcia w księdze zleceń.

Idea: jeśli rynek YES/NO ma cenę 99.8/99.9¢ i odpaliło się jakieś
zdarzenie alertu, ale na BIDACH (kupujący) na cenie 99.7¢ NIE ma
nikogo - to znaczy że nie ma realnego "buy wall" pod rynkiem i alert
może być fałszywy / mało znaczący. Filtr go wycisza.

Funkcja jest celowo PURE (bez I/O, bez konfiguracji globalnej) -
przyjmuje book + parametry, zwraca bool. Logowanie i routing
robi orchestrator (main.py).

Konwencje:
  - `required_price` to ułamek 0.0-1.0 (ten sam format co `monitored_prices`
    w configu). Np. 0.997 = 99.7¢. Świadomie NIE używamy jednostki "centy"
    żeby nie mylić z resztą configu.
  - Side jest argumentem informacyjnym (do logowania w orchestratorze).
    Funkcja sama z siebie nie robi nic z `side` poza zwróceniem informacji
    diagnostycznej.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..polymarket.models import OrderBook


# Tolerancja porównania float - cena jest zapisywana jako "0.997" lub "0.998"
# w book; tolerancja chroni przed problemami precyzji (0.997 != 0.9970000001)
_PRICE_EPS = 1e-9


@dataclass(frozen=True)
class BidSupportResult:
    """Wynik sprawdzenia filtra. Bool jest "shortcutem", reszta do logowania."""

    has_support: bool
    shares_at_price: float       # ile shares było na bidzie na required_price
    required_price: float        # echo argumentu (do logowania)
    side: str                    # echo argumentu (do logowania)

    def __bool__(self) -> bool:
        return self.has_support


def check_bid_support(
    order_book: OrderBook | None,
    side: str,
    required_price: float,
    min_shares: float = 1.0,
) -> BidSupportResult:
    """
    Sprawdza czy w `order_book` na BIDZIE na cenie DOKŁADNIE `required_price`
    jest co najmniej `min_shares` shares.

    Args:
      order_book: stan order booka po stronie alertu (YES albo NO).
        Może być None jeśli WS nie miał jeszcze snapshotu - wtedy zwracamy
        "brak wsparcia" (bezpiecznie - nie wysyłamy alertu bez kontekstu).
      side: 'YES' lub 'NO' - tylko do logowania, nie wpływa na logikę.
      required_price: ułamek 0.0-1.0 (np. 0.997 = 99.7¢). Cena DOKŁADNA -
        nie sumujemy poziomów wokół niej.
      min_shares: minimalna suma shares na tej cenie. Domyślnie 1
        ("cokolwiek").

    Returns:
      BidSupportResult - ma `has_support: bool` oraz informacje
      diagnostyczne (shares_at_price, required_price, side) do logowania.

    Przykłady:
      check_bid_support(book_NO, "NO", 0.997)
        -> jeśli book_NO.bids ma poziom price=0.997 z size>=1 -> True
        -> jeśli na 0.997 jest 0 shares (lub w ogóle brak poziomu) -> False
        -> jeśli na 0.998 jest 5000 ale na 0.997 nic -> False
          (sprawdzamy DOKŁADNĄ cenę, nie zaokrąglamy)
    """
    if order_book is None:
        return BidSupportResult(
            has_support=False, shares_at_price=0.0,
            required_price=required_price, side=side,
        )

    total = 0.0
    for level in order_book.bids:
        if abs(level.price - required_price) < _PRICE_EPS:
            total += level.size

    return BidSupportResult(
        has_support=(total >= min_shares),
        shares_at_price=total,
        required_price=required_price,
        side=side,
    )
