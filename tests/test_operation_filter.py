"""Тесты фильтрации операций (REST-контракт, словари)."""
from __future__ import annotations

from modules.operation_filter import classify_operation, is_qualifying_operation


def test_buy_is_qualifying(operation):
    assert is_qualifying_operation(operation("OPERATION_TYPE_BUY")) is True


def test_sell_is_qualifying(operation):
    assert is_qualifying_operation(operation("OPERATION_TYPE_SELL")) is True


def test_buy_card_is_qualifying(operation):
    assert is_qualifying_operation(operation("OPERATION_TYPE_BUY_CARD")) is True


def test_currency_buy_excluded(operation):
    assert is_qualifying_operation(operation("OPERATION_TYPE_BUY_CURRENCY")) is False


def test_dividend_excluded(operation):
    assert is_qualifying_operation(operation("OPERATION_TYPE_DIVIDEND")) is False


def test_broker_fee_excluded(operation):
    assert is_qualifying_operation(operation("OPERATION_TYPE_BROKER_FEE")) is False


def test_currency_instrument_excluded(operation):
    op = operation("OPERATION_TYPE_BUY", instrument_type="currency")
    assert is_qualifying_operation(op) is False


def test_commodity_instrument_excluded(operation):
    op = operation("OPERATION_TYPE_BUY", instrument_type="commodity")
    assert is_qualifying_operation(op) is False


def test_repo_description_excluded(operation):
    op = operation("OPERATION_TYPE_BUY", description="Сделка РЕПО overnight")
    assert is_qualifying_operation(op) is False


def test_repo_case_insensitive(operation):
    op = operation("OPERATION_TYPE_SELL", description="REPO buyback leg 2")
    assert is_qualifying_operation(op) is False


def test_normal_share_qualifies(operation):
    op = operation("OPERATION_TYPE_BUY", description="Покупка акций SBER", instrument_type="share")
    assert is_qualifying_operation(op) is True


def test_classify_buy(operation):
    assert classify_operation(operation("OPERATION_TYPE_BUY")) == "Покупка"


def test_classify_unknown(operation):
    assert "Прочее" in classify_operation(operation("OPERATION_TYPE_SERVICE_FEE"))
