"""
Конфигурация приложения.
Все параметры читаются из .env или переменных окружения.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()


def _require_env(key: str) -> str:
    """Читает обязательную переменную окружения."""
    value = os.getenv(key, "").strip()
    if not value:
        raise EnvironmentError(
            f"Переменная окружения '{key}' не задана. "
            f"Проверьте файл .env"
        )
    return value


def _optional_env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass(frozen=True)
class Settings:
    # Токен — только чтение (основной ключ + совместимый фолбэк)
    read_token: str = field(
        default_factory=lambda: (
            os.getenv("TINKOFF_READ_TOKEN", "").strip()
            or _require_env("TINKOFF_TOKEN")
        )
    )

    # Режим: торговые заявки запрещены пока False
    live_enabled: bool = field(
        default_factory=lambda: _optional_env(
            "LIVE_ENABLED", "false"
        ).lower() == "true"
    )

    # Квал-цели
    kval_target: Decimal = field(
        default_factory=lambda: Decimal(
            _optional_env("KVAL_TARGET_TURNOVER", "6000000")
        )
    )
    kval_target_with_edu: Decimal = field(
        default_factory=lambda: Decimal(
            _optional_env("KVAL_TARGET_TURNOVER_WITH_ECON_EDU", "4000000")
        )
    )
    safety_buffer: Decimal = field(
        default_factory=lambda: Decimal(
            _optional_env("SAFETY_BUFFER_TURNOVER", "100000")
        )
    )

    # Комиссия брокера в б.п. (1 б.п. = 0.01%). Опционально из env.
    commission_bps: Decimal | None = field(
        default_factory=lambda: (
            Decimal(_optional_env("TINKOFF_COMMISSION_BPS"))
            if _optional_env("TINKOFF_COMMISSION_BPS") else None
        )
    )

    @property
    def effective_target(self) -> Decimal:
        """Цель с учётом буфера безопасности."""
        return self.kval_target + self.safety_buffer

    def __post_init__(self) -> None:
        if self.live_enabled:
            raise RuntimeError(
                "LIVE_ENABLED=true запрещён на Этапе 1. "
                "Торговые операции не реализованы."
            )


# Единственный экземпляр
settings = Settings()
