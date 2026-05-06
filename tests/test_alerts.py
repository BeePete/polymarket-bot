"""
Testy jednostkowe detektora alertów.

Uruchamianie:
    cd polymarket-bot
    python -m pytest tests/ -v
"""
from __future__ import annotations

import pytest

from bot.alerts.detector import Alert, Detector, DetectorThresholds
from bot.alerts.formatter import (
    ALERT_TYPE_EMOJI,
    format_alert_line,
    format_burst_drop_message,
    format_consolidated_message,
    format_shares,
    market_short,
    market_sort_key,
    series_icon,
)
from bot.polymarket.models import (
    OrderBook,
    OrderBookLevel,
    Trade,
)


# -----------------------------------------------------------------------------
# Helpery
# -----------------------------------------------------------------------------


def book(token_id: str = "T", bids=None, asks=None) -> OrderBook:
    """Krótki konstruktor order booka z list par (price, size)."""
    return OrderBook(
        token_id=token_id,
        bids=[OrderBookLevel(price=p, size=s) for p, s in (bids or [])],
        asks=[OrderBookLevel(price=p, size=s) for p, s in (asks or [])],
        timestamp_ms=0,
    )


@pytest.fixture
def detector() -> Detector:
    return Detector(thresholds=DetectorThresholds(
        ask_melting_threshold=30000,
        ask_melting_burst_drop=5000,
        market_buy_min_size=5000,
        new_sell_order_min_size=5000,
        big_limit_buy_min_size=19000,
    ))


# -----------------------------------------------------------------------------
# Test: czy rynek jest "monitorowany" (best ask na 99.8/99.9¢)
# -----------------------------------------------------------------------------


class TestMonitoredCheck:
    def test_token_z_askiem_na_999_jest_monitorowany(self, detector):
        b = book(asks=[(0.999, 5000)])
        assert detector.is_token_monitored(b) is True

    def test_token_z_askiem_na_998_jest_monitorowany(self, detector):
        b = book(asks=[(0.998, 5000)])
        assert detector.is_token_monitored(b) is True

    def test_token_z_askiem_na_99_NIE_jest_monitorowany(self, detector):
        b = book(asks=[(0.99, 5000)])
        assert detector.is_token_monitored(b) is False

    def test_pusty_ask_NIE_jest_monitorowany(self, detector):
        b = book(asks=[])
        assert detector.is_token_monitored(b) is False


# -----------------------------------------------------------------------------
# Alert A - Ask topnieje
# -----------------------------------------------------------------------------


class TestAlertA_AskMelting:
    def test_alert_gdy_suma_askow_ponizej_progu(self, detector):
        new = book(asks=[(0.998, 10000), (0.999, 15000)])  # suma = 25000
        alerts = detector.check_book_change("T1", "YES", old_book=None, new_book=new)
        a_alerts = [a for a in alerts if a.alert_type == "A"]
        assert len(a_alerts) == 1
        assert a_alerts[0].payload["ask_sum"] == 25000

    def test_brak_alertu_gdy_suma_powyzej_progu(self, detector):
        new = book(asks=[(0.998, 20000), (0.999, 20000)])  # suma = 40000
        alerts = detector.check_book_change("T1", "YES", old_book=None, new_book=new)
        a_alerts = [a for a in alerts if a.alert_type == "A"]
        assert len(a_alerts) == 0

    def test_burst_drop_ustawia_bypass_cooldown(self, detector):
        # Pierwszy update: 28k - alert A (suma <30k, brak poprzedniej -> nie burst)
        new1 = book(asks=[(0.999, 28000)])
        alerts1 = detector.check_book_change("T1", "YES", None, new1)
        assert any(a.alert_type == "A" and not a.bypass_cooldown for a in alerts1)

        # Drugi update: 20k (spadek o 8k > 5k threshold) - bypass=True
        new2 = book(asks=[(0.999, 20000)])
        alerts2 = detector.check_book_change("T1", "YES", new1, new2)
        a = next(a for a in alerts2 if a.alert_type == "A")
        assert a.bypass_cooldown is True
        assert a.payload["previous_sum"] == 28000
        assert a.payload["ask_sum"] == 20000

    def test_maly_spadek_NIE_aktywuje_burst(self, detector):
        new1 = book(asks=[(0.999, 28000)])
        detector.check_book_change("T1", "YES", None, new1)

        # Spadek z 28k do 25k = 3k (< 5k threshold) - bypass=False
        new2 = book(asks=[(0.999, 25000)])
        alerts2 = detector.check_book_change("T1", "YES", new1, new2)
        a = next(a for a in alerts2 if a.alert_type == "A")
        assert a.bypass_cooldown is False

    def test_token_oddali_sie_od_999_brak_alertow(self, detector):
        # Pierwsze: blisko 99.9¢
        new1 = book(asks=[(0.999, 10000)])
        detector.check_book_change("T1", "YES", None, new1)

        # Drugie: rynek odpłynął na 99¢ - NIE alertujemy
        new2 = book(asks=[(0.99, 10000)])
        alerts = detector.check_book_change("T1", "YES", new1, new2)
        assert alerts == []


# -----------------------------------------------------------------------------
# Alert B - Duży market buy
# -----------------------------------------------------------------------------


class TestAlertB_MarketBuy:
    def test_alert_gdy_duzy_buy_na_999(self, detector):
        trade = Trade(
            token_id="T1", price=0.999, size=10000, side="BUY", timestamp_ms=0,
        )
        alerts = detector.check_trade(trade, side="YES")
        assert len(alerts) == 1
        assert alerts[0].alert_type == "B"
        assert alerts[0].payload["size"] == 10000

    def test_brak_alertu_gdy_buy_za_maly(self, detector):
        trade = Trade(
            token_id="T1", price=0.999, size=4999, side="BUY", timestamp_ms=0,
        )
        assert detector.check_trade(trade, side="YES") == []

    def test_brak_alertu_gdy_sprzedaz_zamiast_kupna(self, detector):
        trade = Trade(
            token_id="T1", price=0.999, size=10000, side="SELL", timestamp_ms=0,
        )
        assert detector.check_trade(trade, side="YES") == []

    def test_brak_alertu_gdy_cena_nie_w_monitored(self, detector):
        trade = Trade(
            token_id="T1", price=0.95, size=10000, side="BUY", timestamp_ms=0,
        )
        assert detector.check_trade(trade, side="YES") == []


# -----------------------------------------------------------------------------
# Alert C - Nowy sell order
# -----------------------------------------------------------------------------


class TestAlertC_NewSellOrder:
    def test_alert_gdy_ask_urosl_o_5k(self, detector):
        old = book(asks=[(0.999, 3000)])
        new = book(asks=[(0.999, 8000)])  # +5000
        alerts = detector.check_book_change("T1", "YES", old, new)
        c_alerts = [a for a in alerts if a.alert_type == "C"]
        assert len(c_alerts) == 1
        assert c_alerts[0].payload["delta"] == 5000

    def test_brak_alertu_gdy_wzrost_za_maly(self, detector):
        old = book(asks=[(0.999, 3000)])
        new = book(asks=[(0.999, 7000)])  # +4000 (< 5000)
        alerts = detector.check_book_change("T1", "YES", old, new)
        assert not any(a.alert_type == "C" for a in alerts)

    def test_brak_alertu_gdy_ask_zmalal(self, detector):
        old = book(asks=[(0.999, 10000)])
        new = book(asks=[(0.999, 3000)])
        alerts = detector.check_book_change("T1", "YES", old, new)
        assert not any(a.alert_type == "C" for a in alerts)


# -----------------------------------------------------------------------------
# Alert D - Duży limit buy
# -----------------------------------------------------------------------------


