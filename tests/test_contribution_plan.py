"""
Тесты F4.10 contribution_plan — локальный учёт пополнений (не торговля).

Детерминированно: as_of передаётся явно (date). Проверяем init/add/validate,
расчёт факт/план/разрыв по неделе/месяцу/году, пропущенные взносы, статусы
ON_TRACK/BEHIND/DISABLED/NOT_CONFIGURED, отчёты, общую логику с F4.8 и отсутствие
брокера/токенов/сети.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from modules import contribution_plan as cp

AS_OF = date(2026, 6, 27)        # суббота; понедельник недели = 2026-06-22
MON = "2026-06-22"               # начало текущей недели


def _plan(**over):
    base = dict(enabled=True, currency="rub", plan_weekly_rub=50000,
                plan_monthly_rub=200000, plan_start_date="2026-01-01",
                next_planned_contribution_date="2026-07-06", source="manual",
                facts=[])
    base.update(over)
    return base


# ─── missing / disabled / status ──────────────────────────────────────────────

def test_missing_config_not_configured():
    st = cp.compute_status(None, as_of=AS_OF)
    assert st["status"] == "NOT_CONFIGURED"
    assert st["contributions_tracking_enabled"] is False
    assert any(cp.WARN_NOT_CONFIGURED in w for w in st["warnings"])


def test_disabled_plan_status_disabled():
    st = cp.compute_status(_plan(enabled=False), as_of=AS_OF)
    assert st["status"] == "DISABLED"


def test_status_on_track_when_facts_meet_plan():
    plan = _plan(facts=[{"date": MON, "amount_rub": 200000}])
    st = cp.compute_status(plan, as_of=AS_OF)
    assert st["contribution_gap_weekly_rub"] == Decimal("0.00")
    assert st["contribution_gap_monthly_rub"] == Decimal("0.00")
    assert st["status"] == "ON_TRACK"


def test_status_behind_when_below_plan():
    plan = _plan(facts=[{"date": MON, "amount_rub": 10000}])
    st = cp.compute_status(plan, as_of=AS_OF)
    assert st["status"] == "BEHIND"
    assert st["contribution_gap_monthly_rub"] > 0


# ─── period fact calculations ─────────────────────────────────────────────────

def test_weekly_fact_from_monday():
    plan = _plan(facts=[
        {"date": "2026-06-21", "amount_rub": 99999},   # вс (до понедельника) — НЕ в неделе
        {"date": MON, "amount_rub": 50000},            # пн — в неделе
        {"date": "2026-06-27", "amount_rub": 30000},   # сб — в неделе
    ])
    st = cp.compute_status(plan, as_of=AS_OF)
    assert st["contribution_fact_weekly_rub"] == Decimal("80000.00")  # 50000+30000


def test_monthly_fact_from_first_day():
    plan = _plan(facts=[
        {"date": "2026-05-31", "amount_rub": 99999},   # прошлый месяц — НЕ в месяце
        {"date": "2026-06-01", "amount_rub": 70000},   # первый день месяца — в месяце
        {"date": "2026-06-20", "amount_rub": 30000},
    ])
    st = cp.compute_status(plan, as_of=AS_OF)
    assert st["contribution_fact_monthly_rub"] == Decimal("100000.00")


def test_ytd_fact_from_jan1_or_plan_start():
    # plan_start = Jan 1 → факт января учитывается; прошлогодний — нет
    plan = _plan(plan_start_date="2026-01-01", facts=[
        {"date": "2025-12-31", "amount_rub": 99999},   # прошлый год — НЕ в YTD
        {"date": "2026-01-15", "amount_rub": 40000},   # в YTD
        {"date": "2026-06-20", "amount_rub": 60000},
    ])
    st = cp.compute_status(plan, as_of=AS_OF)
    assert st["contribution_fact_ytd_rub"] == Decimal("100000.00")
    # plan_start позже Jan 1 → YTD считается от plan_start
    plan2 = _plan(plan_start_date="2026-06-01", facts=[
        {"date": "2026-01-15", "amount_rub": 40000},   # до plan_start — НЕ в YTD
        {"date": "2026-06-10", "amount_rub": 25000}])
    st2 = cp.compute_status(plan2, as_of=AS_OF)
    assert st2["contribution_fact_ytd_rub"] == Decimal("25000.00")


def test_gaps_and_expected():
    plan = _plan(facts=[{"date": MON, "amount_rub": 50000}])
    st = cp.compute_status(plan, as_of=AS_OF)
    assert st["contribution_expected_weekly_rub"] == Decimal("50000.00")
    assert st["contribution_expected_monthly_rub"] == Decimal("200000.00")
    assert st["contribution_gap_weekly_rub"] == Decimal("0.00")
    assert st["contribution_gap_monthly_rub"] == Decimal("150000.00")
    # gap не уходит в минус (max с 0)
    over = cp.compute_status(_plan(facts=[{"date": MON, "amount_rub": 999999}]),
                             as_of=AS_OF)
    assert over["contribution_gap_monthly_rub"] == Decimal("0.00")


def test_missed_counts():
    plan = _plan(facts=[{"date": MON, "amount_rub": 50000}])
    st = cp.compute_status(plan, as_of=AS_OF)
    # неделя закрыта (факт==план) → 0
    assert st["missed_contributions_count_week"] == 0
    # месяц: gap 150000 / 50000 = ceil 3
    assert st["missed_contributions_count_month"] == 3


def test_missed_month_when_no_weekly_plan():
    plan = _plan(plan_weekly_rub=0, facts=[{"date": "2026-06-02", "amount_rub": 1000}])
    st = cp.compute_status(plan, as_of=AS_OF)
    # weekly план 0 → missed = 1 если месячный gap > 0
    assert st["missed_contributions_count_month"] == 1


def test_next_contribution_days():
    st = cp.compute_status(_plan(), as_of=AS_OF)
    assert st["next_planned_contribution_date"] == "2026-07-06"
    assert st["days_until_next_planned_contribution"] == 9


# ─── validation ───────────────────────────────────────────────────────────────

def test_invalid_currency_rejected():
    errs = cp.validate_plan(_plan(currency="usd"))
    assert any("currency" in e for e in errs)


def test_negative_plan_rejected():
    errs = cp.validate_plan(_plan(plan_weekly_rub=-5))
    assert any("plan_weekly_rub" in e for e in errs)


def test_invalid_date_in_facts_rejected():
    errs = cp.validate_plan(_plan(facts=[{"date": "not-a-date", "amount_rub": 100}]))
    assert any("facts[0].date" in e for e in errs)


def test_zero_fact_amount_rejected():
    errs = cp.validate_plan(_plan(facts=[{"date": "2026-06-10", "amount_rub": 0}]))
    assert any("amount_rub" in e for e in errs)


def test_add_fact_invalid_date_raises():
    with pytest.raises(cp.ContributionPlanError):
        cp.add_fact(_plan(), date_str="2026-13-99", amount_rub=1000)


def test_add_fact_nonpositive_amount_raises():
    with pytest.raises(cp.ContributionPlanError):
        cp.add_fact(_plan(), date_str="2026-06-10", amount_rub=0)
    with pytest.raises(cp.ContributionPlanError):
        cp.add_fact(_plan(), date_str="2026-06-10", amount_rub=-100)


# ─── init / add ───────────────────────────────────────────────────────────────

def test_init_creates_valid_plan(tmp_path):
    plan = cp.init_plan(weekly_rub=50000, monthly_rub=200000,
                        start_date="2026-06-01", next_date="2026-07-06")
    assert cp.validate_plan(plan) == []
    path = cp.save_plan(plan, str(tmp_path / "cp.json"))
    assert Path(path).exists()
    loaded = cp.load_plan(path)
    assert loaded["plan_weekly_rub"] == 50000
    assert loaded["enabled"] is True
    assert loaded["currency"] == "rub"


def test_init_preserves_facts_unless_reset():
    existing = _plan(facts=[{"date": "2026-06-10", "amount_rub": 50000}])
    kept = cp.init_plan(weekly_rub=60000, monthly_rub=240000,
                        start_date="2026-06-01", next_date=None, existing=existing)
    assert kept["facts"] == existing["facts"]
    assert kept["plan_weekly_rub"] == 60000
    reset = cp.init_plan(weekly_rub=60000, monthly_rub=240000,
                         start_date="2026-06-01", next_date=None,
                         existing=existing, reset_facts=True)
    assert reset["facts"] == []


def test_add_fact_appends_and_sorts():
    plan = _plan(facts=[{"date": "2026-06-20", "amount_rub": 50000}])
    plan, added = cp.add_fact(plan, date_str="2026-06-10", amount_rub=30000)
    assert added is True
    dates = [f["date"] for f in plan["facts"]]
    assert dates == ["2026-06-10", "2026-06-20"]    # отсортировано по возрастанию


def test_add_duplicate_rejected_by_default():
    plan = _plan(facts=[{"date": "2026-06-10", "amount_rub": 50000}])
    plan2, added = cp.add_fact(plan, date_str="2026-06-10", amount_rub=50000)
    assert added is False
    assert len(plan2["facts"]) == 1


def test_add_duplicate_allowed_with_flag():
    plan = _plan(facts=[{"date": "2026-06-10", "amount_rub": 50000}])
    plan2, added = cp.add_fact(plan, date_str="2026-06-10", amount_rub=50000,
                               allow_duplicate=True)
    assert added is True
    assert len(plan2["facts"]) == 2


# ─── reports ──────────────────────────────────────────────────────────────────

def test_status_reports_generated(tmp_path):
    st = cp.compute_status(_plan(facts=[{"date": MON, "amount_rub": 50000}]),
                           as_of=AS_OF)
    out = cp.write_status_report(
        st, json_path=str(tmp_path / "s.json"), md_path=str(tmp_path / "s.md"))
    assert Path(out["_output_json"]).exists()
    md = Path(out["_output_md"]).read_text(encoding="utf-8")
    assert "F4.10" in md
    assert "BEHIND" in md
    assert "Факт / план / разрыв" in md
    data = json.loads(Path(out["_output_json"]).read_text(encoding="utf-8"))
    for k in ("status", "contribution_fact_weekly_rub", "contribution_gap_ytd_rub",
              "missed_contributions_count_month", "guards", "token_policy"):
        assert k in data


def test_missing_config_status_report_exit0(tmp_path):
    st = cp.compute_status(None, as_of=AS_OF)
    out = cp.write_status_report(
        st, json_path=str(tmp_path / "s.json"), md_path=str(tmp_path / "s.md"))
    assert Path(out["_output_md"]).read_text(encoding="utf-8").count("NOT_CONFIGURED")


# ─── guards / token policy ────────────────────────────────────────────────────

def test_guards_and_token_policy_safe():
    st = cp.compute_status(_plan(), as_of=AS_OF)
    g = st["guards"]
    assert g["broker_api_called"] is False
    assert g[cp.GUARD_LIVE_ORDER_SENT] is False
    assert g["post_order_called"] is False
    assert g[cp.GUARD_CANCEL_CALLED] is False
    for k in ("sell_order_sent", "market_order_used", "retry_execution",
              "portfolio_mutated", "telegram_sent", "scheduler_created",
              "token_printed"):
        assert g[k] is False
    assert g["config_mutated"] is False
    tp = st["token_policy"]
    assert tp["read_only_token_present"] is False
    assert tp["live_token_used"] is False
    assert tp["sandbox_token_used"] is False
    assert tp["token_printed"] is False


def test_config_mutated_flag_for_mutating_commands():
    st = cp.compute_status(_plan(), as_of=AS_OF, config_mutated=True)
    assert st["guards"]["config_mutated"] is True


# ─── F4.8 shared logic ────────────────────────────────────────────────────────

def test_summarize_for_dashboard_richer():
    plan = _plan(facts=[{"date": MON, "amount_rub": 50000}])
    s = cp.summarize_for_dashboard(plan, as_of=AS_OF)
    # совместимые F4.8-ключи
    for k in ("contributions_tracking_enabled", "contribution_plan_weekly_rub",
              "contribution_plan_monthly_rub", "contribution_fact_monthly_rub",
              "contribution_gap_monthly_rub", "missed_contributions_count_month",
              "next_planned_contribution_date",
              "contribution_required_to_catch_up_rub"):
        assert k in s
    # более богатые ключи
    for k in ("contribution_gap_ytd_rub", "missed_contributions_count_week",
              "days_until_next_planned_contribution", "contribution_status"):
        assert k in s
    assert s["contribution_status"] == "BEHIND"


def test_summarize_disabled_has_warning():
    s = cp.summarize_for_dashboard(None, as_of=AS_OF)
    assert s["contributions_tracking_enabled"] is False
    assert cp.WARN_NOT_CONFIGURED in s["warnings"]


def test_f48_uses_shared_logic(tmp_path):
    # F4.8 contributions_summary должен содержать новые ключи из общей логики
    from datetime import timezone
    from modules import portfolio_dashboard_data as pdd
    plan = _plan(facts=[{"date": MON, "amount_rub": 50000}])
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan), encoding="utf-8")
    import datetime as _dt
    now = _dt.datetime(2026, 6, 27, tzinfo=timezone.utc)
    rep = pdd.load_portfolio_dashboard_data(
        live_account_id="2000001918", reports_dir=str(tmp_path),
        contribution_plan_path=str(p), now=now, read_token_present=False)
    cs = rep["contributions_summary"]
    assert cs["contributions_tracking_enabled"] is True
    assert cs["contribution_status"] == "BEHIND"
    assert "contribution_gap_ytd_rub" in cs


# ─── safety / source scan ─────────────────────────────────────────────────────

def test_module_no_broker_no_token_no_network():
    src = Path(cp.__file__).read_text(encoding="utf-8")
    assert "ReadOnlyClient" not in src
    assert "TINKOFF_TOKEN" not in src
    assert "rest_client" not in src
    for net in ("import requests", "urllib", "http.client", "http.server",
                "socket", "api.client"):
        assert net not in src, net


def test_module_source_has_no_forbidden_literals():
    src = Path(cp.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service", "post" "Order(", "cancel" "Order(",
        "place" "_order", "submit" "_order", "cancel" "_order",
        "live" "_order", "order" "_client", "place" "_limit_" "order",
        "LIVE_" "EXECUTION_" "ENABLED",
    )
    for tok in forbidden:
        assert tok not in src, tok


# ─── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_commands_registered():
    import main
    for name in ("contribution-plan-init", "contribution-plan-add",
                 "contribution-plan-status", "contribution-plan-report"):
        assert name in main._HANDLERS, name
    args = main._parse_args(["contribution-plan-status"])
    assert args.command == "contribution-plan-status"
    a2 = main._parse_args(["contribution-plan-init", "--weekly-rub", "50000",
                           "--monthly-rub", "200000", "--start-date", "2026-06-01"])
    assert a2.weekly_rub == "50000"
