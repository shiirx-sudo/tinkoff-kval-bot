"""
Тесты income_quality_policy_v1 (conservative income mode). Чистая логика, без API.
Никаких заявок: только классификация дохода.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from modules.income_engine import IncomeEnv, compute_income
from modules.income_policy import (
    BUCKET_ESTIMATED,
    BUCKET_EXCLUDED,
    BUCKET_MANUAL,
    BUCKET_RELIABLE,
    BUCKET_UNKNOWN,
    BUCKET_VARIABLE,
    PolicyEnv,
    classify_income_policy,
)

ENV = IncomeEnv(target_monthly_rub=Decimal("100000"), tax_rate_pct=Decimal("13"))
POLICY = PolicyEnv()  # дефолты: cap 15%, haircut 20%, manual_mm_in_base=True


def _pos(ticker, itype, qty, value, auto=None, cls="TQBR", figi=""):
    return {"ticker": ticker, "class_code": cls, "figi": figi,
            "instrument_name": ticker, "instrument_type": itype,
            "position_quantity": Decimal(str(qty)),
            "position_value_rub": Decimal(str(value)),
            "auto_income": auto or {}}


# ─── 1. api_known_future → reliable, в base ──────────────────────────────────

def test_api_known_future_is_reliable_base():
    r = classify_income_policy(
        income_data_source="api_known_future", source_type="dividend",
        raw_annual_income_rub=Decimal("3500"), gross_yield_pct=Decimal("10"),
        has_future_date=True, env=POLICY)
    assert r.policy_bucket == BUCKET_RELIABLE
    assert r.policy_confidence == "high"
    assert r.base_annual_income_rub == Decimal("3500")
    assert r.count_in_base is True
    assert "announced_future_payment" in r.policy_reasons


def test_api_known_future_without_date_degrades_to_estimate():
    r = classify_income_policy(
        income_data_source="api_known_future", source_type="dividend",
        raw_annual_income_rub=Decimal("3500"), has_future_date=False, env=POLICY)
    assert r.policy_bucket == BUCKET_ESTIMATED
    assert r.base_annual_income_rub == Decimal("0")
    assert r.estimate_annual_income_rub == Decimal("3500")


# ─── api_coupon_schedule → reliable, в base ──────────────────────────────────

def test_api_coupon_schedule_is_reliable_base():
    r = classify_income_policy(
        income_data_source="api_coupon_schedule", source_type="coupon",
        raw_annual_income_rub=Decimal("800"), gross_yield_pct=Decimal("9"),
        env=POLICY)
    assert r.policy_bucket == BUCKET_RELIABLE
    assert r.policy_confidence == "high"
    assert r.base_annual_income_rub == Decimal("800")
    assert r.estimate_annual_income_rub == Decimal("0")
    assert r.excluded_annual_income_rub == Decimal("0")
    assert r.conservative_yield_pct == Decimal("9")
    assert "known_coupon_schedule" in r.policy_reasons


# ─── 2. api_trailing_12m → estimated, не в base ──────────────────────────────

def test_api_trailing_is_estimate_not_base():
    r = classify_income_policy(
        income_data_source="api_trailing_12m", source_type="dividend",
        raw_annual_income_rub=Decimal("2000"), gross_yield_pct=Decimal("8"),
        env=POLICY)
    assert r.policy_bucket == BUCKET_ESTIMATED
    assert r.base_annual_income_rub == Decimal("0")
    assert r.estimate_annual_income_rub == Decimal("2000")
    assert "trailing_not_guaranteed" in r.policy_reasons


def test_api_trailing_in_base_when_flag_enabled():
    env = PolicyEnv(use_trailing_dividends_in_base=True)
    r = classify_income_policy(
        income_data_source="api_trailing_12m", source_type="dividend",
        raw_annual_income_rub=Decimal("2000"), gross_yield_pct=Decimal("8"), env=env)
    assert r.base_annual_income_rub == Decimal("2000")
    assert r.policy_bucket == BUCKET_ESTIMATED


# ─── 3. api_trailing_12m выше cap → excluded ─────────────────────────────────

def test_api_trailing_above_cap_excluded():
    r = classify_income_policy(
        income_data_source="api_trailing_12m", source_type="dividend",
        raw_annual_income_rub=Decimal("5000"), gross_yield_pct=Decimal("27.63"),
        env=POLICY)
    assert r.policy_bucket == BUCKET_EXCLUDED
    assert r.excluded_annual_income_rub == Decimal("5000")
    assert r.base_annual_income_rub == Decimal("0")
    assert "trailing_yield_above_cap" in r.policy_reasons


# ─── 4. manual_override money_market → variable, в base с haircut ─────────────

def test_manual_money_market_variable_base_haircut():
    r = classify_income_policy(
        income_data_source="manual_override", source_type="money_market",
        raw_annual_income_rub=Decimal("14000"), gross_yield_pct=Decimal("14"),
        env=POLICY)
    assert r.policy_bucket == BUCKET_VARIABLE
    # haircut 20% → base = 14000 * 0.8 = 11200
    assert r.base_annual_income_rub == Decimal("11200.0")
    assert r.conservative_yield_pct == Decimal("14") * Decimal("0.8")
    assert "manual_money_market_yield" in r.policy_reasons
    assert "haircut_applied" in r.policy_reasons


def test_manual_money_market_not_in_base_when_flag_off():
    env = PolicyEnv(use_manual_mm_in_base=False)
    r = classify_income_policy(
        income_data_source="manual_override", source_type="money_market",
        raw_annual_income_rub=Decimal("14000"), gross_yield_pct=Decimal("14"), env=env)
    assert r.policy_bucket == BUCKET_VARIABLE
    assert r.base_annual_income_rub == Decimal("0")
    assert r.estimate_annual_income_rub == Decimal("14000")


# ─── 5. trailing_30d money_market → variable, в base с haircut ────────────────

def test_trailing_30d_variable_base_haircut():
    r = classify_income_policy(
        income_data_source="trailing_30d", source_type="money_market",
        raw_annual_income_rub=Decimal("13000"), gross_yield_pct=Decimal("13"),
        env=POLICY)
    assert r.policy_bucket == BUCKET_VARIABLE
    assert r.base_annual_income_rub == Decimal("13000") * Decimal("0.8")
    assert r.conservative_yield_pct == Decimal("13") * Decimal("0.8")  # 10.4%
    assert "variable_yield_trailing" in r.policy_reasons


# ─── 6. manual_override dividend → manual, не в base ──────────────────────────

def test_manual_dividend_is_manual_not_base():
    r = classify_income_policy(
        income_data_source="manual_override", source_type="dividend",
        raw_annual_income_rub=Decimal("3500"), gross_yield_pct=Decimal("10"),
        env=POLICY)
    assert r.policy_bucket == BUCKET_MANUAL
    assert r.base_annual_income_rub == Decimal("0")
    assert r.estimate_annual_income_rub == Decimal("3500")
    assert "manual_estimate" in r.policy_reasons


def test_manual_dividend_in_base_when_flag_enabled():
    env = PolicyEnv(use_manual_dividends_in_base=True)
    r = classify_income_policy(
        income_data_source="manual_override", source_type="dividend",
        raw_annual_income_rub=Decimal("3500"), gross_yield_pct=Decimal("10"), env=env)
    assert r.base_annual_income_rub == Decimal("3500")


# ─── 7. unknown → income_unknown, никуда не входит ───────────────────────────

def test_unknown_is_excluded_from_everything():
    r = classify_income_policy(
        income_data_source="unknown", source_type="dividend",
        raw_annual_income_rub=Decimal("0"), env=POLICY)
    assert r.policy_bucket == BUCKET_UNKNOWN
    assert r.base_annual_income_rub == Decimal("0")
    assert r.estimate_annual_income_rub == Decimal("0")
    assert r.excluded_annual_income_rub == Decimal("0")
    assert "unknown_income_data" in r.policy_reasons


# ─── 8. Summary считает raw/base/estimate/excluded ───────────────────────────

def _div_auto(source, dps, future=False, yield_high=False):
    d = {"dividend_source": source,
         "expected_annual_dividend_rub_per_share": Decimal(str(dps)),
         "trailing_12m_dividends_rub_per_share": Decimal(str(dps)),
         "risk_notes": []}
    if future:
        d["next_dividend_date"] = "2026-09-10"
        d["events"] = [{"date": "2026-09-10", "per_share": Decimal(str(dps))}]
    return d


def test_summary_layers_split():
    positions = [
        # api_known_future → base
        _pos("T", "share", 100, 30000,
             auto={"dividend": _div_auto("api_known_future", 35, future=True)}),
        # api_trailing_12m (умеренный yield) → estimate
        _pos("NVTK", "share", 10, 70000,
             auto={"dividend": _div_auto("api_trailing_12m", 70)}),
        # manual money_market → base с haircut
        _pos("LQDT", "etf", 1000, 100000, cls="TQTF"),
        # unknown
        _pos("XXXX", "share", 5, 5000),
    ]
    config = {"manual_yields": {"LQDT": {"expected_annual_yield_pct": 14.0}}}
    s = compute_income(positions, config, ENV, {}, policy_env=POLICY)
    # base: T (3500) + LQDT (14000*0.8=11200) = 14700
    assert s.base_annual_gross_rub == Decimal("14700.0")
    # estimate: NVTK trailing 70*10 = 700
    assert s.estimate_annual_gross_rub == Decimal("700")
    # unknown посчитан
    assert s.unknown_instruments >= 1
    # gross raw = 3500 + 700 + 14000 = 18200
    assert s.gross_annual_rub == Decimal("18200.0")
    # base < raw (часть в estimate + haircut)
    assert s.base_annual_gross_rub < s.gross_annual_rub


# ─── Integration: облигационный купонный график в base income ────────────────

def test_bond_coupon_schedule_in_base_income():
    positions = [_pos(
        "RU000TEST", "bond", 10, 9000,
        auto={"coupon": {"coupon_source": "api_coupon_schedule",
                         "coupon_confidence": "api_known",
                         "known_coupon_income_annualized_rub": Decimal("80"),
                         "next_coupon_date": "2026-09-15", "events": []}},
        cls="TQCB", figi="RU000TEST")]
    s = compute_income(positions, {}, ENV, {}, policy_env=POLICY)
    it = s.items[0]
    assert it.income_data_source == "api_coupon_schedule"
    assert it.policy_bucket == BUCKET_RELIABLE
    # 80 * 10 = 800 годовых купонов → в base
    assert it.expected_annual_income_rub == Decimal("800")
    assert it.base_annual_income_rub == Decimal("800")
    assert s.base_annual_gross_rub == Decimal("800")
    # required_capital считается по консервативной (base) net-доходности
    assert s.conservative_net_yield_pct is not None
    assert s.required_capital_rub is not None


# ─── 9. required_capital по консервативной (base) net-доходности ──────────────

def test_required_capital_uses_conservative_yield():
    positions = [_pos("LQDT", "etf", 1000, 100000, cls="TQTF")]
    config = {"manual_yields": {"LQDT": {"expected_annual_yield_pct": 14.0}}}
    s = compute_income(positions, config, ENV, {}, policy_env=POLICY)
    # консервативная net-доходность считается от base (11200 net / total)
    assert s.conservative_net_yield_pct is not None
    assert s.required_capital_rub is not None
    # required_capital по base (меньшей) доходности > чем по raw было бы
    target_annual_net = ENV.target_monthly_rub * Decimal("12")
    expected = target_annual_net / (s.conservative_net_yield_pct / Decimal("100"))
    assert s.required_capital_rub == expected


def test_required_capital_na_when_no_base_income():
    positions = [_pos("XXXX", "share", 5, 5000)]
    s = compute_income(positions, {}, ENV, {}, policy_env=POLICY)
    assert s.required_capital_rub is None
    assert any("n/a" in w for w in s.warnings)


def test_high_trailing_yield_does_not_inflate_required_capital():
    # trailing выше cap → excluded → не в base → не занижает required capital
    positions = [_pos("LKOH", "share", 10, 30000,
                      auto={"dividend": {"dividend_source": "api_trailing_12m",
                                         "trailing_12m_dividends_rub_per_share": Decimal("1216"),
                                         "expected_annual_dividend_rub_per_share": Decimal("1216"),
                                         "risk_notes": []}})]
    s = compute_income(positions, {}, ENV, {}, policy_env=POLICY)
    # yield 1216*10/30000*100 = 40.5% > cap 15 → excluded
    assert s.excluded_annual_gross_rub == Decimal("12160")
    assert s.base_annual_gross_rub == Decimal("0")
    assert s.items[0].policy_bucket == BUCKET_EXCLUDED


# ─── 10. Reports содержат policy fields ──────────────────────────────────────

def test_summary_reports_have_policy_fields(tmp_path):
    from reports import income_reports
    positions = [_pos("T", "share", 100, 30000,
                      auto={"dividend": _div_auto("api_known_future", 35, future=True)})]
    s = compute_income(positions, {}, ENV, {}, policy_env=POLICY)
    income_reports.write_summary(s, tmp_path)
    header = (tmp_path / "income_summary.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    for col in ("policy_bucket", "policy_confidence", "policy_reasons",
                "base_annual_income_rub", "estimate_annual_income_rub",
                "excluded_annual_income_rub", "conservative_yield_pct"):
        assert col in header, col
    md = (tmp_path / "income_summary.md").read_text(encoding="utf-8")
    assert "Raw expected income" in md
    assert "Консервативная оценка дохода" in md
    assert "Base годовой доход" in md
    assert "Gap по raw" in md and "Gap по base" in md


# ─── 11. Telegram содержит conservative block ────────────────────────────────

def test_telegram_has_conservative_block():
    from reports import income_reports
    positions = [_pos("T", "share", 100, 30000,
                      auto={"dividend": _div_auto("api_known_future", 35, future=True)})]
    s = compute_income(positions, {}, ENV, {}, policy_env=POLICY)
    text = income_reports.build_summary_telegram(s)
    assert "Raw income" in text
    assert "Conservative income" in text
    assert "Base:" in text and "Estimate:" in text and "Excluded:" in text
    assert "Gap by base" in text
    assert "manual override" in text  # дисклеймер
    assert "Заявки не отправляются" in text


# ─── 12. Safety scan ─────────────────────────────────────────────────────────

def test_no_order_endpoints_in_policy_sources():
    files = ["modules/income_policy.py", "modules/income_engine.py",
             "reports/income_reports.py"]
    forbidden = ("OrdersService", "postOrder", "cancelOrder", "place_order",
                 "submit_order", "place_limit_order", "order_client",
                 "LIVE_EXECUTION", "full_token")
    for f in files:
        src = Path(f).read_text(encoding="utf-8")
        for tok in forbidden:
            assert tok not in src, f"{f}: {tok}"
