"""
Formatowanie alertów do wiadomości Telegram (HTML).

Używamy HTML zamiast Markdown bo Telegram MarkdownV2 wymaga escape'owania
prawie wszystkiego (~ ! - = + ...). HTML jest dużo wybaczający.

Style wiadomości (po refaktorze konsolidacji):
  1. SKONSOLIDOWANA - 1+ alertów dla tego samego eventu, po debounce 30s
  2. BURST-DROP    - pojedynczy alert A z spadkiem >5k shares (instant)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .detector import Alert


# -----------------------------------------------------------------------------
# Mapowania emoji
# -----------------------------------------------------------------------------

# Emoji per typ alertu - NOWY format (spec konsolidacji)
ALERT_TYPE_EMOJI = {
    "A": "🔻",   # ask topnieje
    "B": "💰",   # duży market buy
    "C": "📤",   # nowy sell order (ask)
    "D": "🛑",   # duży limit buy (bid)
}

# Mapowanie po prefiksie slug-a eventu -> ikona serii
# Sprawdzamy w kolejności: dłuższe prefiksy najpierw (np. "s-and-p" zanim "s")
_SERIES_ICON_RULES: list[tuple[str, str]] = [
    ("sp-500", "📈"),
    ("s-and-p", "📈"),
    ("bitcoin", "₿"),
    ("btc", "₿"),
    ("ethereum", "Ξ"),
    ("eth", "Ξ"),
]
_DEFAULT_SERIES_ICON = "🎯"


def series_icon(event_slug: str) -> str:
    """
    Mapuje slug eventu na ikonę serii.
    Dopasowanie po prefiksie (case-insensitive).
    """
    s = (event_slug or "").lower()
    for prefix, icon in _SERIES_ICON_RULES:
        if s.startswith(prefix):
            return icon
    return _DEFAULT_SERIES_ICON


# -----------------------------------------------------------------------------
# Skracanie nazw rynków
# -----------------------------------------------------------------------------

# Wyciąga kwoty dolarowe z tytułu rynku, np. "$86,000" lub "$78,000 and $80,000"
_DOLLAR_AMOUNT_RE = re.compile(r"\$[\d,]+")
_TRUNCATE_LIMIT = 40


def market_short(question: str) -> str:
    """
    Skraca tytuł rynku do kluczowej wartości progu.
    Strategia:
      1. Jeśli w tytule są kwoty $XX (np. "$86,000") - bierz NAJWIĘKSZĄ.
         (zgodnie z wytycznymi: górna granica przedziału, najwyższy próg).
      2. Inaczej truncate do 40 znaków z elipsą.
    """
    if not question:
        return "?"
    matches = _DOLLAR_AMOUNT_RE.findall(question)
    if matches:
        # Wybierz NAJWIĘKSZĄ kwotę (górna granica przedziału)
        max_match = max(matches, key=lambda m: int(m.replace("$", "").replace(",", "")))
        return max_match
    # Brak kwoty - truncate
    s = question.strip()
    if len(s) > _TRUNCATE_LIMIT:
        return s[: _TRUNCATE_LIMIT - 1].rstrip() + "…"
    return s


def market_sort_key(question: str) -> tuple[int, int]:
    """
    Klucz sortowania dla skonsolidowanej wiadomości.
    Zwraca (bucket, value):
      bucket=0 - z liczbową kwotą; sortujemy po -value (malejąco)
      bucket=1 - bez kwoty; lądują po prostu na końcu
    Używać jako:  sorted(alerts, key=lambda a: market_sort_key(a.market_question))
    """
    if not question:
        return (1, 0)
    matches = _DOLLAR_AMOUNT_RE.findall(question)
    if not matches:
        return (1, 0)
    max_value = max(
        int(m.replace("$", "").replace(",", ""))
        for m in matches
    )
    return (0, -max_value)  # ujemne -> sort rosnąco daje malejąco po wartości


# -----------------------------------------------------------------------------
# Formatery liczb / cen
# -----------------------------------------------------------------------------


def _fmt_price_pl(p: float) -> str:
    """0.999 -> '99,9' (przecinek dziesiętny, BEZ jednostki ¢ - dodajemy osobno)."""
    return f"{p * 100:.1f}".replace(".", ",")


def format_shares(n: int) -> str:
    """
    Skraca liczbę udziałów do czytelnej formy:
      - n < 1000          → "516"
      - 1000 ≤ n < 10000  → "5.5k", "9.9k"        (1 miejsce po przecinku, truncate)
      - 10000 ≤ n < 1M    → "30k", "123k"          (bez ułamka)
      - n ≥ 1000000       → "1.2M"                  (1 miejsce po przecinku, truncate)

    Truncation a nie rounding — żeby 9999 → "9.9k", nie "10.0k"
    (zachowuje informację "jeszcze nie 10k").
    Akceptujemy też float (zaokrąglamy do int przed klasyfikacją).
    """
    n = int(round(n))
    if n < 0:
        n = 0
    if n < 1000:
        return str(n)
    if n < 10000:
        # n // 100 daje wartość w setkach; dzielimy przez 10 -> 1 miejsce po przecinku
        return f"{(n // 100) / 10:.1f}k"
    if n < 1_000_000:
        return f"{n // 1000}k"
    # n >= 1_000_000
    return f"{(n // 100_000) / 10:.1f}M"


def _escape_html(s: str) -> str:
    """Minimalny escape dla Telegram HTML."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


