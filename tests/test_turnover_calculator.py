"""Тесты подсчёта оборота (REST-контракт)."""
from __future__ import annotations

from decimal import Decimal

from modules.turnover_calculator import calculate_operation_turnover


def test_single_trade(operation, trade):
    op = operation("OPERATION_TYPE_BUY", trades=[trade("100.50", 10)])
    res = calculate_operation_turnover(op, "acc-1")
    assert res.is_approximate is False
    assert res.turnover_exact == Decimal("1005.00")
    assert res.direction == "BUY"
    assert res.trade_count == 1


def test_multiple_trades_summed(operation, trade):
    op = operation("OPERATION_TYPE_SELL", trades=[trade("100", 5), trade("200", 3)])
    res = calculate_operation_turnover(op, "acc-1")
    assert res.turnover_exact == Decimal("1100.00")
    assert res.direction == "SELL"
    assert res.trade_count == 2


def test_turnover_absolute(operation, trade):
    op = operation("OPERATION_TYPE_SELL", trades=[trade("50", -4)])
    res = calculate_operation_turnover(op, "acc-1")
    assert res.turnover_exact == Decimal("200.00")


def test_no_trades_uses_payment(operation, q):
    op = operation("OPERATION_TYPE_BUY", trades=[], payment=q("-1500.25"))
    res = calculate_operation_turnover(op, "acc-1")
    assert res.is_approximate is True
    assert res.turnover_approximate == Decimal("1500.25")
    assert res.warning.startswith("[APPROXIMATE]")


def test_approximate_record(operation, q):
    op = operation("OPERATION_TYPE_BUY", trades=[], payment=q("-999.99"))
    res = calculate_operation_turnover(op, "acc-1")
    assert len(res.trades) == 1
    assert res.trades[0].is_approximate is True
    assert res.trades[0].raw_payment == Decimal("-999.99")


def test_buy_card_direction(operation, trade):
    op = operation("OPERATION_TYPE_BUY_CARD", trades=[trade("10", 1)])
    res = calculate_operation_turnover(op, "acc-1")
    assert res.direction == "BUY"