class TestAlertD_BigLimitBuy:
    def test_alert_gdy_bid_urosl_o_19k(self, detector):
        old = book(bids=[(0.998, 1000)], asks=[(0.999, 5000)])
        new = book(bids=[(0.998, 20000)], asks=[(0.999, 5000)])  # +19000
        alerts = detector.check_book_change("T1", "YES", old, new)
        d_alerts = [a for a in alerts if a.alert_type == "D"]
        assert len(d_alerts) == 1
        assert d_alerts[0].payload["delta"] == 19000

    def test_brak_alertu_gdy_bid_za_maly_wzrost(self, detector):
        old = book(bids=[(0.998, 1000)], asks=[(0.999, 5000)])
        new = book(bids=[(0.998, 19999)], asks=[(0.999, 5000)])  # +18999
        alerts = detector.check_book_change("T1", "YES", old, new)
        assert not any(a.alert_type == "D" for a in alerts)

    def test_alert_d_na_998_oddzielnie_od_999(self, detector):
        old = book(bids=[(0.998, 0), (0.999, 0)], asks=[(0.999, 5000)])
        new = book(bids=[(0.998, 25000), (0.999, 0)], asks=[(0.999, 5000)])
        alerts = detector.check_book_change("T1", "YES", old, new)
        d = [a for a in alerts if a.alert_type == "D"]
        assert len(d) == 1
        assert d[0].price == 0.998


# -----------------------------------------------------------------------------
# Combo: kilka alertów naraz
# -----------------------------------------------------------------------------


class TestCombinedAlerts:
    def test_a_i_d_jednoczesnie(self, detector):
        # Stara: bid 1k, ask suma 40k (powyżej progu A) -> brak alertów
        old = book(bids=[(0.998, 1000)], asks=[(0.998, 20000), (0.999, 20000)])
        # Nowa: bid 25k (+24k -> alert D), ask suma 25k (-15k -> alert A burst)
        new = book(bids=[(0.998, 25000)], asks=[(0.998, 10000), (0.999, 15000)])

        # Najpierw "rozgrzewka" - zarejestruj poprzednią sumę
        detector.check_book_change("T1", "YES", None, old)

        alerts = detector.check_book_change("T1", "YES", old, new)
        types = {a.alert_type for a in alerts}
        assert "A" in types
        assert "D" in types


# -----------------------------------------------------------------------------
# format_shares - skracanie liczb udziałów
# -----------------------------------------------------------------------------


class TestFormatShares:
    """Reguły:
      n < 1000           -> "516"
      1000..9999         -> "5.5k", "9.9k"   (truncate, nie round)
      10000..999999      -> "30k", "123k"
      n >= 1_000_000     -> "1.2M"
    """

    # ----- granice z wymagań -----
    def test_999(self):
        assert format_shares(999) == "999"

    def test_1000(self):
        assert format_shares(1000) == "1.0k"

    def test_9999_jest_9_9k_nie_10k(self):
        # KRYTYCZNE: 9999 ma być "9.9k", nie "10.0k" (truncation)
        assert format_shares(9999) == "9.9k"

    def test_10000(self):
        assert format_shares(10000) == "10k"

    def test_999999(self):
        assert format_shares(999999) == "999k"

    def test_1000000(self):
        assert format_shares(1_000_000) == "1.0M"

    # ----- typowe wartości z bota -----
    def test_zero(self):
        assert format_shares(0) == "0"

    def test_male_setki(self):
        assert format_shares(516) == "516"

    def test_5500_to_5_5k(self):
        assert format_shares(5500) == "5.5k"

    def test_30000_to_30k(self):
        assert format_shares(30000) == "30k"

    def test_123000_to_123k(self):
        assert format_shares(123_000) == "123k"

    def test_1200000_to_1_2M(self):
        assert format_shares(1_200_000) == "1.2M"

    # ----- edge cases -----
    def test_float_jest_zaokraglany(self):
        # bot dostaje float-y z WS (size=28450.5)
        assert format_shares(28_450.5) == "28k"
        assert format_shares(5500.4) == "5.5k"

    def test_ujemne_traktowane_jak_zero(self):
        # nie powinno się zdarzyć w praktyce, ale nie crashujmy
        assert format_shares(-100) == "0"

    def test_duze_M(self):
        assert format_shares(1_500_000) == "1.5M"
        assert format_shares(9_900_000) == "9.9M"
        assert format_shares(10_000_000) == "10.0M"


# -----------------------------------------------------------------------------
# series_icon - mapowanie po prefiksie slug-a
# -----------------------------------------------------------------------------


class TestSeriesIcon:
    def test_bitcoin_above(self):
        assert series_icon("bitcoin-above-86000-on-may-7") == "₿"

    def test_btc_alias(self):
        assert series_icon("btc-up-or-down-on-may-7") == "₿"

    def test_ethereum(self):
        assert series_icon("ethereum-above-3000-on-may-8") == "Ξ"

    def test_eth_alias(self):
        assert series_icon("eth-flippening-2026") == "Ξ"

    def test_sp_500(self):
        assert series_icon("sp-500-above-5500-on-may-7") == "📈"

    def test_s_and_p(self):
        assert series_icon("s-and-p-above-6000") == "📈"

    def test_default_fallback(self):
        assert series_icon("us-election-2028") == "🎯"
        assert series_icon("") == "🎯"
        assert series_icon("random-event-slug") == "🎯"

    def test_case_insensitive(self):
        assert series_icon("BITCOIN-above-86000") == "₿"


# -----------------------------------------------------------------------------
# market_short - skracanie tytułu rynku
# -----------------------------------------------------------------------------


class TestMarketShort:
    def test_kwota_dolarowa(self):
        assert market_short("Will Bitcoin reach $86,000 on May 6") == "$86,000"

    def test_przedzial_bierze_gorna_granice(self):
        assert market_short("Bitcoin between $76,000 and $78,000 on May 7") == "$78,000"

    def test_dip_to(self):
        assert market_short("Will BTC dip to $78,000 on May 6") == "$78,000"

    def test_above(self):
        assert market_short("Bitcoin above $90,000 on May 7") == "$90,000"

    def test_brak_kwoty_truncate(self):
        long_title = "Bitcoin Up or Down on May 7 with extra long description " \
                     "that exceeds limit"
        result = market_short(long_title)
        assert len(result) <= 40
        assert result.endswith("…")

    def test_brak_kwoty_krotki_zostaje(self):
        assert market_short("Bitcoin Up or Down") == "Bitcoin Up or Down"

    def test_pusty_tytul(self):
        assert market_short("") == "?"
        assert market_short(None) == "?"


# -----------------------------------------------------------------------------
# market_sort_key - sortowanie po wartości progu
# -----------------------------------------------------------------------------


class TestMarketSortKey:
    def test_sortowanie_malejaco_po_kwocie(self):
        questions = [
            "Bitcoin above $80,000 on May 7",
            "Bitcoin above $90,000 on May 7",
            "Bitcoin above $86,000 on May 7",
        ]
        sorted_q = sorted(questions, key=market_sort_key)
        assert sorted_q == [
            "Bitcoin above $90,000 on May 7",
            "Bitcoin above $86,000 on May 7",
            "Bitcoin above $80,000 on May 7",
        ]

    def test_przedzial_uzywa_najwyzszej(self):
        # "$76k - $78k" sortuje się po 78k
        a = market_sort_key("Bitcoin between $76,000 and $78,000")  # max=78000
        b = market_sort_key("Bitcoin above $77,000")                # max=77000
        # a powinno być WYŻEJ (mniejszy klucz) bo 78k > 77k
        assert a < b

    def test_bez_kwoty_lada_na_koncu(self):
        a = market_sort_key("Bitcoin above $50,000")
        b = market_sort_key("Bitcoin Up or Down")
        # Bez kwoty -> bucket=1, więc PO wszystkich z kwotą (bucket=0)
        assert a < b

    def test_pusty_lada_na_koncu(self):
        a = market_sort_key("Bitcoin above $1")
        b = market_sort_key("")
        assert a < b


# -----------------------------------------------------------------------------
# format_alert_line - jedna linia w wiadomości
# -----------------------------------------------------------------------------


def make_alert(
    alert_type: str = "A",
    side: str = "NO",
    price: float = 0.999,
    payload: dict | None = None,
    bypass: bool = False,
    token_id: str = "T1",
) -> Alert:
    return Alert(
        alert_type=alert_type, token_id=token_id, side=side, price=price,
        payload=payload or {}, bypass_cooldown=bypass,
    )


