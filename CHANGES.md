# Historia zmian

## 2026-05-06 — Filtr "bid support" (wycisza alerty bez wsparcia w księdze)

### Cel zmian

Niektóre alerty na rynkach blisko 99.9¢ są mało znaczące — np. ktoś
zdjął dużego asku, ale pod ceną NIE ma żadnego buy wall-a, więc rynek
może równie dobrze szybko się odwrócić. Filtr **bid support** wycina
takie alerty: jeśli na **bidzie** (limit buy) na cenie 99.7¢ po stronie
alertu nie ma co najmniej 1 share — alert jest wyciszony.

Logika:

1. Każdy alert (A/B/C/D + burst-drop) przechodzi przez filtr **PRZED**
   routing-iem (cooldown / bufor / burst).
2. Filtr sprawdza `ws.get_book(alert.token_id).bids` na cenie DOKŁADNIE
   `required_price` (domyślnie 0.997 = 99.7¢, parametr).
3. Jeśli suma shares < `min_total_shares` (domyślnie 1) → wycisz +
   log INFO. Inaczej → leci dalej zwykłym pipeline'em.
4. Wyciszony alert **NIE zajmuje miejsca w buforze konsolidacji**.

### Co zostało zmienione

#### Nowy plik `bot/alerts/bid_support.py`

- `BidSupportResult` dataclass: `has_support: bool` + `shares_at_price`,
  `required_price`, `side` (do logowania).
- `check_bid_support(order_book, side, required_price, min_shares=1.0)`
  — pure function: zlicza `bids` na DOKŁADNEJ cenie z tolerancją
  float `1e-9`. Order book `None` → False (bezpieczna defaulta).

#### Zmodyfikowany `bot/config.py` + `config.example.yaml`

Nowa sekcja:
```yaml
bid_support_filter:
  enabled: true
  required_price: 0.997          # 99.7¢ jako ułamek (spójne z monitored_prices)
  min_total_shares: 1
```

UWAGA: Świadomie używamy ułamka `0.997` zamiast centów `99.7` —
spójność z `monitored_prices: [0.998, 0.999]` w configu. W logach
i tak pokazuję `99.7¢` (czytelność).

#### Zmodyfikowany `bot/main.py` (Orchestrator)

- Nowe metody:
  - `_bid_support_check(alert)` → `None` (filtr OFF) | `BidSupportResult`
  - `_log_silenced(alert, market, result)` → log INFO w formacie spec
- `_maybe_send` rozszerzone: filtr leci PRZED wszystkim (przed burst-drop,
  przed cooldown, przed buffer). Wyciszone alerty wracają natychmiast.

Format logu wyciszenia (per spec):
```
INFO | Alert wyciszony (brak bid support) | event=bitcoin-may-7
       market=$86,000 side=NO alert_type=A price_cents=99.7 shares=0
```

### Testy

Nowych testów: **+28** (od 86 → 114), wszystkie zielone.

- `TestCheckBidSupport` (13) — pure function: granice 0/1/1000, dokładna
  cena (0.998/0.996 nie liczą), pusty book / None, side echo, precyzja
  float, asks NIE liczone, parametryzacja `min_shares`.
- `TestBidSupportIntegration` (15) — E2E w Orchestratorze:
  - alert NO/YES bez bidu → wyciszony
  - 1 share / 1000 shares → leci
  - bidy na 0.998 / 0.996 → nie pomagają
  - filtr per typ A/B/C/D
  - burst-drop też filtrowany
  - filtr wyłączony → wszystko leci
  - **3 alerty, 1 wyciszony → bufor ma 2** (test interakcji z konsolidacją)
  - log INFO zawiera wszystkie wymagane pola

### Edytowane pliki

```
bot/alerts/bid_support.py       (NOWY)
bot/config.py                   (+BidSupportFilterConfig)
bot/main.py                     (filtr w _maybe_send + log)
config.example.yaml             (+sekcja bid_support_filter)
tests/test_alerts.py            (+28 testów)
README.md                       (sekcja "Filtr bid support")
CHANGES.md                      (ten wpis)
```

### Tuning filtra

Filtr jest dostrajany przez 3 parametry w `config.yaml`:

- **Wyłączyć** całkowicie: `enabled: false`.
- **Zmniejszyć** czułość (mniej wyciszeń): `min_total_shares: 100`
  (wymagaj co najmniej 100 shares).
- **Inna cena** referencyjna: `required_price: 0.996` (sprawdza 99.6¢).

Po tygodniu można sprawdzić, ile alertów filtr wycisza:
```
docker compose logs --since 7d | grep "Alert wyciszony"
```

### Backward compatibility

Sekcja `bid_support_filter` jest opcjonalna w configu — Pydantic ma
defaulty (enabled: true, required_price: 0.997, min_total_shares: 1).
Stare configi działają bez zmian (filtr automatycznie ON).

### Rollback

Najszybciej: `bid_support_filter.enabled: false` w `config.yaml` +
`docker compose restart`. Trwale: `git revert <commit>`.

---

## 2026-05-06 — Konsolidacja alertów per event + nowy format wiadomości

### Cel zmian

Stary układ "1 alert = 1 wiadomość" zalewał Telegrama, gdy detector wykrył
naraz kilka alertów dla tego samego eventu (np. `bitcoin-above` ma 5 podrynków,
wszystkie blisko 99.9¢ — wielokrotne wyzwolenie naraz dawało wiele wiadomości
pod rząd). Nowe wymagania:

1. **Konsolidacja per event** — wszystkie alerty z tego samego eventu
   wpadające w okno N sekund pokazują się jako **jedna** wiadomość ze
   skonsolidowaną listą.
