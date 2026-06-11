"""
Фильтрация операций T-Invest для квалификационного оборота.

Контракт: операция — JSON-словарь из GetOperationsByCursor (REST), ключи camelCase.
Тип операции — строка 'OPERATION_TYPE_*', тип инструмента — строка (lowercase).

Правила (Т-Инвестиции, 2026):
  УЧИТЫВАЕМ: покупки/продажи ЦБ (BUY, SELL, BUY_CARD), фьючерсы, опционы.
  НЕ УЧИТЫВАЕМ: валюту, драгметаллы, комиссии, налоги, дивиденды, купоны,
                ввод/вывод, РЕПО, овернайты, маржинальные комиссии.
"""
from __future__ import annotations

from typing import Any

# Типы операций, которые УЧИТЫВАЕМ
QUALIFYING_OPERATION_TYPES: frozenset[str] = frozenset({
    "OPERATION_TYPE_BUY",
    "OPERATION_TYPE_SELL",
    "OPERATION_TYPE_BUY_CARD",
})

# Типы инструментов, которые НЕ учитываем (строки REST, lowercase)
EXCLUDED_INSTRUMENT_TYPES: frozenset[str] = frozenset({
    "currency",
    "commodity",   # драгметаллы
    "",
})

# Ключевые слова РЕПО в описании
REPO_KEYWORDS: tuple[str, ...] = (
    "репо", "repo", "обратный выкуп", "overnight repo",
)

_BUY_TYPES = frozenset({"OPERATION_TYPE_BUY", "OPERATION_TYPE_BUY_CARD"})

_TYPE_LABELS = {
    "OPERATION_TYPE_BUY": "Покупка",
    "OPERATION_TYPE_SELL": "Продажа",
    "OPERATION_TYPE_BUY_CARD": "Покупка (карта)",
    "OPERATION_TYPE_BUY_CURRENCY": "Покупка валюты",
    "OPERATION_TYPE_SELL_CURRENCY": "Продажа валюты",
    "OPERATION_TYPE_BROKER_FEE": "Брокерская комиссия",
    "OPERATION_TYPE_DIVIDEND": "Дивиденд",
    "OPERATION_TYPE_COUPON": "Купон",
    "OPERATION_TYPE_TAX": "Налог",
    "OPERATION_TYPE_INPUT": "Ввод средств",
    "OPERATION_TYPE_OUTPUT": "Вывод средств",
    "OPERATION_TYPE_OVERNIGHT": "Овернайт",
}


def operation_type(operation: dict[str, Any]) -> str:
    """Извлекает строковый тип операции (терпимо к именованию ключа)."""
    return str(operation.get("operationType") or operation.get("type") or "")


def instrument_type(operation: dict[str, Any]) -> str:
    return str(operation.get("instrumentType") or "").lower()


def is_buy(operation: dict[str, Any]) -> bool:
    return operation_type(operation) in _BUY_TYPES


def is_qualifying_operation(operation: dict[str, Any]) -> bool:
    """True, если операция учитывается в квалификационном обороте."""
    if operation_type(operation) not in QUALIFYING_OPERATION_TYPES:
        return False

    if instrument_type(operation) in EXCLUDED_INSTRUMENT_TYPES:
        return False

    description = str(operation.get("description") or operation.get("name") or "").lower()
    if any(kw in description for kw in REPO_KEYWORDS):
        return False

    return True


def classify_operation(operation: dict[str, Any]) -> str:
    op_type = operation_type(operation)
    return _TYPE_LABELS.get(op_type, f"Прочее ({op_type})")