class TestAlertLine:
    def test_typ_A(self):
        alert = make_alert("A", "NO", 0.999, {"ask_sum": 25000, "previous_sum": 33000})
        line = format_alert_line(alert, "Will Bitcoin reach $86,000 on May 6")
        # 🔻 $86,000 NO 99,9¢ — pozostało 25k (↓ 8.0k)
        # (format_shares(8000) = "8.0k" - jednocyfrowa liczba k ma 1 dec)
        assert line.startswith("🔻 ")
        assert "$86,000" in line
        assert " NO " in line
        assert "99,9¢" in line
        assert "pozostało 25k" in line
        assert "↓ 8.0k" in line

    def test_typ_A_drop_powyzej_10k(self):
        # Spadek ≥ 10k formatuje się bez ułamka: "↓ 30k"
        alert = make_alert("A", "NO", 0.999, {"ask_sum": 5000, "previous_sum": 35000})
        line = format_alert_line(alert, "Bitcoin $86,000")
        assert "↓ 30k" in line

    def test_typ_A_bez_poprzedniej_sumy(self):
        # Pierwszy pomiar - brak previous_sum -> nie pokazujemy "↓"
        alert = make_alert("A", "NO", 0.998, {"ask_sum": 25000, "previous_sum": None})
        line = format_alert_line(alert, "Bitcoin $90,000")
        assert "pozostało 25k" in line
        assert "↓" not in line

    def test_typ_B_market_buy(self):
        alert = make_alert("B", "YES", 0.999, {"size": 12000, "price": 0.999})
        line = format_alert_line(alert, "Bitcoin above $86,000")
        # 💰 $86,000 YES 99,9¢ — kupiono 12k @99,9¢
        assert line.startswith("💰 ")
        assert " YES " in line
        assert "kupiono 12k" in line
        assert "@99,9¢" in line

    def test_typ_C_new_ask(self):
        alert = make_alert("C", "NO", 0.999, {"old_size": 3000, "new_size": 8000, "delta": 5000})
        line = format_alert_line(alert, "Bitcoin $86,000")
        # 📤 $86,000 NO 99,9¢ — nowy ask 5.0k
        assert line.startswith("📤 ")
        assert "nowy ask 5.0k" in line

    def test_typ_D_new_bid(self):
        alert = make_alert("D", "NO", 0.998, {"old_size": 0, "new_size": 25000, "delta": 25000})
        line = format_alert_line(alert, "Bitcoin $86,000")
        # 🛑 $86,000 NO 99,8¢ — nowy bid 25k
        assert line.startswith("🛑 ")
        assert "99,8¢" in line
        assert "nowy bid 25k" in line

    def test_emoji_zgodne_z_mapowaniem(self):
        for atype, expected in ALERT_TYPE_EMOJI.items():
            alert = make_alert(atype, "NO", 0.999,
                               {"ask_sum": 0, "size": 5000, "delta": 5000})
            line = format_alert_line(alert, "Bitcoin $86,000")
            assert line.startswith(expected + " ")

    def test_market_short_truncate_w_linii(self):
        # Brak kwoty - tytuł truncate
        alert = make_alert("A", "NO", 0.999, {"ask_sum": 25000})
        line = format_alert_line(
            alert,
            "Will Bitcoin do something very dramatic and unprecedented this month",
        )
        # Truncated do <=40 znaków
        # Sprawdzamy że nie ma pełnego tytułu
        assert "unprecedented" not in line


# -----------------------------------------------------------------------------
# format_burst_drop_message
# -----------------------------------------------------------------------------


class TestBurstDropMessage:
    def test_struktura_wiadomosci(self):
        alert = make_alert("A", "NO", 0.999,
                           {"ask_sum": 22000, "previous_sum": 30000},
                           bypass=True)
        from datetime import datetime
        now = datetime(2026, 5, 6, 14, 23)
        msg = format_burst_drop_message(
            alert=alert,
            market_question="Will Bitcoin reach $86,000 on May 6",
            event_title="Bitcoin Above ___ on May 6",
            event_slug="bitcoin-above-on-may-6",
            now=now,
        )
        lines = msg.split("\n")
        # Spec: nagłówek, "", linia, "", url, "", ⚡, 🕒
        assert lines[0].startswith("₿ ")  # ikona bitcoin
        assert "Bitcoin Above" in lines[0]
        assert lines[1] == ""              # pusta po nagłówku
        assert lines[2].startswith("🔻 ")  # linia alertu
        assert "$86,000" in lines[2]
        assert lines[3] == ""              # pusta po linii alertu
        assert lines[4] == "https://polymarket.com/event/bitcoin-above-on-may-6"
        assert lines[5] == ""              # PUSTA po linku (charakterystyczne dla burst-drop)
        assert lines[6] == "⚡ Burst-drop — alert poza cooldownem"
        assert lines[7] == "🕒 14:23"

    def test_godzina_bez_sekund(self):
        alert = make_alert("A", "NO", 0.999, {"ask_sum": 1000}, bypass=True)
        from datetime import datetime
        msg = format_burst_drop_message(
            alert, "Bitcoin $80,000", "Event", "bitcoin-x",
            now=datetime(2026, 5, 6, 9, 5, 30),
        )
        # Format ma być HH:MM (bez sekund)
        assert "🕒 09:05" in msg
        assert "09:05:30" not in msg

    def test_uzywa_event_title_a_nie_slug(self):
        alert = make_alert("A", "NO", 0.999, {"ask_sum": 1000}, bypass=True)
        msg = format_burst_drop_message(
            alert, "Bitcoin $80,000",
            event_title="Bitcoin Above ___ on May 6",   # ladny tytuł
            event_slug="bitcoin-above-on-may-6",         # brzydki slug
        )
        assert "Bitcoin Above ___ on May 6" in msg
        # Slug może wystąpić tylko w URL
        first_line = msg.split("\n")[0]
        assert "bitcoin-above-on-may-6" not in first_line


# -----------------------------------------------------------------------------
# format_consolidated_message - wiele alertów -> jedna wiadomość
# -----------------------------------------------------------------------------


