"""
Ładowanie i walidacja konfiguracji bota z pliku config.yaml.

Plik konfiguracji jest podzielony na sekcje (telegram, thresholds, etc.).
Używamy Pydantic do walidacji typów - jeśli ktoś wpisze np. literę zamiast
liczby, dostanie czytelny błąd zamiast tajemniczego "crash" w środku bota.

Sekrety (token Telegrama, chat_id) można też trzymać w pliku .env -
mają wtedy pierwszeństwo nad wartościami z config.yaml.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


# -----------------------------------------------------------------------------
# Modele Pydantic - opisują strukturę config.yaml
# -----------------------------------------------------------------------------


class TelegramConfig(BaseModel):
    """Sekcja `telegram` - token bota i chat_id właściciela."""

    bot_token: str
    chat_id: str

    @field_validator("bot_token")
    @classmethod
    def _validate_token(cls, v: str) -> str:
        if not v or v.startswith("WSTAW_TUTAJ"):
            raise ValueError(
                "Wpisz prawdziwy token bota od @BotFather w config.yaml "
                "(pole telegram.bot_token)."
            )
        return v.strip()

    @field_validator("chat_id")
    @classmethod
    def _validate_chat_id(cls, v: str) -> str:
        v = str(v).strip()
        if not v or v.startswith("WSTAW_TUTAJ"):
            raise ValueError(
                "Wpisz prawdziwy chat_id w config.yaml (pole telegram.chat_id)."
            )
        return v


class ThresholdsConfig(BaseModel):
    """Sekcja `thresholds` - progi alertów A, B, C, D."""

    ask_melting_threshold: int = 30000
    ask_melting_burst_drop: int = 5000
    market_buy_min_size: int = 5000
    new_sell_order_min_size: int = 5000
    big_limit_buy_min_size: int = 19000


class AdvancedConfig(BaseModel):
    """Sekcja `advanced` - URL-e i parametry techniczne."""

    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_fetch_limit: int = 500
    max_monitored_tokens: int = 200
    log_level: str = "INFO"


class BotConfig(BaseModel):
    """Pełna konfiguracja bota - korzeń pliku config.yaml."""

    telegram: TelegramConfig
    auto_monitor_series: list[str] = Field(default_factory=list)
    manual_events: list[str] = Field(default_factory=list)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    monitored_prices: list[float] = Field(default_factory=lambda: [0.998, 0.999])
    alert_cooldown_seconds: int = 300
    monitor_hours_before_close: int = 24
    discovery_interval_seconds: int = 1800
    rescan_interval_seconds: int = 30
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)


# -----------------------------------------------------------------------------
# Ładowanie z dysku
# -----------------------------------------------------------------------------


DEFAULT_CONFIG_PATH = Path("config.yaml")


def _load_dotenv(path: Path) -> None:
    """
    Prosty parser pliku .env - tylko linie KEY=VALUE.
    Nie używamy biblioteki python-dotenv, żeby zminimalizować zależności.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # nie nadpisujemy zmiennych już istniejących w środowisku
        os.environ.setdefault(key, value)


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Sekrety z .env / ENV mają pierwszeństwo nad config.yaml."""
    telegram = data.setdefault("telegram", {})
    if env_token := os.getenv("TELEGRAM_BOT_TOKEN"):
        telegram["bot_token"] = env_token
    if env_chat := os.getenv("TELEGRAM_CHAT_ID"):
        telegram["chat_id"] = env_chat
    return data


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> BotConfig:
    """
    Wczytuje config.yaml + opcjonalnie .env. Zwraca zwalidowany BotConfig
    albo rzuca czytelny wyjątek z opisem co jest nie tak.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Nie znaleziono pliku konfiguracji: {path}\n"
            f"Skopiuj config.example.yaml do config.yaml i uzupełnij wartości:\n"
            f"  cp config.example.yaml config.yaml"
        )

    _load_dotenv(Path(".env"))

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = _apply_env_overrides(raw)

    return BotConfig.model_validate(raw)


# -----------------------------------------------------------------------------
# Zapis (dla komendy /set_threshold)
# -----------------------------------------------------------------------------


def save_thresholds(config_path: Path, thresholds: ThresholdsConfig) -> None:
    """
    Zapisuje zaktualizowane progi z powrotem do config.yaml, zachowując
    całą resztę pliku (komentarze niestety przepadają - to ograniczenie
    biblioteki PyYAML, ale wartości są bezpieczne).
    """
    config_path = Path(config_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["thresholds"] = thresholds.model_dump()
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
