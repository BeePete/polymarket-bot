# 🎯 Polymarket Bot → Telegram

Bot monitorujący rynki na Polymarket i wysyłający alerty na Telegrama,
gdy wykryje ciekawe ruchy w rynkach blisko 99.9¢ (rynki "praktycznie
rozstrzygnięte"). Pisany od zera w Pythonie, deployowany na Hetzner VPS
w Dockerze.

---

## 📑 Spis treści

1. [Co bot robi](#-co-bot-robi)
2. [Część 1: Stworzenie bota Telegram](#-część-1-stworzenie-bota-telegram)
3. [Część 2: Założenie Hetzner VPS](#-część-2-założenie-hetzner-vps)
4. [Część 3: Pierwsze logowanie na serwer](#-część-3-pierwsze-logowanie-na-serwer)
5. [Część 4: Podstawowa konfiguracja serwera](#-część-4-podstawowa-konfiguracja-serwera)
6. [Część 5: Pobranie i konfiguracja bota](#-część-5-pobranie-i-konfiguracja-bota)
7. [Część 6: Uruchomienie](#-część-6-uruchomienie)
8. [Część 7: Co robić jak coś nie działa](#-część-7-co-robić-jak-coś-nie-działa)
9. [Część 8: Aktualizacja bota](#-część-8-aktualizacja-bota)
10. [Część 9: Bezpieczeństwo](#-część-9-bezpieczeństwo)
11. [Komendy Telegram](#-komendy-telegram)
12. [Architektura i API](#-architektura-i-api)

---

## 🎯 Co bot robi

Bot monitoruje **eventy** na Polymarket. Każdy event ma kilka **rynków**
(np. "Bitcoin above $70,000 on May 6"). Bot obserwuje TYLKO te rynki,
gdzie któraś strona (YES albo NO) ma cenę **99.8¢** lub **99.9¢**.

Wysyła 4 typy alertów (zawsze tylko dla strony "blisko 99.9¢"):

| Typ | Kiedy | Próg domyślny |
|-----|-------|----------------|
| 🔴 **A — Ask topnieje** | Suma asków na 99.8/99.9¢ spadła poniżej progu | 30,000 shares |
| 💰 **B — Duży market buy** | Wykonano BUY na 99.8/99.9¢ o rozmiarze ≥ progu | 5,000 shares |
| 📤 **C — Nowy sell order** | Pojawił się nowy ask ≥ progu | 5,000 shares |
| 🛑 **D — Duży limit buy** | Pojawił się nowy bid ≥ progu | 19,000 shares |

Z **cooldownem** 5 minut między alertami tego samego typu dla tego samego
rynku — żeby nie zalewać spamu. Wyjątek: alert A z spadkiem o > 5,000
shares wysyła się natychmiast, ignorując cooldown.

### Konsolidacja per event (debounce 30s)

Zamiast wysyłać każdy alert osobno (5 alertów = 5 wiadomości), bot **agreguje
alerty z tego samego eventu** w 30-sekundowym oknie i wysyła **jedną
skonsolidowaną wiadomość**. Dzięki temu jeśli kilka podrynków eventu
"Bitcoin Above ___" odpali alerty naraz, dostajesz jedną czytelną
wiadomość zamiast 5 powiadomień pod rząd.

- Pierwszy alert dla eventu startuje timer **30 s** (parametr
  `aggregation_window_seconds`).
- Kolejne alerty dla **tego samego eventu** w trakcie okna → dorzucane do
  listy. **Timer NIE jest resetowany** — żeby okno nie rozjeżdżało się
  w nieskończoność.
- Po 30 s → **jedna wiadomość** ze wszystkimi zebranymi alertami,
  posortowanymi malejąco po wartości progu (najwyższe `$XX,XXX` na górze).

#### Wyjątek: burst-drop instant

Alert A z spadkiem >5,000 shares **omija bufor** i leci natychmiast jako
osobna wiadomość — żeby nie tracić informacji o kluczowym, gwałtownym
ruchu na rynku. Bufor dla tego eventu pracuje dalej niezależnie.

### Format wiadomości

**Skonsolidowana** (najczęstsza):

```
₿ Bitcoin Above ___ on May 6

🔻 $90,000 NO 99,9¢ — pozostało 25k (↓ 8k)
🔻 $86,000 NO 99,9¢ — pozostało 28k (↓ 5k)
🔻 $80,000 NO 99,8¢ — pozostało 22k (↓ 12k)

https://polymarket.com/event/bitcoin-above-on-may-6
🕒 14:23
```

**Burst-drop** (gwałtowny spadek, instant):

```
₿ Bitcoin Above ___ on May 6

🔻 $86,000 NO 99,9¢ — pozostało 22k (↓ 13k)

https://polymarket.com/event/bitcoin-above-on-may-6

⚡ Burst-drop — alert poza cooldownem
🕒 14:23
```

#### Mapowania w wiadomości

- **Ikona w nagłówku** zależy od slug-a eventu:
  - `bitcoin-*` / `btc-*` → **₿**
  - `ethereum-*` / `eth-*` → **Ξ**
  - `sp-500-*` / `s-and-p-*` → **📈**
  - inne → **🎯**

- **Emoji typu alertu** w linii podrynku:
  - **🔻** A — Ask topnieje
  - **💰** B — Duży market buy
  - **📤** C — Nowy sell order
  - **🛑** D — Duży limit buy ("rynek zamknięty")

- **Skrót podrynku** — wyłuskany z tytułu (`Will Bitcoin reach $86,000` →
  `$86,000`). Jeśli tytuł nie ma kwoty dolarowej, truncate do 40 znaków.

- **Cena** w polskiej notacji z przecinkiem: `99,9¢` zamiast `99.9¢`.

- **Liczby udziałów** skracane:
  - `<1000` → `516`
  - `1k–10k` → `5.5k`, `9.9k`
  - `10k–1M` → `30k`, `123k`
  - `≥1M` → `1.2M`

### Filtr "bid support"

Niektóre alerty są mało znaczące, jeśli pod ceną rynkową **NIE ma
żadnego buy wall-a** (kupujących z limit orderem). Bot ma to wycinać
filtrem **bid support**:

> Każdy alert (A/B/C/D + burst-drop) jest puszczany dalej **tylko jeśli**
> po stronie alertu (YES albo NO) na BIDZIE na cenie **dokładnie 99,7¢**
> jest co najmniej **1 share**. Inaczej — alert jest wyciszony i
> zalogowany jako INFO.

**Przykład:**
- Alert na "Bitcoin reach $86,000" NO 99,9¢: ask topnieje do 25k.
- Sprawdzamy book NO: czy na bidzie 99,7¢ ktoś chce kupić ≥ 1 share?
  - **TAK** (np. 100 shares) → alert leci do bufora konsolidacji.
  - **NIE** (0 shares na 99,7¢, choćby były bidy na 99,8¢) → wyciszony.

Filtr jest konfigurowalny w `config.yaml`:

```yaml
bid_support_filter:
  enabled: true              # globalny włącznik
  required_price: 0.997      # 99,7¢ jako ułamek
  min_total_shares: 1        # minimalna suma shares na required_price
```

**Tuning** — po tygodniu sprawdź ile alertów filtr wyciszył:

```bash
docker compose logs --since 7d | grep "Alert wyciszony"
```

Format wpisu w logach (per wyciszenie):

```
INFO | Alert wyciszony (brak bid support) | event=bitcoin-may-7
       market=$86,000 side=NO alert_type=A price_cents=99.7 shares=0
```

Jeśli filtr wycisza za dużo — zwiększ `required_price` (np. `0.996`)
albo wyłącz przez `enabled: false`.

---

## 🤖 Część 1: Stworzenie bota Telegram

### Krok 1.1 — Otwórz BotFather

1. Otwórz aplikację Telegram (na telefonie lub komputerze).
2. W wyszukiwarce na górze wpisz: **`@BotFather`**
3. Kliknij ten z niebieskim ✓ (oficjalny).
4. Wciśnij **Start** (jeśli pierwszy raz) lub po prostu napisz wiadomość.

### Krok 1.2 — Stwórz nowego bota

W rozmowie z BotFather wpisz:

```
/newbot
```

BotFather zapyta o:

1. **Imię bota** (display name) — np. `Polymarket Alerts`. Może być z polskimi znakami.
2. **Username bota** — MUSI kończyć się na `bot`. Np. `moj_polymarket_bot`. Musi być unikalny w całym Telegramie.

Po sukcesie BotFather odeśle wiadomość zawierającą:

```
Use this token to access the HTTP API:
123456789:AAEhBP0av-XXXXX-XXXXX-XXXXX
```

**Skopiuj ten token i zachowaj** — będzie potrzebny w `config.yaml`.
**Nikomu go nie pokazuj** (ktoś z tym tokenem ma pełną kontrolę nad botem).

### Krok 1.3 — Napisz coś do swojego bota

W aplikacji Telegram znajdź swojego bota (po username z poprzedniego kroku)
i kliknij **Start** + napisz cokolwiek (np. `cześć`). To krytyczne — bez
pierwszej wiadomości od Ciebie nie znajdziesz `chat_id`.

### Krok 1.4 — Znajdź swój `chat_id`

Otwórz w przeglądarce ten link (zamień `<TWÓJ_TOKEN>` na token z kroku 1.2):

```
https://api.telegram.org/bot<TWÓJ_TOKEN>/getUpdates
```

Przykład pełnego linku:

```
https://api.telegram.org/bot123456789:AAEhBP0av-XXXXX/getUpdates
```

Zobaczysz JSON-a, w którym szukaj fragmentu:

```json
"chat":{"id":987654321,"first_name":"Jan", ...}
```

Liczba **987654321** (Twoja będzie inna) to Twój **chat_id**. Zapisz.

> Jeśli widzisz `"result":[]` — znaczy, że Twój bot nie dostał jeszcze
> żadnej wiadomości. Wróć do kroku 1.3 i napisz cokolwiek do bota.

### Krok 1.5 — Co masz teraz

Powinieneś mieć dwa stringi:

- **bot_token**: `123456789:AAEhBP0av-XXXXX-XXXXX-XXXXX`
- **chat_id**: `987654321`

Schowaj je w bezpiecznym miejscu (np. menedżer haseł).

---

## 🖥️ Część 2: Założenie Hetzner VPS

### Krok 2.1 — Załóż konto

1. Wejdź na **<https://www.hetzner.com/cloud>**.
2. Kliknij **Sign Up** w prawym górnym rogu.
3. Wypełnij formularz (email, hasło).
4. Hetzner wymaga **weryfikacji** — może poprosić o skan dowodu lub kartę
   kredytową (do potwierdzenia tożsamości). Pierwsza weryfikacja zajmuje
   1-24h. Bez tego nie założysz serwera.

### Krok 2.2 — Stwórz projekt

Po zalogowaniu zobaczysz **Cloud Console**.

1. Kliknij **New Project** (zielony przycisk po lewej).
2. Nazwij go np. `polymarket-bot`.
3. Wejdź w niego.

### Krok 2.3 — Stwórz serwer

1. W projekcie kliknij **Add Server**.
2. **Location**: wybierz **Falkenstein** lub **Helsinki** (najtańsze, dobry ping z Polski).
3. **Image**: **Ubuntu 24.04**.
4. **Type**: **Shared vCPU**, **CX22** (najmniejszy: ~4€/miesiąc, w zupełności wystarczy).
5. **Networking**: zostaw domyślnie zaznaczone (Public IPv4 + IPv6).
6. **SSH Keys** (zalecane, ale można pominąć):
   - Jeśli pominiesz, dostaniesz hasło na email — używaj tylko jeśli nie znasz SSH.
   - **Zalecane:** wygeneruj sobie klucz SSH (zobacz krok 2.4 niżej).
7. **Name**: np. `pmbot-server`.
8. Kliknij **Create & Buy now**.

Po ~10 sekundach serwer będzie gotowy. **Zapisz IP** (np. `91.107.123.45`).

### Krok 2.4 — (Opcjonalnie) wygenerowanie klucza SSH

**Mac/Linux:** otwórz Terminal i wpisz:

```bash
ssh-keygen -t ed25519 -C "pmbot"
```

Naciskaj Enter (domyślne wartości). Po zakończeniu:

```bash
cat ~/.ssh/id_ed25519.pub
```

Skopiuj cały ten string (zaczyna się `ssh-ed25519 ...`) i wklej go w
Hetzner Cloud Console: **Security → SSH Keys → Add SSH Key**. Potem
przy tworzeniu serwera zaznacz ten klucz.

**Windows:** zainstaluj **Windows Terminal** ze sklepu Microsoft, otwórz
go i te same komendy zadziałają (Windows 10+ ma wbudowane OpenSSH).

---

## 🚪 Część 3: Pierwsze logowanie na serwer

### Krok 3.1 — Połączenie SSH

W terminalu (Mac/Linux/Windows) wpisz:

```bash
ssh root@91.107.123.45
```

Zamień `91.107.123.45` na IP swojego serwera.

**Pierwszy raz** zobaczysz pytanie:

```
Are you sure you want to continue connecting (yes/no)?
```

Wpisz `yes` i Enter.

Potem:

- Jeśli używasz **klucza SSH** (krok 2.4), zalogujesz się od razu.
- Jeśli używasz **hasła**, wpisz hasło z emaila od Hetzner (przy pierwszym
  logowaniu serwer może wymusić zmianę hasła — wpisz nowe).

### Krok 3.2 — Co widzisz

Po zalogowaniu zobaczysz coś jak:

```
root@pmbot-server:~#
```

To jest "linia poleceń" (terminal). Tu wpisujesz polecenia, które serwer
wykonuje. Wszystko poniżej w README to polecenia do wpisywania w tym
terminalu.

> **Mała wskazówka:** możesz zostawić to okno otwarte i wracać do niego
> później. Jeśli się rozłączysz — po prostu wpisz `ssh root@<IP>` ponownie.

---

## ⚙️ Część 4: Podstawowa konfiguracja serwera

Wszystkie poniższe komendy wpisuj w terminalu zalogowanym na serwer
(jako `root`).

### Krok 4.1 — Zaktualizuj system

```bash
apt update && apt upgrade -y
```

Trwa 1-2 minuty. Jeśli zapyta o coś — naciskaj Enter (domyślne odpowiedzi).

### Krok 4.2 — Stwórz nieuprzywilejowanego usera

Działanie jako `root` jest niebezpieczne. Robimy konto `pmbot`:

```bash
adduser pmbot
```

Wpisz hasło (zapamiętaj je!) i kliknij Enter na pozostałych pytaniach
(imię, etc. — można pominąć).

Daj mu uprawnienia `sudo`:

```bash
usermod -aG sudo pmbot
```

### Krok 4.3 — Zainstaluj Dockera

Jednolinijkowy oficjalny instalator:

```bash
curl -fsSL https://get.docker.com | sh
```

Trwa 1-2 minuty. Po zakończeniu dodaj usera `pmbot` do grupy `docker`
(żeby mógł używać Dockera bez `sudo`):

```bash
usermod -aG docker pmbot
```

### Krok 4.4 — Skonfiguruj firewall

```bash
ufw allow ssh
ufw enable
```

Na pytanie `Command may disrupt existing ssh connections. Proceed (y|n)?`
odpowiedz `y`. (Bezpieczne — `allow ssh` było pierwszą komendą.)

Sprawdź:

```bash
ufw status
```

Powinno pokazać `Status: active` i regułę `22/tcp ALLOW Anywhere`.

### Krok 4.5 — (Opcjonalnie) Fail2ban — ochrona przed brute-force

```bash
apt install -y fail2ban
systemctl enable --now fail2ban
```

To nie wymaga konfiguracji — domyślnie blokuje IP po 5 nieudanych próbach SSH.

---

## 📦 Część 5: Pobranie i konfiguracja bota

### Krok 5.1 — Przeloguj się na usera `pmbot`

```bash
su - pmbot
```

(Wpisz hasło z kroku 4.2.) Linia poleceń zmieni się na:

```
pmbot@pmbot-server:~$
```

### Krok 5.2 — Sklonuj kod z GitHuba

```bash
cd ~
git clone https://github.com/<TWÓJ_USERNAME>/polymarket-bot.git
cd polymarket-bot
```

> Zamień `<TWÓJ_USERNAME>` na nazwę użytkownika GitHuba osoby która
> udostępniła Ci kod. Jeśli kod jest w prywatnym repo, najpierw skonfiguruj
> SSH key dla GitHuba: <https://docs.github.com/en/authentication/connecting-to-github-with-ssh>

### Krok 5.3 — Stwórz config.yaml

Skopiuj plik przykładowy:

```bash
cp config.example.yaml config.yaml
```

Otwórz go w edytorze:

```bash
nano config.yaml
```

Co tam edytujesz (klawiszem strzałek przesuwasz kursor):

1. **`bot_token`** — wklej token od BotFather (krok 1.2).
   Cudzysłowy zostaw jak są:
   ```yaml
   bot_token: "123456789:AAEhBP0av-XXXXX-XXXXX-XXXXX"
   ```

2. **`chat_id`** — wklej swój chat_id (krok 1.4):
   ```yaml
   chat_id: "987654321"
   ```

3. (Opcjonalnie) zmień `auto_monitor_series` jeśli chcesz monitorować
   inne serie niż domyślny `bitcoin-above`.

**Zapisz:** `Ctrl + O`, potem `Enter`, potem `Ctrl + X` (wyjście z nano).

### Krok 5.4 — Sprawdź konfigurację

```bash
cat config.yaml | head -30
```

Powinieneś zobaczyć swoje wartości. **NIE pokazuj tego nikomu — token
jest sekretny.**

---

## ▶️ Część 6: Uruchomienie

### Krok 6.1 — Zbuduj i uruchom kontener

W folderze `polymarket-bot`:

```bash
docker compose up -d --build
```

- `up` — uruchom
- `-d` — w tle (możesz zamknąć terminal, bot dalej działa)
- `--build` — zbuduj obraz (pierwszy raz lub po update)

Trwa 1-3 minuty (instalacja bibliotek). Po zakończeniu zobaczysz coś jak:

```
[+] Running 2/2
 ✔ Network polymarket-bot_default     Created
 ✔ Container polymarket-bot           Started
```

### Krok 6.2 — Sprawdź logi

```bash
docker compose logs -f
```

Powinieneś zobaczyć:

```
🚀 Polymarket Bot startuje
✅ Bot wystartował - nasłuchuję
```

Wyjdź z podglądu logów: `Ctrl + C` (samo przerwanie podglądu, bot dalej działa).

### Krok 6.3 — Test

Otwórz Telegrama, znajdź swojego bota i napisz:

```
/test
```

Bot powinien odpowiedzieć wiadomością "✅ Test alert". Jeśli tak — **wszystko
działa**! 🎉

Spróbuj też:

```
/status
/list
/help
```

### Krok 6.4 — Zarządzanie kontenerem

| Komenda | Co robi |
|---------|---------|
| `docker compose ps` | Sprawdź czy bot działa |
| `docker compose logs -f` | Podgląd logów na żywo |
| `docker compose restart` | Restart bota |
| `docker compose down` | Zatrzymanie bota |
| `docker compose up -d` | Uruchomienie (po `down`) |
| `docker compose up -d --build` | Restart + przebudowanie obrazu (po update) |

---

## 🆘 Część 7: Co robić jak coś nie działa

### Bot nie odpowiada na `/test`

1. Sprawdź czy kontener żyje:
   ```bash
   docker compose ps
   ```
   Status powinien być `running`. Jeśli `exited` lub `restarting` — sprawdź logi.

2. Sprawdź logi:
   ```bash
   docker compose logs --tail=100
   ```
   Najczęstsze błędy:
   - `Wpisz prawdziwy token...` — nie podmieniłeś `WSTAW_TUTAJ_...` w config.yaml.
   - `Unauthorized` (od Telegrama) — błędny token.
   - Komendy działają, ale alerty nie przychodzą — sprawdź `/status` (czy są monitorowane rynki) i `/pause` (może bot jest w pauzie).

3. Sprawdź, że napisałeś najpierw cokolwiek do bota (krok 1.3) — bez tego
   bot nie ma prawa Ci napisać.

### "Permission denied" przy `docker compose`

User `pmbot` nie jest jeszcze w grupie `docker`. Wyjdź i wejdź ponownie:

```bash
exit       # wyjście z su pmbot, jesteś znowu rootem (lub odłączony)
su - pmbot # ponowne logowanie - grupa się aktywuje
```

### Bot rusza, ale brak alertów

To OK! Alerty przychodzą tylko gdy któryś rynek **JEST** blisko 99.9¢
**I** spełni warunek (np. ask topnieje). Sprawdź `/status` — pole
"Rynki blisko 99.9¢". Jeśli to 0 — żaden monitorowany event nie ma
takiego rynku w danej chwili.

### "No space left" na serwerze

Najczęściej stare obrazy Dockera. Posprzątaj:

```bash
docker system prune -af
```

### Restart po niespodziewanym wyłączeniu serwera

`restart: unless-stopped` w docker-compose sprawia, że bot sam się odpala
po restarcie serwera. Sprawdź:

```bash
docker compose ps
```

Jeśli mimo to nie działa — `docker compose up -d`.

### Gdzie szukać dalszej pomocy

- Plik logów: `~/polymarket-bot/logs/bot.log`
- `docker compose logs --tail=500 > debug.txt` — zrzuć logi do pliku
- Issues w repo GitHub
- Polymarket API docs: <https://docs.polymarket.com>

---

## 🔄 Część 8: Aktualizacja bota

Gdy chcesz wgrać nową wersję kodu:

```bash
cd ~/polymarket-bot
git pull
docker compose up -d --build
```

`docker compose up -d --build` przebuduje obraz z nowym kodem i podmieni
kontener. **Baza danych (`data/bot_state.db`) i config (`config.yaml`)
zostają nietknięte** — są w volumes.

---

## 🔒 Część 9: Bezpieczeństwo

### Co jest już zrobione

✅ Bot działa jako nieuprzywilejowany user `pmbot` (nie root)
✅ Kontener działa jako user `pmbot` (nie root w środku)
✅ `config.yaml`, `.env`, baza są w `.gitignore` — nie trafią na GitHub
✅ Komendy Telegrama tylko od właściciela `chat_id`
✅ Firewall `ufw` blokuje wszystko poza SSH

### Co możesz dodatkowo zrobić

#### Backup bazy danych

Baza zawiera historię alertów i stan monitorowania — cenne. Cron do
codziennego backupu:

```bash
crontab -e
```

Dodaj linijkę:

```
0 3 * * * cp /home/pmbot/polymarket-bot/data/bot_state.db /home/pmbot/backups/bot_state-$(date +\%F).db
```

(Najpierw stwórz folder: `mkdir -p /home/pmbot/backups`.) To kopiuje
bazę codziennie o 3:00. Co tydzień ręcznie usuń stare pliki.

#### Zmiana portu SSH (opcjonalnie)

Brute-force boty atakują domyślny port 22. Możesz przenieść SSH np. na 2222:

```bash
sudo nano /etc/ssh/sshd_config
# zmień: Port 2222
sudo ufw allow 2222
sudo systemctl restart ssh
```

Logujesz się potem przez `ssh -p 2222 pmbot@<IP>`.

> ⚠️ **Najpierw ustaw klucz SSH** zanim zmienisz port — jeśli zostawisz
> tylko hasło i coś źle zrobisz, możesz się zablokować.

#### Wyłącz logowanie roota przez SSH

```bash
sudo nano /etc/ssh/sshd_config
# zmień: PermitRootLogin no
sudo systemctl restart ssh
```

(Najpierw upewnij się, że user `pmbot` ma `sudo` i działa logowanie!)

---

## 📨 Komendy Telegram

| Komenda | Działanie |
|---------|-----------|
| `/start` | Powitanie + lista komend |
| `/help` | Lista komend |
| `/list` | Aktualnie monitorowane eventy i rynki |
| `/status` | Pełny status, statystyki alertów |
| `/depth` | Aktualna głębokość order booka dla rynków blisko 99,9¢ |
| `/add <slug>` | Dodaje event ręcznie (np. `/add bitcoin-above-on-may-7`) |
| `/remove <slug>` | Usuwa event |
| `/series` | Skonfigurowane serie auto-monitorowania |
| `/thresholds` | Aktualne progi alertów |
| `/set_threshold <nazwa> <wartość>` | Zmienia próg w runtime (zapis do config.yaml) |
| `/pause` | Pauzuje wysyłanie alertów (monitoring działa dalej) |
| `/resume` | Wznawia |
| `/test` | Wysyła testowy alert |

### Przykład outputu `/depth`

```
📊 Stan głębokości — 14:23

₿ Bitcoin Above ___ on May 7
  $90,000 NO 99,9¢ — 30k
  $86,000 NO 99,9¢ — 12k
  $80,000 NO 99,8¢ — 5.5k

Ξ Ethereum Above ___ on May 7
  $3,500 NO 99,9¢ — 6k
```

Migawka pokazuje:
- **Sekcję per event** (ikona zależna od slug-a, pełny tytuł z Gamma API)
- **Linie per podrynek+strona** posortowane malejąco po wartości progu
  (np. `$90,000` na górze, `$80,000` niżej)
- **Sumę shares** na obu monitorowanych poziomach (99,8 + 99,9¢) po stronie
  która jest "blisko 99,9¢"
- **Cenę rynkową** = najlepszy ask z monitorowanych poziomów

Pomijane (po cichu, bez crashy):
- Strony rynku gdzie aktualna cena nie jest 99,8/99,9¢
- Rynki gdzie WS jeszcze nie miał snapshotu (np. dopiero co zasubskrybowane)

Jeśli treść przekracza limit Telegrama (4096 znaków), wiadomość jest
automatycznie dzielona na kilka chunków z `(część X/Y)` w nagłówku.

### Przykłady innych komend

```
/add bitcoin-above-72000-on-may-8
/set_threshold ask_melting_threshold 25000
/pause
/resume
/depth
```

---

## 🏗️ Architektura i API

### Co bot używa

- **Gamma API REST** (`https://gamma-api.polymarket.com/events`) —
  wyszukiwanie aktywnych eventów co 30 minut. Endpoint publiczny, bez auth.
- **CLOB WebSocket** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) —
  real-time order book i trades. Też publiczny, bez auth. Wymaga PING
  co 10 sekund.

### Jak działa wewnątrz

```
┌─────────────┐         ┌──────────────────┐
│ Polymarket  │  WS     │ CLOBWebSocket    │
│ CLOB        │────────►│ Manager          │ → asyncio.Queue
└─────────────┘         └──────────────────┘
                               │
                               ▼
                        ┌──────────────┐
                        │ Orchestrator │ ← cooldown z DB
                        └──────────────┘
                               │
                               ▼
                        ┌──────────────┐
                        │   Detector   │ → 4 typy alertów
                        └──────────────┘
                               │
                               ▼
                        ┌──────────────┐    ┌─────────┐
                        │   Telegram   │───►│   Ty    │
                        │   Sender     │    │         │
                        └──────────────┘    └─────────┘

┌─────────────┐         ┌──────────────────┐
│ Gamma API   │   REST  │ DiscoveryScheduler│ co 30 min
│ /events     │────────►│ (auto-discovery) │
└─────────────┘         └──────────────────┘
```

### Pliki

```
polymarket-bot/
├── bot/
│   ├── main.py                # punkt wejścia
│   ├── config.py              # ładowanie config.yaml
│   ├── scheduler.py           # auto-discovery
│   ├── polymarket/
│   │   ├── gamma_api.py       # REST client
│   │   ├── clob_ws.py         # WebSocket client
│   │   └── models.py          # dataclasses
│   ├── alerts/
│   │   ├── detector.py        # 4 typy alertów (PURE LOGIC)
│   │   └── formatter.py       # formatowanie HTML
│   ├── telegram_bot/
│   │   ├── bot.py             # wysyłka
│   │   └── commands.py        # /start /list /add ...
│   └── storage/
│       └── db.py              # SQLite
├── tests/
│   └── test_alerts.py         # 20 testów detektora
├── config.example.yaml
├── .env.example
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .gitignore
└── README.md                  # ten plik
```

### Stan persystowany

- `data/bot_state.db` — SQLite: lista monitorowanych eventów/rynków,
  historia alertów (cooldown), ostatnie order booki, statystyki.
- `config.yaml` — konfiguracja (też modyfikowalna przez `/set_threshold` i `/add`).
- `logs/bot.log` — logi z rotacją (50MB / 14 dni).

### Testy

```bash
# Lokalnie (bez kontenera):
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest tests/ -v
```

---

## 📜 Licencja i podziękowania

Bot napisany do prywatnego użytku. Polymarket nie jest sponsorem ani
partnerem — używamy wyłącznie publicznego API.