class TestConsolidatedMessage:
    def _alert_A(self, price=0.999, ask_sum=25000, prev=33000, side="NO"):
        return make_alert("A", side, price,
                          {"ask_sum": ask_sum, "previous_sum": prev})

    def test_jedna_wiadomosc_wiele_alertow(self):
        from datetime import datetime
        alerts = [
            (self._alert_A(ask_sum=25000, prev=33000),
             "Will Bitcoin reach $86,000 on May 6"),
            (self._alert_A(ask_sum=22000, prev=27000),
             "Will Bitcoin reach $90,000 on May 6"),
            (self._alert_A(ask_sum=18000, prev=30000, price=0.998),
             "Will Bitcoin reach $80,000 on May 6"),
        ]
        msg = format_consolidated_message(
            alerts_with_questions=alerts,
            event_title="Bitcoin Above ___ on May 6",
            event_slug="bitcoin-above-on-may-6",
            now=datetime(2026, 5, 6, 14, 23),
        )

        # Tylko jedna wiadomość
        assert msg.count("Bitcoin Above") == 1  # tytuł raz
        assert msg.count("https://polymarket.com") == 1  # link raz
        assert msg.count("🕒") == 1

        # Wszystkie 3 podrynki są w wiadomości
        assert "$86,000" in msg
        assert "$90,000" in msg
        assert "$80,000" in msg

    def test_sortowanie_malejaco_po_kwocie(self):
        # Wstawiamy w innej kolejności niż docelowa
        alerts = [
            (self._alert_A(), "Bitcoin above $80,000"),  # najmniejsza
            (self._alert_A(), "Bitcoin above $90,000"),  # największa
            (self._alert_A(), "Bitcoin above $86,000"),  # środek
        ]
        msg = format_consolidated_message(
            alerts, "Event Title", "bitcoin-above",
        )
        # Sprawdź kolejność występowania kwot w stringu
        idx_90 = msg.find("$90,000")
        idx_86 = msg.find("$86,000")
        idx_80 = msg.find("$80,000")
        assert idx_90 < idx_86 < idx_80

    def test_brak_kwoty_lada_na_koncu(self):
        alerts = [
            (self._alert_A(), "Bitcoin Up or Down on May 7"),  # bez kwoty
            (self._alert_A(), "Bitcoin above $86,000"),         # z kwotą
        ]
        msg = format_consolidated_message(
            alerts, "Bitcoin events", "bitcoin-misc",
        )
        idx_with = msg.find("$86,000")
        idx_without = msg.find("Bitcoin Up or Down")
        assert idx_with < idx_without

    def test_tie_breaker_starszy_pierwszy(self):
        # Dwa alerty z TĄ SAMĄ kwotą - powinny zachować kolejność wstawienia
        alerts = [
            (make_alert("A", "NO", 0.999, {"ask_sum": 25000}), "$86,000 first"),
            (make_alert("A", "NO", 0.999, {"ask_sum": 22000}), "$86,000 second"),
        ]
        msg = format_consolidated_message(alerts, "Event", "bitcoin-x")
        idx_first = msg.find("pozostało 25k")
        idx_second = msg.find("pozostało 22k")
        assert idx_first < idx_second

    def test_struktura_brak_pustej_linii_po_linku(self):
        from datetime import datetime
        alerts = [(self._alert_A(), "Bitcoin $86,000")]
        msg = format_consolidated_message(
            alerts, "Event", "bitcoin-x", now=datetime(2026, 5, 6, 14, 23),
        )
        lines = msg.split("\n")
        # nagłówek, "", linia, "", url, 🕒
        assert lines[0].startswith("₿ ")
        assert lines[1] == ""
        assert lines[2].startswith("🔻 ")
        assert lines[3] == ""
        assert lines[4] == "https://polymarket.com/event/bitcoin-x"
        # KLUCZOWE: linia 5 to OD RAZU 🕒, BEZ pustej linii
        assert lines[5] == "🕒 14:23"
        assert len(lines) == 6   # nie ma więcej linii

    def test_godzina_bez_sekund(self):
        from datetime import datetime
        alerts = [(self._alert_A(), "Bitcoin $86,000")]
        msg = format_consolidated_message(
            alerts, "Event", "bitcoin-x", now=datetime(2026, 5, 6, 9, 5, 30),
        )
        assert "🕒 09:05" in msg
        assert "09:05:30" not in msg

    def test_ikona_serii_zalezy_od_slug(self):
        alerts = [(self._alert_A(), "$86,000")]
        msg_btc = format_consolidated_message(alerts, "BTC", "bitcoin-above-86000")
        msg_eth = format_consolidated_message(alerts, "ETH", "ethereum-above-3000")
        msg_sp = format_consolidated_message(alerts, "S&P", "sp-500-above-5500")
        msg_other = format_consolidated_message(alerts, "Other", "us-election-2028")

        assert msg_btc.startswith("₿")
        assert msg_eth.startswith("Ξ")
        assert msg_sp.startswith("📈")
        assert msg_other.startswith("🎯")

    def test_pojedynczy_alert_tez_dziala(self):
        # Skonsolidowana wiadomość z 1 alertem - powinna mieć tę samą strukturę
        # (BEZ ⚡ Burst-drop, BEZ pustej linii po linku)
        from datetime import datetime
        alerts = [(self._alert_A(), "Bitcoin above $86,000")]
        msg = format_consolidated_message(
            alerts, "Event", "bitcoin-x", now=datetime(2026, 5, 6, 14, 23),
        )
        assert "⚡" not in msg            # bez burst-drop
        assert "Burst-drop" not in msg
        assert "$86,000" in msg
        assert "🕒 14:23" in msg

    def test_html_escape_w_tytule(self):
        # Tytuł z & < > musi być escape'owany
        alerts = [(self._alert_A(), "$86,000")]
        msg = format_consolidated_message(
            alerts, "BTC <test> & 'more'", "bitcoin-x",
        )
        assert "&lt;test&gt;" in msg
        assert "&amp;" in msg
        # Surowe < > nie powinny zostać
        assert "<test>" not in msg


# -----------------------------------------------------------------------------
# AlertBuffer - debounce per event
# -----------------------------------------------------------------------------


import asyncio as _asyncio  # alias żeby nie kolidować z innymi importami

from bot.alerts.buffer import AlertBuffer, BufferedAlert


class FlushRecorder:
    """Helper do testów - zbiera wywołania callbacku flush."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[BufferedAlert]]] = []

    async def __call__(self, slug: str, alerts: list[BufferedAlert]) -> None:
        self.calls.append((slug, list(alerts)))


@pytest.mark.asyncio
class TestAlertBuffer:
    """
    Używamy bardzo krótkich okien (0.05s = 50ms), żeby testy były szybkie.
    Asyncio.sleep w testach: ~10-20ms slack na timing.
    """

    async def test_pojedynczy_alert_flush_po_oknie(self):
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)

        await buf.add("bitcoin-may-6", make_alert("A"), "$86,000")
        # Przed upłynięciem okna - jeszcze nic nie poszło
        assert rec.calls == []

        await _asyncio.sleep(0.10)
        # Po oknie - dokładnie 1 flush
        assert len(rec.calls) == 1
        slug, alerts = rec.calls[0]
        assert slug == "bitcoin-may-6"
        assert len(alerts) == 1

    async def test_wiele_alertow_w_oknie_jeden_flush(self):
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)

        # Wkładamy 3 alerty w ciągu kilku ms
        await buf.add("bitcoin-may-6", make_alert("A"), "$86,000")
        await buf.add("bitcoin-may-6", make_alert("C"), "$90,000")
        await buf.add("bitcoin-may-6", make_alert("D"), "$80,000")

        await _asyncio.sleep(0.10)
        # Tylko 1 flush, ale z 3 alertami
        assert len(rec.calls) == 1
        slug, alerts = rec.calls[0]
        assert slug == "bitcoin-may-6"
        assert len(alerts) == 3
        # Kolejność wstawienia zachowana (do tie-breakera)
        assert alerts[0].alert.alert_type == "A"
        assert alerts[1].alert.alert_type == "C"
        assert alerts[2].alert.alert_type == "D"

    async def test_timer_NIE_jest_resetowany(self):
        """Krytyczne: kolejny alert w oknie NIE odracza flush'a."""
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.10, on_flush=rec)

        # t=0: pierwszy alert, timer startuje (flush za 100ms)
        await buf.add("ev-1", make_alert("A"), "$86,000")

        # t=80ms: dorzucamy alert. JEŚLI timer byłby resetowany, flush byłby
        # za kolejne 100ms (czyli t=180ms). Sprawdzimy że flush nastąpił do
        # t~110ms.
        await _asyncio.sleep(0.08)
        await buf.add("ev-1", make_alert("C"), "$90,000")

        # t=130ms: flush już powinien się wydarzyć
        await _asyncio.sleep(0.05)
        assert len(rec.calls) == 1
        _, alerts = rec.calls[0]
        assert len(alerts) == 2

    async def test_rozne_eventy_niezalezne_timery(self):
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)

        await buf.add("ev-A", make_alert("A"), "$86,000")
        await buf.add("ev-B", make_alert("A"), "$90,000")
        await buf.add("ev-A", make_alert("C"), "$80,000")

        await _asyncio.sleep(0.10)

        # 2 flushe (po jednym per event)
        assert len(rec.calls) == 2
        by_slug = {slug: alerts for slug, alerts in rec.calls}
        assert len(by_slug["ev-A"]) == 2
        assert len(by_slug["ev-B"]) == 1

    async def test_kolejne_okno_po_flushu(self):
        """Po flushu można rozpocząć nowe okno dla tego samego eventu."""
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)

        await buf.add("ev-1", make_alert("A"), "$86,000")
        await _asyncio.sleep(0.08)  # flush się wydarzył

        # Drugie okno
        await buf.add("ev-1", make_alert("C"), "$90,000")
        await _asyncio.sleep(0.08)

        # Były 2 flushe
        assert len(rec.calls) == 2
        assert len(rec.calls[0][1]) == 1
        assert len(rec.calls[1][1]) == 1

    async def test_pending_count_diagnostyka(self):
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.20, on_flush=rec)

        await buf.add("ev-A", make_alert("A"), "$86,000")
        await buf.add("ev-A", make_alert("C"), "$80,000")
        await buf.add("ev-B", make_alert("D"), "$90,000")

        pending = buf.pending_count()
        assert pending == {"ev-A": 2, "ev-B": 1}

        # Posprzątaj żeby nie zostawić task-ów
        await _asyncio.sleep(0.30)

    async def test_market_question_zachowane(self):
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.03, on_flush=rec)

        await buf.add("ev-1", make_alert("A"), "Will BTC reach $86,000?")
        await _asyncio.sleep(0.08)

        _, alerts = rec.calls[0]
        assert alerts[0].market_question == "Will BTC reach $86,000?"

    async def test_burst_drop_NIE_uzywa_bufora(self):
        """
        Specyfikacja: burst-drop wysyłany natychmiast jako osobna wiadomość.
        AlertBuffer nie ma logiki dla burst-drop - to orchestrator routuje
        inaczej. Tutaj weryfikujemy tylko że jeśli w buforze jest już alert
        dla event_slug, dodanie kolejnego (zwykłego) NIE jest blokowane
        przez burst-drop wywołany OBOK bufora.
        """
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)

        # Pierwszy alert - timer startuje
        await buf.add("ev-1", make_alert("A"), "$86,000")
        # "Burst-drop" simulowany OBOK bufora - my po prostu nie wkładamy go.
        # Bufor pracuje dalej.
        await buf.add("ev-1", make_alert("C"), "$90,000")
        await _asyncio.sleep(0.10)

        # Bufor wyrzucił 2 zwykłe alerty, a burst-drop był poza nim
        assert len(rec.calls) == 1
        _, alerts = rec.calls[0]
        assert len(alerts) == 2

    async def test_shutdown_flushuje_pending(self):
        """shutdown() ma flushnąć wszystko co czeka, bez czekania do końca okna."""
        rec = FlushRecorder()
        # Długie okno - 2s. shutdown nie powinien czekać tyle.
        buf = AlertBuffer(window_seconds=2.0, on_flush=rec)

        await buf.add("ev-1", make_alert("A"), "$86,000")
        await buf.add("ev-2", make_alert("C"), "$90,000")

        import time as _time
        t0 = _time.monotonic()
        await buf.shutdown()
        elapsed = _time.monotonic() - t0

        # Shutdown nie zajął 2s (timer okna)
        assert elapsed < 1.0, f"shutdown trwał {elapsed:.2f}s - czekał na timer?"

        # Oba bufory zostały sflushowane
        assert buf.pending_count() == {}
        assert len(rec.calls) == 2
        slugs = {slug for slug, _ in rec.calls}
        assert slugs == {"ev-1", "ev-2"}

    async def test_shutdown_blokuje_kolejne_add(self):
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)
        await buf.shutdown()
        # add() po shutdown jest no-op
        await buf.add("ev-1", make_alert("A"), "$86,000")
        await _asyncio.sleep(0.10)
        assert rec.calls == []


