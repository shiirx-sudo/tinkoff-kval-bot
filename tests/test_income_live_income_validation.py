"""
Тесты F4.6 income_live_income_validation — read-only валидация доходных данных.

Никакой сети: доходные данные инъектируются через income_provider (словарь в
формате income_sources). Проверяем: надёжный годовой дивиденд → расчёт new-fill и
total раздельно; trailing-оценка/одно событие не аннуализируются; отсутствие
источника → null + blocking reason; маскирование account; неиспользование
live/sandbox токена; отсутствие сети при недостающих идентификаторах/без токена.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from modules import income_live_income_validation as iliv

ORDER_ID = "80578688754"
ACCOUNT = "2000001918"
FIGI = "TCS80A107UL4"
UID = "87db07bc-0e02-4e29-90bb-05e8ef791d7b"


def _f44(**over):
    base = {
        "kind": "income_live_fill_attribution",
        "ticker": "T",
        "order_id": ORDER_ID,
        "figi": FIGI,
        "instrument_uid": UID,
        "class_code": "TQBR",
        "lot_size": 1,
        "current_price": 268.26,
        "current_total_position_units": 27.0,
        "current_total_position_value": 7243.02,
        "fill_quantity_units": 1.0,
        "fill_cash_outflow": 276.22,
        "fill_currency": "rub",
    }
    base.update(over)
    return base


def _f45(**over):
    base = {
        "kind": "income_live_fill_economics",
        "ticker": "T",
        "order_id": ORDER_ID,
        "current_price": 268.26,
        "current_total_position_units": 27.0,
        "current_total_position_value": 7243.02,
        "fill_quantity_units": 1.0,
        "fill_cash_outflow": 276.22,
        "fill_currency": "rub",
    }
    base.update(over)
    return base


# income_sources-shaped dividend dicts
def _div_known(per_share="30", date="2026-07-15"):
    return {
        "dividend_source": "api_known_future",
        "dividend_confidence": "api_known",
        "expected_annual_dividend_rub_per_share": per_share,
        "known_future_dividends_rub_per_share": per_share,
        "trailing_12m_dividends_rub_per_share": None,
        "next_dividend_date": date,
        "events": [{"date": date, "per_share": per_share}],
        "risk_notes": [],
    }


def _div_trailing(per_share="12"):
    return {
        "dividend_source": "api_trailing_12m",
        "dividend_confidence": "estimated",
        "expected_annual_dividend_rub_per_share": per_share,
        "known_future_dividends_rub_per_share": None,
        "trailing_12m_dividends_rub_per_share": per_share,
        "next_dividend_date": "",
        "events": [],
        "risk_notes": ["trailing_not_guaranteed"],
    }


def _div_event_only(date="2026-09-01", per_share="5"):
    # известно одно будущее событие, но НАДЁЖНОЙ аннуализации нет
    return {
        "dividend_source": "unknown",
        "dividend_confidence": "unknown",
        "expected_annual_dividend_rub_per_share": None,
        "next_dividend_date": date,
        "events": [{"date": date, "per_share": per_share}],
        "risk_notes": [],
    }


def _div_unknown():
    return {
        "dividend_source": "unknown",
        "dividend_confidence": "unknown",
        "expected_annual_dividend_rub_per_share": None,
        "next_dividend_date": "",
        "events": [],
        "risk_notes": [],
    }


def _write(tmp: Path, name: str, data) -> str:
    p = tmp / name
    if data is not None:
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


_S = object()


def _run(tmp: Path, *, f44=_S, f45=_S, f43=_S, income_provider=None, **kw):
    f44v = _f44() if f44 is _S else f44
    f45v = _f45() if f45 is _S else f45
    f43v = None if f43 is _S else f43
    kw.setdefault("read_token_present", True)
    return iliv.run(
        ticker="T", order_id=ORDER_ID, live_account_id=ACCOUNT,
        f44_report=_write(tmp, "f44.json", f44v),
        f45_report=_write(tmp, "f45.json", f45v),
        f43_report=_write(tmp, "f43.json", f43v),
        f42_report=str(tmp / "no42.json"),
        f41_report=str(tmp / "no41.json"),
        output_json=str(tmp / "iv.json"), output_md=str(tmp / "iv.md"),
        income_provider=income_provider, **kw)


# ─── no reliable source ───────────────────────────────────────────────────────

def test_no_reliable_source_succeeds_with_nulls(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_unknown())
    assert rep["_exit_code"] == 0
    assert rep["reliable_income_data_found"] is False
    assert rep["income_data_confidence"] == "none"
    assert rep["expected_dividend_per_unit_rub"] is None
    assert rep["expected_income_rub_monthly_new_fill"] is None
    assert rep["expected_income_rub_yearly_total_position"] is None
    assert rep["income_validation_passed"] is False
    assert "no_reliable_income_source" in rep["income_validation_blocking_reasons"]
    assert rep["warnings"]


# ─── reliable annual per-unit income ──────────────────────────────────────────

def test_reliable_income_new_fill_calculation(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_known("30"))
    assert rep["reliable_income_data_found"] is True
    assert rep["income_data_confidence"] == "high"
    assert rep["income_data_source"] == "api_known_future"
    assert rep["expected_dividend_per_unit_rub"] == Decimal("30")
    assert rep["expected_income_rub_yearly_new_fill"] == Decimal("30.00")
    assert rep["expected_income_rub_monthly_new_fill"] == Decimal("2.50")
    assert rep["income_target_coverage_pct_new_fill"] == Decimal("0.0017")
    assert rep["income_validation_passed"] is True


def test_reliable_income_total_position_calculation(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_known("30"))
    # total: 30 * 27 units
    assert rep["expected_income_rub_yearly_total_position"] == Decimal("810.00")
    assert rep["expected_income_rub_monthly_total_position"] == Decimal("67.50")
    assert rep["income_target_coverage_pct_total_position"] == Decimal("0.045")
    # new-fill и total — разные величины (раздельно)
    assert rep["expected_income_rub_yearly_new_fill"] != \
        rep["expected_income_rub_yearly_total_position"]


def test_coverage_uses_150000_target(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_known("30"))
    assert rep["base_monthly_living_basket_rub"] == 150000
    # 2.50 / 150000 * 100 = 0.0017
    assert rep["income_target_coverage_pct_new_fill"] == Decimal("0.0017")
    # 67.50 / 150000 * 100 = 0.045
    assert rep["income_target_coverage_pct_total_position"] == Decimal("0.045")


def test_dividend_yield_uses_current_price(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_known("30"))
    # 30 / 268.26 * 100
    assert rep["expected_dividend_yield_pct"] == Decimal("11.1832")


# ─── single future event NOT auto-annualized ──────────────────────────────────

def test_future_event_not_auto_annualized(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_event_only())
    assert rep["reliable_income_data_found"] is False
    # доход НЕ аннуализирован
    assert rep["expected_dividend_per_unit_rub"] is None
    assert rep["expected_income_rub_monthly_new_fill"] is None
    assert rep["expected_income_rub_yearly_total_position"] is None
    # но событие сообщено отдельно
    assert rep["next_known_income_event_date"] == "2026-09-01"
    assert rep["next_known_income_event_type"] == "dividend"
    assert rep["next_known_income_event_amount_per_unit"] == Decimal("5")
    assert "future_event_present_but_not_annualizable" in \
        rep["income_validation_blocking_reasons"]


def test_trailing_estimate_not_treated_as_reliable(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_trailing("12"))
    assert rep["reliable_income_data_found"] is False
    assert rep["income_data_confidence"] == "low"
    assert rep["expected_dividend_per_unit_rub"] is None
    assert "income_estimate_trailing_not_guaranteed" in \
        rep["income_validation_blocking_reasons"]


# ─── gross / tax ──────────────────────────────────────────────────────────────

def test_gross_only_when_tax_unknown(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_known("30"))
    assert rep["withholding_tax_assumption"] is None
    assert rep["withholding_tax_source"] is None
    assert any("брутто" in w.lower() or "налог" in w.lower()
               for w in rep["warnings"])


# ─── separation: position avg/PnL not used ────────────────────────────────────

def test_income_independent_of_position_avg_and_pnl(tmp_path):
    a = _run(tmp_path, f44=_f44(current_average_position_price=304.02,
                                current_total_unrealized_pnl=-965.52),
             income_provider=lambda ctx: _div_known("30"))
    b = _run(tmp_path, f44=_f44(current_average_position_price=999.99,
                                current_total_unrealized_pnl=123.45),
             income_provider=lambda ctx: _div_known("30"))
    for key in ("expected_income_rub_yearly_new_fill",
                "expected_income_rub_yearly_total_position",
                "expected_dividend_per_unit_rub"):
        assert a[key] == b[key]


# ─── missing identifiers / fallback / token / network ─────────────────────────

def test_missing_identifiers_clean_failure_no_network(tmp_path):
    calls = {"n": 0}

    def watchdog(ctx):
        calls["n"] += 1
        raise AssertionError("network/provider must not be called")

    # f44/f45 без figi/uid; f41/f43 отсутствуют
    rep = _run(tmp_path, f44=_f44(figi=None, instrument_uid=None),
               f45=_f45(), f43=None, income_provider=watchdog)
    assert calls["n"] == 0
    assert rep["_exit_code"] == 1
    assert any("идентификатор" in e.lower() for e in rep["errors"])
    assert Path(rep["_output_json"]).exists()


def test_fallback_to_f44_f43_when_f45_missing(tmp_path):
    rep = _run(tmp_path, f45=None, f43={
        "position_quantity_units": 27, "current_position_value": 7243.02,
        "current_price": 268.26, "currency": "rub"},
        income_provider=lambda ctx: _div_known("30"))
    assert rep["figi"] == FIGI
    assert rep["current_price"] == Decimal("268.26")
    assert rep["current_total_position_units"] == Decimal("27")
    assert rep["expected_income_rub_yearly_new_fill"] == Decimal("30.00")


def test_no_token_sufficient_reports_succeeds_no_network(tmp_path):
    calls = {"n": 0}

    def watchdog(ctx):
        calls["n"] += 1
        raise AssertionError("provider must not be called without token")

    # client=None и income_provider=None → провайдер не строится, сети нет
    rep = _run(tmp_path, income_provider=None, client=None,
               read_token_present=False)
    assert rep["_exit_code"] == 0
    assert rep["reliable_income_data_found"] is False
    assert rep["income_data_checked"] is False
    assert rep["token_policy"]["read_only_token_present"] is False
    assert any("no_reliable_income_source" in r
               for r in rep["income_validation_blocking_reasons"])


def test_token_present_unsupported_method_no_fake_data(tmp_path):
    class _StubClient:  # нет метода доходных данных
        pass

    rep = _run(tmp_path, client=_StubClient(), income_provider=None,
               read_token_present=True)
    assert rep["_exit_code"] == 0
    assert rep["reliable_income_data_found"] is False
    assert rep["income_data_checked"] is False
    assert rep["income_data_source"] == "unsupported_by_current_client"
    assert "unsupported_by_current_client" in \
        rep["income_validation_blocking_reasons"]
    # никаких выдуманных income-значений
    assert rep["expected_dividend_per_unit_rub"] is None


# ─── безопасность / токены / guards ───────────────────────────────────────────

def test_account_masked(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_unknown())
    assert rep["live_account_id_masked"] and rep["live_account_id_masked"] != ACCOUNT
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    assert ACCOUNT not in js


def test_no_token_value_leaks(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKOFF_TOKEN", "READ-SECRET")
    monkeypatch.setenv("TINKOFF_LIVE_TRADING_TOKEN", "LIVE-SECRET")
    monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "SANDBOX-SECRET")
    rep = _run(tmp_path, income_provider=lambda ctx: _div_known("30"))
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    for secret in ("READ-SECRET", "LIVE-SECRET", "SANDBOX-SECRET"):
        assert secret not in js and secret not in md


def test_guards_all_safe(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_known("30"))
    g = rep["guards"]
    assert g[iliv.GUARD_LIVE_ORDER_SENT] is False
    assert g["post_order_called"] is False
    assert g[iliv.GUARD_CANCEL_CALLED] is False
    assert g["sell_order_sent"] is False
    assert g["market_order_used"] is False
    assert g["retry_execution"] is False
    assert g["portfolio_mutated"] is False
    assert g["config_mutated"] is False
    assert g["telegram_sent"] is False
    assert g["live_token_used"] is False
    assert g["sandbox_token_used"] is False
    assert g["token_printed"] is False


def test_token_policy_no_live_no_sandbox(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_known("30"))
    tp = rep["token_policy"]
    assert tp["read_only_token_env"] == "TINKOFF_TOKEN"
    assert tp["live_trading_token_env"] == "TINKOFF_LIVE_TRADING_TOKEN"
    assert tp["live_trading_token_required"] is False
    assert tp["live_token_used"] is False
    assert tp["sandbox_token_used"] is False
    assert tp["token_printed"] is False


# ─── required args / отчёты ───────────────────────────────────────────────────

def test_required_args(tmp_path):
    for bad in ({"ticker": ""}, {"order_id": ""}, {"live_account_id": ""}):
        kw = {"ticker": "T", "order_id": ORDER_ID, "live_account_id": ACCOUNT,
              "output_json": str(tmp_path / "x.json"),
              "output_md": str(tmp_path / "x.md")}
        kw.update(bad)
        with pytest.raises(iliv.IncomeValidationError):
            iliv.run(**kw)


def test_reports_created_and_fields(tmp_path):
    rep = _run(tmp_path, income_provider=lambda ctx: _div_known("30"))
    assert Path(rep["_output_json"]).exists()
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "F4.6" in md
    assert "Read-only income validation" in md
    data = json.loads(Path(rep["_output_json"]).read_text(encoding="utf-8"))
    for key in (
        "stage", "mode", "ticker", "order_id", "live_account_id_masked", "figi",
        "instrument_uid", "class_code", "lot_size", "currency", "current_price",
        "current_total_position_units", "current_total_position_value",
        "new_fill_quantity_units", "new_fill_cash_outflow",
        "base_monthly_living_basket_rub", "income_data_checked",
        "income_data_sources_checked", "reliable_income_data_found",
        "income_data_confidence", "income_data_source", "income_data_as_of",
        "expected_dividend_per_unit_rub", "expected_dividend_yield_pct",
        "expected_income_rub_monthly_new_fill", "expected_income_rub_yearly_new_fill",
        "expected_income_rub_monthly_total_position",
        "expected_income_rub_yearly_total_position",
        "income_target_coverage_pct_new_fill",
        "income_target_coverage_pct_total_position",
        "next_known_income_event_date", "next_known_income_event_type",
        "next_known_income_event_amount_per_unit", "withholding_tax_assumption",
        "withholding_tax_source", "income_validation_passed",
        "income_validation_blocking_reasons", "warnings", "errors", "checked_at",
        "token_policy", "guards",
    ):
        assert key in data, key
    assert data["stage"] == "F4_6_LIVE_INCOME_VALIDATION_READ_ONLY"
    assert data["mode"] == "INCOME_VALIDATION_READ_ONLY"


def test_default_output_paths():
    assert iliv.DEFAULT_OUTPUT_JSON == \
        "data/reports/income_live_income_validation_report.json"
    assert iliv.DEFAULT_OUTPUT_MD == \
        "data/reports/income_live_income_validation_report.md"


# ─── статическая проверка: нет запрещённых литералов ──────────────────────────

def test_module_source_has_no_forbidden_literals():
    src = Path(iliv.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service", "post" "Order(", "cancel" "Order(",
        "place" "_order", "submit" "_order", "cancel" "_order",
        "live" "_order", "order" "_client", "place" "_limit_" "order",
    )
    for tok in forbidden:
        assert tok not in src, tok
