"""
Punkt wejścia bota - spina wszystkie komponenty i uruchamia event loop.

Uruchamianie:
    python -m bot.main

Komponenty (każdy działa jako osobny asyncio task):
  1. CLOBWebSocketManager   - słucha zdarzeń z Polymarket WebSocket
  2. Orchestrator           - konsumuje zdarzenia z kolejki, wywołuje detektor,
                              filtruje cooldown, wysyła alerty
  3. DiscoveryScheduler     - co 30 min auto-discovery nowych eventów
  4. Telegram Application   - polling komend od użytkownika

Wszystkie taski są w try/except - bot nie umiera nawet gdy któryś rzuci błąd,
tylko go restartujemy. Sygnał SIGTERM/SIGINT wywołuje graceful shutdown.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger
from telegram.ext import Application

from .alerts.detector import (
    Alert,
    Detector,
    DetectorThresholds,
    resolve_side,
)
from .alerts.formatter import format_alert
from .config import BotConfig, load_config
from .polymarket.clob_ws import (
    BookSnapshotEvent,
    CLOBWebSocketManager,
    PriceChangeEvent,
    TickSizeChangeEvent,
    TradeEvent,
    WSEvent,
)
from .polymarket.gamma_api import GammaAPIClient, GammaAPIError
from .polymarket.models import OrderBook, OrderBookLevel
from .scheduler import DiscoveryScheduler
from .storage.db import Database
from .telegram_bot.bot import TelegramSender
from .telegram_bot.commands import CommandHandlers


CONFIG_PATH = Path(os.environ.get("BOT_CONFIG_PATH", "config.yaml"))
DB_PATH = Path(os.environ.get("BOT_DB_PATH", "bot_state.db"))
LOG_FILE = Path(os.environ.get("BOT_LOG_FILE", "logs/bot.log"))


# =============================================================================
#  Logging setup
# =============================================================================


def setup_logging(level: str) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
               "| <level>{level: <8}</level> "
               "| <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    )
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        LOG_FILE,
        level=level,
        rotation="50 MB",
        retention="14 days",
        compression="zip",
        encoding="utf-8",
    )


# =============================================================================
#  Orchestrator - konsument zdarzeń WS, łączy detektor i Telegrama
# =============================================================================


class Orchestrator:
    """Konsument zdarzeń z kolejki WebSocketa - serce alertowania."""

    def __init__(
        self,
        config: BotConfig,
        db: Database,
        ws: CLOBWebSocketManager,
        detector: Detector,
        sender: TelegramSender,
    ):
        self.config = config
        self.db = db
        self.ws = ws
        self.detector = detector
        self.sender = sender
        self._stop = asyncio.Event()

    async def run(self) -> None:
        logger.info("Orchestrator: start")
        while not self._stop.is_set():
            try:
                event = await asyncio.wait_for(
                    self.ws.events.get(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue
            try:
                await self._handle(event)
            except Exception as exc:
                logger.exception(f"Orchestrator: błąd przy zdarzeniu: {exc!r}")

    def stop(self) -> None:
        self._stop.set()

    # -------------------------------------------------------------------------
    # Routing zdarzeń
    # -------------------------------------------------------------------------

    async def _handle(self, event: WSEvent) -> None:
        if isinstance(event, BookSnapshotEvent):
            await self._handle_book(event.token_id, event.book, is_snapshot=True)
        elif isinstance(event, PriceChangeEvent):
            await self._handle_book(event.token_id, event.new_book, is_snapshot=False)
        elif isinstance(event, TradeEvent):
            await self._handle_trade(event)
        elif isinstance(event, TickSizeChangeEvent):
            logger.debug(f"Tick size change na {event.token_id}: "
                         f"{event.old_tick_size} -> {event.new_tick_size}")

    # -------------------------------------------------------------------------
    # Order book
    # -------------------------------------------------------------------------

    async def _handle_book(
        self, token_id: str, new_book: OrderBook, is_snapshot: bool
    ) -> None:
        market = self.db.get_market_by_token(token_id)
        if not market:
            return  # nieznany token - pomijamy

        # Wczytaj poprzedni stan z DB (po restarcie bot ma kontekst)
        prev = self.db.load_order_book(token_id)
        old_book: OrderBook | None = None
        if prev:
            bids_raw, asks_raw = prev
            old_book = OrderBook(
                token_id=token_id,
                bids=[OrderBookLevel(float(b["price"]), float(b["size"]))
                      for b in bids_raw],
                asks=[OrderBookLevel(float(a["price"]), float(a["size"]))
                      for a in asks_raw],
            )
            old_book.bids.sort(key=lambda l: -l.price)
            old_book.asks.sort(key=lambda l: l.price)

        side = resolve_side(token_id, market["token_yes_id"], market["token_no_id"])

        # Aktualizuj flagę monitorowania w DB
        is_monitored = self.detector.is_token_monitored(new_book)
        self.db.set_market_monitored(
            condition_id=market["condition_id"],
            monitored=is_monitored,
            side=side if is_monitored else None,
        )

        # Wykryj alerty
        alerts = self.detector.check_book_change(token_id, side, old_book, new_book)

        # Zapisz nowy snapshot order booka do DB
        bids_dump = [{"price": str(l.price), "size": str(l.size)} for l in new_book.bids]
        asks_dump = [{"price": str(l.price), "size": str(l.size)} for l in new_book.asks]
        self.db.save_order_book(token_id, bids_dump, asks_dump)

        for alert in alerts:
            await self._maybe_send(alert, market)

    # -------------------------------------------------------------------------
    # Trade
    # -------------------------------------------------------------------------

    async def _handle_trade(self, event: TradeEvent) -> None:
        market = self.db.get_market_by_token(event.trade.token_id)
        if not market:
            return
        side = resolve_side(
            event.trade.token_id,
            market["token_yes_id"],
            market["token_no_id"],
        )
        alerts = self.detector.check_trade(event.trade, side)
        for alert in alerts:
            await self._maybe_send(alert, market)

    # -------------------------------------------------------------------------
    # Cooldown + wysyłka
    # -------------------------------------------------------------------------

    async def _maybe_send(self, alert: Alert, market: Any) -> None:
        # Cooldown - chyba że alert ma bypass (burst-drop dla A)
        if not alert.bypass_cooldown:
            last = self.db.last_alert_at(alert.alert_type, alert.token_id)
            if last:
                age = int(time.time()) - last
                if age < self.config.alert_cooldown_seconds:
                    logger.debug(
                        f"Alert {alert.alert_type} dla {alert.token_id[:10]}... "
                        f"w cooldownie ({age}s/{self.config.alert_cooldown_seconds}s)"
                    )
                    return

        message = format_alert(
            alert=alert,
            market_question=market["question"] or "?",
            event_slug=market["event_slug"] or "",
        )

        ok = await self.sender.send_html(message)
        if ok:
            self.db.record_alert(
                alert_type=alert.alert_type,
                token_id=alert.token_id,
                condition_id=market["condition_id"],
                payload=alert.payload,
            )
            logger.info(
                f"📨 Alert {alert.alert_type} wysłany "
                f"({market['question'][:40]} / {alert.side})"
            )


# =============================================================================
#  Komendy /add /remove - callbacks dla CommandHandlers
# =============================================================================


async def add_event_callback(
    config: BotConfig,
    db: Database,
    ws: CLOBWebSocketManager,
    config_path: Path,
    slug: str,
) -> bool:
    """Dodaje event ręcznie - ściąga z Gamma API, dodaje do bazy + WS."""
    try:
        async with GammaAPIClient(
            base_url=config.advanced.gamma_api_url,
        ) as client:
            event = await client.get_event_by_slug(slug)
    except GammaAPIError as exc:
        logger.error(f"add_event: Gamma API error: {exc}")
        return False

    if not event:
        return False
    if event.closed:
        logger.warning(f"add_event: event '{slug}' jest zamknięty")
        return False

    db.upsert_event(
        slug=event.slug, event_id=event.id, title=event.title,
        end_date=event.end_date, source="manual",
    )
    tokens: list[str] = []
    for m in event.markets:
        db.upsert_market(
            condition_id=m.condition_id, event_slug=event.slug,
            question=m.question, token_yes_id=m.token_yes_id,
            token_no_id=m.token_no_id, end_date=m.end_date,
        )
        tokens.extend(m.all_token_ids)
    if tokens:
        await ws.subscribe(tokens)

    # Dopisz do config.yaml żeby przetrwało restart
    if slug not in config.manual_events:
        config.manual_events.append(slug)
        _save_manual_events(config_path, config.manual_events)
    return True


async def remove_event_callback(
    config: BotConfig,
    db: Database,
    ws: CLOBWebSocketManager,
    config_path: Path,
    slug: str,
) -> bool:
    if not db.get_event(slug):
        return False

    tokens_to_unsub: list[str] = []
    for m in db.list_markets_for_event(slug):
        if m["token_yes_id"]:
            tokens_to_unsub.append(m["token_yes_id"])
        if m["token_no_id"]:
            tokens_to_unsub.append(m["token_no_id"])
    db.remove_event(slug)
    if tokens_to_unsub:
        await ws.unsubscribe(tokens_to_unsub)

    if slug in config.manual_events:
        config.manual_events.remove(slug)
        _save_manual_events(config_path, config.manual_events)
    return True


def _save_manual_events(config_path: Path, manual_events: list[str]) -> None:
    """Zapisuje zaktualizowaną listę manual_events do config.yaml."""
    import yaml
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["manual_events"] = manual_events
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# =============================================================================
#  Main
# =============================================================================


async def main_async() -> None:
    config = load_config(CONFIG_PATH)
    setup_logging(config.advanced.log_level)
    logger.info("=" * 60)
    logger.info("🚀 Polymarket Bot startuje")
    logger.info(f"  Config: {CONFIG_PATH}")
    logger.info(f"  DB:     {DB_PATH}")
    logger.info(f"  Serie:  {config.auto_monitor_series}")
    logger.info(f"  Manual: {config.manual_events}")
    logger.info("=" * 60)

    # ------- Inicjalizacja komponentów -------
    db = Database(DB_PATH)
    ws = CLOBWebSocketManager(ws_url=config.advanced.clob_ws_url)
    detector = Detector(
        thresholds=DetectorThresholds(**config.thresholds.model_dump()),
        monitored_prices=tuple(config.monitored_prices),
    )
    sender = TelegramSender(
        bot_token=config.telegram.bot_token,
        chat_id=config.telegram.chat_id,
    )

    # Wczytaj stan pause z DB
    if db.kv_get("paused") == "1":
        sender.pause()
        logger.info("Bot wystartował w trybie PAUSE (z poprzedniego wyłączenia)")

    orchestrator = Orchestrator(config, db, ws, detector, sender)
    scheduler = DiscoveryScheduler(config, db, ws)

    # Re-subskrypcja: przy starcie odczytaj wszystkie znane tokeny z DB
    # i każ WS-ManagerOWI je zasubskrybować po pierwszym połączeniu
    initial_tokens: list[str] = []
    for m in db.list_all_markets():
        if m["token_yes_id"]:
            initial_tokens.append(m["token_yes_id"])
        if m["token_no_id"]:
            initial_tokens.append(m["token_no_id"])
    if initial_tokens:
        await ws.subscribe(initial_tokens)
        logger.info(f"Re-subskrypcja po restarcie: {len(initial_tokens)} tokenów")

    # ------- Telegram Application -------
    tg_app = Application.builder().token(config.telegram.bot_token).build()

    def runtime_stats() -> dict:
        return {
            "ws_connected": ws._ws is not None and not ws._ws.closed,
            "subscribed_tokens": len(ws._subscribed),
        }

    cmd_handlers = CommandHandlers(
        config=config,
        config_path=str(CONFIG_PATH),
        db=db,
        sender=sender,
        on_add_event=lambda slug: add_event_callback(
            config, db, ws, CONFIG_PATH, slug
        ),
        on_remove_event=lambda slug: remove_event_callback(
            config, db, ws, CONFIG_PATH, slug
        ),
        get_runtime_stats=runtime_stats,
    )
    cmd_handlers.register(tg_app)

    # ------- Sygnały - graceful shutdown -------
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _on_signal(sig_name: str) -> None:
        logger.warning(f"Otrzymano sygnał {sig_name} - graceful shutdown...")
        shutdown_event.set()

    for sig_name in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(
                getattr(signal, sig_name),
                lambda s=sig_name: _on_signal(s),
            )
        except NotImplementedError:
            # Windows nie wspiera niektórych sygnałów - olej
            pass

    # ------- Uruchom wszystkie taski -------
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)

    tasks = [
        asyncio.create_task(ws.start(), name="ws"),
        asyncio.create_task(orchestrator.run(), name="orchestrator"),
        asyncio.create_task(scheduler.run(), name="scheduler"),
    ]

    logger.success("✅ Bot wystartował - nasłuchuję")

    # Czekaj na sygnał shutdown lub na wywalenie się któregoś taska
    try:
        await shutdown_event.wait()
    finally:
        logger.info("Zamykanie...")
        scheduler.stop()
        orchestrator.stop()
        await ws.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        try:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception as exc:
            logger.warning(f"Błąd przy zamykaniu Telegrama: {exc!r}")

        db.close()
        logger.success("Bot zatrzymany czysto. Do widzenia 👋")


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Krytyczny błąd przy starcie bota")
        sys.exit(1)


if __name__ == "__main__":
    main()