# -----------------------------------------------------------------------------
# AlertBuffer - DEDUPLIKACJA per (token_id, alert_type)
# -----------------------------------------------------------------------------


class TestAlertBufferDeduplication:
    """
    Bug fix: bufor wysyłał ten sam alert wielokrotnie w jednej wiadomości
    skonsolidowanej. Po fix: w obrębie jednego okna deduplikujemy po
    (token_id, alert_type) - max 1 wpis per (podrynek + strona + typ).
    """

    async def test_5_alertow_A_dla_tego_samego_tokena_jeden_wpis(self):
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)

        # 5 alertów A dla TEGO SAMEGO tokena - wartości się zmieniają
        for ask_sum in (28000, 25000, 22000, 18000, 15000):
            await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                          {"ask_sum": ask_sum, "previous_sum": 33000},
                          token_id="TOK_83k_NO"), "Bitcoin $83,000")

        await _asyncio.sleep(0.10)

        assert len(rec.calls) == 1
        slug, alerts = rec.calls[0]
        # KLUCZOWE: tylko 1 wpis pomimo 5 add'ów
        assert len(alerts) == 1
        # I to z NAJNOWSZYMI wartościami (ostatnia wartość: 15000)
        assert alerts[0].alert.payload["ask_sum"] == 15000

    async def test_alert_A_i_D_dla_tego_samego_tokena_dwa_wpisy(self):
        """Różne typy alertu = osobne wpisy (nie deduplikujemy across types)."""
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)

        await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                      {"ask_sum": 25000}, token_id="TOK_83k_NO"),
                      "Bitcoin $83,000")
        await buf.add("ev-btc", make_alert("D", "NO", 0.998,
                      {"old_size": 0, "new_size": 25000, "delta": 25000},
                      token_id="TOK_83k_NO"),
                      "Bitcoin $83,000")

        await _asyncio.sleep(0.10)

        slug, alerts = rec.calls[0]
        types = sorted(a.alert.alert_type for a in alerts)
        assert types == ["A", "D"]
        assert len(alerts) == 2

    async def test_3_podrynki_kazdy_po_10_razy_trzy_wpisy(self):
        """Spec: alerty dla 3 różnych podrynków, każdy po 10 razy → 3 wpisy."""
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)

        # 10x ten sam alert dla każdego z 3 podrynków
        for _ in range(10):
            await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                          {"ask_sum": 25000}, token_id="TOK_83k_NO"),
                          "Bitcoin $83,000")
            await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                          {"ask_sum": 18000}, token_id="TOK_85k_NO"),
                          "Bitcoin $85,000")
            await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                          {"ask_sum": 12000}, token_id="TOK_88k_NO"),
                          "Bitcoin $88,000")

        await _asyncio.sleep(0.10)

        slug, alerts = rec.calls[0]
        # 30 add'ów -> 3 unikalne wpisy
        assert len(alerts) == 3
        # Każdy podrynek reprezentowany dokładnie raz
        token_ids = sorted(a.alert.token_id for a in alerts)
        assert token_ids == ["TOK_83k_NO", "TOK_85k_NO", "TOK_88k_NO"]

    async def test_dedup_per_side_yes_i_no_osobno(self):
        """YES i NO tego samego rynku to osobne wpisy (różne token_id)."""
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)

        await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                      {"ask_sum": 11000}, token_id="TOK_83k_NO"),
                      "Bitcoin $83,000")
        await buf.add("ev-btc", make_alert("A", "YES", 0.999,
                      {"ask_sum": 4600}, token_id="TOK_83k_YES"),
                      "Bitcoin $83,000")

        await _asyncio.sleep(0.10)

        slug, alerts = rec.calls[0]
        sides = sorted(a.alert.side for a in alerts)
        assert sides == ["NO", "YES"]
        assert len(alerts) == 2

    async def test_aktualizacja_zachowuje_najnowsze_wartosci(self):
        """Spec: 'Aktualizuj istniejący wpis najnowszymi wartościami
        (current depth, drop, itd.)'"""
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.05, on_flush=rec)

        # Pierwszy alert - ask_sum 28k, drop 5k
        await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                      {"ask_sum": 28000, "previous_sum": 33000},
                      token_id="TOK1"), "Bitcoin $83,000 v1")
        # Drugi alert - ask_sum 22k, drop 11k (świeższe wartości)
        await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                      {"ask_sum": 22000, "previous_sum": 33000},
                      token_id="TOK1"), "Bitcoin $83,000 v2")

        await _asyncio.sleep(0.10)

        slug, alerts = rec.calls[0]
        assert len(alerts) == 1
        # NAJNOWSZE wartości - z drugiego add'a
        assert alerts[0].alert.payload["ask_sum"] == 22000
        # market_question też najnowsze
        assert alerts[0].market_question == "Bitcoin $83,000 v2"

    async def test_dedup_nie_resetuje_timera(self):
        """Aktualizacja istniejącego wpisu NIE może resetować timera flush."""
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.10, on_flush=rec)

        # t=0: pierwszy alert, timer startuje (flush za 100ms)
        await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                      {"ask_sum": 28000}, token_id="TOK1"), "Bitcoin $83,000")

        # t=80ms: aktualizacja (dedup) - JEŚLI resetowałby timer, flush byłby
        # za kolejne 100ms (czyli t=180ms). Sprawdzimy że flush nastąpił do
        # t~110ms.
        await _asyncio.sleep(0.08)
        await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                      {"ask_sum": 22000}, token_id="TOK1"), "Bitcoin $83,000")

        # t=130ms: flush już powinien się wydarzyć (timer NIE zresetowany)
        await _asyncio.sleep(0.05)
        assert len(rec.calls) == 1

    async def test_dedup_zachowuje_oryginalny_detected_at(self):
        """detected_at zachowane przy aktualizacji - do tie-breakera."""
        rec = FlushRecorder()
        buf = AlertBuffer(window_seconds=0.10, on_flush=rec)

        # Najpierw dodaj alert dla TOK1 (older)
        await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                      {"ask_sum": 25000}, token_id="TOK1"), "$83,000")
        await _asyncio.sleep(0.02)
        # Potem TOK2 (newer)
        await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                      {"ask_sum": 20000}, token_id="TOK2"), "$83,000")
        await _asyncio.sleep(0.02)
        # Aktualizuj TOK1 - detected_at TOK1 powinien być nadal STARSZY niż TOK2
        await buf.add("ev-btc", make_alert("A", "NO", 0.999,
                      {"ask_sum": 18000}, token_id="TOK1"), "$83,000 updated")

        await _asyncio.sleep(0.15)
        slug, alerts = rec.calls[0]
        assert len(alerts) == 2
        tok1 = next(a for a in alerts if a.alert.token_id == "TOK1")
        tok2 = next(a for a in alerts if a.alert.token_id == "TOK2")
        # TOK1 wykryty jako pierwszy, więc jego detected_at < detected_at TOK2
        assert tok1.detected_at < tok2.detected_at


