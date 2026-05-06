"""
Formatowanie alertów do wiadomości Telegram (HTML).

Używamy HTML zamiast Markdown bo Telegram MarkdownV2 wymaga escape'owania
prawie wszystkiego (~ ! - = + ...). HTML jest dużo wybaczający.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .detector import Alert


# Emoji per typ alertu (zgodnie ze specyfikacją)
ALERT_EMOJI = {
    "A": "🔴",   # ask topnieje
    "B": "💰",   # duży market buy
    "C": "📤",   # nowy sell order
    "D": "🛑",   # duży limit buy ("rynek zamknięty")
}

ALERT_TITLE = {
    "A": "Ask topnieje (poniżej progu)",
    "B": "Duży market buy",
    "C": "Nowy sell order",
    "D": "Duży limit buy (rynek zamknięty?)",
}


def _fmt_price(p: float) -> str:
    """0.999 -> '99.9¢'."""
    return f"{p * 100:.1f}¢"


def _fmt_int(x: float) -> str:
    """Format z separatorami tysięcy: 28450 -> '28,450'."""
    return f"{int(round(x)):,}"


def _polymarket_url(event_slug: str) -> str:
    return f"https://polymarket.com/event/{event_slug}"


def _escape_html(s: str) -> str:
    """Minimalny escape dla Telegram HTML."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def format_alert(
    alert: Alert,
    market_question: str,
    event_slug: str,
    now: datetime | None = None,
) -> str:
    """
    Buduje finalną wiadomość HTML do wysłania na Telegram.

    Format docelowy (z każdego alertu wynika podobnie):

      🔴 ALERT: Ask topnieje (poniżej 30k)
      📊 Rynek: "Bitcoin above $70,000 on May 6" (YES)
      💰 Cena: 99.9¢
      📉 Shares w ask na 99.8/99.9¢: 28,450
      🔗 https://polymarket.com/event/bitcoin-above-on-may-6
      ⏰ 14:23:45
    """
    now = now or datetime.now(timezone.utc).astimezone()
    emoji = ALERT_EMOJI.get(alert.alert_type, "❓")
    title = ALERT_TITLE.get(alert.alert_type, "Alert")
    question = _escape_html(market_question or "?")
    url = _polymarket_url(event_slug or "")

    lines: list[str] = []
    lines.append(f"{emoji} <b>ALERT: {_escape_html(title)}</b>")
    lines.append(f"📊 Rynek: \"<b>{question}</b>\" ({alert.side})")
    lines.append(f"💰 Cena: <b>{_fmt_price(alert.price)}</b>")

    # Linijki specyficzne per typ alertu
    if alert.alert_type == "A":
        ask_sum = alert.payload.get("ask_sum", 0)
        prev = alert.payload.get("previous_sum")
        threshold = alert.payload.get("threshold", 30000)
        prefix = "📉"
        details = (f"Shares w ask na 99.8/99.9¢: <b>{_fmt_int(ask_sum)}</b> "
                   f"(próg: {_fmt_int(threshold)})")
        lines.append(f"{prefix} {details}")
        if prev is not None:
            drop = prev - ask_sum
            lines.append(f"   ↘️ poprzednio: {_fmt_int(prev)} (spadek: "
                         f"{_fmt_int(drop)})")
        if alert.bypass_cooldown:
            lines.append("⚡ Burst-drop: spadek &gt; 5k - alert poza cooldownem")

    elif alert.alert_type == "B":
        size = alert.payload.get("size", 0)
        lines.append(f"💰 Trade kupna: <b>{_fmt_int(size)}</b> shares "
                     f"po {_fmt_price(alert.price)}")

    elif alert.alert_type == "C":
        old = alert.payload.get("old_size", 0)
        new = alert.payload.get("new_size", 0)
        delta = alert.payload.get("delta", 0)
        lines.append(f"📤 Nowy sell order: <b>+{_fmt_int(delta)}</b> shares "
                     f"({_fmt_int(old)} → {_fmt_int(new)})")

    elif alert.alert_type == "D":
        old = alert.payload.get("old_size", 0)
        new = alert.payload.get("new_size", 0)
        delta = alert.payload.get("delta", 0)
        lines.append(f"🛑 Nowy limit buy: <b>+{_fmt_int(delta)}</b> shares "
                     f"({_fmt_int(old)} → {_fmt_int(new)})")

    lines.append(f"🔗 {url}")
    lines.append(f"⏰ {now.strftime('%H:%M:%S')}")

    return "\n".join(lines)


def format_test_alert() -> str:
    """Wiadomość dla komendy /test - sprawdzenie czy Telegram działa."""
    now = datetime.now(timezone.utc).astimezone()
    return (
        "✅ <b>Test alert</b>\n"
        "Bot żyje i może pisać do tego chatu.\n"
        f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S')}"
    )
