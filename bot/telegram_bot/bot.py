"""
Wysyłanie wiadomości na Telegram.

Używamy biblioteki python-telegram-bot v21+ (asyncio-friendly).
Klasa TelegramSender opakowuje Bota i daje pojedynczą metodę send_html()
która łyka wszystkie błędy (timeout, brak internetu) i je loguje, żeby
nigdy nie wywaliła głównej pętli bota.
"""
from __future__ import annotations

import asyncio

from loguru import logger
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError


class TelegramSender:
    """Cienka otoczka na telegram.Bot - wysyłanie z retry i obsługą błędów."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot = Bot(token=bot_token)
        self.chat_id = str(chat_id)
        self._paused = False
        self._send_lock = asyncio.Lock()

    async def send_html(self, message: str, disable_preview: bool = True) -> bool:
        """
        Wysyła wiadomość HTML. Zwraca True jeśli udało, False jeśli błąd
        (ale NIE rzuca wyjątku - bot ma działać dalej).
        """
        if self._paused:
            logger.debug("Telegram: w trybie pause - pomijam wysyłkę")
            return False

        async with self._send_lock:
            for attempt in range(1, 4):
                try:
                    await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=message,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=disable_preview,
                    )
                    return True
                except TelegramError as exc:
                    logger.warning(
                        f"Telegram: błąd wysyłki (próba {attempt}): {exc!r}"
                    )
                    await asyncio.sleep(2 ** attempt)
                except Exception as exc:
                    logger.error(f"Telegram: nieoczekiwany błąd: {exc!r}")
                    return False
            return False

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused
