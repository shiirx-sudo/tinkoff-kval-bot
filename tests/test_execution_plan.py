"""Тесты Execution Planner (DRY-RUN). Никаких реальных заявок."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from modules.execution_planner import ExecutionPlanError, build

AS_OF = date(2026, 7, 1)
NORMAL = "SECURITY_TRADING_STATUS_NORMAL_TRADING"
NOT_TRADING = "SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING"


def _write_plan(tmp, missing=4, current_turnover="0", current_trades=0,
                suggested="508333.33"):
    months = [{
        "month": "2026-07", "status": "future_required",
        "planned_required_trade_count": 4, "current_trade_count": current_trades,
        "missing_trade_count": missing, "current_turnover": current_turnover,
        "suggested_turnover": suggested,
    }]
    plan = {
        "as_of": "2026-07-01", "period_policy": "official_completed_quarters",
        "period_kind": "forecast_plan",
        "earliest_possible_check_date": "2027-07-01",
        "earliest_possible_period": {"start": "2026-07-01", "end": "2027-06-30"},
        "monthly_plan": months, "quarterly_plan": [],
    }
    (tmp / "kval_plan.json").write_text(json.dumps(plan), encoding="utf-8")


def _write_scan(tmp, verdict="GOOD", spread="0.50", depth="300000",
                trading_ok=True, data_ok=True, commission="5"):
    res = {
        "ticker": "LQDT", "name": "ВИМ - Ликвидность", "class_code": "SPBRU",
        "resolved_class_code": "SPBRU", "bid_best": "100.00", "ask_best": "100.01",
        "spread_bps": spread, "min_side_top_depth_rub": depth,
        "estimated_roundtrip_cost_bps": "10.50", "estimated_monthly_cost_rub": "533.75",
        "lot": 1, "currency": "rub",
        "trading_status": NORMAL if trading_ok else NOT_TRADING,
        "verdict": verdict, "trading_status_ok": trading_ok, "data_ok": data_ok,
        "suitable_for_turnover": verdict == "GOOD",
    }
    scan = {"as_of": "2026-07-01", "commission_bps": commission,
            "target_monthly_turnover": "508333.33", "results": [res], "warnings": []}
    (tmp / "instrument_scan.json").write_text(json.dumps(scan), encoding="utf-8")


def _setup(tmp, **scan_kw):
    plan_kw = {k: scan_kw.pop(k) for k in
               ("missing", "current_turnover", "current_trades", "suggested")
               if k in scan_kw}
    _write_plan(tmp, **plan_kw)
    _write_scan(tmp, **scan_kw)


def _build(tmp, **kw):
    return build(tmp, as_of=AS_OF, instrument="LQDT", **kw)


def test_july_4_missing_two_cycles(tmp_path):
    _setup(tmp_path)
    p = _build(tmp_path)
    assert p.broker_trade_count_missing == 4
    assert p.roundtrip_cycle_count_required == 2


def test_side_notional_quarter_of_remaining(tmp_path):
    _setup(tmp_path)
    p = _build(tmp_path)
    # remaining 508333.33 / (2 cycles * 2) = 127083.3325 -> 127083.33
    assert p.side_notional == Decimal("127083.33")


def test_planned_actions_length_four(tmp_path):
    _setup(tmp_path)
    p = _build(tmp_path)
    assert len(p.planned_actions) == 4


def test_actions_alternate_buy_sell(tmp_path):
    _setup(tmp_path)
    p = _build(tmp_path)
    assert [a.side for a in p.planned_actions] == ["BUY", "SELL", "BUY", "SELL"]
    assert all(a.dry_run is True for a in p.planned_actions)


def test_odd_missing_warns_extra_trade(tmp_path):
    _setup(tmp_path, missing=3)
    p = _build(tmp_path)
    assert p.roundtrip_cycle_count_required == 2
    assert len(p.planned_actions) == 4          # на 1 больше минимума
    assert any("больше минимума" in w or "нечёт" in w for w in p.warnings)


def test_wide_spread_blocks(tmp_path):
    _setup(tmp_path, spread="50.0")
    p = _build(tmp_path, spread_bps_limit=Decimal("5"))
    assert p.status == "BLOCKED"
    assert any(c.name == "spread_within_limit" and not c.ok for c in p.risk_checks)


def test_not_good_blocks(tmp_path):
    _setup(tmp_path, verdict="WATCH")
    p = _build(tmp_path)
    assert p.status == "BLOCKED"
    assert any(c.name == "instrument_good" and not c.ok for c in p.risk_checks)


def test_insufficient_depth_blocks(tmp_path):
    _setup(tmp_path, depth="1000")              # << side_notional ~127083
    p = _build(tmp_path)
    assert p.status == "BLOCKED"
    assert any(c.name == "depth_sufficient" and not c.ok for c in p.risk_checks)


def test_missing_input_files_friendly_error(tmp_path):
    with pytest.raises(ExecutionPlanError):
        _build(tmp_path)


def test_dry_run_forced_even_if_false(tmp_path):
    _setup(tmp_path)
    p = _build(tmp_path, dry_run=False)
    assert p.dry_run is True
    assert all(a.dry_run for a in p.planned_actions)
    assert any("Live" in w or "dry-run" in w for w in p.warnings)


def test_no_order_endpoints_in_source():
    for mod in ("modules/execution_planner.py",
                "reports/execution_plan_reports.py",
                "reports/console_execution.py"):
        src = Path(mod).read_text(encoding="utf-8")
        for forbidden in ("postOrder", "cancelOrder", "PostOrder", "OrdersService",
                          "post_order", "TINKOFF_TOKEN", "full_token",
                          "place_order", "submit_order"):
            assert forbidden not in src, f"{mod}: {forbidden}"


def test_reports_have_stable_columns(tmp_path):
    from reports import execution_plan_reports
    _setup(tmp_path)
    p = _build(tmp_path)
    written = execution_plan_reports.write_all(p, tmp_path)
    assert set(written) == {"execution_plan.json", "execution_plan.csv"}

    header = (tmp_path / "execution_plan.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    assert header == [
        "seq", "side", "ticker", "class_code", "notional_rub", "estimated_lots",
        "estimated_price", "expected_turnover_contribution", "dry_run",
    ]

    data = json.loads((tmp_path / "execution_plan.json").read_text(encoding="utf-8"))
    for key in ("generated_at_utc", "as_of", "period", "instrument",
                "broker_trade_count_required", "broker_trade_count_missing",
                "roundtrip_cycle_count_required", "side_notional", "cycle_turnover",
                "planned_actions", "risk_checks", "warnings", "dry_run"):
        assert key in data
    assert data["dry_run"] is True
