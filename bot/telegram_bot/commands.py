"""
Handlery komend Telegrama.

Wszystkie komendy są ograniczone do właściciela bota (sprawdzamy
update.effective_chat.id == config.telegram.chat_id). Inne osoby są
ciche - nawet nie dostaną odpowiedzi "nie masz dostępu", żeby bot
nie ujawniał że istnieje.

Komendy są zarejestrowane w klasie CommandHandlers i podpinane do
telegram.ext.Application w main.py.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from ..alerts.formatter import build_depth_messages, format_test_alert

if TYPE_CHECKING:  # tylko do type-checkingu, unika circular imports
    from ..config import BotConfig
    from ..polymarket.clob_ws import CLOBWebSocketManager
    from ..storage.db import Database
    from .bot import TelegramSender


HELP_TEXT = (
    "<b>Polymarket Bot - dostępne komendy:</b>\n\n"
    "/list — aktualnie monitorowane eventy i rynki\n"
    "/status — pełny status bota i statystyki\n"
    "/depth — aktualna głębokość order booka dla rynków blisko 99,9¢\n"
    "/add &lt;slug&gt; — dodaj event do monitorowania\n"
    "/remove &lt;slug&gt; — usuń event\n"
    "/series — lista skonfigurowanych serii auto-monitorowania\n"
    "/thresholds — aktualne progi alertów\n"
    "/set_threshold &lt;nazwa&gt; &lt;wartość&gt; — zmień próg w runtime\n"
    "/pause — zatrzymaj wysyłanie alertów\n"
    "/resume — wznów wysyłanie alertów\n"
    "/test — testowa wiadomość (sprawdza Telegram)\n"
    "/help — ta pomoc\n"
)


class CommandHandlers:
    """
    Wszystkie handlery komend zebrane w jednej klasie - mają wspólny stan
    (config, db, sender, czas startu, callback do main loop).
    """

    def __init__(
        self,
        config: "BotConfig",
        config_path: str,
        db: "Database",
        sender: "TelegramSender",
        ws: "CLOBWebSocketManager | None" = None,
        on_add_event=None,           # async callable(slug: str) -> bool
        on_remove_event=None,        # async callable(slug: str) -> bool
        get_runtime_stats=None,      # callable() -> dict (uptime, queues, ...)
    ):
        self.config = config
        self.config_path = config_path
        self.db = db
        self.sender = sender
        self.ws = ws
        self._on_add_event = on_add_event
        self._on_remove_event = on_remove_event
        self._get_runtime_stats = get_runtime_stats
        self.start_time = time.time()

    # -------------------------------------------------------------------------
    # Filtr autoryzacji
    # -------------------------------------------------------------------------

    def _is_owner(self, update: Update) -> bool:
        if not update.effective_chat:
            return False
        return str(update.effective_chat.id) == str(self.config.telegram.chat_id)

    async def _reject_unauthorized(self, update: Update) -> None:
        """Po cichu loguje próbę nieautoryzowaną - bez odpowiedzi."""
        chat_id = update.effective_chat.id if update.effective_chat else "?"
        user = update.effective_user.username if update.effective_user else "?"
        logger.warning(
            f"Telegram: odrzucona komenda od chat_id={chat_id} user=@{user}"
        )

    async def _send(self, update: Update, text: str) -> None:
        await update.effective_chat.send_message(
            text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )

    # -------------------------------------------------------------------------
    # Komendy
    # -------------------------------------------------------------------------

    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)
        await self._send(update,
            "👋 Cześć! Jestem botem monitorującym Polymarket.\n\n" + HELP_TEXT)

    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)
        await self._send(update, HELP_TEXT)

    async def cmd_test(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)
        await self.sender.send_html(format_test_alert())

    async def cmd_pause(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)
        self.sender.pause()
        self.db.kv_set("paused", "1")
        await self._send(update, "⏸ Pauza włączona - alerty są wstrzymane "
                                  "(monitoring działa dalej).")

    async def cmd_resume(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)
        self.sender.resume()
        self.db.kv_set("paused", "0")
        await self._send(update, "▶️ Pauza wyłączona - bot znów wysyła alerty.")

    async def cmd_list(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)

        events = self.db.list_events()
        if not events:
            await self._send(update, "📭 Brak monitorowanych eventów.")
            return

        lines: list[str] = ["<b>📋 Monitorowane eventy:</b>\n"]
        for ev in events:
            markets = self.db.list_markets_for_event(ev["slug"])
            monitored_count = sum(1 for m in markets if m["is_monitored"])
            src = "🤖 auto" if ev["source"] == "auto" else "✋ manual"
            lines.append(
                f"• {src} <b>{_html(ev['slug'])}</b>\n"
                f"   rynków: {len(markets)} (blisko 99.9¢: <b>{monitored_count}</b>)"
            )
            if ev["end_date"]:
                lines.append(f"   końca: {_html(ev['end_date'])}")
            lines.append("")
        await self._send(update, "\n".join(lines))

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)

        events = self.db.list_events()
        markets = self.db.list_all_markets()
        monitored = [m for m in markets if m["is_monitored"]]

        # Statystyki alertów - 24h i total
        since_24h = int(time.time()) - 24 * 3600
        last_24h = self.db.count_alerts_since(since_24h)
        total = self.db.total_alerts_count()
        uptime = _format_uptime(time.time() - self.start_time)

        runtime = ""
        if self._get_runtime_stats:
            stats = self._get_runtime_stats()
            ws_state = "🟢 connected" if stats.get("ws_connected") else "🔴 disconnected"
            runtime = (f"\n<b>WebSocket:</b> {ws_state}\n"
                       f"<b>Tokeny w subskrypcji:</b> {stats.get('subscribed_tokens', 0)}")

        paused_text = "⏸ tak" if self.sender.paused else "▶️ nie"

        text = (
            f"<b>📊 Status bota</b>\n"
            f"<b>Uptime:</b> {uptime}\n"
            f"<b>Pauza:</b> {paused_text}{runtime}\n\n"
            f"<b>Eventy:</b> {len(events)}\n"
            f"<b>Rynki łącznie:</b> {len(markets)}\n"
            f"<b>Rynki blisko 99.9¢:</b> {len(monitored)}\n\n"
            f"<b>Alerty (ostatnie 24h):</b>\n"
            f"  A (ask topnieje): {last_24h.get('A', 0)}\n"
            f"  B (market buy):   {last_24h.get('B', 0)}\n"
            f"  C (new sell):     {last_24h.get('C', 0)}\n"
            f"  D (limit buy):    {last_24h.get('D', 0)}\n"
            f"<b>Razem od startu:</b> {total}"
        )
        await self._send(update, text)

    async def cmd_add(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)
        if not ctx.args:
            await self._send(update, "Użycie: /add &lt;slug-eventu&gt;")
            return
        slug = ctx.args[0].strip().lower()
        if not self._on_add_event:
            await self._send(update, "❌ Funkcja niedostępna (bot się jeszcze startuje).")
            return
        ok = await self._on_add_event(slug)
        if ok:
            await self._send(update, f"✅ Dodano event: <code>{_html(slug)}</code>")
        else:
            await self._send(update, f"❌ Nie udało się dodać <code>{_html(slug)}</code>"
                                      f" (sprawdź czy slug jest poprawny i event aktywny).")

    async def cmd_remove(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)
        if not ctx.args:
            await self._send(update, "Użycie: /remove &lt;slug-eventu&gt;")
            return
        slug = ctx.args[0].strip().lower()
        if not self._on_remove_event:
            await self._send(update, "❌ Funkcja niedostępna.")
            return
        ok = await self._on_remove_event(slug)
        if ok:
            await self._send(update, f"✅ Usunięto event: <code>{_html(slug)}</code>")
        else:
            await self._send(update, f"❌ Event <code>{_html(slug)}</code> nie był"
                                      f" monitorowany.")

    async def cmd_series(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)
        series = self.config.auto_monitor_series or []
        if not series:
            await self._send(update, "📭 Brak skonfigurowanych serii.")
            return
        lines = ["<b>🤖 Serie auto-monitorowania:</b>\n"]
        for s in series:
            lines.append(f"• <code>{_html(s)}</code>")
        await self._send(update, "\n".join(lines))

    async def cmd_depth(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        """Pokazuje aktualną głębokość order booka dla rynków blisko 99,9¢."""
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)

        if self.ws is None:
            await self._send(update, "❌ WebSocket niedostępny - bot się jeszcze startuje.")
            return

        # Eventy posortowane alfabetycznie po slug (deterministycznie)
        events = sorted(self.db.list_events(), key=lambda e: e["slug"])

        events_with_markets: list[tuple[dict, list[dict]]] = []
        for ev in events:
            event_dict = {
                "slug": ev["slug"],
                "title": ev["title"],
            }
            markets = self.db.list_markets_for_event(ev["slug"])
            market_dicts = [
                {
                    "question": m["question"],
                    "token_yes_id": m["token_yes_id"],
                    "token_no_id": m["token_no_id"],
                }
                for m in markets
            ]
            events_with_markets.append((event_dict, market_dicts))

        messages = build_depth_messages(
            events_with_markets=events_with_markets,
            book_lookup=self.ws.get_book,
            monitored_prices=list(self.config.monitored_prices),
        )

        # Każda wiadomość osobno (Telegram nie łączy)
        for msg in messages:
            await self._send(update, msg)

        logger.info(
            f"Komenda /depth: wysłano {len(messages)} wiadomość(ci) "
            f"({sum(len(m) for m in messages)} znaków łącznie)"
        )

    async def cmd_thresholds(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)
        t = self.config.thresholds
        text = (
            "<b>⚙️ Aktualne progi:</b>\n"
            f"• <code>ask_melting_threshold</code>: <b>{t.ask_melting_threshold:,}</b> "
            f"(alert A)\n"
            f"• <code>ask_melting_burst_drop</code>: <b>{t.ask_melting_burst_drop:,}</b>"
            f" (A bypass cooldown)\n"
            f"• <code>market_buy_min_size</code>: <b>{t.market_buy_min_size:,}</b> "
            f"(alert B)\n"
            f"• <code>new_sell_order_min_size</code>: <b>{t.new_sell_order_min_size:,}</b>"
            f" (alert C)\n"
            f"• <code>big_limit_buy_min_size</code>: <b>{t.big_limit_buy_min_size:,}</b>"
            f" (alert D)\n\n"
            "Zmiana: <code>/set_threshold &lt;nazwa&gt; &lt;wartość&gt;</code>"
        )
        await self._send(update, text)

    async def cmd_set_threshold(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return await self._reject_unauthorized(update)

        if len(ctx.args) != 2:
            await self._send(update,
                "Użycie: /set_threshold &lt;nazwa&gt; &lt;wartość&gt;\n"
                "Nazwy: ask_melting_threshold, ask_melting_burst_drop, "
                "market_buy_min_size, new_sell_order_min_size, "
                "big_limit_buy_min_size")
            return

        name, raw_value = ctx.args[0], ctx.args[1]
        valid = {
            "ask_melting_threshold", "ask_melting_burst_drop",
            "market_buy_min_size", "new_sell_order_min_size",
            "big_limit_buy_min_size",
        }
        if name not in valid:
            await self._send(update, f"❌ Nieznana nazwa progu: <code>{_html(name)}</code>")
            return
        try:
            value = int(raw_value)
            if value < 0:
                raise ValueError("ujemna")
        except ValueError:
            await self._send(update,
                f"❌ Wartość musi być nieujemną liczbą całkowitą, było: "
                f"<code>{_html(raw_value)}</code>")
            return

        # Aktualizuj w runtime
        setattr(self.config.thresholds, name, value)
        # Zapisz do pliku
        try:
            from ..config import save_thresholds
            save_thresholds(self.config_path, self.config.thresholds)
        except Exception as exc:
            logger.error(f"Nie udało się zapisać thresholdów: {exc!r}")
            await self._send(update,
                f"⚠️ Próg zmieniony w runtime, ale zapis do pliku zawiódł:\n"
                f"<code>{_html(str(exc))}</code>")
            return

        await self._send(update,
            f"✅ Próg <code>{_html(name)}</code> zmieniony na <b>{value:,}</b> "
            f"(zapisano w config.yaml).")

    # -------------------------------------------------------------------------
    # Rejestracja w Application
    # -------------------------------------------------------------------------

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("test", self.cmd_test))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("list", self.cmd_list))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("depth", self.cmd_depth))
        app.add_handler(CommandHandler("add", self.cmd_add))
        app.add_handler(CommandHandler("remove", self.cmd_remove))
        app.add_handler(CommandHandler("series", self.cmd_series))
        app.add_handler(CommandHandler("thresholds", self.cmd_thresholds))
        app.add_handler(CommandHandler("set_threshold", self.cmd_set_threshold))


# -----------------------------------------------------------------------------
# Pomocnicze
# -----------------------------------------------------------------------------


def _html(s: str) -> str:
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;"))


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, secs = divmod(s, 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m {secs}s"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"
