"""
Read-only фасад над REST-коннектором Tinkoff.

Только read-only REST-вызовы (по факту HTTP POST к Tinkoff Invest API — это
особенность gRPC-over-REST у Т-Инвестиций, а не запись). Токен берётся из
настроек (read-only). Методы записи (postOrder/cancelOrder) не реализованы.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from brokers.tinkoff.rest_client import TinkoffReadOnlyClient
from config.settings import settings

# Типы операций, которые запрашиваем у API (фильтрацию делаем и в operation_filter)
_FETCH_OPERATION_TYPES = [
    "OPERATION_TYPE_BUY",
    "OPERATION_TYPE_SELL",
    "OPERATION_TYPE_BUY_CARD",
]


def _to_iso(dt: datetime) -> str:
    """datetime → RFC3339 c 'Z' для UTC."""
    s = dt.isoformat()
    return s.replace("+00:00", "Z")


class ReadOnlyClient:
    """Фасад: брокерские счета + операции за период. Read-only."""

    def __init__(self, rest: TinkoffReadOnlyClient | None = None) -> None:
        self._rest = rest or TinkoffReadOnlyClient(settings.read_token)

    def get_broker_accounts(self) -> list[dict[str, Any]]:
        return self._rest.get_broker_accounts()

    def get_all_accounts(self) -> list[dict[str, Any]]:
        """Все счета по токену (без фильтра по типу) — для команды accounts."""
        return self._rest.get_accounts()

    def instrument_resolver(self):
        """Резолвер инструментов, привязанный к этому REST-клиенту."""
        from brokers.tinkoff.instruments import InstrumentResolver
        return InstrumentResolver(self._rest)

    def get_operations(
        self,
        account_id: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[dict[str, Any]]:
        ops = self._rest.get_operations(
            account_id,
            _to_iso(from_dt),
            _to_iso(to_dt),
            operation_types=_FETCH_OPERATION_TYPES,
        )
        logger.info(f"Счёт {account_id}: {len(ops)} операций за {from_dt.date()} … {to_dt.date()}")
        return ops
