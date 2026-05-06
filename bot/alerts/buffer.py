"""
AlertBuffer - bufor pośredni między detektorem a wysyłką.

Idea (debounce per event):
  - Pierwszy alert dla danego event_slug startuje timer N sekund.
  - Kolejne alerty dla TEGO SAMEGO event_slug w trakcie okna -
    dorzucane do listy, timer NIE jest resetowany.
  - Po upłynięciu okna - wszystkie zebrane alerty lecą do callbacku
    `on_flush(slug, alerts)` jako jedna skonsolidowana wiadomość.

Burst-drop NIE używa tego bufora - leci osobną drogą (instant). Jeśli
dla event_slug jest aktywny timer agregacji, NIE jest on przerywany
przez burst-drop - pracuje dalej niezależnie.

Wzorzec async: każdy event_slug ma własny `asyncio.Task` z `sleep(N)`
i flush. Bufor i mapa task-ów są chronione przez `asyncio.Lock`.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from loguru import logger

from .detector import Alert


@dataclass
class BufferedAlert:
    """Alert w buforze + jego kontekst (market_question) + czas detekcji."""

    alert: Alert
    market_question: str
    detected_at: float = field(default_factory=time.monotonic)


# Sygnatura callbacku flush: async (event_slug, list[BufferedAlert]) -> None
FlushCallback = Callable[[str, list[BufferedAlert]], Awaitable[None]]


class AlertBuffer:
    """
    Bufor agregujący alerty per event_slug z debounce timerem.

    Użycie:
        buf = AlertBuffer(window_seconds=30, on_flush=my_async_handler)
        await buf.add(event_slug, alert, market_question)
        ...
        await buf.shutdown()    # przy wyłączaniu - flush wszystkich pending
    """

    def __init__(
        self,
        window_seconds: float,
        on_flush: FlushCallback,
    ):
        if window_seconds <= 0:
            raise ValueError("window_seconds musi być > 0")
        self.window_seconds = float(window_seconds)
        self._on_flush = on_flush

        self._buffers: dict[str, list[BufferedAlert]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._shutting_down = False

    # -------------------------------------------------------------------------
    # API publiczne
    # -------------------------------------------------------------------------

    async def add(
        self,
        event_slug: str,
        alert: Alert,
        market_question: str,
    ) -> None:
        """Dodaje alert do bufora dla event_slug; startuje timer jeśli pierwszy."""
        if self._shutting_down:
            logger.debug("AlertBuffer: shutdown - pomijam add()")
            return

        async with self._lock:
            buffered = BufferedAlert(
                alert=alert, market_question=market_question,
            )
            existing = self._buffers.get(event_slug)
            if existing is None:
                # Pierwszy alert dla tego eventu - twórz bufor + timer
                self._buffers[event_slug] = [buffered]
                self._tasks[event_slug] = asyncio.create_task(
                    self._flush_after_delay(event_slug),
                    name=f"alertbuf-{event_slug[:30]}",
                )
                logger.debug(
                    f"AlertBuffer: nowy bufor dla '{event_slug}' "
                    f"(flush za {self.window_seconds}s)"
                )
            else:
                # Kolejny alert w oknie - DORZUĆ, timer NIE resetowany
                existing.append(buffered)
                logger.debug(
                    f"AlertBuffer: dodano alert {alert.alert_type} do bufora "
                    f"'{event_slug}' (size={len(existing)})"
                )

    async def shutdown(self) -> None:
        """
        Czysto kończy bufor - flushuje wszystkie pending bufory natychmiast
        (bez czekania na timer) i czeka na ich zakończenie.
        Po wywołaniu add() już nic nie wkłada.

        UWAGA: każde flushowanie wywoła on_flush, więc orchestrator dostanie
        normalne wiadomości ze skonsolidowanymi alertami. To jest pożądane
        zachowanie przy SIGTERM - nie chcemy gubić pending alertów.
        """
        self._shutting_down = True

        # Skopiuj klucze i task-i poza lockiem
        async with self._lock:
            slugs = list(self._buffers.keys())
            tasks = list(self._tasks.values())

        # 1. Cancel timery (żeby nie czekać do końca okna)
        for t in tasks:
            t.cancel()

        # 2. Synchronicznie wyflushuj każdy event_slug w głównym task'u shutdown.
        #    NIE używamy `await task` - cancelled task propaguje CancelledError.
        for slug in slugs:
            try:
                await self._do_flush(slug)
            except Exception as exc:
                logger.warning(f"AlertBuffer: błąd flush podczas shutdown: {exc!r}")

        # 3. Pozwól zcancel-owanym taskom się zakończyć (sprzątanie loop-a).
        #    return_exceptions=True - CancelledError od task nie propaguje.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    async def _flush_after_delay(self, event_slug: str) -> None:
        """
        Czeka window_seconds, potem flushuje. Jeśli zostanie zcancelowany
        (np. shutdown) - od razu kończy się bez flush (shutdown sam wywoła
        flush poza tą koroutyną, żeby uniknąć dwóch flushów na tym samym
        event_slug i propagacji CancelledError).
        """
        try:
            await asyncio.sleep(self.window_seconds)
        except asyncio.CancelledError:
            return  # shutdown sam zrobi _do_flush
        await self._do_flush(event_slug)

    async def _do_flush(self, event_slug: str) -> None:
        async with self._lock:
            alerts = self._buffers.pop(event_slug, None)
            self._tasks.pop(event_slug, None)
        if not alerts:
            return
        try:
            await self._on_flush(event_slug, alerts)
        except Exception as exc:
            logger.exception(
                f"AlertBuffer: błąd w callbacku on_flush dla '{event_slug}': {exc!r}"
            )

    # -------------------------------------------------------------------------
    # Diagnostyka (do testów / komendy /status)
    # -------------------------------------------------------------------------

    def pending_count(self) -> dict[str, int]:
        """Zwraca {event_slug: liczba_alertow_w_buforze}."""
        return {slug: len(lst) for slug, lst in self._buffers.items()}
