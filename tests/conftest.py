"""
Общие фикстуры. Токен выставляется ДО импорта config.settings.
Операции — JSON-словари в формате GetOperationsByCursor (REST).
"""
from __future__ import annotations

import os

os.environ.setdefault("TINKOFF_READ_TOKEN", "test-token-readonly")
os.environ.setdefault("LIVE_ENABLED", "false")

import pytest


def quotation(value) -> dict:
    """Decimal/str/number → Tinkoff Quotation {units, nano} (строками, как REST)."""
    from decimal import Decimal
    d = Decimal(str(value))
    units = int(d)
    nano = int((d - units) * Decimal("1000000000"))
    return {"units": str(units), "nano": nano}


def make_trade(price, quantity) -> dict:
    return {"price": quotation(price), "quantity": str(quantity)}


def make_operation(op_type, **kwargs) -> dict:
    op = {
        "id": kwargs.get("id", "op-1"),
        "operationType": op_type,
        "figi": kwargs.get("figi", "BBG000000001"),
        "instrumentUid": kwargs.get("instrument_uid", "uid-1"),
        "instrumentType": kwargs.get("instrument_type", "share"),
        "description": kwargs.get("description", ""),
        "date": kwargs.get("date", "2025-06-01T10:00:00Z"),
        "payment": kwargs.get("payment", quotation("0")),
        "trades": kwargs.get("trades", []),
    }
    return op


def make_account(acc_id, name, acc_type="ACCOUNT_TYPE_TINKOFF") -> dict:
    return {"id": acc_id, "name": name, "type": acc_type, "status": "OPEN"}


@pytest.fixture
def q():
    return quotation


@pytest.fixture
def trade():
    return make_trade


@pytest.fixture
def operation():
    return make_operation


@pytest.fixture
def account():
    return make_account