2. **Burst-drop instant** — alert A z dramatycznym spadkiem (>5k shares)
   omija bufor i leci natychmiast jako osobna wiadomość.
3. **Zwięzły, czytelny format** — pełny tytuł eventu w nagłówku, jedna
   linia per alert (`🔻 $86,000 NO 99,9¢ — pozostało 25k (↓ 8k)`),
   sortowanie po wartości progu.

### Co zostało zmienione

#### Nowy plik `bot/alerts/buffer.py`

`AlertBuffer` — async bufor z debounce timerem per `event_slug`.

- Pierwszy alert dla event_slug startuje `asyncio.Task` z `sleep(window)`.
- Kolejne alerty w trakcie okna **dorzucane** do listy; **timer NIE jest
  resetowany**.
- Po upłynięciu okna → callback `on_flush(slug, alerts)` z pełną listą.
- `shutdown()` cancel-uje pending timery i synchronicznie flushuje
  wszystkie bufory (żeby SIGTERM nie gubił alertów).

#### Zmodyfikowany `bot/alerts/formatter.py`

Usunięte legacy:
- `format_alert(...)` (stary format)
- `_fmt_price`, `_fmt_int`, `_polymarket_url` (helpery legacy)
- `ALERT_EMOJI`, `ALERT_TITLE` (mapowania legacy)

Dodane:
- `format_shares(n)` — `"516"` / `"5.5k"` / `"30k"` / `"1.2M"` (truncation)
- `series_icon(slug)` — `bitcoin-*`→₿, `ethereum-*`→Ξ, `sp-500-*`/`s-and-p-*`→📈,
  `btc-*`/`eth-*` jako aliasy, default→🎯
- `market_short(question)` — wyciąga największą kwotę `$X,XXX` regexem,
  lub truncate do 40 znaków z `…`
- `market_sort_key(question)` — klucz sortowania (kwota progu malejąco,
  brak kwoty na końcu listy)
- `_fmt_price_pl(0.999)` → `"99,9"` (przecinek dziesiętny)
- `format_alert_line(alert, question)` — pojedyncza linia
- `format_consolidated_message(alerts, title, slug, now)` — wiadomość
  skonsolidowana
- `format_burst_drop_message(alert, question, title, slug, now)` —
  wiadomość burst-drop (z pustą linią po linku + `⚡ Burst-drop ...`)
- `ALERT_TYPE_EMOJI` — nowe mapowanie `A→🔻 B→💰 C→📤 D→🛑`

#### Zmodyfikowany `bot/main.py` (Orchestrator)

- `Orchestrator.__init__` przyjmuje teraz `buffer: AlertBuffer`.
- `_maybe_send` przepisane na 3-drogowy router:
  - alert z `bypass_cooldown=True` → `_send_burst_drop` (instant)
  - inny alert → cooldown gate → `buffer.add(...)` (czeka na okno)
- Nowa metoda `on_buffer_flush(slug, alerts)` — callback z bufora,
  wywołuje `format_consolidated_message` i `sender.send_html`.
- `main_async()` tworzy `AlertBuffer` i przekazuje do Orchestratora.
- W `finally` jest `await buffer.shutdown()` przed zamknięciem Telegrama.

#### Zmodyfikowany `bot/config.py` + `config.example.yaml`

Nowe pole konfiguracji:
```yaml
aggregation_window_seconds: 30   # debounce per event
```

#### Naprawiony `bot/polymarket/clob_ws.py`

`WSEvent` używa `Union[...]` zamiast `X | Y` — kompatybilność z
Pythonem 3.9 (lokalne testy). Produkcja w Dockerze nadal na 3.11+.

#### Nowy `pyproject.toml`

Konfiguracja `pytest-asyncio` (`asyncio_mode = "auto"`) - async testy
buf'a działają bez dekoratora `@pytest.mark.asyncio`.

### Testy

Nowych testów: **+66** (od 20 → 86), wszystkie zielone.

Klasy testów:
- `TestFormatShares` — granice + edge (15)
- `TestSeriesIcon` — mapowania serii (8)
- `TestMarketShort` — skracanie tytułów (7)
- `TestMarketSortKey` — sortowanie po cenie (4)
- `TestAlertLine` — pojedyncza linia (8)
- `TestBurstDropMessage` — struktura burst-drop (3)
- `TestConsolidatedMessage` — struktura skonsolidowanej (9)
- `TestAlertBuffer` — async buffer (10) — w tym test "timer nie jest resetowany"
- `TestE2EBufferIntegration` — sanity end-to-end (2)

### Edytowane pliki

```
bot/alerts/buffer.py            (NOWY)
bot/alerts/formatter.py         (przepisany)
bot/config.py                   (+aggregation_window_seconds)
bot/main.py                     (Orchestrator + integracja)
bot/polymarket/clob_ws.py       (Union zamiast |)
config.example.yaml             (+aggregation_window_seconds)
pyproject.toml                  (NOWY)
tests/test_alerts.py            (+66 testów)
```

### Backward compatibility

Stara funkcja `format_alert` była używana TYLKO w `main.py` (1 wywołanie)
i tylko w `bot/`. Brak wersji "publicznej" do utrzymania. Bezpieczne
usunięcie.

### Rollback

Zmiany w jednym commicie - prosty `git revert` cofa wszystko. SQLite ma
nowe pole `events.title` używane przez Orchestrator (już istniało, ale
teraz wymagane do format_consolidated_message). W razie problemu można
też tylko zmniejszyć `aggregation_window_seconds: 1` żeby praktycznie
wyłączyć agregację (każdy alert po 1s leci jako "skonsolidowana
wiadomość z 1 elementem").
