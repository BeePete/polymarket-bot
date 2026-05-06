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
