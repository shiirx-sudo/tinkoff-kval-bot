"""
Тесты F4.8 portfolio_dashboard_data — read-only модель данных портфельного дашборда.

Никакой сети: read-only портфель/операции/дивиденды инъектируются провайдерами или
читаются из локальных F4.x отчётов. Проверяем partial-режим, включение старой
позиции T (27 шт.) как реальной, расчёт пассивного дохода/покрытия/gap, определение
оборота (buy+sell gross, не дивиденды), отдельный учёт комиссий, взносы, риск,
маскирование account и неиспользование live/sandbox токена.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from modules import portfolio_dashboard_data as pdd

ACCOUNT = "2000001918"
MASKED = "***1918"
T_UID = "87db07bc-0e02-4e29-90bb-05e8ef791d7b"
T_FIGI = "TCS80A107UL4"
NOW = __import__("datetime").datetime(2026, 6, 26, tzinfo=__import__(
    "datetime").timezone.utc)


# ── F4.x фикстуры (реальные значения T) ──

def _f43():
    return {"position_found": True, "ticker": "T", "order_id": "80578688754",
            "live_account_id_masked": MASKED, "instrument_uid": T_UID,
            "figi": T_FIGI, "class_code": "TQBR", "instrument_type": "share",
            "lot_size": 1, "position_quantity_units": 27.0,
            "average_position_price": 304.02, "current_price": 268.26,
            "current_position_value": 7243.02, "unrealized_pnl": -965.52,
            "currency": "rub"}


def _f44():
    return {"ticker": "T", "order_id": "80578688754", "fill_gross_amount": 276.08,
            "fill_commission_abs": 0.14, "fill_cash_outflow": 276.22,
            "fill_quantity_units": 1.0, "fill_datetime": "2026-06-24T06:36:25Z"}


def _f45():
    return {"new_fill_net_unrealized_pnl_after_commission": -7.96,
            "fill_cash_outflow": 276.22, "fill_quantity_units": 1.0}


def _f46():
    return {"instrument_uid": T_UID, "figi": T_FIGI,
            "reliable_income_data_found": True, "income_data_source": "api_known_future",
            "income_data_confidence": "high", "expected_dividend_per_unit_rub": 4.6,
            "expected_income_rub_yearly_total_position": 124.2,
            "expected_income_rub_monthly_total_position": 10.35,
            "income_target_coverage_pct_total_position": 0.0069,
            "next_known_income_event_date": "2026-08-24",
            "next_known_income_event_amount_per_unit": 4.6,
            "expected_income_rub_yearly_new_fill": 4.6,
            "expected_income_rub_monthly_new_fill": 0.38,
            "withholding_tax_assumption": None, "income_validation_passed": True}


_FILES = {
    "f41": "income_live_execution_report.json",
    "f42": "income_live_order_status_report.json",
    "f43": "income_live_position_report.json",
    "f44": "income_live_fill_attribution_report.json",
    "f45": "income_live_fill_economics_report.json",
    "f46": "income_live_income_validation_report.json",
}


def _write_reports(tmp: Path, data: dict) -> str:
    for key, payload in data.items():
        (tmp / _FILES[key]).write_text(json.dumps(payload, ensure_ascii=False),
                                       encoding="utf-8")
    return str(tmp)


def _quot(value, currency=None):
    units = int(value)
    nano = int(round((Decimal(str(value)) - units) * Decimal(10**9)))
    d = {"units": str(units), "nano": nano}
    if currency:
        d["currency"] = currency
    return d


def _run(tmp: Path, *, reports=None, **kw):
    reports = reports if reports is not None else {
        "f43": _f43(), "f44": _f44(), "f45": _f45(), "f46": _f46()}
    d = _write_reports(tmp, reports)
    kw.setdefault("read_token_present", False)
    kw.setdefault("contribution_plan_path", str(tmp / "no_plan.json"))
    return pdd.load_portfolio_dashboard_data(
        live_account_id=ACCOUNT, reports_dir=d, now=NOW, **kw)


# ─── partial mode / positions ─────────────────────────────────────────────────

def test_local_reports_only_partial_succeeds(tmp_path):
    rep = _run(tmp_path)
    assert rep["kind"] == "portfolio_dashboard_data"
    assert rep["stage"] == "F4_8_PORTFOLIO_DASHBOARD_DATA_READ_ONLY"
    assert rep["mode"] == "PORTFOLIO_DATA_READ_ONLY"
    assert rep["_exit_code"] == 0
    assert rep["data_freshness"]["overall"] == "partial"
    assert rep["portfolio_summary"]["partial"] is True


def test_known_T_position_included(tmp_path):
    rep = _run(tmp_path)
    pos = rep["positions"]
    assert len(pos) == 1
    t = pos[0]
    assert t["ticker"] == "T"
    assert t["quantity_units"] == Decimal("27")
    assert t["average_price"] == Decimal("304.02")
    assert t["current_price"] == Decimal("268.26")
    assert t["market_value_rub"] == Decimal("7243.02")
    assert t["unrealized_pnl_rub"] == Decimal("-965.52")
    assert t["expected_income_rub_yearly"] == Decimal("124.20")
    assert t["expected_income_rub_monthly"] == Decimal("10.35")
    assert t["next_income_event_date"] == "2026-08-24"
    assert t["income_data_source"] == "api_known_future"
    assert t["income_data_confidence"] == "high"
    assert t["weight_pct"] == Decimal("100.0000")


def test_old_position_27_is_real_portfolio_not_ignored(tmp_path):
    rep = _run(tmp_path)
    # вся позиция 27 шт. учитывается на уровне портфеля (не только 1 шт. сделки)
    assert rep["portfolio_summary"]["positions_value_rub"] == Decimal("7243.02")
    assert rep["portfolio_summary"]["positions_count"] == 1
    assert rep["positions"][0]["quantity_units"] == Decimal("27")


def test_last_trade_summarized_not_whole_portfolio(tmp_path):
    rep = _run(tmp_path)
    lt = rep["last_trade_audit_summary"]
    assert lt["last_tracked_trade_ticker"] == "T"
    assert lt["last_tracked_trade_quantity"] == Decimal("1")
    assert lt["last_tracked_trade_cash_outflow"] == Decimal("276.22")
    assert lt["last_tracked_trade_net_pnl_after_commission"] == Decimal("-7.96")
    # позиция (27) ≠ сделка (1): сделка не подменяет портфель
    assert rep["positions"][0]["quantity_units"] != lt["last_tracked_trade_quantity"]


# ─── income ───────────────────────────────────────────────────────────────────

def test_passive_income_from_reliable_f46(tmp_path):
    inc = _run(tmp_path)["income_summary"]
    assert inc["passive_income_rub_yearly_gross"] == Decimal("124.20")
    assert inc["passive_income_rub_monthly_gross"] == Decimal("10.35")


def test_income_coverage_uses_150000(tmp_path):
    inc = _run(tmp_path)["income_summary"]
    assert inc["target_monthly_income_rub"] == 150000
    assert inc["income_target_coverage_pct"] == Decimal("0.0069")


def test_income_gap_calculated(tmp_path):
    inc = _run(tmp_path)["income_summary"]
    assert inc["income_gap_rub_monthly"] == Decimal("149989.65")


def test_net_income_null_when_tax_unknown(tmp_path):
    inc = _run(tmp_path)["income_summary"]
    assert inc["passive_income_rub_monthly_net"] is None
    assert inc["passive_income_rub_yearly_net"] is None
    assert inc["income_net_estimation_available"] is False
    assert inc["income_tax_warning"]


def test_required_capital_with_explicit_assumption(tmp_path):
    inc = _run(tmp_path)["income_summary"]
    # 150000*12 / 10% = 18 000 000
    assert inc["required_capital_rub"] == Decimal("18000000.00")
    assert inc["required_capital_assumption_yield_pct"] == Decimal("10.0")


# ─── F4.11 income-to-target model ─────────────────────────────────────────────

def test_scheduled_income_equals_dividend_coupon_logic(tmp_path):
    inc = _run(tmp_path)["income_summary"]
    # scheduled = прежняя логика дивидендов/купонов (F4.6/F4.8)
    assert inc["scheduled_income_monthly_gross_rub"] == Decimal("10.35")
    assert inc["scheduled_income_yearly_gross_rub"] == Decimal("124.20")
    assert inc["scheduled_income_tax_warning"]
    assert inc["monthly_income_target_rub"] == 150000


def test_conservative_total_equals_scheduled_when_strategy_not_configured(tmp_path):
    inc = _run(tmp_path)["income_summary"]
    assert inc["strategy_income_status"] == "NOT_CONFIGURED"
    # стратегия не настроена → консервативный итог == scheduled gross
    assert inc["total_income_monthly_conservative_rub"] == Decimal("10.35")
    assert inc["target_coverage_conservative_pct"] == Decimal("0.0069")
    assert inc["income_gap_conservative_rub_monthly"] == Decimal("149989.65")


def test_strategy_income_placeholders_default(tmp_path):
    inc = _run(tmp_path)["income_summary"]
    assert inc["strategy_income_status"] == "NOT_CONFIGURED"
    assert inc["strategy_income_confidence"] == "none"
    assert inc["strategy_income_monthly_realized_net_rub"] == Decimal("0.00")
    assert inc["strategy_income_monthly_paper_rub"] is None
    assert inc["strategy_income_monthly_model_rub"] is None
    assert inc["strategy_income_included_in_conservative_coverage"] is False


def test_paper_model_not_in_conservative_coverage(tmp_path):
    inc = _run(tmp_path)["income_summary"]
    # paper/model = null → with_paper/with_model == conservative; покрытие совпадает
    assert inc["total_income_monthly_with_paper_rub"] == Decimal("10.35")
    assert inc["total_income_monthly_with_model_rub"] == Decimal("10.35")
    assert inc["target_coverage_with_paper_pct"] == inc[
        "target_coverage_conservative_pct"]
    assert inc["target_coverage_with_model_pct"] == inc[
        "target_coverage_conservative_pct"]
    assert inc["strategy_income_included_in_conservative_coverage"] is False


def test_legacy_passive_aliases_still_present(tmp_path):
    inc = _run(tmp_path)["income_summary"]
    # устаревшие алиасы сохранены и равны новым полям
    assert inc["passive_income_rub_monthly_gross"] == inc[
        "scheduled_income_monthly_gross_rub"]
    assert inc["passive_income_rub_yearly_gross"] == inc[
        "scheduled_income_yearly_gross_rub"]
    assert inc["target_monthly_income_rub"] == inc["monthly_income_target_rub"]
    assert inc["income_target_coverage_pct"] == inc[
        "target_coverage_conservative_pct"]
    assert inc["income_gap_rub_monthly"] == inc[
        "income_gap_conservative_rub_monthly"]


def test_strategy_warning_present_in_report_warnings(tmp_path):
    rep = _run(tmp_path)
    from modules.portfolio_dashboard_data import WARN_STRATEGY_INCOME
    assert WARN_STRATEGY_INCOME in rep["warnings"]


def test_income_summary_no_income_keeps_none_coverage(tmp_path):
    # портфель без надёжного дохода → scheduled None, покрытие None (не угадываем)
    rep = _run(tmp_path, reports={"f43": _f43()})  # без f46 → нет income
    inc = rep["income_summary"]
    assert inc["scheduled_income_monthly_gross_rub"] is None
    assert inc["total_income_monthly_conservative_rub"] is None
    assert inc["target_coverage_conservative_pct"] is None
    assert inc["income_gap_conservative_rub_monthly"] is None
    # legacy alias тоже None
    assert inc["income_target_coverage_pct"] is None


# ─── turnover ─────────────────────────────────────────────────────────────────

def _op(op_type, price, qty, commission, date, uid="U1", itype="share"):
    return {
        "id": f"op-{op_type}-{date}", "operationType": op_type,
        "instrumentType": itype, "instrumentUid": uid, "figi": "F1", "date": date,
        "payment": _quot(price * qty, "rub"),
        "commission": _quot(commission, "rub"),
        "tradesInfo": {"trades": [
            {"price": _quot(price), "quantity": str(qty), "num": "1"}]},
    }


def test_turnover_is_buy_sell_gross_not_dividends(tmp_path):
    ops = [
        _op("OPERATION_TYPE_BUY", 10, 100, 0.5, "2026-06-10T10:00:00Z"),
        _op("OPERATION_TYPE_SELL", 20, 50, 0.7, "2026-06-12T10:00:00Z"),
        _op("OPERATION_TYPE_DIVIDEND", 5, 27, 0, "2026-06-15T10:00:00Z"),
    ]
    rep = _run(tmp_path, operations_provider=lambda acc: ops)
    tn = rep["turnover_summary"]
    assert tn["turnover_definition"] == "sum_abs_buy_sell_gross_amount"
    # BUY 1000 + SELL 1000 = 2000; дивиденд НЕ учитывается
    assert tn["turnover_ytd_rub"] == Decimal("2000.00")
    assert tn["turnover_by_side"]["BUY"] == Decimal("1000.00")
    assert tn["turnover_by_side"]["SELL"] == Decimal("1000.00")


def test_commissions_tracked_separately_from_turnover(tmp_path):
    ops = [
        _op("OPERATION_TYPE_BUY", 10, 100, 0.5, "2026-06-10T10:00:00Z"),
        _op("OPERATION_TYPE_SELL", 20, 50, 0.7, "2026-06-12T10:00:00Z"),
    ]
    tn = _run(tmp_path, operations_provider=lambda acc: ops)["turnover_summary"]
    assert tn["turnover_ytd_rub"] == Decimal("2000.00")
    assert tn["commissions_ytd_rub"] == Decimal("1.20")  # 0.5 + 0.7, отдельно
    assert tn["commission_rate_pct_of_turnover"] is not None


def test_turnover_partial_from_known_T_buy_when_no_operations(tmp_path):
    tn = _run(tmp_path)["turnover_summary"]  # без operations_provider
    assert tn["turnover_partial"] is True
    assert tn["turnover_ytd_rub"] == Decimal("276.08")          # gross из F4.4
    assert tn["commissions_ytd_rub"] == Decimal("0.14")         # комиссия отдельно
    assert tn["turnover_by_side"]["BUY"] == Decimal("276.08")


def test_turnover_targets_present(tmp_path):
    tn = _run(tmp_path)["turnover_summary"]
    # F4.11: цель оборота = путь к квалинвестору (6M за trailing 4 квартала)
    assert tn["turnover_annual_target_rub"] == 6000000
    assert tn["turnover_monthly_target_rub"] == 500000
    assert tn["turnover_quarterly_target_rub"] == 1500000
    assert tn["kval_turnover_target_rub"] == 6000000
    assert tn["kval_turnover_period"] == "trailing_4_quarters"


# ─── F4.11 kval-турновер/частота (trailing 4 квартала) ────────────────────────

# окно trailing 4 квартала относительно NOW=2026-06-26 (Q2): 2025-07 … 2026-06
WINDOW_MONTHS = ([f"2025-{m:02d}" for m in range(7, 13)]
                 + [f"2026-{m:02d}" for m in range(1, 7)])


def _ops_each_month(months, per_month=4, price=10, qty=100):
    ops = []
    for m in months:
        for i in range(per_month):
            ops.append(_op("OPERATION_TYPE_BUY", price, qty, 0.1, f"{m}-1{i}T10:00:00Z"))
    return ops


def test_monthly_turnover_progress_uses_500k(tmp_path):
    ops = [_op("OPERATION_TYPE_BUY", 2500, 100, 0.5, "2026-06-10T10:00:00Z")]  # 250000
    tn = _run(tmp_path, operations_provider=lambda acc: ops)["turnover_summary"]
    assert tn["turnover_current_month_target_rub"] == 500000
    assert tn["turnover_current_month_rub"] == Decimal("250000.00")
    assert tn["turnover_current_month_progress_pct"] == Decimal("50.0000")
    assert tn["turnover_current_month_gap_rub"] == Decimal("250000.00")


def test_quarterly_turnover_progress_uses_1_5m(tmp_path):
    ops = [_op("OPERATION_TYPE_BUY", 2500, 100, 0.5, "2026-06-10T10:00:00Z")]  # 250000
    tn = _run(tmp_path, operations_provider=lambda acc: ops)["turnover_summary"]
    assert tn["turnover_current_quarter_target_rub"] == 1500000
    assert tn["turnover_current_quarter_rub"] == Decimal("250000.00")
    assert tn["turnover_current_quarter_progress_pct"] == Decimal("16.6667")


def test_trailing_4q_progress_uses_6m(tmp_path):
    ops = [_op("OPERATION_TYPE_BUY", 2500, 100, 0.5, "2026-06-10T10:00:00Z")]  # 250000
    tn = _run(tmp_path, operations_provider=lambda acc: ops)["turnover_summary"]
    assert tn["kval_turnover_target_rub"] == 6000000
    assert tn["kval_turnover_trailing_4q_rub"] == Decimal("250000.00")
    assert tn["kval_turnover_progress_pct"] == Decimal("4.1667")
    assert tn["kval_turnover_gap_rub"] == Decimal("5750000.00")


def test_trade_count_by_month_and_quarter(tmp_path):
    ops = [_op("OPERATION_TYPE_BUY", 10, 100, 0.5, "2026-06-10T10:00:00Z"),
           _op("OPERATION_TYPE_SELL", 20, 50, 0.7, "2026-06-12T10:00:00Z")]
    tn = _run(tmp_path, operations_provider=lambda acc: ops)["turnover_summary"]
    assert tn["trades_by_month"]["2026-06"] == 2
    assert tn["trades_by_quarter"]["2026-Q2"] == 2
    assert tn["kval_trade_count_trailing_4q"] == 2


def test_month_without_trades_flagged(tmp_path):
    ops = [_op("OPERATION_TYPE_BUY", 10, 100, 0.5, "2026-06-10T10:00:00Z")]
    tn = _run(tmp_path, operations_provider=lambda acc: ops)["turnover_summary"]
    # из 12 месяцев окна сделки только в июне → 11 месяцев без сделок
    assert tn["kval_months_without_trades"] == 11
    assert tn["kval_frequency_passed"] is False


def test_frequency_passes_with_enough_trades_every_month(tmp_path):
    ops = _ops_each_month(WINDOW_MONTHS, per_month=4)   # 48 сделок, ≥1/мес
    tn = _run(tmp_path, operations_provider=lambda acc: ops)["turnover_summary"]
    assert tn["kval_avg_trades_per_quarter"] == Decimal("12.00")  # 48/4
    assert tn["kval_months_without_trades"] == 0
    assert tn["kval_frequency_passed"] is True


def test_missing_month_fails_frequency(tmp_path):
    # пропускаем один месяц окна → частота не пройдена даже при высоком среднем
    ops = _ops_each_month([m for m in WINDOW_MONTHS if m != "2025-09"], per_month=4)
    tn = _run(tmp_path, operations_provider=lambda acc: ops)["turnover_summary"]
    assert tn["kval_avg_trades_per_quarter"] >= Decimal("10")
    assert tn["kval_months_without_trades"] >= 1
    assert tn["kval_frequency_passed"] is False


def test_incomplete_history_gives_partial_data(tmp_path):
    # нет operations (только F4.4) → PARTIAL_DATA, не ложная уверенность
    tn = _run(tmp_path)["turnover_summary"]
    assert tn["turnover_partial"] is True
    assert tn["kval_criteria_status"] == "PARTIAL_DATA"
    assert "operations_history_incomplete_for_kval_tracking" in tn["kval_warnings"]


def test_kval_criteria_status_not_passed_with_full_but_below(tmp_path):
    ops = [_op("OPERATION_TYPE_BUY", 10, 100, 0.5, "2026-06-10T10:00:00Z")]
    tn = _run(tmp_path, operations_provider=lambda acc: ops)["turnover_summary"]
    assert tn["kval_criteria_status"] == "NOT_PASSED"
    assert tn["kval_turnover_passed"] is False
    assert tn["kval_criteria_passed"] is False


def test_no_60m_hardcoded_in_modules():
    from pathlib import Path
    for mod in ("modules/portfolio_dashboard_data.py", "modules/portfolio_dashboard.py"):
        src = Path(mod).read_text(encoding="utf-8")
        assert "60000000" not in src, mod
        assert "60_000_000" not in src, mod


# ─── contributions ────────────────────────────────────────────────────────────

def test_contribution_plan_missing_disabled(tmp_path):
    rep = _run(tmp_path)
    cn = rep["contributions_summary"]
    assert cn["contributions_tracking_enabled"] is False
    assert "contribution_plan_not_configured" in cn["warnings"]
    assert "contribution_plan_not_configured" in rep["warnings"]


def test_contribution_plan_fixture_calculates_gaps(tmp_path):
    plan = {"enabled": True, "plan_weekly_rub": 50000, "plan_monthly_rub": 200000,
            "source": "manual", "next_planned_contribution_date": "2026-07-06",
            "facts": [{"date": "2026-06-08", "amount_rub": 50000},
                      {"date": "2026-06-15", "amount_rub": 50000}]}
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan), encoding="utf-8")
    rep = _run(tmp_path, contribution_plan_path=str(p))
    cn = rep["contributions_summary"]
    assert cn["contributions_tracking_enabled"] is True
    assert cn["contribution_plan_monthly_rub"] == Decimal("200000.00")
    assert cn["contribution_fact_monthly_rub"] == Decimal("100000.00")
    assert cn["contribution_gap_monthly_rub"] == Decimal("100000.00")
    assert cn["missed_contributions_count_month"] is not None


# ─── F4.10.1 API-based contribution facts ─────────────────────────────────────

def _cash_op(op_type, amount, date_str, op_id):
    return {"id": op_id, "operationType": op_type, "instrumentType": "",
            "date": date_str, "payment": _quot(amount, "rub"),
            "commission": _quot(0, "rub")}


def _api_plan(tmp_path, **over):
    plan = {"enabled": True, "currency": "rub", "plan_weekly_rub": 50000,
            "plan_monthly_rub": 200000, "plan_start_date": "2026-06-01",
            "next_planned_contribution_date": "2026-07-06", "source": "manual",
            "fact_source": "api_operations", "manual_facts_enabled": False,
            "facts": []}
    plan.update(over)
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan), encoding="utf-8")
    return str(p)


def test_f48_passes_operations_to_contribution_summary(tmp_path):
    plan_path = _api_plan(tmp_path)
    ops = [_cash_op("OPERATION_TYPE_INPUT", 200000, "2026-06-10T10:00:00Z", "d1"),
           _cash_op("OPERATION_TYPE_OUTPUT", -50000, "2026-06-11T10:00:00Z", "w1")]
    rep = _run(tmp_path, contribution_plan_path=plan_path,
               operations_provider=lambda acc: ops)
    cn = rep["contributions_summary"]
    assert cn["contribution_source"] == "readonly_operations_api"
    assert cn["contribution_data_quality"] == "full"
    assert cn["contribution_fact_monthly_rub"] == Decimal("200000.00")
    assert cn["withdrawal_fact_monthly_rub"] == Decimal("50000.00")
    assert cn["net_cash_flow_monthly_rub"] == Decimal("150000.00")
    assert cn["contribution_api_deposit_facts_count"] == 1


def test_f48_contribution_manual_fallback_without_operations(tmp_path):
    plan_path = _api_plan(tmp_path, facts=[{"date": "2026-06-10",
                                            "amount_rub": 120000}])
    rep = _run(tmp_path, contribution_plan_path=plan_path)  # без operations_provider
    cn = rep["contributions_summary"]
    assert cn["contribution_source"] == "manual_fallback"
    assert "contribution_api_operations_unavailable_manual_fallback" in rep["warnings"]
    assert cn["contribution_fact_monthly_rub"] == Decimal("120000.00")


def test_f48_report_contains_api_contribution_fields(tmp_path):
    plan_path = _api_plan(tmp_path)
    ops = [_cash_op("OPERATION_TYPE_INPUT", 200000, "2026-06-10T10:00:00Z", "d1")]
    rep = _run(tmp_path, contribution_plan_path=plan_path,
               operations_provider=lambda acc: ops)
    cn = rep["contributions_summary"]
    for key in ("contribution_source", "contribution_data_quality",
                "contribution_fact_source_preferred",
                "contribution_api_deposit_facts_count",
                "contribution_api_withdrawal_facts_count",
                "withdrawal_fact_monthly_rub", "net_cash_flow_monthly_rub",
                "last_contribution_date", "last_contribution_amount_rub",
                "contribution_facts_preview", "contribution_plan_started",
                "days_until_plan_start"):
        assert key in cn, key


def test_f48_contribution_pre_start_not_behind(tmp_path):
    plan_path = _api_plan(tmp_path, plan_start_date="2027-01-01")  # будущий старт
    ops = [_cash_op("OPERATION_TYPE_INPUT", 5000, "2026-06-10T10:00:00Z", "d1")]
    rep = _run(tmp_path, contribution_plan_path=plan_path,
               operations_provider=lambda acc: ops)
    cn = rep["contributions_summary"]
    assert cn["contribution_status"] == "NOT_STARTED"
    assert cn["contribution_plan_started"] is False
    assert cn["contribution_gap_monthly_rub"] == Decimal("0.00")
    assert cn["missed_contributions_count_month"] == 0


# ─── risk ─────────────────────────────────────────────────────────────────────

def test_risk_summary_weights_and_cash(tmp_path):
    rk = _run(tmp_path)["risk_summary"]
    assert rk["top_position_weight_pct"] == Decimal("100.0000")
    assert rk["negative_pnl_positions_count"] == 1
    assert rk["risk_data_quality"] == "partial"
    assert rk["concentration_warnings"]  # 100% → концентрация


def test_missing_cash_total_null_no_crash(tmp_path):
    rep = _run(tmp_path)  # partial: кэша нет
    assert rep["portfolio_summary"]["cash_rub"] is None
    assert rep["portfolio_summary"]["cash_pct"] is None
    assert rep["cash_summary"]["cash_rub"] is None


# ─── token-present enrichment via injected portfolio ──────────────────────────

def _portfolio_raw():
    return {
        "totalAmountPortfolio": _quot(50000, "rub"),
        "totalAmountCurrencies": _quot(10000, "rub"),
        "expectedYield": _quot(5, "rub"),
        "positions": [
            {"instrumentType": "share", "ticker": "T", "figi": T_FIGI,
             "instrumentUid": T_UID, "quantity": _quot(27),
             "averagePositionPrice": _quot(304.02, "rub"),
             "currentPrice": _quot(268.26, "rub")},
            {"instrumentType": "share", "ticker": "SBER", "figi": "BBG", "name": "Sber",
             "instrumentUid": "U2", "quantity": _quot(100),
             "averagePositionPrice": _quot(250, "rub"),
             "currentPrice": _quot(300, "rub")},
            {"instrumentType": "currency", "ticker": "RUB", "figi": "",
             "instrumentUid": "RUBUID", "quantity": _quot(10000),
             "averagePositionPrice": _quot(1, "rub"),
             "currentPrice": _quot(1, "rub")},
        ],
    }


def test_token_present_portfolio_enrichment_no_live_sandbox(tmp_path):
    rep = _run(tmp_path, read_token_present=True,
               portfolio_provider=lambda acc: _portfolio_raw())
    pf = rep["portfolio_summary"]
    assert pf["partial"] is False
    assert pf["portfolio_source"] == "readonly_portfolio_api"
    assert pf["total_portfolio_value_rub"] == Decimal("50000.00")
    assert pf["cash_rub"] == Decimal("10000.00")
    # currency-позиция исключена из positions; две реальные позиции
    tickers = {p["ticker"] for p in rep["positions"]}
    assert tickers == {"T", "SBER"}
    assert rep["data_freshness"]["portfolio_api"] == "live"
    # live/sandbox токен не используется
    tp = rep["token_policy"]
    assert tp["live_token_used"] is False and tp["sandbox_token_used"] is False
    assert rep["guards"]["live_token_used"] is False


def test_token_absent_no_network(tmp_path):
    calls = {"n": 0}

    def watchdog(acc):
        calls["n"] += 1
        raise AssertionError("network must not be called")

    rep = _run(tmp_path, client=None, portfolio_provider=None,
               operations_provider=None, read_token_present=False)
    assert calls["n"] == 0
    assert rep["_exit_code"] == 0
    assert rep["token_policy"]["read_only_token_present"] is False


# ─── KPI / safety / report ────────────────────────────────────────────────────

def test_dashboard_kpi_present(tmp_path):
    kpi = _run(tmp_path)["dashboard_kpi"]
    assert kpi["portfolio_value_rub"] == Decimal("7243.02")
    assert kpi["passive_income_monthly_rub"] == Decimal("10.35")
    assert kpi["passive_income_coverage_pct"] == Decimal("0.0069")
    assert kpi["safety_status"] == "READ_ONLY_SAFE"


def test_account_masked(tmp_path):
    rep = _run(tmp_path)
    assert rep["live_account_id_masked"] == MASKED
    assert ACCOUNT not in json.dumps(rep, default=str)


def test_no_token_value_leaks(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKOFF_TOKEN", "READ-SECRET")
    monkeypatch.setenv("TINKOFF_LIVE_TRADING_TOKEN", "LIVE-SECRET")
    monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "SANDBOX-SECRET")
    rep = _run(tmp_path)
    js = json.dumps(rep, default=str)
    for secret in ("READ-SECRET", "LIVE-SECRET", "SANDBOX-SECRET"):
        assert secret not in js


def test_guards_all_safe(tmp_path):
    g = _run(tmp_path)["guards"]
    assert g[pdd.GUARD_LIVE_ORDER_SENT] is False
    assert g["post_order_called"] is False
    assert g[pdd.GUARD_CANCEL_CALLED] is False
    for key in ("sell_order_sent", "market_order_used", "retry_execution",
                "portfolio_mutated", "config_mutated", "telegram_sent",
                "live_token_used", "sandbox_token_used", "token_printed"):
        assert g[key] is False


def test_token_policy_no_live_no_sandbox(tmp_path):
    tp = _run(tmp_path)["token_policy"]
    assert tp["read_only_token_env"] == "TINKOFF_TOKEN"
    assert tp["live_trading_token_env"] == "TINKOFF_LIVE_TRADING_TOKEN"
    assert tp["live_trading_token_required"] is False
    assert tp["live_token_used"] is False
    assert tp["sandbox_token_used"] is False
    assert tp["token_printed"] is False


def test_run_writes_reports_and_md(tmp_path):
    d = _write_reports(tmp_path, {"f43": _f43(), "f44": _f44(), "f45": _f45(),
                                  "f46": _f46()})
    rep = pdd.run(
        live_account_id=ACCOUNT, reports_dir=d, now=NOW,
        output_json=str(tmp_path / "pdd.json"), output_md=str(tmp_path / "pdd.md"),
        contribution_plan_path=str(tmp_path / "no_plan.json"),
        read_token_present=False)
    assert Path(rep["_output_json"]).exists()
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "F4.8" in md
    assert "Dashboard KPI" in md          # KPI-сводка для будущего дашборда
    assert "READ_ONLY_SAFE" in md
    data = json.loads(Path(rep["_output_json"]).read_text(encoding="utf-8"))
    for key in ("kind", "stage", "mode", "live_account_id_masked",
                "data_sources_used", "data_freshness", "portfolio_summary",
                "positions", "cash_summary", "income_summary", "turnover_summary",
                "contributions_summary", "risk_summary", "last_trade_audit_summary",
                "dashboard_kpi", "guards", "token_policy"):
        assert key in data, key


def test_required_account_id(tmp_path):
    with pytest.raises(pdd.PortfolioDashboardError):
        pdd.run(live_account_id="", reports_dir=str(tmp_path),
                output_json=str(tmp_path / "x.json"),
                output_md=str(tmp_path / "x.md"))


def test_default_output_paths():
    assert pdd.DEFAULT_OUTPUT_JSON == "data/reports/portfolio_dashboard_data.json"
    assert pdd.DEFAULT_OUTPUT_MD == "data/reports/portfolio_dashboard_data.md"


def test_cli_registers_portfolio_dashboard_data():
    import main
    args = main._parse_args(["portfolio-dashboard-data",
                             "--live-account-id", ACCOUNT])
    assert args.command == "portfolio-dashboard-data"
    assert "portfolio-dashboard-data" in main._HANDLERS


# ─── статическая проверка: нет запрещённых литералов ──────────────────────────

def test_module_source_has_no_forbidden_literals():
    src = Path(pdd.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service", "post" "Order(", "cancel" "Order(",
        "place" "_order", "submit" "_order", "cancel" "_order",
        "live" "_order", "order" "_client", "place" "_limit_" "order",
        "LIVE_" "EXECUTION_" "ENABLED",
    )
    for tok in forbidden:
        assert tok not in src, tok
