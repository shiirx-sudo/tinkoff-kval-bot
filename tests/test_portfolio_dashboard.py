"""
Тесты F4.9 portfolio_dashboard — локальный read-only портфельный кокпит.

Чистые функции (load/sanitize/build) тестируются без сервера; маршрутизация и
привязка к 127.0.0.1 — через loopback-сервер на порту 0 (без интернета).
Проверяем рендер F4.8, дружелюбную страницу при отсутствии отчёта, KPI/интерпретацию,
таблицу позиций, доход/оборот/взносы/риск, вторичность последней сделки, сырой JSON,
маскирование account, редактирование токенов, отсутствие брокера/токенов/POST/действий.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from modules import portfolio_dashboard as pdash

MASKED = "***1918"


def _f48():
    """Срез реального отчёта F4.8 (9 позиций, READ_ONLY_SAFE)."""
    positions = [
        {"ticker": "T", "name": "T-Tech", "quantity_units": 27.0,
         "current_price": 268.26, "market_value_rub": 7243.02, "weight_pct": 28.2312,
         "unrealized_pnl_rub": -965.52, "unrealized_pnl_pct": -11.76,
         "expected_income_rub_yearly": 124.2, "expected_income_rub_monthly": 10.35,
         "next_income_event_date": "2026-08-24", "income_data_source": "api_known_future",
         "income_data_confidence": "high"},
    ]
    # добиваем до 9 позиций (8 из них в минусе)
    for i in range(8):
        positions.append({
            "ticker": f"P{i}", "name": f"Pos {i}", "quantity_units": 10.0,
            "current_price": 100.0, "market_value_rub": 1000.0,
            "weight_pct": round((84.654 - 28.2312) / 8, 4),
            "unrealized_pnl_rub": -250.0, "unrealized_pnl_pct": -20.0,
            "expected_income_rub_yearly": None, "expected_income_rub_monthly": None,
            "next_income_event_date": None, "income_data_source": None,
            "income_data_confidence": None})
    return {
        "kind": "portfolio_dashboard_data",
        "stage": "F4_8_PORTFOLIO_DASHBOARD_DATA_READ_ONLY",
        "generated_at": "2026-06-26T16:00:00+00:00",
        "live_account_id_masked": MASKED,
        "data_freshness": {"overall": "full", "portfolio_api": "live",
                           "operations_api": "live"},
        "portfolio_summary": {
            "total_portfolio_value_rub": 26298.74, "positions_value_rub": 25736.40,
            "cash_rub": 562.34, "cash_pct": 2.1383, "unrealized_pnl_rub": -2970.94,
            "unrealized_pnl_pct": -10.3491, "positions_count": 9, "currency": "rub",
            "portfolio_source": "readonly_portfolio_api", "partial": False},
        "positions": positions,
        "cash_summary": {"cash_rub": 562.34, "cash_pct": 2.1383, "currency": "rub",
                         "partial": False},
        "income_summary": {
            # F4.11 income-to-target model
            "monthly_income_target_rub": 150000,
            "income_target_indexed_rub": 150000.0,
            "scheduled_income_monthly_gross_rub": 26.53,
            "scheduled_income_yearly_gross_rub": 318.40,
            "scheduled_income_monthly_net_rub": None,
            "scheduled_income_yearly_net_rub": None,
            "scheduled_income_tax_warning":
                "Налоговый режим неизвестен — net не считаем.",
            "scheduled_income_sources": {"api_known_future": {"yearly_rub": 318.40}},
            "strategy_income_monthly_realized_net_rub": 0.0,
            "strategy_income_monthly_paper_rub": None,
            "strategy_income_monthly_model_rub": None,
            "strategy_income_status": "NOT_CONFIGURED",
            "strategy_income_confidence": "none",
            "strategy_income_included_in_conservative_coverage": False,
            "total_income_monthly_conservative_rub": 26.53,
            "total_income_monthly_with_paper_rub": 26.53,
            "total_income_monthly_with_model_rub": 26.53,
            "target_coverage_conservative_pct": 0.0177,
            "target_coverage_with_paper_pct": 0.0177,
            "target_coverage_with_model_pct": 0.0177,
            "income_gap_conservative_rub_monthly": 149973.47,
            "income_strategy_warning":
                "Доход стратегии учитывается только реализованным и за вычетом "
                "комиссий. Paper/model-оценки показаны отдельно и не гарантированы.",
            "required_capital_rub": 18000000.0,
            "required_capital_assumption_yield_pct": 10.0,
            "required_capital_gap_rub": 17973701.26,
            "income_sources_breakdown": {"api_known_future": {"yearly_rub": 318.40}},
            "income_calendar_monthly": {"2026-08": 124.2},
            "next_income_events": [
                {"date": "2026-08-24", "ticker": "T", "type": "dividend",
                 "amount_total_rub": 124.2}],
            # legacy-алиасы (устаревшие)
            "passive_income_rub_monthly_gross": 26.53,
            "passive_income_rub_yearly_gross": 318.40,
            "passive_income_rub_monthly_net": None,
            "passive_income_rub_yearly_net": None,
            "income_net_estimation_available": False,
            "income_tax_warning": "Налоговый режим неизвестен — net не считаем.",
            "target_monthly_income_rub": 150000,
            "income_target_coverage_pct": 0.0177,
            "income_gap_rub_monthly": 149973.47},
        "turnover_summary": {
            "turnover_definition": "sum_abs_buy_sell_gross_amount",
            "turnover_partial": False, "turnover_ytd_rub": 34669.88,
            "turnover_mtd_rub": 34669.88, "turnover_qtd_rub": 34669.88,
            "turnover_annual_target_rub": 6000000,
            "turnover_monthly_target_rub": 500000,
            "turnover_quarterly_target_rub": 1500000,
            "turnover_ytd_plan_to_date_rub": 953424.66,
            "turnover_ytd_gap_rub": 918754.78, "turnover_ytd_progress_pct": 0.5778,
            "commissions_ytd_rub": 62.26, "commission_rate_pct_of_turnover": 0.1796,
            "turnover_current_month_rub": 34669.88,
            "turnover_current_month_target_rub": 500000,
            "turnover_current_month_progress_pct": 6.9340,
            "turnover_current_month_gap_rub": 465330.12,
            "turnover_current_quarter_rub": 34669.88,
            "turnover_current_quarter_target_rub": 1500000,
            "turnover_current_quarter_progress_pct": 2.3113,
            "turnover_current_quarter_gap_rub": 1465330.12,
            "turnover_daily_required_month_rub": 232665.06,
            "turnover_daily_required_quarter_rub": 25264.31,
            "turnover_by_side": {"BUY": 20000.0, "SELL": 14669.88},
            "turnover_by_instrument": {"T": 276.08, "P0": 34393.80},
            "turnover_by_month": {"2026-06": 34669.88},
            "turnover_by_quarter": {"2026-Q2": 34669.88},
            "trades_by_month": {"2026-06": 4}, "trades_by_quarter": {"2026-Q2": 4},
            "kval_turnover_target_rub": 6000000,
            "kval_turnover_period": "trailing_4_quarters",
            "kval_turnover_trailing_4q_rub": 34669.88,
            "kval_turnover_progress_pct": 0.5778,
            "kval_turnover_gap_rub": 5965330.12,
            "kval_quarters_checked": 4, "kval_months_checked": 12,
            "kval_trade_count_trailing_4q": 4,
            "kval_trade_count_by_quarter": {"2025-Q3": 0, "2025-Q4": 0,
                                            "2026-Q1": 0, "2026-Q2": 4},
            "kval_trade_count_by_month": {"2026-06": 4},
            "kval_avg_trades_per_quarter": 1.0,
            "kval_min_trades_per_quarter_required": 10,
            "kval_monthly_activity_required": True,
            "kval_months_without_trades": 11,
            "kval_frequency_passed": False, "kval_turnover_passed": False,
            "kval_criteria_passed": False, "kval_criteria_status": "NOT_PASSED",
            "kval_warnings": []},
        "target_path_summary": {
            "monthly_income_target_rub": 150000.0,
            "annual_income_target_rub": 1800000.0,
            "current_capital_rub": 26298.74,
            "current_planned_monthly_contribution_rub": 8000.0,
            "model": "simple_no_growth_no_return",
            "yield_scenarios": [
                {"yield_pct": 8.0, "required_capital_rub": 22500000.0,
                 "capital_gap_rub": 22473701.26,
                 "months_to_target_at_current_contribution": 2809.2,
                 "years_to_target_at_current_contribution": 234.1,
                 "required_monthly_contribution_3y_rub": 624269.48,
                 "required_monthly_contribution_5y_rub": 374561.69,
                 "required_monthly_contribution_10y_rub": 187280.84,
                 "required_monthly_contribution_15y_rub": 124853.90},
                {"yield_pct": 10.0, "required_capital_rub": 18000000.0,
                 "capital_gap_rub": 17973701.26,
                 "months_to_target_at_current_contribution": 2246.7,
                 "years_to_target_at_current_contribution": 187.2,
                 "required_monthly_contribution_3y_rub": 499269.48,
                 "required_monthly_contribution_5y_rub": 299561.69,
                 "required_monthly_contribution_10y_rub": 149780.84,
                 "required_monthly_contribution_15y_rub": 99853.90},
                {"yield_pct": 12.0, "required_capital_rub": 15000000.0,
                 "capital_gap_rub": 14973701.26,
                 "months_to_target_at_current_contribution": 1871.7,
                 "years_to_target_at_current_contribution": 156.0,
                 "required_monthly_contribution_3y_rub": 415936.15,
                 "required_monthly_contribution_5y_rub": 249561.69,
                 "required_monthly_contribution_10y_rub": 124780.84,
                 "required_monthly_contribution_15y_rub": 83187.23},
                {"yield_pct": 15.0, "required_capital_rub": 12000000.0,
                 "capital_gap_rub": 11973701.26,
                 "months_to_target_at_current_contribution": 1496.7,
                 "years_to_target_at_current_contribution": 124.7,
                 "required_monthly_contribution_3y_rub": 332602.81,
                 "required_monthly_contribution_5y_rub": 199561.69,
                 "required_monthly_contribution_10y_rub": 99780.84,
                 "required_monthly_contribution_15y_rub": 66520.56},
                {"yield_pct": 18.0, "required_capital_rub": 10000000.0,
                 "capital_gap_rub": 9973701.26,
                 "months_to_target_at_current_contribution": 1246.7,
                 "years_to_target_at_current_contribution": 103.9,
                 "required_monthly_contribution_3y_rub": 277047.26,
                 "required_monthly_contribution_5y_rub": 166228.35,
                 "required_monthly_contribution_10y_rub": 83114.18,
                 "required_monthly_contribution_15y_rub": 55409.45}],
            "warnings": ["target_path_simple_model_no_growth_no_return",
                         "target_path_not_investment_advice"]},
        "contributions_summary": {
            "contributions_tracking_enabled": False,
            "contribution_plan_weekly_rub": None,
            "contribution_plan_monthly_rub": None,
            "contribution_fact_monthly_rub": None,
            "contribution_gap_monthly_rub": None,
            "missed_contributions_count_month": None,
            "next_planned_contribution_date": None,
            "contribution_required_to_catch_up_rub": None, "warnings": [
                "contribution_plan_not_configured"]},
        "risk_summary": {
            "top_position_weight_pct": 28.2312, "top_5_positions_weight_pct": 84.654,
            "cash_pct": 2.1383, "concentration_warnings": [], "cash_warnings": [],
            "negative_pnl_positions_count": 8, "portfolio_unrealized_pnl_rub": -2970.94,
            "portfolio_unrealized_pnl_pct": -10.3491, "risk_data_quality": "full"},
        "last_trade_audit_summary": {
            "last_tracked_trade_ticker": "T", "last_tracked_trade_order_id": "80578688754",
            "last_tracked_trade_quantity": 1.0,
            "last_tracked_trade_cash_outflow": 276.22,
            "last_tracked_trade_net_pnl_after_commission": -7.96,
            "last_tracked_trade_income_yearly": 4.6,
            "last_tracked_trade_income_monthly": 0.38,
            "last_tracked_trade_audit_passed": True},
        "dashboard_kpi": {
            "portfolio_value_rub": 26298.74, "cash_rub": 562.34, "cash_pct": 2.1383,
            "passive_income_monthly_rub": 26.53, "passive_income_target_rub": 150000,
            "passive_income_coverage_pct": 0.0177, "income_gap_rub_monthly": 149973.47,
            "turnover_ytd_rub": 34669.88, "turnover_annual_target_rub": 6000000,
            "turnover_ytd_progress_pct": 0.5778, "turnover_gap_rub": 918754.78,
            "kval_turnover_trailing_4q_rub": 34669.88,
            "kval_turnover_target_rub": 6000000, "kval_turnover_progress_pct": 0.5778,
            "kval_turnover_gap_rub": 5965330.12, "kval_criteria_status": "NOT_PASSED",
            "portfolio_unrealized_pnl_rub": -2970.94,
            "portfolio_unrealized_pnl_pct": -10.3491, "safety_status": "READ_ONLY_SAFE"},
        "guards": {"live_order_sent": False, "post_order_called": False,
                   "cancel_order_called": False, "live_token_used": False,
                   "sandbox_token_used": False, "token_printed": False},
        "token_policy": {"read_only_token_env": "TINKOFF_TOKEN",
                         "live_token_used": False, "sandbox_token_used": False,
                         "token_printed": False},
        "warnings": ["contribution_plan_not_configured"], "errors": [],
    }


def _write(tmp: Path, data=None) -> str:
    p = tmp / "portfolio_dashboard_data.json"
    if data is not None:
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _html(tmp: Path, data=None) -> str:
    data = _f48() if data is None else data
    state = pdash.load_portfolio_dashboard_report(_write(tmp, data))
    return pdash.build_portfolio_dashboard_html(state)


# ─── load + build ─────────────────────────────────────────────────────────────

def test_loads_f48_and_builds_state(tmp_path):
    state = pdash.load_portfolio_dashboard_report(_write(tmp_path, _f48()))
    assert state["_report_present"] is True
    assert len(state["positions"]) == 9
    html = pdash.build_portfolio_dashboard_html(state)
    assert "Portfolio cockpit" in html and "<html" in html.lower()


def test_missing_report_friendly_page_no_crash(tmp_path):
    state = pdash.load_portfolio_dashboard_report(str(tmp_path / "nope.json"))
    assert state["_report_present"] is False
    html = pdash.build_portfolio_dashboard_html(state)
    assert "Portfolio cockpit" in html
    assert "F4.8" in html
    assert "portfolio-dashboard-data" in html  # подсказка как починить


# ─── KPI / interpretation ─────────────────────────────────────────────────────

def test_kpi_strip_contains_key_metrics(tmp_path):
    html = _html(tmp_path)
    assert 'class="kpis"' in html
    for label in ("Стоимость портфеля", "Свободный кэш", "Доход к цели / мес.",
                  "Покрытие цели 150 000 ₽/мес.", "Квал-оборот 4Q",
                  "PnL портфеля", "Взносы", "Безопасность"):
        assert label in html, label
    assert "Пассивный доход / мес." not in html   # F4.11: больше не верхний уровень
    assert "26 298.74 ₽" in html       # стоимость портфеля
    assert "READ_ONLY_SAFE" in html


def test_interpretation_block_target_and_coverage(tmp_path):
    html = _html(tmp_path)
    assert "Что сейчас" in html
    assert "150 000 ₽/мес." in html
    assert "покрывает только" in html
    assert "0.0177%" in html
    assert "не торговая рекомендация" in html.lower() or \
        "НЕ торговая рекомендация" in html


# ─── sections ─────────────────────────────────────────────────────────────────

def test_positions_table_renders_all(tmp_path):
    html = _html(tmp_path)
    assert "B · Позиции (9)" in html
    assert ">T<" in html or ">T " in html or "T-Tech" in html
    for i in range(8):
        assert f"Pos {i}" in html


def test_income_section_renders(tmp_path):
    html = _html(tmp_path)
    assert "C · Доход к цели" in html
    assert "C · Пассивный доход" not in html      # F4.11 переименование
    assert "318.40 ₽" in html          # scheduled годовой брутто
    assert "150 000" in html
    assert "0.0177%" in html
    assert "149 973.47 ₽" in html      # gap (консервативный)
    assert "Налоговый режим неизвестен" in html  # tax warning (net недоступен)


def test_target_path_section_renders(tmp_path):
    html = _html(tmp_path)
    assert "D · Путь к цели" in html
    assert "Нужный капитал" in html
    assert "Простая модель" in html
    # ключевые русские заголовки таблицы
    for label in ("Доходность", "Не хватает", "Лет при текущем взносе",
                  "Взнос/мес. для 5 лет", "Взнос/мес. для 10 лет",
                  "Взнос/мес. для 15 лет"):
        assert label in html, label
    # цель и модель
    assert "150 000.00 ₽" in html          # цель/мес.
    assert "1 800 000.00 ₽" in html        # цель/год
    # дисклеймер: не прогноз / не рекомендация
    assert "не прогноз доходности и не инвестиционная рекомендация" in html
    # нав-якорь
    assert 'href="#targetpath"' in html and 'id="targetpath"' in html


def test_income_scheduled_and_strategy_shown_separately(tmp_path):
    html = _html(tmp_path)
    assert "Доход к цели / мес." in html          # KPI (тест #7)
    assert "Scheduled income" in html             # дивиденды/купоны
    assert "Strategy income" in html              # бот/стратегия
    assert "Реализованный net" in html
    assert "не настроен" in html                  # strategy NOT_CONFIGURED
    # paper/model подписаны как исключённые из покрытия
    assert "исключено из покрытия" in html
    # дисклеймер о стратегии (реализованный net, paper/model отдельно)
    assert "Paper/model-оценки показаны отдельно и не гарантированы" in html


def test_turnover_section_buy_sell_gross_not_dividends(tmp_path):
    html = _html(tmp_path)
    assert "D · Оборот" in html
    assert "sum_abs_buy_sell_gross_amount" in html
    assert "buy+sell gross" in html.lower()
    assert "НЕ дивиденды" in html
    assert "34 669.88 ₽" in html


def test_dashboard_uses_6m_trailing_4q_not_60m(tmp_path):
    html = _html(tmp_path)
    assert "60M" not in html
    assert "6M за 4 квартала" in html
    assert "Квал-оборот 4Q" in html                  # KPI label
    assert "Квал-оборот к цели 6M за 4 квартала" in html  # progress bar


def test_turnover_section_kval_tracker(tmp_path):
    html = _html(tmp_path)
    # цели месяца/квартала и trailing 4Q
    assert "Текущий месяц" in html and "Текущий квартал" in html
    assert "Квал-оборот 4Q" in html
    # частота сделок
    assert "Сделок за 4 квартала" in html
    assert "Месяцев без сделок" in html
    assert "Критерии квалинвестора" in html
    # бейдж pass/fail/partial и дисклеймер (не сертификация)
    assert "NOT_PASSED" in html
    assert "НЕ юридическая сертификация" in html


def test_contributions_disabled_warning(tmp_path):
    html = _html(tmp_path)
    assert "E · Взносы" in html
    assert "data/config/contribution_plan.json" in html
    assert "config/contribution_plan.example.json" in html


def test_contributions_api_source_block(tmp_path):
    data = _f48()
    data["contributions_summary"] = {
        "contributions_tracking_enabled": True,
        "contribution_source": "readonly_operations_api",
        "contribution_data_quality": "full",
        "contribution_plan_started": True,
        "days_until_plan_start": 0,
        "contribution_plan_weekly_rub": 50000, "contribution_plan_monthly_rub": 200000,
        "contribution_fact_weekly_rub": 50000, "contribution_fact_monthly_rub": 200000,
        "contribution_gap_monthly_rub": 0, "missed_contributions_count_month": 0,
        "contribution_api_deposit_facts_count": 4, "contribution_manual_facts_count": 0,
        "contribution_api_withdrawal_facts_count": 1,
        "withdrawal_fact_monthly_rub": 50000, "withdrawal_fact_ytd_rub": 50000,
        "net_cash_flow_monthly_rub": 150000, "net_cash_flow_ytd_rub": 150000,
        "last_contribution_date": "2026-06-22", "last_contribution_amount_rub": 50000,
        "next_planned_contribution_date": "2026-07-06",
        "contribution_required_to_catch_up_rub": 0, "warnings": []}
    html = _html(tmp_path, data)
    assert "E · Взносы" in html
    assert "Источник факта" in html
    assert "API операций (read-only)" in html
    assert "Качество данных" in html
    assert "Последний взнос" in html
    assert "2026-06-22" in html
    assert "Net cash flow" in html


def test_contributions_manual_fallback_caution(tmp_path):
    data = _f48()
    cn = dict(data["contributions_summary"])
    cn.update({"contributions_tracking_enabled": True,
               "contribution_source": "manual_fallback",
               "contribution_data_quality": "manual_fallback",
               "contribution_plan_started": True,
               "contribution_plan_weekly_rub": 50000})
    data["contributions_summary"] = cn
    html = _html(tmp_path, data)
    assert "ручной fallback" in html
    assert "операции API недоступны" in html


def test_risk_section_concentration_and_negatives(tmp_path):
    html = _html(tmp_path)
    assert "F · Риск" in html
    assert "84.65" in html             # топ-5 концентрация
    assert "8" in html                 # позиций в минусе
    # пороги отображения: топ-5 >= 70%, >=50% в минусе, кэш < 5%
    assert "пороги дашборда" in html


def test_last_trade_secondary_not_whole_portfolio(tmp_path):
    html = _html(tmp_path)
    assert "G · Последняя сделка" in html
    assert "80578688754" in html
    assert "не весь портфель" in html.lower() or "НЕ весь портфель" in html


def test_raw_json_collapsed_by_default(tmp_path):
    html = _html(tmp_path)
    assert "H · Сырой отчёт F4.8" in html
    assert "<details open" not in html
    assert "<details>" in html


# ─── well-formedness ──────────────────────────────────────────────────────────

def test_html_well_formed_no_nested_cards(tmp_path):
    from html.parser import HTMLParser
    html = _html(tmp_path)
    assert html.count("<table") == html.count("</table>")

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.bal = 0
            self.nested = False
            self.maxd = 0

        def handle_starttag(self, tag, attrs):
            if tag != "div":
                return
            card = "card" in (dict(attrs).get("class", "") or "").split()
            if card and any(self.stack):
                self.nested = True
            self.stack.append(card)
            self.bal += 1
            self.maxd = max(self.maxd, sum(self.stack))

        def handle_endtag(self, tag):
            if tag == "div":
                self.bal -= 1
                if self.stack:
                    self.stack.pop()

    p = _P()
    p.feed(html)
    assert p.bal == 0
    assert p.nested is False
    assert p.maxd == 1


def test_no_external_resources(tmp_path):
    html = _html(tmp_path)
    assert "https://" not in html
    assert "<script src" not in html.lower()
    assert "cdn" not in html.lower()


# ─── masking / redaction / token safety ───────────────────────────────────────

def test_account_masked_no_raw_id(tmp_path):
    data = _f48()
    data["raw_account_id"] = "2000001918"  # сырой id (защитное маскирование)
    html = _html(tmp_path, data)
    state = pdash.sanitize_portfolio_dashboard_state(
        pdash.load_portfolio_dashboard_report(_write(tmp_path, data)))
    assert "2000001918" not in html
    assert state["raw_account_id"] == "***1918"


def test_token_like_values_redacted(tmp_path):
    leak = "t.abcdEFGH1234567890_klmnopqrstuvwxyz"
    data = _f48()
    data["leak"] = leak
    html = _html(tmp_path, data)
    state = pdash.sanitize_portfolio_dashboard_state(
        pdash.load_portfolio_dashboard_report(_write(tmp_path, data)))
    assert leak not in html
    assert "***REDACTED***" in state["leak"]


def test_no_env_token_read(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKOFF_TOKEN", "READ-SECRET")
    monkeypatch.setenv("TINKOFF_LIVE_TRADING_TOKEN", "LIVE-SECRET")
    monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "SANDBOX-SECRET")
    html = _html(tmp_path)
    for secret in ("READ-SECRET", "LIVE-SECRET", "SANDBOX-SECRET"):
        assert secret not in html


def test_module_does_not_import_broker():
    src = Path(pdash.__file__).read_text(encoding="utf-8")
    assert "ReadOnlyClient" not in src
    assert "TINKOFF_TOKEN" not in src
    assert "rest_client" not in src
    assert "import requests" not in src


def test_module_source_has_no_forbidden_literals():
    src = Path(pdash.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service", "post" "Order(", "cancel" "Order(",
        "place" "_order", "submit" "_order", "cancel" "_order",
        "live" "_order", "order" "_client", "place" "_limit_" "order",
        "LIVE_" "EXECUTION_" "ENABLED",
    )
    for tok in forbidden:
        assert tok not in src, tok


# ─── loopback server ──────────────────────────────────────────────────────────

@contextmanager
def _server(report_path):
    handler = pdash.make_handler(report_path, pdash.DEFAULT_HOST)
    httpd = ThreadingHTTPServer((pdash.DEFAULT_HOST, 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_no_post_handler():
    handler = pdash.make_handler("data/reports/portfolio_dashboard_data.json",
                                 pdash.DEFAULT_HOST)
    assert not hasattr(handler, "do_POST")
    assert not hasattr(handler, "do_PUT")
    assert not hasattr(handler, "do_DELETE")


def test_server_binds_localhost(tmp_path):
    httpd = pdash.serve(host=pdash.DEFAULT_HOST, port=0,
                        report_path=_write(tmp_path, _f48()))
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_get_root_and_state_ok(tmp_path):
    path = _write(tmp_path, _f48())
    with _server(path) as base:
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert r.status == 200
            assert "Portfolio cockpit" in r.read().decode("utf-8")
        with urllib.request.urlopen(base + "/state.json", timeout=5) as r:
            assert r.status == 200
            data = json.loads(r.read().decode("utf-8"))
            assert data["_report_present"] is True


@pytest.mark.parametrize("path", ["/order", "/buy", "/sell", "/cancel",
                                  "/retry", "/execute", "/whatever"])
def test_action_endpoints_404(tmp_path, path):
    with _server(_write(tmp_path, _f48())) as base:
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                assert r.status == 404
        except urllib.error.HTTPError as exc:
            assert exc.code == 404


def test_post_not_allowed(tmp_path):
    with _server(_write(tmp_path, _f48())) as base:
        req = urllib.request.Request(base + "/", data=b"x", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code in (404, 501)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_registers_portfolio_dashboard():
    import main
    args = main._parse_args(["portfolio-dashboard"])
    assert args.command == "portfolio-dashboard"
    assert args.host == "127.0.0.1"
    assert args.port == 8766
    assert "portfolio-dashboard" in main._HANDLERS


def test_existing_dashboard_command_still_registered():
    import main
    args = main._parse_args(["dashboard"])
    assert args.command == "dashboard"
    assert "dashboard" in main._HANDLERS


def test_default_constants():
    assert pdash.DEFAULT_PORT == 8766
    assert pdash.DEFAULT_HOST == "127.0.0.1"
    assert pdash.DEFAULT_REPORT_PATH == "data/reports/portfolio_dashboard_data.json"


# ─── F4.9.1 visual redesign ───────────────────────────────────────────────────

def test_sidebar_navigation_anchors(tmp_path):
    html = _html(tmp_path)
    assert 'class="sidebar"' in html
    for anchor, label in (("overview", "Overview"), ("portfolio", "Portfolio"),
                          ("income", "Income"), ("turnover", "Turnover"),
                          ("contributions", "Contributions"), ("risk", "Risk"),
                          ("lasttrade", "Last trade"), ("raw", "Raw JSON")):
        assert f'href="#{anchor}"' in html, anchor
        assert label in html, label
    # секции имеют соответствующие id-якоря
    for anchor in ("overview", "portfolio", "income", "turnover", "contributions",
                   "risk", "positions", "lasttrade", "raw"):
        assert f'id="{anchor}"' in html, anchor


def test_header_subtitle_and_badges(tmp_path):
    html = _html(tmp_path)
    assert "Read-only portfolio overview from F4.8" in html
    assert "READ_ONLY_SAFE" in html
    assert "full" in html               # data freshness badge
    assert "***1918" in html            # account masked badge
    assert "2026-06-26" in html         # generated_at


def test_kpi_grid_redesigned(tmp_path):
    html = _html(tmp_path)
    assert 'class="kpis"' in html
    assert 'class="kpi ' in html or 'class="kpi"' in html
    for label in ("Стоимость портфеля", "Доход к цели / мес.",
                  "Покрытие цели 150 000 ₽/мес.", "Свободный кэш",
                  "Квал-оборот 4Q", "PnL портфеля"):
        assert label in html, label


def test_passive_income_progress_bar(tmp_path):
    html = _html(tmp_path)
    assert "Доход к цели 150 000 ₽/мес." in html
    assert "Пассивный доход к цели" not in html       # F4.11 переименование
    assert 'class="track"' in html and 'class="fill' in html


def test_turnover_progress_bar(tmp_path):
    html = _html(tmp_path)
    assert "Квал-оборот к цели 6M за 4 квартала" in html
    assert 'class="track"' in html


def test_position_weight_allocation_donut_svg(tmp_path):
    html = _html(tmp_path)
    assert "<svg" in html
    assert "Position weight allocation" in html
    assert "это НЕ классы активов" in html or "НЕ классы активов" in html


def test_income_calendar_bar_chart(tmp_path):
    html = _html(tmp_path)
    assert "Календарь scheduled-дохода по месяцам" in html
    assert 'class="chart"' in html
    assert "2026-08" in html


def test_turnover_side_and_month_bars(tmp_path):
    html = _html(tmp_path)
    assert "Оборот по сторонам" in html
    assert "Оборот по месяцам" in html
    assert html.count('class="chart"') >= 2  # календарь + обороты


def test_executive_summary_cards(tmp_path):
    html = _html(tmp_path)
    assert 'class="execs"' in html
    assert 'class="exec"' in html
    for t in ("Portfolio", "Income", "Turnover", "Contributions", "Risk"):
        assert f'class="et">{t}' in html
    assert "Что сейчас" in html
    assert "не торговая рекомендация" in html.lower() or \
        "НЕ торговая рекомендация" in html


def test_positions_section_after_overview(tmp_path):
    html = _html(tmp_path)
    assert html.find('id="positions"') > html.find('id="overview"')
    assert html.find('id="positions"') > html.find('id="risk"')
    # таблица позиций по-прежнему рендерит все позиции с раскраской строк
    assert "B · Позиции (9)" in html
    assert 'class="tbl"' in html
    assert "row-neg" in html


def test_risk_display_threshold_labeled(tmp_path):
    html = _html(tmp_path)
    assert "пороги дашборда" in html
    assert "Display threshold, not investment advice" in html


def test_missing_report_page_visually_consistent(tmp_path):
    state = pdash.load_portfolio_dashboard_report(str(tmp_path / "nope.json"))
    html = pdash.build_portfolio_dashboard_html(state)
    assert 'class="sidebar"' in html       # тот же каркас
    assert "Portfolio cockpit" in html
    assert "Отчёт F4.8 не найден" in html
    assert "portfolio-dashboard-data" in html


def test_redesign_html_well_formed(tmp_path):
    from html.parser import HTMLParser
    html = _html(tmp_path)
    assert html.count("<table") == html.count("</table>")

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.bal = 0
            self.nested = False
            self.maxd = 0

        def handle_starttag(self, tag, attrs):
            if tag != "div":
                return
            card = "card" in (dict(attrs).get("class", "") or "").split()
            if card and any(self.stack):
                self.nested = True
            self.stack.append(card)
            self.bal += 1
            self.maxd = max(self.maxd, sum(self.stack))

        def handle_endtag(self, tag):
            if tag == "div":
                self.bal -= 1
                if self.stack:
                    self.stack.pop()

    p = _P()
    p.feed(html)
    assert p.bal == 0
    assert p.nested is False
    assert p.maxd == 1