# =============================================================================
#  Wiadomości - implementacja
# =============================================================================


def _alert_detail_text(alert: Alert) -> str:
    """
    Generuje fragment '— {detail}' linii alertu zgodnie ze specyfikacją:
      A: pozostało {curr} (↓ {drop})
      B: kupiono {size} @{price}¢
      C: nowy ask {size}
      D: nowy bid {size}
    """
    p = alert.payload
    if alert.alert_type == "A":
        curr = p.get("ask_sum", 0)
        prev = p.get("previous_sum")
        drop = (prev - curr) if prev is not None else None
        if drop is not None and drop > 0:
            return f"pozostało {format_shares(curr)} (↓ {format_shares(drop)})"
        return f"pozostało {format_shares(curr)}"

    if alert.alert_type == "B":
        size = p.get("size", 0)
        price_pct = _fmt_price_pl(alert.price)
        return f"kupiono {format_shares(size)} @{price_pct}¢"

    if alert.alert_type == "C":
        delta = p.get("delta", 0)
        return f"nowy ask {format_shares(delta)}"

    if alert.alert_type == "D":
        delta = p.get("delta", 0)
        return f"nowy bid {format_shares(delta)}"

    return "?"


def format_alert_line(alert: Alert, market_question: str) -> str:
    """
    Pojedyncza linia w wiadomości:
      "{type_emoji} {market_short} {side} {price}¢ — {detail}"
    """
    emoji = ALERT_TYPE_EMOJI.get(alert.alert_type, "❓")
    short = _escape_html(market_short(market_question))
    side = (alert.side or "?").upper()
    price = _fmt_price_pl(alert.price)
    detail = _alert_detail_text(alert)
    return f"{emoji} {short} {side} {price}¢ — {detail}"


def _polymarket_event_url(event_slug: str) -> str:
    return f"https://polymarket.com/event/{event_slug}" if event_slug else ""


def format_consolidated_message(
    alerts_with_questions: list[tuple[Alert, str]],
    event_title: str,
    event_slug: str,
    now: datetime | None = None,
) -> str:
    """
    Wiadomość skonsolidowana - 1+ alertów z tego samego eventu, po debounce.

    Format:
      {icon} {event_title}

      {linia_alertu}
      {linia_alertu}
      ...

      {event_url}
      🕒 {HH:MM}

    Args:
      alerts_with_questions: lista (Alert, market_question) - po jednym
        elemencie na alert. Krotki, NIE dict, żeby tie-breaker po
        kolejności wstawienia był zachowany przy stable sort.
      event_title: pełny title eventu z Gamma API (NIE slug)
      event_slug: slug do URL
      now: czas wysłania (do testowania)

    UWAGA: NIE ma pustej linii po linku - od razu 🕒. Tym różni się od
    format_burst_drop_message.
    """
    now = now or datetime.now(timezone.utc).astimezone()
    icon = series_icon(event_slug)
    title = _escape_html(event_title or event_slug or "?")
    url = _polymarket_event_url(event_slug)

    # Sortowanie: najwyższa kwota progu pierwsza; tie -> stable (kolejność wejścia)
    sorted_alerts = sorted(
        alerts_with_questions,
        key=lambda pair: market_sort_key(pair[1]),
    )

    lines: list[str] = [
        f"{icon} <b>{title}</b>",
        "",
    ]
    for alert, question in sorted_alerts:
        lines.append(format_alert_line(alert, question))
    lines.append("")
    lines.append(url)
    lines.append(f"🕒 {now.strftime('%H:%M')}")
    return "\n".join(lines)


