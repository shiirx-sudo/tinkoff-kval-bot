"""Тесты Execution Preflight (READ-ONLY). Никаких заявок."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from modules.execution_planner import build as build_plan
from modules.execution_preflight import run
from reports import execution_plan_reports

AS_OF = date(2026, 7, 1)
NORMAL = "SECURITY_TRADING_STATUS_NORMAL_TRADING"
NOT_TRADING = "SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING"


def _write_plan_json(tmp, missing=4, suggested="508333.33"):
    months = [{
        "month": "2026-07", "status": "future_required",
        "planned_required_trade_count": 4, "current_trade_count": 0,
        "missing_trade_count": missing, "current_turnover": "0",
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


def _setup(tmp, max_side="130000", **scan_kw):
    _write_plan_json(tmp, missing=scan_kw.pop("missing", 4))
    _write_scan(tmp, **scan_kw)
    # сохранить согласованный execution_plan.json
    p = build_plan(tmp, as_of=AS_OF, instrument="LQDT",
                   max_side_notional_rub=Decimal(str(max_side)))
    execution_plan_reports.write_all(p, tmp)


def _run(tmp, **kw):
    kw.setdefault("max_side_notional_rub", Decimal("130000"))
    return run(tmp, as_of=AS_OF, instrument="LQDT", **kw)


def test_ready_dry_run(tmp_path):
    _setup(tmp_path)
    r = _run(tmp_path)
    assert r.status == "READY_DRY_RUN"
    assert r.side_notional == Decimal("127083.33")


def test_blocked_side_over_max(tmp_path):
    _setup(tmp_path)
    r = _run(tmp_path, max_side_notional_rub=Decimal("100000"))
    assert r.status == "BLOCKED"
    assert any(c.name == "side_within_max" and not c.ok for c in r.checks)
    assert any("side_notional" in e for e in r.errors)


def test_blocked_wide_spread(tmp_path):
    _setup(tmp_path, spread="50.0")
    r = _run(tmp_path)
    assert r.status == "BLOCKED"
    assert any(c.name == "spread_within_limit" and not c.ok for c in r.checks)


def test_blocked_not_good(tmp_path):
    _setup(tmp_path, verdict="WATCH")
    r = _run(tmp_path)
    assert r.status == "BLOCKED"
    assert any(c.name == "verdict_good" and not c.ok for c in r.checks)


def test_missing_reports(tmp_path):
    r = _run(tmp_path)
    assert r.status == "MISSING_REPORTS"
    assert r.errors and not r.checks


def test_all_actions_dry_run(tmp_path):
    _setup(tmp_path)
    r = _run(tmp_path)
    assert any(c.name == "actions_are_dry_run" and c.ok for c in r.checks)


def test_no_live_adapter_or_order_endpoints(tmp_path):
    _setup(tmp_path)
    r = _run(tmp_path)
    assert any(c.name == "no_order_endpoints" and c.ok for c in r.checks)
    assert any(c.name == "no_live_adapter" and c.ok for c in r.checks)


def test_reports_created(tmp_path):
    from reports import execution_preflight_reports
    _setup(tmp_path)
    r = _run(tmp_path)
    written = execution_preflight_reports.write_all(r, tmp_path)
    assert set(written) == {
        "execution_preflight.json", "execution_preflight.csv",
        "execution_preflight.md",
    }
    header = (tmp_path / "execution_preflight.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    assert header == ["check", "ok", "blocking", "detail"]
    data = json.loads((tmp_path / "execution_preflight.json").read_text(encoding="utf-8"))
    for key in ("generated_at_utc", "as_of", "status", "instrument", "period",
                "broker_trade_count_missing", "roundtrip_cycle_count_required",
                "side_notional", "planned_actions_count", "checks", "warnings",
                "errors", "source_reports"):
        assert key in data
    assert (tmp_path / "execution_preflight.md").read_text(
        encoding="utf-8").startswith("# Execution Preflight")


def test_missing_reports_no_traceback(tmp_path):
    # только kval_plan есть → всё равно MISSING_REPORTS, без исключения
    _write_plan_json(tmp_path)
    r = _run(tmp_path)
    assert r.status == "MISSING_REPORTS"


def test_preflight_source_has_no_order_endpoints():
    src = Path("modules/execution_preflight.py").read_text(encoding="utf-8")
    for forbidden in ("place_limit_order", "cancel_order", "OrdersService",
                      "order_client", "place_order", "submit_order",
                      "LIVE_EXECUTION_ENABLED"):
        assert forbidden not in src, forbidden
