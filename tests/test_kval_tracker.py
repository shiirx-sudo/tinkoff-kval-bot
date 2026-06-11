"""Тесты агрегирования прогресса (с фейковым API-клиентом)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from tinkoff.invest.schemas import AccountType, OperationType

from modules.kval_tracker import KvalTracker
from tests.conftest import FakeAccount, FakeOperation, FakeTrade, quotation


class FakeClient:
    """Заглушка ReadOnlyClient: возвращает заранее заданные данные."""

    def __init__(self, accounts, operations_by_account):
        self._accounts = accounts
        self._ops = operations_by_account

    def get_broker_accounts(self):
        return self._accounts

    def get_operations(self, account_id, from_dt, to_dt):
        return self._ops.get(account_id, [])


def _op(op_type, dt, trades):
    return FakeOperation(
        type=op_type,
        id=f"op-{dt.isoformat()}",
        date=dt,
        trades=trades,
    )


def test_aggregates_total_turnover():
    accounts = [
        FakeAccount("acc-1", "Основной", AccountType.ACCOUNT_TYPE_TINKOFF),
    ]
    ops = {
        "acc-1": [
            _op(
                OperationType.OPERATION_TYPE_BUY,
                datetime(2025, 5, 10, tzinfo=timezone.utc),
                [FakeTrade(quotation("100"), 10)],   # 1000
            ),
            _op(
                OperationType.OPERATION_TYPE_SELL,
                datetime(2026, 1, 20, tzinfo=timezone.utc),
                [FakeTrade(quotation("200"), 5)],    # 1000
            ),
        ]
    }
    tracker = KvalTracker(client=FakeClient(accounts, ops))
    progress = tracker.analyze(as_of=date(2026, 6, 11))

    assert progress.total_turnover == Decimal("2000.00")
    assert progress.total_operation_count == 2
    assert len(progress.accounts) == 1


def test_currency_op_excluded():
    accounts = [
        FakeAccount("acc-1", "Основной", AccountType.ACCOUNT_TYPE_TINKOFF),
    ]
    ops = {
        "acc-1": [
            _op(
                OperationType.OPERATION_TYPE_BUY,
                datetime(2025, 5, 10, tzinfo=timezone.utc),
                [FakeTrade(quotation("100"), 10)],
            ),
            _op(
                OperationType.OPERATION_TYPE_BUY_CURRENCY,  # должен отфильтроваться
                datetime(2025, 6, 10, tzinfo=timezone.utc),
                [FakeTrade(quotation("999"), 999)],
            ),
        ]
    }
    tracker = KvalTracker(client=FakeClient(accounts, ops))
    progress = tracker.analyze(as_of=date(2026, 6, 11))
    assert progress.total_turnover == Decimal("1000.00")
    assert progress.total_operation_count == 1


def test_quarter_bucketing():
    accounts = [
        FakeAccount("acc-1", "Основной", AccountType.ACCOUNT_TYPE_TINKOFF),
    ]
    ops = {
        "acc-1": [
            _op(  # 2025Q2
                OperationType.OPERATION_TYPE_BUY,
                datetime(2025, 5, 10, tzinfo=timezone.utc),
                [FakeTrade(quotation("100"), 10)],
            ),
            _op(  # 2026Q1
                OperationType.OPERATION_TYPE_BUY,
                datetime(2026, 2, 10, tzinfo=timezone.utc),
                [FakeTrade(quotation("100"), 20)],
            ),
        ]
    }
    tracker = KvalTracker(client=FakeClient(accounts, ops))
    progress = tracker.analyze(as_of=date(2026, 6, 11))
    acc = progress.accounts[0]
    assert acc.by_quarter["2025Q2"].turnover == Decimal("1000.00")
    assert acc.by_quarter["2026Q1"].turnover == Decimal("2000.00")
    assert acc.by_quarter["2025Q3"].turnover == Decimal("0")


def test_progress_metrics():
    accounts = [
        FakeAccount("acc-1", "Основной", AccountType.ACCOUNT_TYPE_TINKOFF),
    ]
    # Оборот ровно 3 000 000 при цели 6 000 000 → 50%
    ops = {
        "acc-1": [
            _op(
                OperationType.OPERATION_TYPE_BUY,
                datetime(2025, 5, 10, tzinfo=timezone.utc),
                [FakeTrade(quotation("1000000"), 3)],
            ),
        ]
    }
    tracker = KvalTracker(client=FakeClient(accounts, ops))
    progress = tracker.analyze(as_of=date(2026, 6, 11))
    assert progress.total_turnover == Decimal("3000000.00")
    assert progress.progress_pct == Decimal("50.00")
    assert progress.achieved is False
    assert progress.remaining_to_target == Decimal("3000000.00")


def test_no_accounts():
    tracker = KvalTracker(client=FakeClient([], {}))
    progress = tracker.analyze(as_of=date(2026, 6, 11))
    assert progress.total_turnover == Decimal("0")
    assert progress.accounts == []
