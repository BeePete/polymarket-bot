"""
Scheduler - cyklicznie odświeża listę monitorowanych eventów.

Co `discovery_interval_seconds` (domyślnie 30 min):
  1. Pobiera aktywne eventy z Gamma API.
  2. Dla każdej serii w `auto_monitor_series` znajduje eventy z prefiksem slug.
  3. Dodaje nowe eventy do bazy + subskrybuje ich tokeny w WebSocket.
  4. Usuwa eventy które już się zamknęły lub są dalej niż
     `monitor_hours_before_close` od teraz.
  5. Sprawdza też `manual_events` z config.yaml (dla nowo dodanych ręcznie).

Scheduler nie sprawdza order booka - od tego jest WebSocket + detektor.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from .polymarket.gamma_api import GammaAPIClient, GammaAPIError
from .polymarket.models import Event

if TYPE_CHECKING:
    from .config import BotConfig
    from .polymarket.clob_ws import CLOBWebSocketManager
    from .storage.db import Database


class DiscoveryScheduler:
    """Pętla auto-discovery i obsługi cyklu życia eventów."""

    def __init__(
        self,
        config: "BotConfig",
        db: "Database",
        ws_manager: "CLOBWebSocketManager",
    ):
        self.config = config
        self.db = db
        self.ws = ws_manager
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Pętla główna - blokująca, puszczać jako asyncio task."""
        # Pierwszy przebieg natychmiast (żeby po starcie szybko się zasubskrybować)
        await self._discovery_pass(initial=True)

        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.config.discovery_interval_seconds,
                )
                # Doczekaliśmy się stop -> wyjście z pętli
                break
            except asyncio.TimeoutError:
                # Timeout = czas na kolejne discovery
                pass

            await self._discovery_pass(initial=False)

    def stop(self) -> None:
        self._stop.set()

    # -------------------------------------------------------------------------
    # Logika
    # -------------------------------------------------------------------------

    async def _discovery_pass(self, initial: bool) -> None:
        """Pojedynczy przebieg auto-discovery."""
        try:
            await self._do_discovery(initial)
        except GammaAPIError as exc:
            logger.error(f"Scheduler: błąd Gamma API: {exc}")
        except Exception as exc:
            logger.exception(f"Scheduler: nieoczekiwany błąd discovery: {exc!r}")

    async def _do_discovery(self, initial: bool) -> None:
        all_events: list[Event] = []

        async with GammaAPIClient(
            base_url=self.config.advanced.gamma_api_url,
            timeout_seconds=30,
            max_retries=3,
        ) as client:
            # 1) Eventy z serii
            for prefix in self.config.auto_monitor_series:
                try:
                    matches = await client.find_events_in_series(
                        prefix, limit=self.config.advanced.gamma_fetch_limit,
                    )
                    for ev in matches:
                        ev._series_prefix = prefix  # type: ignore[attr-defined]
                        all_events.append(ev)
                except GammaAPIError as exc:
                    logger.warning(f"Scheduler: błąd dla serii '{prefix}': {exc}")

            # 2) Eventy ręczne (z config.yaml + dodane przez /add)
            for slug in list(self.config.manual_events):
                try:
                    ev = await client.get_event_by_slug(slug)
                    if ev:
                        all_events.append(ev)
                    else:
                        logger.warning(f"Scheduler: manual event '{slug}' nie znaleziono")
                except GammaAPIError as exc:
                    logger.warning(f"Scheduler: błąd dla manual '{slug}': {exc}")

        # Filtruj eventy: niezamknięte i zamykające się w odpowiednim oknie
        max_hours = self.config.monitor_hours_before_close
        kept: list[Event] = []
        for ev in all_events:
            if ev.closed:
                continue
            hours = ev.hours_to_close()
            if hours is None:
                # Brak end_date - akceptujemy (lepiej za dużo niż za mało)
                kept.append(ev)
                continue
            if hours <= 0:
                continue  # już po końcu
            if hours > max_hours:
                continue  # za daleko w przyszłość
            kept.append(ev)

        # Deduplikacja po slug (event z serii + manual mogą się nakładać)
        kept_by_slug: dict[str, Event] = {}
        for ev in kept:
            kept_by_slug.setdefault(ev.slug, ev)

        # Zaktualizuj bazę + subskrypcje WebSocket
        await self._reconcile_db_and_ws(kept_by_slug, initial=initial)

        logger.info(
            f"Scheduler: discovery zakończone - aktywnych eventów: {len(kept_by_slug)}"
        )

    async def _reconcile_db_and_ws(
        self, current: dict[str, Event], initial: bool
    ) -> None:
        """
        Synchronizuje stan w bazie i WebSocketach z aktualną listą eventów.
        - Dodaje nowe eventy/rynki, subskrybuje ich tokeny.
        - Usuwa eventy które przestały być aktywne, odsubskrybuje tokeny.
        - Eventy 'manual' z config.manual_events nie są usuwane przez auto-cleanup
          (chyba że się zamkną).
        """
        existing_events = {row["slug"]: row for row in self.db.list_events()}

        # 1) Dodaj/zaktualizuj
        tokens_to_subscribe: list[str] = []
        for slug, ev in current.items():
            source = "manual" if slug in self.config.manual_events else "auto"
            series_prefix = getattr(ev, "_series_prefix", None) if source == "auto" else None
            self.db.upsert_event(
                slug=ev.slug,
                event_id=ev.id,
                title=ev.title,
                end_date=ev.end_date,
                source=source,
                series_prefix=series_prefix,
            )
            for m in ev.markets:
                self.db.upsert_market(
                    condition_id=m.condition_id,
                    event_slug=ev.slug,
                    question=m.question,
                    token_yes_id=m.token_yes_id,
                    token_no_id=m.token_no_id,
                    end_date=m.end_date,
                )
                tokens_to_subscribe.extend(m.all_token_ids)

        # 2) Usuń eventy których już nie ma w current (zamknięte / wygasłe)
        tokens_to_unsubscribe: list[str] = []
        for slug, row in existing_events.items():
            if slug in current:
                continue
            # Manualnie dodane przez config.yaml ZOSTAJĄ - sam użytkownik je zdjął.
            # Ale jeśli się zamknęły (nie ma w current przez filtr czasowy) - usuń.
            for m in self.db.list_markets_for_event(slug):
                if m["token_yes_id"]:
                    tokens_to_unsubscribe.append(m["token_yes_id"])
                if m["token_no_id"]:
                    tokens_to_unsubscribe.append(m["token_no_id"])
            self.db.remove_event(slug)
            logger.info(f"Scheduler: usunięto event '{slug}' (zamknięty/wygasły)")

        # 3) Subskrypcje
        if tokens_to_subscribe:
            await self.ws.subscribe(tokens_to_subscribe)
        if tokens_to_unsubscribe:
            await self.ws.unsubscribe(tokens_to_unsubscribe)
