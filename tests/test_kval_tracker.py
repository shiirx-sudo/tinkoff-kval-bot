"""Тесты агрегатора прогресса (с фейковым REST-фасадом)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from modules.kval_tracker import KvalTracker
from tests.conftest import make_account, make_operation, make_trade


class FakeClient:
    def __init__(self, accounts, ops_by_account):
        self._accounts = accounts
        self._ops = ops_by_account

    def get_broker_accounts(self):
        return self._accounts

    def get_operations(self, account_id, from_dt, to_dt):
        return self._ops.get(account_id, [])


def _op(op_type, dt_iso, trades):
    return make_operation(op_type, id=f"op-{dt_iso}", date=dt_iso, trades=trades)


def test_total_turnover():
    accounts = [make_account("acc-1", "Основной")]
    ops = {"acc-1": [
        _op("OPERATION_TYPE_BUY", "2025-05-10T10:00:00Z", [make_trade("100", 10)]),
        _op("OPERATION_TYPE_SELL", "2026-01-20T10:00:00Z", [make_trade("200", 5)]),
    ]}
    p = KvalTracker(client=FakeClient(accounts, ops)).analyze(as_of=date(2026, 6, 11))
    assert p.total_turnover == Decimal("2000.00")
    assert p.total_operation_count == 2
    assert len(p.accounts) == 1


def test_currency_excluded():
    accounts = [make_account("acc-1", "Основной")]
    ops = {"acc-1": [
        _op("OPERATION_TYPE_BUY", "2025-05-10T10:00:00Z", [make_trade("100", 10)]),
        _op("OPERATION_TYPE_BUY_CURRENCY", "2025-06-10T10:00:00Z", [make_trade("999", 999)]),
    ]}
    p = KvalTracker(client=FakeClient(accounts, ops)).analyze(as_of=date(2026, 6, 11))
    assert p.total_turnover == Decimal("1000.00")
    assert p.total_operation_count == 1


def test_quarter_bucketing():
    accounts = [make_account("acc-1", "Основной")]
    ops = {"acc-1": [
        _op("OPERATION_TYPE_BUY", "2025-05-10T10:00:00Z", [make_trade("100", 10)]),   # 2025Q2
        _op("OPERATION_TYPE_BUY", "2026-02-10T10:00:00Z", [make_trade("100", 20)]),   # 2026Q1
    ]}
    p = KvalTracker(client=FakeClient(accounts, ops)).analyze(as_of=date(2026, 6, 11))
    acc = p.accounts[0]
    assert acc.by_quarter["2025Q2"].turnover == Decimal("1000.00")
    assert acc.by_quarter["2026Q1"].turnover == Decimal("2000.00")
    assert acc.by_quarter["2025Q3"].turnover == Decimal("0")


def test_progress_metrics():
    accounts = [make_account("acc-1", "Основной")]
    ops = {"acc-1": [
        _op("OPERATION_TYPE_BUY", "2025-05-10T10:00:00Z", [make_trade("1000000", 3)]),
    ]}
    p = KvalTracker(client=FakeClient(accounts, ops)).analyze(as_of=date(2026, 6, 11))
    assert p.total_turnover == Decimal("3000000.00")
    assert p.progress_pct == Decimal("50.00")
    assert p.achieved is False
    assert p.remaining_to_target == Decimal("3000000.00")


def test_no_accounts():
    p = KvalTracker(client=FakeClient([], {})).analyze(as_of=date(2026, 6, 11))
    assert p.total_turnover == Decimal("0")
    assert p.accounts == []
