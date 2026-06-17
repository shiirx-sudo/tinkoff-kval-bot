"""Тесты read-only income engine. Никаких заявок."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from modules.income_engine import (
    IncomeEnv,
    build_calendar,
    compute_income,
    income_for_item,
    load_income_config,
)

CONFIG = {
    "manual_yields": {"LQDT": {"class_code": "TQTF", "type": "money_market_fund",
                               "expected_annual_yield_pct": 14.0}},
    "manual_dividends": {"SBER": {"class_code": "TQBR",
                                  "expected_annual_dividend_rub_per_share": 35,
                                  "confidence": "medium",
                                  "next_dividend_date": "2026-07-10"}},
    "manual_bonds": {"RU000TEST": {"expected_coupon_rub": 40,
                                   "coupon_frequency_per_year": 2,
                                   "maturity_date": "2027-03-15", "confidence": "low"}},
}
ENV = IncomeEnv(target_monthly_rub=Decimal("100000"), horizon_months=12,
                tax_rate_pct=Decimal("13"))

FUND = {"GAZP": {"class_code": "TQBR", "management_alignment": "negative",
                 "cash_return": "weak", "state_role": "negative", "market_growth": "weak"}}


def _pos(ticker, itype, qty, value, cls="TQBR", figi=""):
    return {"ticker": ticker, "class_code": cls, "figi": figi,
            "instrument_name": ticker, "instrument_type": itype,
            "position_quantity": Decimal(str(qty)),
            "position_value_rub": Decimal(str(value))}


def test_money_market_income():
    it = income_for_item(_pos("LQDT", "etf", 1000, 100000, cls="TQTF"), CONFIG, ENV)
    assert it.source_type == "money_market"
    assert it.expected_annual_income_rub == Decimal("14000.0")     # 100000 * 14%
    assert it.expected_monthly_income_rub == Decimal("14000.0") / Decimal("12")
    assert it.confidence == "manual"


def test_dividend_income_gross_net():
    it = income_for_item(_pos("SBER", "share", 100, 30000), CONFIG, ENV)
    assert it.source_type == "dividend"
    assert it.expected_annual_income_rub == Decimal("3500")        # 35 * 100
    assert it.gross_yield_pct == Decimal("3500") / Decimal("30000") * Decimal("100")
    assert it.net_yield_pct < it.gross_yield_pct
    assert it.next_payment_date == "2026-07-10"


def test_bond_coupon_income():
    it = income_for_item(_pos("RU000TEST", "bond", 10, 9000), CONFIG, ENV)
    assert it.source_type == "coupon"
    # 40 * 2 * 10
    assert it.expected_annual_income_rub == Decimal("800")


def test_unknown_income_no_crash():
    it = income_for_item(_pos("XXXX", "share", 5, 5000), CONFIG, ENV)
    assert it.expected_annual_income_rub == Decimal("0")
    assert it.confidence == "unknown"
    assert it.income_verdict == "income_unknown"


def test_target_gap_and_required_capital():
    positions = [_pos("LQDT", "etf", 1000, 100000, cls="TQTF")]
    s = compute_income(positions, CONFIG, ENV, {}, free_cash_rub=Decimal("0"))
    # net annual = 14000 * 0.87 = 12180 → net monthly = 1015
    assert s.net_annual_rub == Decimal("14000.0") * Decimal("0.87")
    assert s.gap_monthly_rub == ENV.target_monthly_rub - s.net_monthly_rub
    # required_capital = target_annual_net / net_yield_fraction
    assert s.required_capital_rub is not None
    assert s.required_capital_rub > s.total_value_rub


def test_required_capital_na_when_no_yield():
    positions = [_pos("XXXX", "share", 5, 5000)]
    s = compute_income(positions, CONFIG, ENV, {}, free_cash_rub=Decimal("0"))
    assert s.required_capital_rub is None
    assert any("n/a" in w for w in s.warnings)


def test_calendar_groups():
    positions = [_pos("SBER", "share", 100, 30000),
                 _pos("LQDT", "etf", 1000, 100000, cls="TQTF")]
    s = compute_income(positions, CONFIG, ENV, {})
    rows = build_calendar(s.items, 12, ENV.tax_rate_pct)
    months = {r["month"] for r in rows}
    assert any(m.startswith("2026-07") for m in months)            # дивиденд SBER
    assert any(m.startswith("M+") for m in months)                 # MM помесячно


def test_fundamental_risk_integration():
    positions = [_pos("GAZP", "share", 100, 50000)]
    cfg = {**CONFIG, "manual_dividends": {
        **CONFIG["manual_dividends"],
        "GAZP": {"class_code": "TQBR", "expected_annual_dividend_rub_per_share": 10,
                 "confidence": "low"}}}
    s = compute_income(positions, cfg, ENV, FUND)
    it = s.items[0]
    assert "state_control_risk" in it.risk_notes
    assert it.income_verdict == "income_risk"


def test_reports_have_fields(tmp_path):
    from reports import income_reports
    positions = [_pos("SBER", "share", 100, 30000)]
    s = compute_income(positions, CONFIG, ENV, {})
    income_reports.write_summary(s, tmp_path)
    header = (tmp_path / "income_summary.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    for col in ("ticker", "source_type", "expected_annual_income_rub", "confidence",
                "net_yield_pct", "income_verdict", "risk_notes"):
        assert col in header
    assert (tmp_path / "income_summary.md").read_text(
        encoding="utf-8").startswith("# Income summary — READ ONLY")


def test_telegram_summary_text():
    from reports import income_reports
    positions = [_pos("SBER", "share", 100, 30000)]
    s = compute_income(positions, CONFIG, ENV, {})
    text = income_reports.build_summary_telegram(s)
    assert "READ ONLY" in text
    assert "не рекомендация" in text
    assert "Заявки не отправляются" in text


def test_example_yaml_loads():
    data = load_income_config("config/income_engine.example.yaml")
    assert "manual_yields" in data and "LQDT" in data["manual_yields"]


def test_no_order_endpoints_in_income_sources():
    files = ["modules/income_engine.py", "reports/income_reports.py",
             "config/income_engine.example.yaml"]
    for f in files:
        src = Path(f).read_text(encoding="utf-8")
        for forbidden in ("OrdersService", "postOrder", "cancelOrder", "place_order",
                          "submit_order", "place_limit_order", "order_client",
                          "LIVE_EXECUTION", "full_token"):
            assert forbidden not in src, f"{f}: {forbidden}"
