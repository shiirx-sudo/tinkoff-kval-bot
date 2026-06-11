"""Тесты фильтрации операций для квалификационного оборота."""
from __future__ import annotations

from tinkoff.invest.schemas import InstrumentType, OperationType

from modules.operation_filter import (
    classify_operation,
    is_qualifying_operation,
)


class TestIsQualifying:
    def test_buy_is_qualifying(self, make_operation):
        op = make_operation(OperationType.OPERATION_TYPE_BUY)
        assert is_qualifying_operation(op) is True

    def test_sell_is_qualifying(self, make_operation):
        op = make_operation(OperationType.OPERATION_TYPE_SELL)
        assert is_qualifying_operation(op) is True

    def test_buy_card_is_qualifying(self, make_operation):
        op = make_operation(OperationType.OPERATION_TYPE_BUY_CARD)
        assert is_qualifying_operation(op) is True

    def test_currency_buy_excluded(self, make_operation):
        op = make_operation(OperationType.OPERATION_TYPE_BUY_CURRENCY)
        assert is_qualifying_operation(op) is False

    def test_dividend_excluded(self, make_operation):
        op = make_operation(OperationType.OPERATION_TYPE_DIVIDEND)
        assert is_qualifying_operation(op) is False

    def test_broker_fee_excluded(self, make_operation):
        op = make_operation(OperationType.OPERATION_TYPE_BROKER_FEE)
        assert is_qualifying_operation(op) is False

    def test_currency_instrument_excluded(self, make_operation):
        op = make_operation(
            OperationType.OPERATION_TYPE_BUY,
            instrument_type=InstrumentType.INSTRUMENT_TYPE_CURRENCY,
        )
        assert is_qualifying_operation(op) is False

    def test_repo_description_excluded(self, make_operation):
        op = make_operation(
            OperationType.OPERATION_TYPE_BUY,
            description="Сделка РЕПО overnight",
        )
        assert is_qualifying_operation(op) is False

    def test_repo_keyword_case_insensitive(self, make_operation):
        op = make_operation(
            OperationType.OPERATION_TYPE_SELL,
            description="REPO buyback leg 2",
        )
        assert is_qualifying_operation(op) is False

    def test_normal_share_not_excluded_by_description(self, make_operation):
        op = make_operation(
            OperationType.OPERATION_TYPE_BUY,
            description="Покупка акций SBER",
            instrument_type=InstrumentType.INSTRUMENT_TYPE_SHARE,
        )
        assert is_qualifying_operation(op) is True


class TestClassify:
    def test_buy_label(self, make_operation):
        op = make_operation(OperationType.OPERATION_TYPE_BUY)
        assert classify_operation(op) == "Покупка"

    def test_unknown_label(self, make_operation):
        # SERVICE_FEE существует в enum, но отсутствует в карте подписей
        op = make_operation(OperationType.OPERATION_TYPE_SERVICE_FEE)
        assert "Прочее" in classify_operation(op)
