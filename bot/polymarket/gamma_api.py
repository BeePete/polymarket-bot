"""
Klient Gamma API - pobiera listy eventów i szczegóły z REST-owego API
Polymarket. Używamy tego do "auto-discovery" - szukania nowych eventów
z monitorowanych serii (np. codzienne 'bitcoin-above-...').

Gamma API NIE wymaga autoryzacji dla publicznych endpointów.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from loguru import logger

from .models import Event, parse_event


class GammaAPIError(Exception):
    """Błąd komunikacji z Gamma API."""


class GammaAPIClient:
    """
    Asynchroniczny klient Gamma API z prostym retry.
    Używaj jako async context manager:
        async with GammaAPIClient() as client:
            events = await client.get_active_events()
    """

    def __init__(
        self,
        base_url: str = "https://gamma-api.polymarket.com",
        timeout_seconds: int = 30,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.max_retries = max_retries
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "GammaAPIClient":
        self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._session:
            raise RuntimeError("Użyj GammaAPIClient jako async context manager.")

        url = f"{self.base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 429:
                        # Rate limit - poczekaj dłużej
                        wait = 2 ** attempt
                        logger.warning(
                            f"Gamma API: rate limit (429), próba {attempt}, "
                            f"odczekuję {wait}s"
                        )
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 500:
                        wait = 2 ** attempt
                        logger.warning(
                            f"Gamma API: błąd serwera {resp.status}, próba {attempt}, "
                            f"odczekuję {wait}s"
                        )
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                wait = 2 ** attempt
                logger.warning(
                    f"Gamma API: błąd sieci ({exc!r}), próba {attempt}, "
                    f"odczekuję {wait}s"
                )
                await asyncio.sleep(wait)

        raise GammaAPIError(
            f"Gamma API: nie udało się pobrać {url} po {self.max_retries} próbach"
            f" (ostatni błąd: {last_error!r})"
        )

    # -------------------------------------------------------------------------
    # Wysokopoziomowe metody
    # -------------------------------------------------------------------------

    async def get_active_events(self, limit: int = 500) -> list[Event]:
        """
        Pobiera aktywne, niezamknięte eventy.
        UWAGA: Gamma API nie ma filtra slug__contains, więc filtrowanie
        po prefiksie robimy po stronie bota.
        """
        data = await self._get(
            "/events",
            params={
                "closed": "false",
                "active": "true",
                "limit": str(limit),
                "order": "endDate",
                "ascending": "true",
            },
        )
        if not isinstance(data, list):
            raise GammaAPIError(f"Niespodziewana odpowiedź /events: {type(data)}")
        return [parse_event(raw) for raw in data]

    async def get_event_by_slug(self, slug: str) -> Event | None:
        """Szuka pojedynczego eventu po slug. Zwraca None jeśli nie znaleziono."""
        data = await self._get("/events", params={"slug": slug})
        if not isinstance(data, list) or not data:
            return None
        # API zwraca listę nawet dla pojedynczego slug - bierzemy pierwszy
        return parse_event(data[0])

    async def find_events_in_series(
        self, prefix: str, limit: int = 500
    ) -> list[Event]:
        """
        Znajduje aktywne eventy, których slug zaczyna się od `prefix`.
        Np. prefix='bitcoin-above' łapie:
            bitcoin-above-70000-on-may-7
            bitcoin-above-72000-on-may-8
        """
        all_events = await self.get_active_events(limit=limit)
        prefix_lower = prefix.lower()
        matches = [e for e in all_events if e.slug.lower().startswith(prefix_lower)]
        logger.debug(
            f"Auto-discovery '{prefix}': sprawdzono {len(all_events)} eventów, "
            f"dopasowano {len(matches)}"
        )
        return matches
