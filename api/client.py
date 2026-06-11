"""
Read-only клиент T-Invest API.

Только GET-методы. Торговые операции не реализованы.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Iterator

from loguru import logger
from tinkoff.invest import Client  # type: ignore[import]
from tinkoff.invest.exceptions import RequestError  # type: ignore[import]
from tinkoff.invest.schemas import (  # type: ignore[import]
    AccountType,
    GetOperationsByCursorRequest,
    OperationState,
    OperationType,
)

from config.settings import settings

# Операции, которые запрашиваем у API
_FETCH_OPERATION_TYPES = [
    OperationType.OPERATION_TYPE_BUY,
    OperationType.OPERATION_TYPE_SELL,
    OperationType.OPERATION_TYPE_BUY_CARD,
    # Намеренно не фильтруем на уровне API валюту и прочее —
    # фильтрацию делаем в operation_filter.py для прозрачности.
    # Но чтобы не тянуть лишнее — добавим явный список.
]

_RATE_LIMIT_SLEEP = 1.0   # секунды между повторными попытками
_MAX_RETRIES = 5


def _with_retry(func, *args, **kwargs):
    """Обёртка с повторными попытками при rate-limit."""
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except RequestError as exc:
            # Код 429 или RESOURCE_EXHAUSTED
            msg = str(exc).lower()
            if "resource_exhausted" in msg or "429" in msg or "rate" in msg:
                wait = _RATE_LIMIT_SLEEP * attempt
                logger.warning(
                    f"Rate-limit (попытка {attempt}/{_MAX_RETRIES}). "
                    f"Ждём {wait:.1f}с..."
                )
                time.sleep(wait)
                last_exc = exc
            else:
                raise
        except Exception:
            raise
    raise last_exc  # type: ignore[misc]


class ReadOnlyClient:
    """
    Тонкая обёртка над T-Invest API.
    Использует исключительно read-only токен.
    Методы записи не реализованы намеренно.
    """

    def __init__(self) -> None:
        self._token = settings.read_token

    # ─── Счета ──────────────────────────────────────────────────────────────

    def get_broker_accounts(self) -> list:
        """
        Возвращает все брокерские счета, доступные по токену.
        Фильтруем только ACCOUNT_TYPE_TINKOFF (брокерский).
        """
        with Client(self._token) as client:
            response = _with_retry(client.users.get_accounts)
        accounts = [
            acc for acc in response.accounts
            if acc.type == AccountType.ACCOUNT_TYPE_TINKOFF
        ]
        logger.info(f"Найдено брокерских счетов: {len(accounts)}")
        return accounts

    def get_all_accounts(self) -> list:
        """Все счета без фильтрации по типу (для диагностики)."""
        with Client(self._token) as client:
            response = _with_retry(client.users.get_accounts)
        return list(response.accounts)

    # ─── Операции ───────────────────────────────────────────────────────────

    def iter_operations(
        self,
        account_id: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> Iterator:
        """
        Итерирует все выполненные операции за период с пагинацией.

        Использует GetOperationsByCursor согласно документации T-Bank.
        state = OPERATION_STATE_EXECUTED
        without_trades = False  (нужны детали trades для точного оборота)
        limit = 1000

        Yields
        ------
        OperationItem
            Одна операция.
        """
        cursor = ""
        page = 0

        while True:
            page += 1
            logger.debug(
                f"Запрос операций: счёт={account_id}, "
                f"страница={page}, cursor='{cursor[:20]}...'"
                if cursor else
                f"Запрос операций: счёт={account_id}, страница={page}"
            )

            request = GetOperationsByCursorRequest(
                account_id=account_id,
                from_=from_dt,
                to=to_dt,
                cursor=cursor,
                limit=1000,
                operation_types=_FETCH_OPERATION_TYPES,
                state=OperationState.OPERATION_STATE_EXECUTED,
                without_commissions=True,   # комиссии нам не нужны
                without_trades=False,        # trades нужны для точного оборота
                without_overnights=True,     # овернайты не нужны
            )

            with Client(self._token) as client:
                response = _with_retry(
                    client.operations.get_operations_by_cursor,
                    request,
                )

            items = list(response.items)
            logger.debug(f"Получено операций на странице {page}: {len(items)}")

            yield from items

            if not response.has_next:
                break

            cursor = response.next_cursor
            # Уважаем rate-limit: небольшая пауза между страницами
            time.sleep(0.1)

    def get_operations(
        self,
        account_id: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list:
        """Загружает все операции в список (обёртка над iter_operations)."""
        ops = list(self.iter_operations(account_id, from_dt, to_dt))
        logger.info(
            f"Счёт {account_id}: загружено {len(ops)} операций "
            f"за {from_dt.date()} … {to_dt.date()}"
        )
        return ops