# -----------------------------------------------------------------------------
# check_bid_support - filtr "bid support"
# -----------------------------------------------------------------------------


from bot.alerts.bid_support import BidSupportResult, check_bid_support


class TestCheckBidSupport:
    """
    Filtr sprawdza czy na BIDZIE (kupujący) na cenie DOKŁADNIE 0.997
    jest co najmniej min_shares shares.

    Cena 0.997 = 99.7¢ - parametryzowana, ale w testach używamy domyślnej.
    """

    REQUIRED = 0.997

    def _book(self, bids=None, asks=None):
        return book(bids=bids or [], asks=asks or [])

    # ----- granice 0/1/1000 -----

    def test_zero_shares_brak_supportu(self):
        b = self._book(bids=[(0.997, 0)])  # poziom istnieje ale puste
        result = check_bid_support(b, "NO", self.REQUIRED, min_shares=1)
        assert result.has_support is False
        assert result.shares_at_price == 0
        assert bool(result) is False

    def test_jeden_share_jest_support(self):
        b = self._book(bids=[(0.997, 1)])
        result = check_bid_support(b, "NO", self.REQUIRED, min_shares=1)
        assert result.has_support is True
        assert result.shares_at_price == 1

    def test_1000_shares_jest_support(self):
        b = self._book(bids=[(0.997, 1000)])
        result = check_bid_support(b, "NO", self.REQUIRED)
        assert result.has_support is True
        assert result.shares_at_price == 1000

    # ----- DOKŁADNA cena 0.997 (nie 0.998 ani 0.996) -----

    def test_bid_tylko_na_998_brak_supportu(self):
        # Na 0.998 jest dużo shares, ale na 0.997 NIC -> False
        b = self._book(bids=[(0.998, 5000)])
        result = check_bid_support(b, "NO", self.REQUIRED)
        assert result.has_support is False
        assert result.shares_at_price == 0

    def test_bid_tylko_na_996_brak_supportu(self):
        b = self._book(bids=[(0.996, 5000)])
        result = check_bid_support(b, "NO", self.REQUIRED)
        assert result.has_support is False

    def test_kilka_poziomow_liczy_tylko_997(self):
        # Bidy na 0.996, 0.997, 0.998 - liczymy TYLKO 0.997
        b = self._book(bids=[
            (0.996, 100),
            (0.997, 50),
            (0.998, 200),
        ])
        result = check_bid_support(b, "NO", self.REQUIRED)
        assert result.has_support is True
        assert result.shares_at_price == 50

    # ----- pusty book / None -----

    def test_pusty_book_brak_supportu(self):
        b = self._book(bids=[])
        result = check_bid_support(b, "NO", self.REQUIRED)
        assert result.has_support is False

    def test_brak_bookow_w_ogole(self):
        # WS jeszcze nie miał snapshotu -> get_book zwrócił None
        result = check_bid_support(None, "NO", self.REQUIRED)
        assert result.has_support is False
        assert result.shares_at_price == 0

    # ----- side jest tylko diagnostyczny -----

    def test_side_zachowany_w_wyniku(self):
        b = self._book(bids=[(0.997, 100)])
        r_yes = check_bid_support(b, "YES", self.REQUIRED)
        r_no = check_bid_support(b, "NO", self.REQUIRED)
        # Logika identyczna - ten sam book, więc ten sam wynik
        assert r_yes.has_support == r_no.has_support
        # Ale `side` jest echo'owane do późniejszego logowania
        assert r_yes.side == "YES"
        assert r_no.side == "NO"

    # ----- precyzja float -----

    def test_precyzja_float(self):
        # 0.997 może być reprezentowane nieidealnie - test że nasza
        # tolerancja (1e-9) działa
        b = self._book(bids=[(0.997, 100)])
        result = check_bid_support(b, "NO", 0.997, min_shares=1)
        assert result.has_support is True

    # ----- summing ASKS (bug check - upewnij się że NIE patrzymy na asks) -----

    def test_asks_NIE_liczone(self):
        # Asks na 0.997 (nawet duże) NIE dają supportu - liczymy tylko bids
        b = self._book(bids=[], asks=[(0.997, 100000)])
        result = check_bid_support(b, "NO", self.REQUIRED)
        assert result.has_support is False
        assert result.shares_at_price == 0

    # ----- min_shares parametryzowane -----

    def test_min_shares_500(self):
        b = self._book(bids=[(0.997, 400)])
        # Z min_shares=500, 400 to za mało
        assert check_bid_support(b, "NO", self.REQUIRED, min_shares=500).has_support is False
        # Z min_shares=300, 400 wystarcza
        assert check_bid_support(b, "NO", self.REQUIRED, min_shares=300).has_support is True

    def test_bool_konwersja(self):
        b_ok = self._book(bids=[(0.997, 100)])
        b_no = self._book(bids=[])
        assert bool(check_bid_support(b_ok, "NO", self.REQUIRED)) is True
        assert bool(check_bid_support(b_no, "NO", self.REQUIRED)) is False


# =============================================================================
#  Integracja filtra bid_support z Orchestratorem (E2E)
# =============================================================================
#  Tu testujemy _maybe_send - czy alerty są wyciszane / przepuszczane.
#  Używamy Fake-ów dla WS i Sender, ale REALNYCH Database, Detector, AlertBuffer.
# =============================================================================


from bot.config import (
    BidSupportFilterConfig,
    BotConfig,
    TelegramConfig,
)
from bot.main import Orchestrator
from bot.storage.db import Database


class _FakeWS:
    """Fake CLOBWebSocketManager - tylko trzyma books per token_id."""

    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}

    def set_book(self, token_id: str, b: OrderBook) -> None:
        self._books[token_id] = b

    def get_book(self, token_id: str):
        return self._books.get(token_id)