def format_burst_drop_message(
    alert: Alert,
    market_question: str,
    event_title: str,
    event_slug: str,
    now: datetime | None = None,
) -> str:
    """
    Wiadomość burst-drop (alert A z spadkiem >5k - wysyłana natychmiast,
    omija bufor agregacji).

    Format:
      {icon} {event_title}

      {linia_alertu}

      {event_url}

      ⚡ Burst-drop — alert poza cooldownem
      🕒 {HH:MM}
    """
    now = now or datetime.now(timezone.utc).astimezone()
    icon = series_icon(event_slug)
    title = _escape_html(event_title or event_slug or "?")
    line = format_alert_line(alert, market_question)
    url = _polymarket_event_url(event_slug)

    parts: list[str] = [
        f"{icon} <b>{title}</b>",
        "",
        line,
        "",
        url,
        "",
        "⚡ Burst-drop — alert poza cooldownem",
        f"🕒 {now.strftime('%H:%M')}",
    ]
    return "\n".join(parts)


def format_test_alert() -> str:
    """Wiadomość dla komendy /test - sprawdzenie czy Telegram działa."""
    now = datetime.now(timezone.utc).astimezone()
    return (
        "✅ <b>Test alert</b>\n"
        "Bot żyje i może pisać do tego chatu.\n"
        f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S')}"
    )


# =============================================================================
#  Komenda /depth - aktualna głębokość order booka per rynek
# =============================================================================
#  Pure functions - przyjmują dane wejściowe, zwracają stringi HTML.
#  Bez I/O i bez stanu - łatwe do testowania.
# =============================================================================


from typing import Callable

from ..polymarket.models import OrderBook


# Maksymalna długość pojedynczej wiadomości Telegrama. Limit techniczny
# wynosi 4096; dajemy margin na nagłówek i HTML escape.
DEPTH_MAX_MESSAGE_CHARS = 4000


def _depth_line(
    market: dict,
    side: str,
    book: OrderBook,
    monitored_prices: list[float],
) -> str | None:
    """
    Buduje pojedynczą linię '/depth' dla rynku po danej stronie, lub None
    jeśli ta strona nie jest blisko 99,8/99,9¢ (czyli żaden ask na tej
    stronie nie ma ceny w monitored_prices).

    Format: '  $86,000 NO 99,9¢ — 5.5k'
    """
    monitored_set = {round(p, 6) for p in monitored_prices}

    # Najlepszy ask którego cena jest w monitored_prices.
    # Jeśli nie ma żadnego - ta strona nie jest blisko 99,9¢, pomiń.
    best_monitored_price: float | None = None
    for lvl in book.asks:
        if round(lvl.price, 6) in monitored_set:
            if best_monitored_price is None or lvl.price < best_monitored_price:
                best_monitored_price = lvl.price
    if best_monitored_price is None:
        return None

    # Suma asków na monitored_prices (np. 99,8 + 99,9¢ łącznie)
    total = book.total_ask_size_at_prices(monitored_prices)
    if total <= 0:
        return None

    short = _escape_html(market_short(market.get("question") or "?"))
    return (
        f"  {short} {side} {_fmt_price_pl(best_monitored_price)}¢ "
        f"— {format_shares(total)}"
    )


def _depth_event_section(
    event: dict,
    markets: list[dict],
    book_lookup: Callable[[str], OrderBook | None],
    monitored_prices: list[float],
) -> str | None:
    """
    Buduje sekcję dla jednego eventu (nagłówek + linie podrynków).
    Zwraca None jeśli żaden podrynek/strona nie jest blisko 99,9¢.

    Sortowanie linii: malejąco po wartości progu (`market_sort_key`),
    tie-breaker: YES przed NO.
    """
    icon = series_icon(event.get("slug", "") or "")
    title = _escape_html(event.get("title") or event.get("slug") or "?")

    rows: list[tuple[tuple, str]] = []  # (sort_key, line_text)
    for market in markets:
        for side, token_key in (("YES", "token_yes_id"), ("NO", "token_no_id")):
            token_id = market.get(token_key)
            if not token_id:
                continue
            book = book_lookup(token_id)
            if book is None:
                continue
            line = _depth_line(market, side, book, monitored_prices)
            if line is None:
                continue
            sort_key = (
                market_sort_key(market.get("question") or ""),
                0 if side == "YES" else 1,    # YES przed NO przy tie
            )
            rows.append((sort_key, line))

    if not rows:
        return None

    rows.sort(key=lambda r: r[0])
    return "\n".join([f"{icon} <b>{title}</b>"] + [r[1] for r in rows])


