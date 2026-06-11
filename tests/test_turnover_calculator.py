"""Тесты подсчёта оборота по операциям."""
from __future__ import annotations

from decimal import Decimal

from tinkoff.invest.schemas import OperationType

from modules.turnover_calculator import calculate_operation_turnover


class TestTradeBased:
    def test_single_trade(self, make_operation, make_trade):
        op = make_operation(
            OperationType.OPERATION_TYPE_BUY,
            trades=[make_trade("100.50", 10)],
        )
        res = calculate_operation_turnover(op, "acc-1")
        assert res.is_approximate is False
        assert res.turnover_exact == Decimal("1005.00")
        assert res.direction == "BUY"
        assert res.trade_count == 1

    def test_multiple_trades_summed(self, make_operation, make_trade):
        op = make_operation(
            OperationType.OPERATION_TYPE_SELL,
            trades=[make_trade("100", 5), make_trade("200", 3)],
        )
        res = calculate_operation_turnover(op, "acc-1")
        # 100*5 + 200*3 = 500 + 600 = 1100
        assert res.turnover_exact == Decimal("1100.00")
        assert res.direction == "SELL"
        assert res.trade_count == 2

    def test_turnover_is_absolute(self, make_operation, make_trade):
        # Отрицательное количество не должно давать отрицательный оборот
        op = make_operation(
            OperationType.OPERATION_TYPE_SELL,
            trades=[make_trade("50", -4)],
        )
        res = calculate_operation_turnover(op, "acc-1")
        assert res.turnover_exact == Decimal("200.00")


class TestApproximateFallback:
    def test_no_trades_uses_payment(self, make_operation, make_quotation):
        op = make_operation(
            OperationType.OPERATION_TYPE_BUY,
            trades=[],
            payment=make_quotation("-1500.25"),
        )
        res = calculate_operation_turnover(op, "acc-1")
        assert res.is_approximate is True
        assert res.turnover_approximate == Decimal("1500.25")
        assert res.warning.startswith("[APPROXIMATE]")

    def test_approximate_record_created(self, make_operation, make_quotation):
        op = make_operation(
            OperationType.OPERATION_TYPE_BUY,
            trades=[],
            payment=make_quotation("-999.99"),
        )
        res = calculate_operation_turnover(op, "acc-1")
        assert len(res.trades) == 1
        assert res.trades[0].is_approximate is True
        assert res.trades[0].raw_payment == Decimal("-999.99")


class TestDirection:
    def test_buy_card_is_buy(self, make_operation, make_trade):
        op = make_operation(
            OperationType.OPERATION_TYPE_BUY_CARD,
            trades=[make_trade("10", 1)],
        )
        res = calculate_operation_turnover(op, "acc-1")
        assert res.direction == "BUY"