class _FakeSender:
    """Fake TelegramSender - przechwytuje wysłane wiadomości."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    async def send_html(self, message: str, disable_preview: bool = True) -> bool:
        self.sent.append(message)
        return True


def _market_row(
    token_yes_id: str = "TYES",
    token_no_id: str = "TNO",
    event_slug: str = "bitcoin-may-7",
    question: str = "Will Bitcoin reach $86,000 on May 7",
    condition_id: str = "COND1",
):
    """Sqlite Row jest dict-like - dla testów wystarczy zwykły dict."""
    return {
        "condition_id": condition_id,
        "event_slug": event_slug,
        "question": question,
        "token_yes_id": token_yes_id,
        "token_no_id": token_no_id,
    }


def _build_orchestrator(
    tmp_path,
    *,
    book_yes: OrderBook | None = None,
    book_no: OrderBook | None = None,
    filter_enabled: bool = True,
    required_price: float = 0.997,
    min_total_shares: float = 1.0,
    aggregation_window_seconds: int = 600,   # długi - żeby flush nie zaszedł
    cooldown_seconds: int = 0,                # 0 = brak cooldownu w testach
):
    """
    Buduje minimalnego Orchestratora z fake WS/Sender + realnymi
    DB/Detector/AlertBuffer. Zwraca tuple (orchestrator, ws, sender, buffer).
    """
    cfg = BotConfig(
        telegram=TelegramConfig(bot_token="123:abc", chat_id="42"),
        bid_support_filter=BidSupportFilterConfig(
            enabled=filter_enabled,
            required_price=required_price,
            min_total_shares=min_total_shares,
        ),
        alert_cooldown_seconds=cooldown_seconds,
        aggregation_window_seconds=aggregation_window_seconds,
    )
    db = Database(tmp_path / "test.db")
    # Wpis o evencie żeby _event_title działał
    db.upsert_event(
        slug="bitcoin-may-7", event_id="E1",
        title="Bitcoin Above ___ on May 7",
        end_date=None, source="manual",
    )
    ws = _FakeWS()
    if book_yes:
        ws.set_book("TYES", book_yes)
    if book_no:
        ws.set_book("TNO", book_no)
    sender = _FakeSender()
    detector = Detector(thresholds=DetectorThresholds())
    # Lambda-late-binding na orchestrator (jak w main.py)
    buffer = AlertBuffer(
        window_seconds=aggregation_window_seconds,
        on_flush=lambda s, a: orchestrator.on_buffer_flush(s, a),
    )
    orchestrator = Orchestrator(cfg, db, ws, detector, sender, buffer)
    return orchestrator, ws, sender, buffer


class TestBidSupportIntegration:
    """E2E: filtr bid_support stosowany w Orchestrator._maybe_send.

    Warning 'Task was destroyed but it is pending' przy końcu testów to
    pending task AlertBuffer-a z długim oknem (600s). Niegroźne -
    event loop jest closed razem z taskiem. Ignorujemy.
    """

    # --- alert NIE leci gdy bidów na 0.997 brak ---

    async def test_alert_NO_bez_bidu_wyciszony(self, tmp_path):
        # Book NO ma asks (rynek "blisko 99.9¢") ale BID na 0.997 jest pusty
        book_no = book(bids=[], asks=[(0.999, 25000)])
        orch, ws, sender, buf = _build_orchestrator(tmp_path, book_no=book_no)

        alert = make_alert("A", "NO", 0.999,
                           {"ask_sum": 25000, "previous_sum": 33000},
                           token_id="TNO")
        market = _market_row()
        await orch._maybe_send(alert, market)

        assert sender.sent == []                # brak wysyłki
        assert buf.pending_count() == {}        # brak w buforze

    async def test_alert_YES_bez_bidu_wyciszony(self, tmp_path):
        book_yes = book(bids=[], asks=[(0.999, 25000)])
        orch, ws, sender, buf = _build_orchestrator(tmp_path, book_yes=book_yes)

        alert = make_alert("A", "YES", 0.999,
                           {"ask_sum": 25000, "previous_sum": 33000},
                           token_id="TYES")
        await orch._maybe_send(alert, _market_row())

        assert sender.sent == []
        assert buf.pending_count() == {}

    # --- granica 1 share / 1000 shares ---

    async def test_alert_z_bidem_1_share_leci(self, tmp_path):
        book_no = book(bids=[(0.997, 1)], asks=[(0.999, 25000)])
        orch, _, sender, buf = _build_orchestrator(tmp_path, book_no=book_no)

        alert = make_alert("A", "NO", 0.999,
                           {"ask_sum": 25000, "previous_sum": 33000},
                           token_id="TNO")
        await orch._maybe_send(alert, _market_row())

        # Alert poszedł do bufora (nie wyciszony) - 1 wpis dla 'bitcoin-may-7'
        assert buf.pending_count() == {"bitcoin-may-7": 1}

    async def test_alert_z_bidem_1000_shares_leci(self, tmp_path):
        book_no = book(bids=[(0.997, 1000)], asks=[(0.999, 25000)])
        orch, _, sender, buf = _build_orchestrator(tmp_path, book_no=book_no)

        alert = make_alert("A", "NO", 0.999,
                           {"ask_sum": 25000, "previous_sum": 33000},
                           token_id="TNO")
        await orch._maybe_send(alert, _market_row())

        assert buf.pending_count() == {"bitcoin-may-7": 1}

    # --- bidy na innych cenach NIE pomagają ---

    async def test_bidy_tylko_na_998_brak_supportu(self, tmp_path):
        # Bidy na 0.998 (i nawet duże), ale NIE na 0.997 -> filtr wycisza
        book_no = book(bids=[(0.998, 5000)], asks=[(0.999, 25000)])
        orch, _, sender, buf = _build_orchestrator(tmp_path, book_no=book_no)

        alert = make_alert("A", "NO", 0.999,
                           {"ask_sum": 25000}, token_id="TNO")
        await orch._maybe_send(alert, _market_row())

        assert sender.sent == []
        assert buf.pending_count() == {}

    async def test_bidy_tylko_na_996_brak_supportu(self, tmp_path):
        book_no = book(bids=[(0.996, 5000)], asks=[(0.999, 25000)])
        orch, _, sender, buf = _build_orchestrator(tmp_path, book_no=book_no)

        alert = make_alert("A", "NO", 0.999,
                           {"ask_sum": 25000}, token_id="TNO")
        await orch._maybe_send(alert, _market_row())

        assert sender.sent == []
        assert buf.pending_count() == {}

    # --- filtr stosowany dla wszystkich typów alertów A/B/C/D ---

    async def test_filter_typ_A_wyciszony_bez_bidu(self, tmp_path):
        book_no = book(bids=[], asks=[(0.999, 25000)])
        orch, _, _, buf = _build_orchestrator(tmp_path, book_no=book_no)
        alert = make_alert("A", "NO", 0.999,
                           {"ask_sum": 25000}, token_id="TNO")
        await orch._maybe_send(alert, _market_row())
        assert buf.pending_count() == {}

    async def test_filter_typ_B_wyciszony_bez_bidu(self, tmp_path):
        book_no = book(bids=[], asks=[(0.999, 25000)])
        orch, _, _, buf = _build_orchestrator(tmp_path, book_no=book_no)
        alert = make_alert("B", "NO", 0.999,
                           {"size": 12000, "price": 0.999}, token_id="TNO")
        await orch._maybe_send(alert, _market_row())
        assert buf.pending_count() == {}

    async def test_filter_typ_C_wyciszony_bez_bidu(self, tmp_path):
        book_no = book(bids=[], asks=[(0.999, 25000)])
        orch, _, _, buf = _build_orchestrator(tmp_path, book_no=book_no)
        alert = make_alert("C", "NO", 0.999,
                           {"old_size": 3000, "new_size": 8000, "delta": 5000},
                           token_id="TNO")
        await orch._maybe_send(alert, _market_row())
        assert buf.pending_count() == {}

    async def test_filter_typ_D_wyciszony_bez_bidu(self, tmp_path):
        book_no = book(bids=[], asks=[(0.999, 25000)])
        orch, _, _, buf = _build_orchestrator(tmp_path, book_no=book_no)
        alert = make_alert("D", "NO", 0.998,
                           {"old_size": 0, "new_size": 25000, "delta": 25000},
                           token_id="TNO")
        await orch._maybe_send(alert, _market_row())
        assert buf.pending_count() == {}

    # --- burst-drop też filtrowany ---

    async def test_burst_drop_wyciszony_bez_bidu(self, tmp_path):
        book_no = book(bids=[], asks=[(0.999, 22000)])
        orch, _, sender, buf = _build_orchestrator(tmp_path, book_no=book_no)
        alert = make_alert("A", "NO", 0.999,
                           {"ask_sum": 22000, "previous_sum": 30000},
                           bypass=True, token_id="TNO")
        await orch._maybe_send(alert, _market_row())

        # Burst-drop NIE poszedł - sender pusty
        assert sender.sent == []
        assert buf.pending_count() == {}

    async def test_burst_drop_leci_z_bidem(self, tmp_path):
        # Burst-drop ma bid support -> wysłany NATYCHMIAST przez sender
        book_no = book(bids=[(0.997, 100)], asks=[(0.999, 22000)])
        orch, _, sender, buf = _build_orchestrator(tmp_path, book_no=book_no)
        alert = make_alert("A", "NO", 0.999,
                           {"ask_sum": 22000, "previous_sum": 30000},
                           bypass=True, token_id="TNO")
        await orch._maybe_send(alert, _market_row())

        assert len(sender.sent) == 1
        assert "Burst-drop" in sender.sent[0]
        assert buf.pending_count() == {}        # burst nie idzie do bufora

    # --- filtr wyłączony - wszystko leci ---

    async def test_filter_wylaczony_bez_bidu_alert_leci(self, tmp_path):
        # Filtr OFF, brak bidu -> alert i tak leci do bufora
        book_no = book(bids=[], asks=[(0.999, 25000)])
        orch, _, _, buf = _build_orchestrator(
            tmp_path, book_no=book_no, filter_enabled=False,
        )
        alert = make_alert("A", "NO", 0.999,
                           {"ask_sum": 25000}, token_id="TNO")
        await orch._maybe_send(alert, _market_row())
        assert buf.pending_count() == {"bitcoin-may-7": 1}

    # --- KLUCZOWE: wyciszony alert NIE zajmuje miejsca w buforze ---

    async def test_3_alerty_jeden_wyciszony_w_buforze_2(self, tmp_path):
        """
        Spec: "dla 3 alertów z których 1 jest wyciszony, w wiadomości
        skonsolidowanej widać 2 alerty, nie 3".

        Mamy 3 podrynki tego samego eventu:
          - $86k NO: bid 100 shares na 0.997 -> alert leci
          - $90k NO: bid 0 shares na 0.997   -> alert WYCISZONY
          - $80k NO: bid 50 shares na 0.997  -> alert leci
        Bufor powinien mieć 2 wpisy (nie 3).
        """
        # 3 osobne tokeny dla 3 podrynków
        ws_books = {
            "TNO_86": book(bids=[(0.997, 100)], asks=[(0.999, 25000)]),  # OK
            "TNO_90": book(bids=[],            asks=[(0.999, 22000)]),   # silenced
            "TNO_80": book(bids=[(0.997, 50)],  asks=[(0.998, 18000)]),  # OK
        }

        orch, ws, _, buf = _build_orchestrator(tmp_path)
        for tok, b in ws_books.items():
            ws.set_book(tok, b)

        async def fire(token, q, price):
            alert = make_alert("A", "NO", price,
                               {"ask_sum": 22000}, token_id=token)
            market = _market_row(
                token_no_id=token,
                question=q,
                condition_id=token,
            )
            await orch._maybe_send(alert, market)

        await fire("TNO_86", "Bitcoin reach $86,000", 0.999)
        await fire("TNO_90", "Bitcoin reach $90,000", 0.999)   # WYCISZONY
        await fire("TNO_80", "Bitcoin reach $80,000", 0.998)

        # Bufor ma DOKŁADNIE 2 wpisy, nie 3
        assert buf.pending_count() == {"bitcoin-may-7": 2}

    # --- Etap 4: logowanie wyciszeń ---

    async def test_log_INFO_przy_wyciszeniu(self, tmp_path, caplog):
        """Sprawdza format logu zgodnie ze specyfikacją."""
        import logging

        book_no = book(bids=[], asks=[(0.999, 25000)])
        orch, _, _, _ = _build_orchestrator(tmp_path, book_no=book_no)

        # Loguru -> propagacja do standard logging żeby caplog złapał
        from loguru import logger as _loguru
        handler_id = _loguru.add(
            lambda m: logging.getLogger("loguru").info(m.rstrip()),
            level="INFO", format="{message}",
        )
        try:
            with caplog.at_level(logging.INFO, logger="loguru"):
                alert = make_alert("A", "NO", 0.999,
                                   {"ask_sum": 25000}, token_id="TNO")
                await orch._maybe_send(alert, _market_row())
        finally:
            _loguru.remove(handler_id)

        # Wszystkie pola wymagane przez spec są w logu
        log_text = " | ".join(r.getMessage() for r in caplog.records)
        assert "Alert wyciszony (brak bid support)" in log_text
        assert "event=bitcoin-may-7" in log_text
        assert "side=NO" in log_text
        assert "alert_type=A" in log_text
        assert "price_cents=99.7" in log_text
        assert "shares=0" in log_text


# -----------------------------------------------------------------------------
# Sanity test integracyjny: burst-drop INSTANT + zwykłe alerty AGREGUJĄ
# -----------------------------------------------------------------------------


class FakeSender:
    """Fake TelegramSender do testu - zbiera wysłane wiadomości."""

    def __init__(self):
        self.messages: list[str] = []

    async def send_html(self, message: str, disable_preview: bool = True) -> bool:
        self.messages.append(message)
        return True


@pytest.mark.asyncio
class TestE2EBufferIntegration:
    """
    Symuluje pełny przepływ: alert -> AlertBuffer -> formatter -> sender.
    Weryfikuje że burst-drop idzie INSTANT, a inne alerty czekają na okno.
    """

    async def test_burst_drop_instant_inne_aggreguja(self):
        sender = FakeSender()

        # Symulujemy logikę z Orchestrator._maybe_send w uproszczeniu:
        # - burst-drop -> sender INSTANT (omija buffer)
        # - inne -> buffer.add (po oknie -> sender)

        async def on_flush(slug: str, alerts: list[BufferedAlert]) -> None:
            msg = format_consolidated_message(
                alerts_with_questions=[(a.alert, a.market_question) for a in alerts],
                event_title="Bitcoin Above ___ on May 6",
                event_slug=slug,
            )
            await sender.send_html(msg)

        buf = AlertBuffer(window_seconds=0.05, on_flush=on_flush)

        # Alert C - zwykły, do bufora
        c_alert = make_alert("C", "NO", 0.999,
                             {"old_size": 3000, "new_size": 8000, "delta": 5000})
        await buf.add("bitcoin-above-on-may-6", c_alert, "Will BTC reach $86,000 on May 6")

        # Alert A burst-drop - INSTANT, omija buffer
        burst_alert = make_alert("A", "NO", 0.999,
                                 {"ask_sum": 22000, "previous_sum": 35000},
                                 bypass=True)
        burst_msg = format_burst_drop_message(
            alert=burst_alert,
            market_question="Will BTC reach $86,000 on May 6",
            event_title="Bitcoin Above ___ on May 6",
            event_slug="bitcoin-above-on-may-6",
        )
        await sender.send_html(burst_msg)

        # Alert D - zwykły, do bufora (timer NIE resetowany)
        d_alert = make_alert("D", "NO", 0.998,
                             {"old_size": 0, "new_size": 25000, "delta": 25000})
        await buf.add("bitcoin-above-on-may-6", d_alert, "Will BTC reach $90,000 on May 6")

        # W tym momencie wysłano: 1 wiadomość (burst-drop)
        assert len(sender.messages) == 1
        assert "⚡ Burst-drop" in sender.messages[0]

        # Czekamy na flush bufora
        await _asyncio.sleep(0.10)

        # Teraz wysłano: 2 wiadomości
        # 1. burst-drop (instant)
        # 2. skonsolidowana z C+D
        assert len(sender.messages) == 2
        consolidated = sender.messages[1]

        # Skonsolidowana ma OBA alerty
        assert "$86,000" in consolidated  # alert C market_question
        assert "$90,000" in consolidated  # alert D market_question
        assert "📤" in consolidated         # emoji C
        assert "🛑" in consolidated         # emoji D

        # Skonsolidowana NIE ma burst-drop (to inna wiadomość)
        assert "⚡ Burst-drop" not in consolidated

        # Sortowanie: $90,000 PRZED $86,000 (malejąco po kwocie)
        idx_90 = consolidated.find("$90,000")
        idx_86 = consolidated.find("$86,000")
        assert idx_90 < idx_86

    async def test_dwa_eventy_dwie_wiadomosci(self):
        sender = FakeSender()

        async def on_flush(slug, alerts):
            msg = format_consolidated_message(
                alerts_with_questions=[(a.alert, a.market_question) for a in alerts],
                event_title=f"Event {slug}",
                event_slug=slug,
            )
            await sender.send_html(msg)

        buf = AlertBuffer(window_seconds=0.05, on_flush=on_flush)

        # Dwa eventy
        await buf.add("bitcoin-may-6",
                      make_alert("C", "NO", 0.999, {"old_size": 0, "new_size": 6000, "delta": 6000}),
                      "BTC $86,000")
        await buf.add("ethereum-may-7",
                      make_alert("D", "NO", 0.998, {"old_size": 0, "new_size": 20000, "delta": 20000}),
                      "ETH $3,500")

        await _asyncio.sleep(0.10)

        # Dwie wiadomości - po jednej per event
        assert len(sender.messages) == 2
        # Bitcoin ma ikonę ₿, Ethereum Ξ
        msgs_text = "\n----\n".join(sender.messages)
        assert "₿" in msgs_text
        assert "Ξ" in msgs_text
