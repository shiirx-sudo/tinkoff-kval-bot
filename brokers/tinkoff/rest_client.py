"""
Read-only REST-клиент Tinkoff Invest API v2.

Перенос подхода из MOEX Advisor (brokers/tinkoff/client.py): прямой REST через
requests + Bearer-токен, без SDK. Добавлено: GetOperationsByCursor с пагинацией
и обход всех доступных счетов.

ТОЛЬКО чтение. Методы записи (postOrder/cancelOrder) намеренно не реализованы.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

import requests
from loguru import logger

_BASE_URL = "https://invest-public-api.tinkoff.ru/rest"
_DEFAULT_TIMEOUT = 10
_MAX_RETRIES = 5
_RATE_LIMIT_SLEEP = 1.0

_USERS = "tinkoff.public.invest.api.contract.v1.UsersService"
_OPERATIONS = "tinkoff.public.invest.api.contract.v1.OperationsService"
_INSTRUMENTS = "tinkoff.public.invest.api.contract.v1.InstrumentsService"

# Типы счетов Т-Инвестиций
ACCOUNT_TYPE_TINKOFF = "ACCOUNT_TYPE_TINKOFF"          # обычный брокерский
ACCOUNT_TYPE_TINKOFF_IIS = "ACCOUNT_TYPE_TINKOFF_IIS"  # ИИС
ACCOUNT_TYPE_INVEST_BOX = "ACCOUNT_TYPE_INVEST_BOX"    # Инвесткопилка
ACCOUNT_TYPE_UNSPECIFIED = "ACCOUNT_TYPE_UNSPECIFIED"

# Типы счетов, оборот по которым УЧИТЫВАЕТСЯ в квалификационном расчёте.
# И обычный брокерский, и ИИС — это самостоятельные брокерские счета, сделки
# по которым идут в оборот. Инвесткопилка и UNSPECIFIED не учитываются.
TURNOVER_ACCOUNT_TYPES: frozenset[str] = frozenset({
    ACCOUNT_TYPE_TINKOFF,
    ACCOUNT_TYPE_TINKOFF_IIS,
})

# Человекочитаемые метки типов счетов
ACCOUNT_TYPE_LABELS: dict[str, str] = {
    ACCOUNT_TYPE_TINKOFF: "broker",
    ACCOUNT_TYPE_TINKOFF_IIS: "iis",
    ACCOUNT_TYPE_INVEST_BOX: "invest_box",
    ACCOUNT_TYPE_UNSPECIFIED: "unspecified",
}

OPERATION_STATE_EXECUTED = "OPERATION_STATE_EXECUTED"

# Типы идентификаторов инструмента для GetInstrumentBy
INSTRUMENT_ID_TYPE_FIGI = "INSTRUMENT_ID_TYPE_FIGI"
INSTRUMENT_ID_TYPE_UID = "INSTRUMENT_ID_TYPE_UID"


def account_type_label(acc_type: str) -> str:
    """Короткая метка типа счёта (broker / iis / invest_box / …)."""
    return ACCOUNT_TYPE_LABELS.get(acc_type, (acc_type or "").lower())


def is_turnover_account(account: dict[str, Any]) -> bool:
    """True, если оборот по счёту учитывается в квалификационном расчёте."""
    return account.get("type") in TURNOVER_ACCOUNT_TYPES


class TinkoffReadOnlyClient:
    """Тонкий read-only REST-клиент. Использует только токен на чтение."""

    def __init__(self, token: str, timeout_seconds: int = _DEFAULT_TIMEOUT) -> None:
        if not token:
            raise ValueError("token must be a non-empty string")
        self._timeout = timeout_seconds
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ─── Транспорт ──────────────────────────────────────────────────────────

    def _post(self, service: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Единственная точка HTTP. Ретраи на 429/RESOURCE_EXHAUSTED."""
        url = f"{_BASE_URL}/{service}/{method}"
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._session.post(url, json=payload, timeout=self._timeout)
                if resp.status_code == 429:
                    wait = _RATE_LIMIT_SLEEP * attempt
                    logger.warning(f"Rate-limit на {method} (попытка {attempt}). Ждём {wait:.1f}с")
                    time.sleep(wait)
                    last_exc = requests.HTTPError("429 Too Many Requests")
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as exc:
                msg = str(exc).lower()
                if "resource_exhausted" in msg or "429" in msg:
                    wait = _RATE_LIMIT_SLEEP * attempt
                    logger.warning(f"Rate-limit на {method} (попытка {attempt}). Ждём {wait:.1f}с")
                    time.sleep(wait)
                    last_exc = exc
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    # ─── Счета ──────────────────────────────────────────────────────────────

    def get_accounts(self) -> list[dict[str, Any]]:
        """Все счета, доступные по токену (сырой список из поля 'accounts')."""
        resp = self._post(_USERS, "GetAccounts", {})
        return resp.get("accounts", []) or []

    def get_broker_accounts(self) -> list[dict[str, Any]]:
        """
        Счета, оборот по которым учитывается в квал-расчёте: обычный брокерский
        (ACCOUNT_TYPE_TINKOFF) и ИИС (ACCOUNT_TYPE_TINKOFF_IIS).
        """
        accounts = [acc for acc in self.get_accounts() if is_turnover_account(acc)]
        logger.info(f"Счетов, учитываемых в обороте: {len(accounts)}")
        return accounts

    # ─── Операции ───────────────────────────────────────────────────────────

    def iter_operations(
        self,
        account_id: str,
        from_iso: str,
        to_iso: str,
        operation_types: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """
        Итерирует выполненные операции по счёту за период с курсорной пагинацией
        (OperationsService/GetOperationsByCursor).

        Parameters
        ----------
        from_iso, to_iso : str
            Границы периода в RFC3339 (например '2025-04-01T00:00:00Z').
        operation_types : list[str], optional
            Фильтр типов на стороне API (строки 'OPERATION_TYPE_*').
        """
        cursor = ""
        page = 0
        while True:
            page += 1
            payload: dict[str, Any] = {
                "accountId": account_id,
                "from": from_iso,
                "to": to_iso,
                "state": OPERATION_STATE_EXECUTED,
                "limit": 1000,
                "withoutCommissions": True,
                "withoutTrades": False,    # trades нужны для точного оборота
                "withoutOvernights": True,
            }
            if cursor:
                payload["cursor"] = cursor
            if operation_types:
                payload["operationTypes"] = operation_types

            resp = self._post(_OPERATIONS, "GetOperationsByCursor", payload)
            items = resp.get("items", []) or []
            logger.debug(f"Счёт {account_id}: страница {page}, операций {len(items)}")
            yield from items

            if not resp.get("hasNext"):
                break
            cursor = resp.get("nextCursor", "") or ""
            if not cursor:
                break
            time.sleep(0.1)

    def get_operations(
        self,
        account_id: str,
        from_iso: str,
        to_iso: str,
        operation_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        ops = list(self.iter_operations(account_id, from_iso, to_iso, operation_types))
        logger.info(f"Счёт {account_id}: загружено {len(ops)} операций за {from_iso[:10]} … {to_iso[:10]}")
        return ops

    # ─── Портфель (для последующих этапов) ──────────────────────────────────

    def get_portfolio(self, account_id: str) -> dict[str, Any]:
        """RAW-портфель по счёту (нормализация — задача следующих этапов)."""
        if not account_id:
            raise ValueError("account_id must be a non-empty string")
        return self._post(_OPERATIONS, "GetPortfolio", {"accountId": account_id})

    # ─── Инструменты ────────────────────────────────────────────────────────

    def get_instrument_by(self, id_type: str, id_value: str) -> dict[str, Any]:
        """
        InstrumentsService/GetInstrumentBy — справочник по figi или uid.
        Возвращает сырой ответ {'instrument': {...}}.
        """
        return self._post(
            _INSTRUMENTS, "GetInstrumentBy",
            {"idType": id_type, "id": id_value},
        )
