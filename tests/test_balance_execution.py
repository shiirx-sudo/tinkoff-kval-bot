"""Тесты balance-adaptive execution-plan и read-only passive-income."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from modules.execution_planner import build

AS_OF = date(2026, 7, 1)
NORMAL = "SECURITY_TRADING_STATUS_NORMAL_TRADING"


def _setup(tmp, missing=4, suggested="508333.33", depth="5000000"):
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
    res = {
        "ticker": "LQDT", "name": "ВИМ - Ликвидность", "class_code": "SPBRU",
        "resolved_class_code": "SPBRU", "bid_best": "100.00", "ask_best": "100.01",
        "spread_bps": "0.50", "min_side_top_depth_rub": depth,
        "estimated_roundtrip_cost_bps": "10.50", "estimated_monthly_cost_rub": "533.75",
        "lot": 1, "currency": "rub", "trading_status": NORMAL, "verdict": "GOOD",
        "trading_status_ok": True, "data_ok": True, "suitable_for_turnover": True,
    }
    scan = {"as_of": "2026-07-01", "commission_bps": "5",
            "target_monthly_turnover": suggested, "results": [res], "warnings": []}
    (tmp / "instrument_scan.json").write_text(json.dumps(scan), encoding="utf-8")


def _bal(tmp, cash, **kw):
    kw.setdefault("balance_utilization_pct", Decimal("1.0"))
    kw.setdefault("min_cash_reserve_rub", Decimal("0"))
    kw.setdefault("min_monthly_actions", 4)
    kw.setdefault("min_depth_multiplier", Decimal("1.2"))
    return build(tmp, as_of=AS_OF, instrument="LQDT", mode=kw.pop("mode", "roundtrip"),
                 size_mode="balance",
                 available_cash_rub=(Decimal(str(cash)) if cash is not None else None),
                 max_side_notional_rub=Decimal("0"), **kw)


def test_cap_50k_plans_12(tmp_path):
    _setup(tmp_path)
    p = _bal(tmp_path, 50000)
    assert p.sizing.planned_actions == 12
    assert len(p.planned_actions) == 12


def test_cap_100k_plans_6(tmp_path):
    _setup(tmp_path)
    p = _bal(tmp_path, 100000)
    assert p.sizing.planned_actions == 6


def test_cap_130k_plans_4(tmp_path):
    _setup(tmp_path)
    p = _bal(tmp_path, 130000)
    assert p.sizing.planned_actions == 4
    assert p.side_notional == Decimal("127083.33")


def test_cap_300k_min_actions_floor(tmp_path):
    _setup(tmp_path)
    p = _bal(tmp_path, 300000)
    assert p.sizing.actions_by_turnover == 2
    assert p.sizing.planned_actions == 4          # пол из EXECUTION_MIN_MONTHLY_ACTIONS


def test_reserve_violation_blocks(tmp_path):
    _setup(tmp_path)
    p = _bal(tmp_path, 3000, balance_utilization_pct=Decimal("0.80"),
             min_cash_reserve_rub=Decimal("5000"))
    assert p.status == "BLOCKED"
    assert any(c.name == "reserve_preserved" and not c.ok for c in p.risk_checks)


def test_no_balance_blocks_no_crash(tmp_path):
    _setup(tmp_path)
    p = _bal(tmp_path, None)
    assert p.status == "BLOCKED"
    assert any(c.name == "available_cash_present" and not c.ok for c in p.risk_checks)


def test_kval_min_total_trades_considered(tmp_path):
    _setup(tmp_path, missing=2)
    # gross + min_monthly_actions=3 → planned 3 → projected 36 < 41
    p = _bal(tmp_path, 10_000_000, mode="gross", min_monthly_actions=3,
             kval_min_total_trades=41)
    assert p.sizing.planned_actions == 3
    assert p.sizing.projected_total_trades == 36
    chk = next(c for c in p.risk_checks if c.name == "min_total_trades_met")
    assert chk.ok is False
    assert "min=41" in chk.detail


def test_kval_target_total_trades_used(tmp_path):
    _setup(tmp_path)
    p = _bal(tmp_path, 130000)
    assert p.sizing.kval_target_total_trades == 48
    assert p.sizing.projected_total_trades == 48   # 4 действия × 12


def test_telegram_summary_has_sizing():
    from notifications import telegram as tg
    data = {
        "status": "READY_DRY_RUN", "ticker": "LQDT", "verdict": "GOOD",
        "trading_status": NORMAL, "period": "2026-07", "check_date": "2027-07-01",
        "spread": "0.50", "side_notional": "127083.33",
        "broker_trade_count_missing": 4, "roundtrip_cycle_count_required": 2,
        "size_mode": "balance", "available_cash_rub": "130000",
        "planned_actions": 4, "projected_total_trades": 48,
        "kval_min_total_trades": 41, "warnings": [],
    }
    text = tg.build_summary_message(data, today=date(2026, 6, 16))
    assert "Sizing: balance" in text
    assert "Свободный баланс:" in text
    assert "Планируемых действий: 4" in text
    assert "/ 41" in text


def test_no_order_endpoints_in_balance_sources():
    for mod in ("modules/balance.py", "modules/execution_planner.py"):
        src = Path(mod).read_text(encoding="utf-8")
        for forbidden in ("place_limit_order", "cancel_order", "OrdersService",
                          "order_client", "postOrder", "place_order", "submit_order",
                          "LIVE_EXECUTION_ENABLED", "TINKOFF_TOKEN"):
            assert forbidden not in src, f"{mod}: {forbidden}"


# ─── passive-income (read-only) ──────────────────────────────────────────────

class _PFClient:
    def get_broker_accounts(self):
        return [{"id": "2057431918", "type": "ACCOUNT_TYPE_TINKOFF"}]

    def get_positions(self, account_id):
        return {"money": [{"currency": "rub", "units": "25000", "nano": 0}]}

    def get_portfolio(self, account_id):
        return {
            "expectedYield": {"units": "1500", "nano": 0},
            "positions": [
                {"figi": "F1", "ticker": "LQDT", "instrumentType": "etf",
                 "quantity": {"units": "1000", "nano": 0},
                 "currentPrice": {"units": "100", "nano": 0}},
                {"figi": "F2", "ticker": "SU26240", "instrumentType": "bond",
                 "quantity": {"units": "10", "nano": 0},
                 "currentPrice": {"units": "900", "nano": 0}},
                {"figi": "F3", "ticker": "SBER", "instrumentType": "share",
                 "quantity": {"units": "5", "nano": 0},
                 "currentPrice": {"units": "300", "nano": 0}},
            ],
        }


def test_portfolio_breakdown_categorizes():
    from modules.balance import portfolio_breakdown
    b = portfolio_breakdown(_PFClient(), None)
    assert b.free_rub == Decimal("25000")
    assert b.money_market_funds_rub == Decimal("100000")   # 1000 * 100
    assert b.bonds_rub == Decimal("9000")                  # 10 * 900
    assert b.dividend_shares_rub == Decimal("1500")        # 5 * 300
    assert b.account_id_masked.endswith("1918")
    assert b.total_rub == Decimal("135500")


def test_balance_low_cash_depth_not_false(tmp_path):
    # свободные < резерва → side=0; depth_sufficient неприменим (ok=True),
    # блокировка остаётся по balance/reserve (0019/cosmetic hotfix)
    _setup(tmp_path)
    p = _bal(tmp_path, 2105, balance_utilization_pct=Decimal("0.80"),
             min_cash_reserve_rub=Decimal("5000"))
    assert p.status == "BLOCKED"
    assert p.side_notional == Decimal("0")
    assert p.sizing.planned_actions == 0
    depth = next(c for c in p.risk_checks if c.name == "depth_sufficient")
    assert depth.ok is True
    assert "n/a" in depth.detail
    # основная блокировка — balance/reserve, не глубина
    failed = [c.name for c in p.risk_checks if not c.ok]
    assert "depth_sufficient" not in failed
    assert any(n in failed for n in ("side_notional_within_balance", "reserve_preserved"))
