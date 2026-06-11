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


def test_broker_plus_iis_both_counted():
    accounts = [
        make_account("acc-1", "Брокерский", "ACCOUNT_TYPE_TINKOFF"),
        make_account("acc-2", "ИИС", "ACCOUNT_TYPE_TINKOFF_IIS"),
    ]
    ops = {
        "acc-1": [_op("OPERATION_TYPE_BUY", "2025-05-10T10:00:00Z", [make_trade("100", 10)])],   # 1000
        "acc-2": [_op("OPERATION_TYPE_SELL", "2025-07-10T10:00:00Z", [make_trade("100", 30)])],  # 3000
    }
    p = KvalTracker(client=FakeClient(accounts, ops)).analyze(as_of=date(2026, 6, 11))
    assert p.total_turnover == Decimal("4000.00")  # оборот ИИС учтён наравне с брокерским
    assert len(p.accounts) == 2
    by_type = {a.account_id: a.account_type for a in p.accounts}
    assert by_type == {"acc-1": "broker", "acc-2": "iis"}


# ─── Помесячная / поквартальная готовность ──────────────────────────────────

from modules.kval_tracker import _period_months  # noqa: E402
from modules.period_calculator import calculate_kval_period  # noqa: E402

AS_OF = date(2026, 6, 11)


def _ops_for_months(labels, trades_per_month, price="200000", qty=1):
    ops = []
    for lbl in labels:
        ops.append(make_operation(
            "OPERATION_TYPE_BUY",
            id=f"op-{lbl}",
            date=f"{lbl}-15T10:00:00Z",
            trades=[make_trade(price, qty) for _ in range(trades_per_month)],
        ))
    return ops


def _period_month_labels():
    return _period_months(calculate_kval_period(AS_OF))


def test_zero_ops_all_fail():
    accounts = [make_account("acc-1", "Основной")]
    p = KvalTracker(client=FakeClient(accounts, {"acc-1": []})).analyze(as_of=AS_OF)
    assert all(not m.ok for m in p.months)
    assert all(not q.ok for q in p.quarter_checks)
    assert p.months_ok is False
    assert p.quarters_ok is False
    assert p.turnover_ok is False
    assert p.qualification_ready is False
    assert len(p.months) == 12


def test_turnover_ok_but_one_empty_month_not_ready():
    labels = _period_month_labels()
    # Пропускаем первый месяц периода → он пустой; обороту это не мешает.
    ops = _ops_for_months(labels[1:], trades_per_month=4)  # 11 месяцев × 4 сделки
    accounts = [make_account("acc-1", "Основной")]
    p = KvalTracker(client=FakeClient(accounts, {"acc-1": ops})).analyze(as_of=AS_OF)
    assert p.turnover_ok is True            # 11*4*200000 = 8.8M >= 6.1M
    assert p.months_ok is False             # первый месяц пустой
    assert p.qualification_ready is False
    empty = next(m for m in p.months if m.label == labels[0])
    assert empty.trade_count == 0


def test_full_eligibility_ready():
    labels = _period_month_labels()
    ops = _ops_for_months(labels, trades_per_month=4)  # 12 мес × 4 = квартал 12 сделок
    accounts = [make_account("acc-1", "Основной")]
    p = KvalTracker(client=FakeClient(accounts, {"acc-1": ops})).analyze(as_of=AS_OF)
    assert p.turnover_ok is True            # 12*4*200000 = 9.6M >= 6.1M
    assert p.months_ok is True              # каждый месяц >= 1
    assert p.quarters_ok is True            # каждый квартал >= 10 (по 12)
    assert p.qualification_ready is True
    assert all(q.trade_count >= 10 for q in p.quarter_checks)


def test_twelve_ops_one_quarter_only_that_quarter_ok():
    from tests.conftest import quotation
    # 12 приближённых операций (без trades) в одном квартале 2025Q3 (август).
    ops = [
        make_operation("OPERATION_TYPE_BUY", id=f"ap-{i}",
                       date="2025-08-15T10:00:00Z", trades=[],
                       payment=quotation("-1500"))
        for i in range(12)
    ]
    accounts = [make_account("acc-1", "Основной")]
    p = KvalTracker(client=FakeClient(accounts, {"acc-1": ops})).analyze(as_of=AS_OF)

    by_q = {q.label: q for q in p.quarter_checks}
    assert by_q["2025Q3"].trade_count == 12      # приближённые считаются как сделки
    assert by_q["2025Q3"].ok is True             # >= 10
    assert all(q.ok is False for lbl, q in by_q.items() if lbl != "2025Q3")
    assert p.quarters_ok is False                # остальные кварталы пустые
    assert p.total_approximate_trade_count == 12
    assert p.total_exact_trade_count == 0
    assert p.qualification_ready is False
