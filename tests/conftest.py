"""
Общие фикстуры и фейковые объекты для тестов.

Важно: переменная окружения с токеном выставляется ДО импорта config.settings,
чтобы Settings() не падал на отсутствии .env в CI.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

# Должно быть выставлено до любого импорта config.settings
os.environ.setdefault("TINKOFF_READ_TOKEN", "test-token-readonly")
os.environ.setdefault("LIVE_ENABLED", "false")


def quotation(value: Decimal | str | float) -> SimpleNamespace:
    """
    Фейковый Quotation: units + nano.
    quotation_to_decimal читает .units и .nano.
    """
    d = Decimal(str(value))
    units = int(d)
    nano = int((d - units) * Decimal("1000000000"))
    return SimpleNamespace(units=units, nano=nano)


@dataclass
class FakeTrade:
    price: object
    quantity: int


@dataclass
class FakeOperation:
    """Минимальный фейк OperationItem из T-Invest API."""
    type: object
    id: str = "op-1"
    figi: str = "BBG000000001"
    instrument_uid: str = "uid-1"
    instrument_type: object = None
    description: str = ""
    date: datetime = field(
        default_factory=lambda: datetime(2025, 6, 1, tzinfo=timezone.utc)
    )
    payment: object = field(default_factory=lambda: quotation("0"))
    trades: list = field(default_factory=list)


@dataclass
class FakeAccount:
    id: str
    name: str
    type: object


@pytest.fixture
def make_quotation():
    return quotation


@pytest.fixture
def make_trade():
    def _factory(price, quantity):
        return FakeTrade(price=quotation(price), quantity=quantity)
    return _factory


@pytest.fixture
def make_operation():
    def _factory(op_type, **kwargs):
        return FakeOperation(type=op_type, **kwargs)
    return _factory
