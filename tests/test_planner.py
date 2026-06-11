"""Тесты планировщика выхода на квал-статус."""
from __future__ import annotations

import json
from datetime import date

from modules.kval_planner import KvalPlanner
from tests.conftest import quotation


def _op(date_str, n_trades=1, price="100", qty="1", op_id="o"):
    return {
        "id": op_id, "operationType": "OPERATION_TYPE_BUY",
        "figi": "BBG1", "instrumentUid": "uid", "instrumentType": "share",
        "date": date_str,
        "tradesInfo": {"trades": [
            {"num": f"{op_id}-{i}", "date": date_str, "quantity": qty,
             "price": quotation(price)}
            for i in range(n_trades)
        ]},
    }


class FakeClient:
    def __init__(self, ops):
        self._ops = ops

    def get_broker_accounts(self):
        return [{"id": "acc-1", "name": "A", "type": "ACCOUNT_TYPE_TINKOFF"}]

    def get_operations(self, account_id, from_dt, to_dt):
        return self._ops


def _window0_months():
    # 2025-07 … 2026-06
    labels = []
    y, m = 2025, 7
    for _ in range(12):
        labels.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return labels


AS_OF = date(2026, 7, 1)


def test_current_window_not_ready():
    ops = [_op("2026-05-15T10:00:00Z", op_id="m5"),
           _op("2026-06-15T10:00:00Z", op_id="m6")]
    plan = KvalPlanner(client=FakeClient(ops)).plan(as_of=AS_OF)
    w0 = plan.windows[0]
    assert w0.included_quarters[0] == "2025Q3"
    assert w0.included_quarters[-1] == "2026Q2"
    assert w0.qualification_ready is False


def test_windows_with_2026q2_impossible_due_to_empty_april():
    ops = [_op("2026-05-15T10:00:00Z"), _op("2026-06-15T10:00:00Z")]
    plan = KvalPlanner(client=FakeClient(ops)).plan(as_of=AS_OF)
    affected = [w for w in plan.windows if "2026Q2" in w.included_quarters]
    assert affected  # такие окна есть
    assert all(w.impossible_due_to_past_gaps for w in affected)


def test_earliest_possible_check_date_is_2027_07_01():
    ops = [_op("2026-05-15T10:00:00Z"), _op("2026-06-15T10:00:00Z")]
    plan = KvalPlanner(client=FakeClient(ops)).plan(as_of=AS_OF, horizon_quarters=8)
    assert plan.earliest is not None
    assert plan.earliest.check_date == date(2027, 7, 1)
    assert plan.earliest.period_start == date(2026, 7, 1)
    assert plan.earliest.period_end == date(2027, 6, 30)


def test_turnover_ok_but_empty_month_not_ready():
    # 11 месяцев из 12 с крупным оборотом, 2025-07 пуст
    months = _window0_months()[1:]
    ops = [_op(f"{m}-15T10:00:00Z", price="600000", op_id=m) for m in months]
    plan = KvalPlanner(client=FakeClient(ops)).plan(as_of=AS_OF)
    w0 = plan.windows[0]
    assert w0.turnover_ok is True
    assert w0.months_ok is False
    assert w0.qualification_ready is False


def test_all_ok_ready():
    months = _window0_months()
    # 4 сделки/месяц → квартал 12 (>=10); оборот 12*4*200000 = 9.6M >= 6.1M
    ops = [_op(f"{m}-15T10:00:00Z", n_trades=4, price="200000", op_id=m)
           for m in months]
    plan = KvalPlanner(client=FakeClient(ops)).plan(as_of=AS_OF)
    w0 = plan.windows[0]
    assert w0.turnover_ok is True
    assert w0.months_ok is True
    assert w0.quarters_ok is True
    assert w0.qualification_ready is True
    assert plan.earliest is not None and plan.earliest.index == 0


def test_plan_reports_written_with_expected_columns(tmp_path):
    from reports import kval_plan_reports
    ops = [_op("2026-05-15T10:00:00Z"), _op("2026-06-15T10:00:00Z")]
    plan = KvalPlanner(client=FakeClient(ops)).plan(as_of=AS_OF)
    written = kval_plan_reports.write_all(plan, tmp_path)
    assert set(written) >= {
        "kval_plan.json", "kval_candidate_windows.csv",
        "kval_plan_months.csv", "kval_plan_quarters.csv",
    }

    wh = (tmp_path / "kval_candidate_windows.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    for col in ("check_date", "period_start", "period_end", "total_turnover",
                "qualification_ready", "impossible_due_to_past_gaps"):
        assert col in wh

    mh = (tmp_path / "kval_plan_months.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    assert mh == ["month", "status", "current_trade_count",
                  "planned_required_trade_count", "missing_trade_count",
                  "current_turnover", "suggested_turnover"]

    qh = (tmp_path / "kval_plan_quarters.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    assert "status" in qh and "suggested_turnover" in qh

    data = json.loads((tmp_path / "kval_plan.json").read_text(encoding="utf-8"))
    for key in ("generated_at_utc", "as_of", "target", "effective_target",
                "earliest_possible_check_date", "earliest_possible_period",
                "candidate_windows", "monthly_plan", "quarterly_plan"):
        assert key in data
    assert data["earliest_possible_check_date"] == "2027-07-01"


# ─── Распределение минимума сделок по месяцам внутри квартала ────────────────

def test_empty_future_quarter_requires_4_3_3():
    plan = KvalPlanner(client=FakeClient([])).plan(as_of=AS_OF)
    assert plan.earliest is not None
    mp = {m.month: m for m in plan.monthly_plan}
    # ближайшее окно начинается с 2026Q3 → июль/август/сентябрь 2026
    req = [mp[f"2026-0{d}"].planned_required_trade_count for d in (7, 8, 9)]
    assert req == [4, 3, 3]
    assert sum(req) >= 10
    assert all(r >= 1 for r in req)


def test_prefilled_first_month_redistributes_to_remaining():
    ops = [_op("2026-07-15T10:00:00Z", n_trades=5, op_id="jul")]
    plan = KvalPlanner(client=FakeClient(ops)).plan(as_of=AS_OF)
    mp = {m.month: m for m in plan.monthly_plan}
    assert mp["2026-07"].current_trade_count == 5
    # остальные месяцы квартала требуют минимум по 1
    assert mp["2026-08"].planned_required_trade_count >= 1
    assert mp["2026-09"].planned_required_trade_count >= 1
    # квартал нацелен минимум на 10 сделок
    total = sum(mp[f"2026-0{d}"].planned_required_trade_count for d in (7, 8, 9))
    assert total >= 10
    # уже сделанные 5 не планируются повторно
    assert mp["2026-07"].missing_trade_count == 0


def test_no_retroactive_planning_for_locked_month():
    # as_of в середине августа: июль уже закрыт, но в нём есть сделка
    ops = [_op("2026-07-15T10:00:00Z", n_trades=1, op_id="jul")]
    plan = KvalPlanner(client=FakeClient(ops)).plan(as_of=date(2026, 8, 15))
    assert plan.earliest is not None
    mp = {m.month: m for m in plan.monthly_plan}
    jul = mp["2026-07"]
    assert jul.status == "done_ok"                       # закрыт и со сделкой
    assert jul.planned_required_trade_count == jul.current_trade_count
    assert jul.missing_trade_count == 0
    assert jul.suggested_turnover == 0                   # задним числом не планируем
