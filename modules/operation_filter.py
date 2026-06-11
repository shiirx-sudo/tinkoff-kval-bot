"""
Фильтрация операций T-Invest API для расчёта квалификационного оборота.

Правила (Т-Инвестиции, 2026):
  УЧИТЫВАЕМ:
    - Покупки ценных бумаг  (BUY, BUY_CARD)
    - Продажи ценных бумаг  (SELL)
    - Операции с фьючерсами (учитываются как ЦБ в API)
    - Опционы               (аналогично)

  НЕ УЧИТЫВАЕМ:
    - Валюта (OPERATION_TYPE_BUY_CURRENCY, SELL_CURRENCY)
    - Драгоценные металлы
    - Комиссии, налоги
    - Дивиденды, купоны
    - Ввод/вывод средств
    - Операции РЕПО (Т-Банк исключает вторые сделки РЕПО)
    - Овернайты, маржинальные комиссии
"""
from __future__ import annotations

from tinkoff.invest.schemas import (  # type: ignore[import]
    OperationType,
    InstrumentType,
)

# Типы операций, которые УЧИТЫВАЕМ в обороте
QUALIFYING_OPERATION_TYPES: frozenset[OperationType] = frozenset({
    OperationType.OPERATION_TYPE_BUY,
    OperationType.OPERATION_TYPE_SELL,
    OperationType.OPERATION_TYPE_BUY_CARD,
})

# Типы операций, которые ЯВНО исключаем (для логирования)
EXCLUDED_OPERATION_TYPES: frozenset[OperationType] = frozenset({
    OperationType.OPERATION_TYPE_BUY_CURRENCY,
    OperationType.OPERATION_TYPE_SELL_CURRENCY,
    OperationType.OPERATION_TYPE_COUPON,
    OperationType.OPERATION_TYPE_DIVIDEND,
    OperationType.OPERATION_TYPE_TAX,
    OperationType.OPERATION_TYPE_BROKER_FEE,
    OperationType.OPERATION_TYPE_SERVICE_FEE,
    OperationType.OPERATION_TYPE_MARGIN_FEE,
    OperationType.OPERATION_TYPE_INPUT,
    OperationType.OPERATION_TYPE_OUTPUT,
    OperationType.OPERATION_TYPE_INPUT_SECURITIES,
    OperationType.OPERATION_TYPE_OUTPUT_SECURITIES,
    OperationType.OPERATION_TYPE_OVERNIGHT,
    OperationType.OPERATION_TYPE_BOND_REPAYMENT,
    OperationType.OPERATION_TYPE_BOND_REPAYMENT_FULL,
    OperationType.OPERATION_TYPE_TRACK_MFEE,
    OperationType.OPERATION_TYPE_TRACK_FFEE,
    OperationType.OPERATION_TYPE_BENEFIT_TAX,
    OperationType.OPERATION_TYPE_TAX_PROGRESSIVE,
    OperationType.OPERATION_TYPE_ACCRUING_VARMARGIN,
    OperationType.OPERATION_TYPE_WRITING_OFF_VARMARGIN,
    OperationType.OPERATION_TYPE_DELIVERY_BUY,
    OperationType.OPERATION_TYPE_DELIVERY_SELL,
})

# Типы инструментов, которые НЕ учитываем
EXCLUDED_INSTRUMENT_TYPES: frozenset[InstrumentType] = frozenset({
    InstrumentType.INSTRUMENT_TYPE_CURRENCY,
    InstrumentType.INSTRUMENT_TYPE_COMMODITY,  # драгметаллы
    InstrumentType.INSTRUMENT_TYPE_UNSPECIFIED,
})

# Ключевые слова в описании РЕПО-операций
REPO_KEYWORDS: tuple[str, ...] = (
    "репо",
    "repo",
    "обратный выкуп",
    "overnight repo",
)


def is_qualifying_operation(operation) -> bool:
    """
    Возвращает True, если операция должна учитываться в квалификационном обороте.

    Parameters
    ----------
    operation : OperationItem
        Операция из T-Invest API (GetOperationsByCursor).
    """
    # Тип операции должен быть в whitelist
    if operation.type not in QUALIFYING_OPERATION_TYPES:
        return False

    # Исключаем нежелательные типы инструментов
    if hasattr(operation, "instrument_type"):
        if operation.instrument_type in EXCLUDED_INSTRUMENT_TYPES:
            return False

    # Исключаем РЕПО по ключевым словам в описании
    description = getattr(operation, "description", "") or ""
    description_lower = description.lower()
    for keyword in REPO_KEYWORDS:
        if keyword in description_lower:
            return False

    return True


def classify_operation(operation) -> str:
    """Возвращает человекочитаемое описание типа операции."""
    type_labels = {
        OperationType.OPERATION_TYPE_BUY: "Покупка",
        OperationType.OPERATION_TYPE_SELL: "Продажа",
        OperationType.OPERATION_TYPE_BUY_CARD: "Покупка (карта)",
        OperationType.OPERATION_TYPE_BUY_CURRENCY: "Покупка валюты",
        OperationType.OPERATION_TYPE_SELL_CURRENCY: "Продажа валюты",
        OperationType.OPERATION_TYPE_BROKER_FEE: "Брокерская комиссия",
        OperationType.OPERATION_TYPE_DIVIDEND: "Дивиденд",
        OperationType.OPERATION_TYPE_COUPON: "Купон",
        OperationType.OPERATION_TYPE_TAX: "Налог",
        OperationType.OPERATION_TYPE_INPUT: "Ввод средств",
        OperationType.OPERATION_TYPE_OUTPUT: "Вывод средств",
        OperationType.OPERATION_TYPE_OVERNIGHT: "Овернайт",
    }
    return type_labels.get(operation.type, f"Прочее ({operation.type})")
