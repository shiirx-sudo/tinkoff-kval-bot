"""
Подсчёт оборота по операциям T-Invest API.

Правила:
  1. Если у операции есть trades → оборот = sum(abs(price * quantity))
  2. Если trades отсутствуют → оборот = abs(payment), помечаем как approximate
  3. Сделки считаем по количеству trades, не по количеству операций
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from tinkoff.invest.utils import quotation_to_decimal  # type: ignore[import]
from loguru import logger


@dataclass
class TradeRecord:
    """Запись об одной сделке (одном trade внутри операции)."""
    operation_id: str
    account_id: str
    date: str                   # ISO-строка для сериализации
    ticker: str
    figi: str
    direction: str              # BUY / SELL
    price: Decimal
    quantity: int
    turnover: Decimal           # abs(price * quantity)
    is_approximate: bool = False
    raw_payment: Decimal = Decimal("0")  # для сверки


@dataclass
class OperationTurnoverResult:
    """Результат обработки одной операции."""
    operation_id: str
    account_id: str
    figi: str
    ticker: str
    direction: str
    date: str
    trade_count: int            # количество фактических trades
    turnover_exact: Decimal     # trade-based (точный)
    turnover_approximate: Decimal  # payment-based (приближённый)
    is_approximate: bool        # True если использовался fallback
    discrepancy: Decimal        # abs(exact - approximate)
    trades: list[TradeRecord] = field(default_factory=list)
    warning: str = ""


def _to_decimal(quotation) -> Decimal:
    """Безопасное преобразование Quotation → Decimal."""
    if quotation is None:
        return Decimal("0")
    try:
        return quotation_to_decimal(quotation)
    except Exception:
        return Decimal("0")


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _get_ticker(operation) -> str:
    """Пытаемся извлечь тикер из операции."""
    # В разных версиях SDK поле может называться по-разному
    for attr in ("instrument_uid", "figi", "ticker"):
        val = getattr(operation, attr, "")
        if val:
            return str(val)
    return "UNKNOWN"


def calculate_operation_turnover(
    operation,
    account_id: str,
) -> OperationTurnoverResult:
    """
    Рассчитывает оборот по одной операции.

    Parameters
    ----------
    operation : OperationItem
        Объект операции из T-Invest API.
    account_id : str
        ID счёта (для маркировки записей).
    """
    op_id = str(getattr(operation, "id", ""))
    figi = str(getattr(operation, "figi", "") or "")
    ticker = _get_ticker(operation)
    op_date = getattr(operation, "date", None)
    date_str = op_date.isoformat() if op_date else ""

    # Направление операции
    from tinkoff.invest.schemas import OperationType  # type: ignore[import]
    op_type = getattr(operation, "type", None)
    direction = "BUY" if op_type in (
        OperationType.OPERATION_TYPE_BUY,
        OperationType.OPERATION_TYPE_BUY_CARD,
    ) else "SELL"

    # Payment-based (fallback)
    payment = _to_decimal(getattr(operation, "payment", None))
    turnover_approximate = abs(payment)

    # Trades-based (точный)
    raw_trades = getattr(operation, "trades", None) or []

    trade_records: list[TradeRecord] = []
    turnover_exact = Decimal("0")

    if raw_trades:
        for t in raw_trades:
            price = _to_decimal(getattr(t, "price", None))
            qty_raw = getattr(t, "quantity", 0)
            try:
                qty = int(qty_raw)
            except (TypeError, ValueError):
                qty = 0
            trade_turnover = _round(abs(price * qty))
            turnover_exact += trade_turnover

            trade_records.append(TradeRecord(
                operation_id=op_id,
                account_id=account_id,
                date=date_str,
                ticker=ticker,
                figi=figi,
                direction=direction,
                price=price,
                quantity=qty,
                turnover=trade_turnover,
                is_approximate=False,
                raw_payment=Decimal("0"),
            ))

    is_approximate = len(trade_records) == 0
    warning = ""

    if is_approximate:
        # Fallback: нет trades → используем payment
        warning = (
            f"[APPROXIMATE] Операция {op_id} не содержит trades. "
            f"Оборот = abs(payment) = {turnover_approximate} ₽. "
            f"Требуется сверка с брокерским отчётом."
        )
        logger.warning(warning)
        # Создаём одну синтетическую запись
        trade_records.append(TradeRecord(
            operation_id=op_id,
            account_id=account_id,
            date=date_str,
            ticker=ticker,
            figi=figi,
            direction=direction,
            price=Decimal("0"),
            quantity=0,
            turnover=turnover_approximate,
            is_approximate=True,
            raw_payment=payment,
        ))

    # Расхождение (только если есть оба значения)
    discrepancy = Decimal("0")
    if not is_approximate and turnover_approximate > 0:
        discrepancy = _round(abs(turnover_exact - turnover_approximate))

    return OperationTurnoverResult(
        operation_id=op_id,
        account_id=account_id,
        figi=figi,
        ticker=ticker,
        direction=direction,
        date=date_str,
        trade_count=len(trade_records),
        turnover_exact=_round(turnover_exact),
        turnover_approximate=_round(turnover_approximate),
        is_approximate=is_approximate,
        discrepancy=discrepancy,
        trades=trade_records,
        warning=warning,
    )
