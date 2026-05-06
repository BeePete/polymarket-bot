"""
Testy jednostkowe detektora alertów.

Uruchamianie:
    cd polymarket-bot
    python -m pytest tests/ -v
"""
from __future__ import annotations

import pytest

from bot.alerts.detector import Detector, DetectorThresholds
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
