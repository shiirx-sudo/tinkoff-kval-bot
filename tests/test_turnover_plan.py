"""Тесты read-only Manual Turnover Plan."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from modules.turnover_planner import TurnoverPlanError, build

NORMAL = "SECURITY_TRADING_STATUS_NORMAL_TRADING"


def _months(july_overrides=None):
    base = []
    labels = [f"2026-{m:02d}" for m in range(7, 13)] + [f"2027-{m:02d}" for m in range(1, 7)]
    for lbl in labels:
        base.append({
            "month": lbl, "status": "future_required",
            "planned_required_trade_count": 4, "current_trade_count": 0,
            "missing_trade_count": 4, "current_turnover": "0",
            "suggested_turnover": "508333",
        })
    if july_overrides:
        base[0].update(july_overrides)
    return base


def _write_plan(tmp, monthly=None):
    plan = {
        "as_of": "2026-07-01",
        "period_policy": "official_completed_quarters",
        "period_kind": "forecast_plan",
        "earliest_possible_check_date": "2027-07-01",
        "earliest_possible_period": {"start": "2026-07-01", "end": "2027-06-30"},
        "monthly_plan": monthly if monthly is not None else _months(),
        "quarterly_plan": [{
            "quarter": "2026Q3", "required_min_trade_count": 10,
            "current_trade_count": 0, "missing_trade_count": 10,
            "current_turnover": "0", "suggested_turnover": "1524999",
            "status": "future_required",
        }],
    }
    (tmp / "kval_plan.json").write_text(json.dumps(plan), encoding="utf-8")


def _scan_result(ticker="LQDT", verdict="GOOD", trading_ok=True, data_ok=True):
    return {
        "ticker": ticker, "name": "ВИМ - Ликвидность", "class_code": "SPBRU",
        "resolved_class_code": "SPBRU", "bid_best": "100.00", "ask_best": "100.01",
        "spread_bps": "0.50", "estimated_roundtrip_cost_bps": "10.50",
        "estimated_monthly_cost_rub": "533.75", "lot": 1, "currency": "rub",
        "trading_status": NORMAL, "verdict": verdict,
        "trading_status_ok": trading_ok, "data_ok": data_ok,
        "suitable_for_turnover": verdict == "GOOD",
    }


def _write_scan(tmp, results, commission="5"):
    scan = {"as_of": "2026-07-01", "commission_bps": commission,
            "target_monthly_turnover": "508333", "results": results, "warnings": []}
    (tmp / "instrument_scan.json").write_text(json.dumps(scan), encoding="utf-8")


def _setup(tmp, results=None, monthly=None):
    _write_plan(tmp, monthly)
    _write_scan(tmp, results if results is not None else [_scan_result()])


AS_OF = date(2026, 7, 1)


def test_auto_selects_good(tmp_path):
    _setup(tmp_path)
    p = build(tmp_path, as_of=AS_OF)
    assert p.selected_instrument.ticker == "LQDT"
    assert p.selected_instrument.verdict == "GOOD"


def test_explicit_instrument(tmp_path):
    _setup(tmp_path, results=[_scan_result("AAA"), _scan_result("LQDT")])
    p = build(tmp_path, as_of=AS_OF, instrument="LQDT")
    assert p.selected_instrument.ticker == "LQDT"


def test_watch_when_no_good(tmp_path):
    _setup(tmp_path, results=[_scan_result("WCH", verdict="WATCH")])
    p = build(tmp_path, as_of=AS_OF)
    assert p.selected_instrument.ticker == "WCH"
    assert any("WATCH" in w for w in p.warnings)


def test_no_suitable_instrument(tmp_path):
    _setup(tmp_path, results=[_scan_result("BAD1", verdict="NOT_FOUND",
                                           trading_ok=False, data_ok=False)])
    with pytest.raises(TurnoverPlanError):
        build(tmp_path, as_of=AS_OF)


def test_gross_mode_full_nominal(tmp_path):
    _setup(tmp_path)
    p = build(tmp_path, as_of=AS_OF, mode="gross", round_lots=False)
    # remaining 508333 / missing 4 = 127083.25
    assert p.recommendations.recommended_trade_turnover == Decimal("127083.25")
    assert p.recommendations.recommended_roundtrip_side_notional == Decimal("0")


def test_roundtrip_mode_splits_side(tmp_path):
    _setup(tmp_path)
    p = build(tmp_path, as_of=AS_OF, mode="roundtrip", round_lots=False)
    assert p.recommendations.recommended_trade_turnover == Decimal("127083.25")
    # corrected: remaining / (cycles*2) = 508333 / 4 = 127083.25 (не per_trade/2)
    assert p.recommendations.recommended_roundtrip_side_notional == Decimal("127083.25")
    assert p.recommendations.roundtrip_cycle_count_required == 2
    assert p.recommendations.side_notional == Decimal("127083.25")


def test_current_turnover_reduces_remaining(tmp_path):
    _setup(tmp_path, monthly=_months({"current_turnover": "100000"}))
    p = build(tmp_path, as_of=AS_OF, mode="gross", round_lots=False)
    assert p.current_month_plan.remaining_turnover == Decimal("408333")
    # 408333 / 4 = 102083.25
    assert p.recommendations.recommended_trade_turnover == Decimal("102083.25")


def test_missing_zero_closes_plan(tmp_path):
    closed = _months({"status": "done_ok", "current_trade_count": 4,
                      "missing_trade_count": 0, "current_turnover": "508333"})
    # все месяцы закрыты → нет future_required
    for m in closed:
        m["status"] = "done_ok"
        m["missing_trade_count"] = 0
        m["current_trade_count"] = 4
        m["current_turnover"] = "508333"
    _setup(tmp_path, monthly=closed)
    p = build(tmp_path, as_of=AS_OF)
    assert p.recommendations.trade_plan_closed is True
    assert p.recommendations.recommended_trade_turnover == Decimal("0")
    assert "закрыт" in p.recommendations.note


def test_missing_input_files_friendly_error(tmp_path):
    with pytest.raises(TurnoverPlanError) as ei:
        build(tmp_path, as_of=AS_OF)
    assert "kval-status" in str(ei.value) or "kval_plan" in str(ei.value)

    _write_plan(tmp_path)  # есть план, но нет скана
    with pytest.raises(TurnoverPlanError) as ei2:
        build(tmp_path, as_of=AS_OF)
    assert "instrument-scan" in str(ei2.value) or "instrument_scan" in str(ei2.value)


def test_reports_stable_columns(tmp_path):
    from reports import turnover_plan_reports
    _setup(tmp_path)
    p = build(tmp_path, as_of=AS_OF)
    written = turnover_plan_reports.write_all(p, tmp_path)
    assert set(written) == {"manual_turnover_plan.json", "manual_turnover_plan.csv"}

    header = (tmp_path / "manual_turnover_plan.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    assert header == [
        "month", "status", "planned_required_trade_count", "current_trade_count",
        "missing_trade_count", "suggested_turnover", "current_turnover",
        "remaining_turnover", "recommended_trade_turnover",
        "recommended_roundtrip_side_notional", "selected_ticker",
        "selected_instrument_name", "estimated_roundtrip_cost_bps",
        "estimated_cost_rub", "warning",
    ]

    data = json.loads((tmp_path / "manual_turnover_plan.json").read_text(encoding="utf-8"))
    for key in ("generated_at_utc", "as_of", "period_policy", "period_kind",
                "check_date", "target_monthly_turnover", "selected_instrument",
                "current_month_plan", "current_quarter_plan", "recommendations",
                "warnings", "source_files"):
        assert key in data


def test_no_order_endpoints():
    for mod in ("modules/turnover_planner.py",
                "reports/turnover_plan_reports.py",
                "reports/console_turnover.py"):
        src = Path(mod).read_text(encoding="utf-8")
        for forbidden in ("postOrder", "cancelOrder", "OrdersService",
                          "TINKOFF_TOKEN", "full_token"):
            assert forbidden not in src
