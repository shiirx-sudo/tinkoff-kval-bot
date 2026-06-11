"""
Подсчёт оборота по операциям (REST-контракт).

Правила:
  1. Есть trades → оборот = sum(abs(price * quantity)) по сделкам (точный).
  2. Нет trades → оборот = abs(payment), помечаем как approximate (нужна сверка).
  3. Сделки считаем по количеству trades.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from loguru import logger

from common.helpers import quotation_to_decimal
from modules.operation_filter import is_buy


@dataclass
class TradeRecord:
    operation_id: str
    account_id: str
    date: str
    ticker: str
    figi: str
    direction: str            # BUY / SELL
    price: Decimal
    quantity: int
    turnover: Decimal
    is_approximate: bool = False
    raw_payment: Decimal = Decimal("0")


@dataclass
class OperationTurnoverResult:
    operation_id: str
    account_id: str
    figi: str
    ticker: str
    direction: str
    date: str
    trade_count: int
    turnover_exact: Decimal
    turnover_approximate: Decimal
    is_approximate: bool
    discrepancy: Decimal
    trades: list[TradeRecord] = field(default_factory=list)
    warning: str = ""


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _ticker(operation: dict[str, Any]) -> str:
    for key in ("instrumentUid", "figi", "ticker"):
        val = operation.get(key)
        if val:
            return str(val)
    return "UNKNOWN"


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def calculate_operation_turnover(
    operation: dict[str, Any],
    account_id: str,
) -> OperationTurnoverResult:
    """Оборот по одной операции (JSON-словарь GetOperationsByCursor)."""
    op_id = str(operation.get("id") or "")
    figi = str(operation.get("figi") or "")
    ticker = _ticker(operation)
    date_str = str(operation.get("date") or "")
    direction = "BUY" if is_buy(operation) else "SELL"

    payment = quotation_to_decimal(operation.get("payment"))
    turnover_approximate = abs(payment)

    raw_trades = operation.get("trades") or []
    trade_records: list[TradeRecord] = []
    turnover_exact = Decimal("0")

    for t in raw_trades:
        price = quotation_to_decimal(t.get("price"))
        qty = _int(t.get("quantity", 0))
        trade_turnover = _round(abs(price * qty))
        turnover_exact += trade_turnover
        trade_records.append(TradeRecord(
            operation_id=op_id, account_id=account_id, date=date_str,
            ticker=ticker, figi=figi, direction=direction,
            price=price, quantity=qty, turnover=trade_turnover,
            is_approximate=False, raw_payment=Decimal("0"),
        ))

    is_approximate = len(trade_records) == 0
    warning = ""

    if is_approximate:
        warning = (
            f"[APPROXIMATE] Операция {op_id} не содержит trades. "
            f"Оборот = abs(payment) = {turnover_approximate} ₽. Требуется сверка."
        )
        logger.warning(warning)
        trade_records.append(TradeRecord(
            operation_id=op_id, account_id=account_id, date=date_str,
            ticker=ticker, figi=figi, direction=direction,
            price=Decimal("0"), quantity=0, turnover=turnover_approximate,
            is_approximate=True, raw_payment=payment,
        ))

    discrepancy = Decimal("0")
    if not is_approximate and turnover_approximate > 0:
        discrepancy = _round(abs(turnover_exact - turnover_approximate))

    return OperationTurnoverResult(
        operation_id=op_id, account_id=account_id, figi=figi, ticker=ticker,
        direction=direction, date=date_str, trade_count=len(trade_records),
        turnover_exact=_round(turnover_exact),
        turnover_approximate=_round(turnover_approximate),
        is_approximate=is_approximate, discrepancy=discrepancy,
        trades=trade_records, warning=warning,
    )