def _depth_chunk(sections: list[str], header: str) -> str:
    """Składa nagłówek + sekcje rozdzielone pustymi liniami."""
    return header + "\n\n" + "\n\n".join(sections)


def build_depth_messages(
    events_with_markets: list[tuple[dict, list[dict]]],
    book_lookup: Callable[[str], OrderBook | None],
    monitored_prices: list[float],
    now: datetime | None = None,
    max_chars: int = DEPTH_MAX_MESSAGE_CHARS,
) -> list[str]:
    """
    Buduje 1+ wiadomości HTML dla komendy /depth.

    Args:
      events_with_markets: lista (event_dict, list[market_dict])
        event_dict wymagane pola: 'slug', 'title'
        market_dict wymagane pola: 'question', 'token_yes_id', 'token_no_id'
      book_lookup: callable(token_id) -> OrderBook | None
        zwraca aktualny order book dla tokenu (np. ws.get_book)
        None gdy WS jeszcze nie miał snapshotu - linia pomijana po cichu
      monitored_prices: ceny do sumowania, np. [0.998, 0.999]
      now: czas wysłania (do nagłówka). Domyślnie - teraz.
      max_chars: limit na pojedynczą wiadomość Telegrama (4000 = margin
        pod 4096 limit Telegrama, na nagłówek i HTML escape).

    Returns:
      Lista 1+ wiadomości HTML:
      - 1 wiadomość "📊 Brak rynków..." jeśli żaden rynek nie jest blisko 99,9¢
      - 1 wiadomość jeśli całość mieści się w max_chars
      - 2+ wiadomości z nagłówkiem "📊 Stan głębokości — HH:MM (część X/Y)"
        jeśli treść przekracza limit. Dzielenie odbywa się na granicy sekcji
        (event), żeby nie rozbijać sekcji w połowie.

    Sortowanie:
      - eventy: spec mówi "alfabetycznie po slug" - kolejność z events_with_markets
        jest zachowana (caller sortuje przed wywołaniem)
      - linie wewnątrz eventu: malejąco po wartości progu, YES przed NO
    """
    now = now or datetime.now(timezone.utc).astimezone()
    time_str = now.strftime("%H:%M")

    # Buduj sekcje per event
    sections: list[str] = []
    for event, markets in events_with_markets:
        section = _depth_event_section(event, markets, book_lookup, monitored_prices)
        if section:
            sections.append(section)

    if not sections:
        return ["📊 Brak rynków blisko 99,9¢ w tej chwili"]

    # Spróbuj jednej wiadomości
    single_header = f"📊 <b>Stan głębokości — {time_str}</b>"
    one_message = _depth_chunk(sections, single_header)
    if len(one_message) <= max_chars:
        return [one_message]

    # Trzeba podzielić na chunki - greedy fill, granica = sekcja (event).
    # "Część X/Y" w nagłówku może mieć max ~8 znaków extra ("(część 99/99)" = 13)
    header_overhead = 80  # hojna estymata na nagłówek z numerem części
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for section in sections:
        # +2 za "\n\n" separator między sekcjami w chunk-u
        section_with_sep = len(section) + 2
        if current and (current_len + section_with_sep + header_overhead > max_chars):
            chunks.append(current)
            current = [section]
            current_len = len(section)
        else:
            current.append(section)
            current_len += section_with_sep if current else len(section)
    if current:
        chunks.append(current)

    total_parts = len(chunks)
    messages = []
    for i, chunk_sections in enumerate(chunks, start=1):
        header = (
            f"📊 <b>Stan głębokości — {time_str} "
            f"(część {i}/{total_parts})</b>"
        )
        messages.append(_depth_chunk(chunk_sections, header))
    return messages
