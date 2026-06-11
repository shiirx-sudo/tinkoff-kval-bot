"""
Подсчёт оборота по операциям (REST-контракт) + резолв инструмента.

Правила:
  1. Есть trades → оборот = sum(abs(price * quantity)) по сделкам (точный).
  2. Нет trades → оборот = abs(payment), помечаем как approximate (нужна сверка).
  3. Сделки считаем по количеству trades.

instrument_uid (UUID) НИКОГДА не попадает в ticker. ticker — это настоящий тикер
из резолвера, либо пустая строка, если резолв не удался.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from loguru import logger

from common.helpers import as_decimal, quotation_to_decimal
from modules.operation_filter import is_buy


@dataclass
class TradeRecord:
    operation_id: str
    account_id: str
    date: str
    instrument_uid: str
    ticker: str
    instrument_name: str
    figi: str
    instrument_type: str
    direction: str            # BUY / SELL
    price: Decimal
    quantity: int
    turnover: Decimal
    is_approximate: bool = False
    raw_payment: Decimal = Decimal("0")
    trade_id: str = ""        # num из tradesInfo.trades


@dataclass
class OperationTurnoverResult:
    operation_id: str
    account_id: str
    figi: str
    instrument_uid: str
    ticker: str
    instrument_name: str
    instrument_type: str
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


def _instrument_uid(operation: dict[str, Any]) -> str:
    return str(operation.get("instrumentUid") or operation.get("instrument_uid") or "")


def _extract_trades(operation: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Источник сделок: сначала operation['trades'], затем
    operation['tradesInfo']['trades'] (фактический формат GetOperationsByCursor).
    """
    trades = operation.get("trades")
    if trades:
        return trades
    info = operation.get("tradesInfo") or {}
    return info.get("trades") or []


def _resolve(resolver, figi: str, instrument_uid: str, op: dict[str, Any]):
    """Безопасный резолв: при любой ошибке возвращает пустые ticker/name."""
    ticker, name = "", ""
    instr_type = str(op.get("instrumentType") or "")
    if resolver is not None:
        try:
            info = resolver.resolve(figi=figi, instrument_uid=instrument_uid)
            ticker = info.ticker or ""
            name = info.name or ""
            instr_type = info.instrument_type or instr_type
        except Exception as exc:  # noqa: BLE001 — резолв не ломает расчёт
            logger.warning(f"Резолв инструмента не удался (op {op.get('id')}): {exc}")
    return ticker, name, instr_type


def calculate_operation_turnover(
    operation: dict[str, Any],
    account_id: str,
    resolver=None,
) -> OperationTurnoverResult:
    """Оборот по одной операции (JSON-словарь GetOperationsByCursor)."""
    op_id = str(operation.get("id") or "")
    figi = str(operation.get("figi") or "")
    instrument_uid = _instrument_uid(operation)
    date_str = str(operation.get("date") or "")
    direction = "BUY" if is_buy(operation) else "SELL"

    ticker, name, instr_type = _resolve(resolver, figi, instrument_uid, operation)

    def _mk(price, qty, turnover, is_approx, raw_payment=Decimal("0"),
            trade_date=None, trade_id="") -> TradeRecord:
        return TradeRecord(
            operation_id=op_id, account_id=account_id,
            date=trade_date or date_str,
            instrument_uid=instrument_uid, ticker=ticker, instrument_name=name,
            figi=figi, instrument_type=instr_type, direction=direction,
            price=price, quantity=qty, turnover=turnover,
            is_approximate=is_approx, raw_payment=raw_payment, trade_id=trade_id,
        )

    payment = quotation_to_decimal(operation.get("payment"))
    turnover_approximate = abs(payment)

    raw_trades = _extract_trades(operation)
    trade_records: list[TradeRecord] = []
    turnover_exact = Decimal("0")

    for t in raw_trades:
        price = quotation_to_decimal(t.get("price"))
        qty_dec = as_decimal(t.get("quantity", 0))
        trade_turnover = _round(abs(price * qty_dec))
        turnover_exact += trade_turnover
        trade_records.append(_mk(
            price, int(qty_dec), trade_turnover, False,
            trade_date=str(t.get("date") or "") or date_str,
            trade_id=str(t.get("num") or ""),
        ))

    is_approximate = len(trade_records) == 0
    warning = ""

    if is_approximate:
        label = ticker or name or instrument_uid[:8] or op_id
        warning = (
            f"[APPROXIMATE] op {op_id} ({label}"
            f"{' ' + name if name and label != name else ''}): нет trades, "
            f"оборот = abs(payment) = {turnover_approximate} ₽. Требуется сверка."
        )
        logger.warning(warning)
        trade_records.append(_mk(Decimal("0"), 0, turnover_approximate, True, payment))

    discrepancy = Decimal("0")
    if not is_approximate and turnover_approximate > 0:
        discrepancy = _round(abs(turnover_exact - turnover_approximate))

    return OperationTurnoverResult(
        operation_id=op_id, account_id=account_id, figi=figi,
        instrument_uid=instrument_uid, ticker=ticker, instrument_name=name,
        instrument_type=instr_type, direction=direction, date=date_str,
        trade_count=len(trade_records),
        turnover_exact=_round(turnover_exact),
        turnover_approximate=_round(turnover_approximate),
        is_approximate=is_approximate, discrepancy=discrepancy,
        trades=trade_records, warning=warning,
    )
