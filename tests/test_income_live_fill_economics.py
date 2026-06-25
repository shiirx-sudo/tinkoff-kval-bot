"""
Тесты F4.5 income_live_fill_economics — read-only экономика новой сделки.

Никакой сети: входной источник — F4.4 отчёт (инъектируется как файл во tmp_path),
цена refresh инъектируется провайдером. Проверяем gross vs net PnL, комиссионный
drag, безубыток, отделение PnL всей позиции, отсутствие угадывания, маскирование
account и неиспользование live/sandbox токена.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from modules import income_live_fill_economics as ilfe

ORDER_ID = "80578688754"
ACCOUNT = "2000001918"
FIGI = "TCS80A107UL4"
UID = "87db07bc-0e02-4e29-90bb-05e8ef791d7b"
FILL = "EXECUTION_REPORT_STATUS_FILL"


def _f44(**over):
    """Реальный кейс F4.4 после фикса знака комиссии."""
    base = {
        "kind": "income_live_fill_attribution",
        "stage": "F4_4_LIVE_FILL_ATTRIBUTION_READ_ONLY",
        "ticker": "T",
        "order_id": ORDER_ID,
        "order_status": FILL,
        "instrument_uid": UID,
        "figi": FIGI,
        "class_code": "TQBR",
        "lot_size": 1,
        "current_total_position_units": 27.0,
        "current_total_position_lots": 27.0,
        "current_average_position_price": 304.02,
        "current_price": 268.26,
        "current_total_position_value": 7243.02,
        "current_total_unrealized_pnl": -965.52,
        "fill_quantity_units": 1.0,
        "fill_quantity_lots": 1.0,
        "fill_price": 276.08,
        "fill_gross_amount": 276.08,
        "fill_commission_raw": -0.14,
        "fill_commission_abs": 0.14,
        "fill_cash_outflow": 276.22,
        "fill_currency": "rub",
        "fill_attribution_confidence": "medium",
        "attribution_method": "operations_instrument_qty_price_date_match",
        "estimated_previous_position_units": 26.0,
        "estimated_previous_average_price": 305.0946,
        "old_position_estimation_warning": "Прежняя позиция реконструирована (estimated).",
        "base_monthly_living_basket_rub": 150000,
        "estimated_income_contribution_rub_monthly": None,
        "estimated_income_contribution_rub_yearly": None,
        "income_target_coverage_pct": None,
        "income_data_source": None,
        "income_estimation_warning": "Надёжных данных о доходе нет.",
    }
    base.update(over)
    return base


def _write(tmp: Path, name: str, data) -> str:
    p = tmp / name
    if data is not None:
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


_S = object()


def _run(tmp: Path, *, f44=_S, f43=_S, **kw):
    f44v = _f44() if f44 is _S else f44
    f43v = None if f43 is _S else f43
    kw.setdefault("read_token_present", True)
    return ilfe.run(
        ticker="T", order_id=ORDER_ID, live_account_id=ACCOUNT,
        f44_report=_write(tmp, "f44.json", f44v),
        f43_report=_write(tmp, "f43.json", f43v),
        f42_report=str(tmp / "no42.json"),
        f41_report=str(tmp / "no41.json"),
        output_json=str(tmp / "fe.json"), output_md=str(tmp / "fe.md"),
        **kw)


# ─── gross / net PnL ──────────────────────────────────────────────────────────

def test_gross_pnl_from_fill_and_current_price(tmp_path):
    rep = _run(tmp_path)
    assert rep["new_fill_current_value"] == Decimal("268.26")
    assert rep["new_fill_gross_unrealized_pnl"] == Decimal("-7.82")
    assert rep["new_fill_gross_unrealized_pnl_pct"] == Decimal("-2.8325")


def test_net_pnl_after_commission_from_cash_outflow(tmp_path):
    rep = _run(tmp_path)
    assert rep["new_fill_net_unrealized_pnl_after_commission"] == Decimal("-7.96")
    assert rep["new_fill_net_unrealized_pnl_after_commission_pct"] == \
        Decimal("-2.8818")


def test_commission_drag(tmp_path):
    rep = _run(tmp_path)
    assert rep["commission_drag_rub"] == Decimal("0.14")
    assert rep["commission_drag_pct_of_gross_amount"] == Decimal("0.0507")


def test_break_even_price_after_commission(tmp_path):
    rep = _run(tmp_path)
    assert rep["break_even_price_after_commission"] == Decimal("276.22")
    assert rep["distance_to_break_even_rub"] == Decimal("-7.96")
    assert rep["distance_to_break_even_pct"] == Decimal("-2.8818")


def test_new_fill_weight_in_total_position(tmp_path):
    rep = _run(tmp_path)
    assert rep["new_fill_weight_in_total_position_pct"] == Decimal("3.7037")


# ─── separation of total vs new-fill PnL ──────────────────────────────────────

def test_total_pnl_kept_separate_from_new_fill(tmp_path):
    rep = _run(tmp_path)
    assert rep["total_position_pnl_kept_separate"] is True
    assert rep["current_total_position_units"] == Decimal("27")
    assert rep["current_total_unrealized_pnl"] == Decimal("-965.52")
    # new-fill PnL — отдельная, маленькая величина, не равна total
    assert rep["new_fill_gross_unrealized_pnl"] == Decimal("-7.82")
    assert rep["new_fill_net_unrealized_pnl_after_commission"] == Decimal("-7.96")
    assert rep["new_fill_net_unrealized_pnl_after_commission"] != \
        rep["current_total_unrealized_pnl"]


def test_total_average_price_not_used_for_new_fill_pnl(tmp_path):
    # Меняем среднее всей позиции — экономика новой сделки НЕ должна измениться.
    rep_a = _run(tmp_path, f44=_f44(current_average_position_price=304.02))
    rep_b = _run(tmp_path, f44=_f44(current_average_position_price=999.99))
    for key in ("new_fill_current_value", "new_fill_gross_unrealized_pnl",
                "new_fill_net_unrealized_pnl_after_commission",
                "break_even_price_after_commission", "distance_to_break_even_rub"):
        assert rep_a[key] == rep_b[key]
    # безубыток считается из денежного оттока сделки, а не из среднего позиции
    assert rep_b["break_even_price_after_commission"] == Decimal("276.22")


# ─── no guessing: commission / current price ──────────────────────────────────

def test_missing_commission_net_null_gross_calculated(tmp_path):
    f44 = _f44(fill_commission_raw=None, fill_commission_abs=None,
               fill_cash_outflow=None)
    rep = _run(tmp_path, f44=f44)
    # gross считается (есть цена)
    assert rep["new_fill_current_value"] == Decimal("268.26")
    assert rep["new_fill_gross_unrealized_pnl"] == Decimal("-7.82")
    # net/безубыток/drag = null
    assert rep["new_fill_net_unrealized_pnl_after_commission"] is None
    assert rep["new_fill_net_unrealized_pnl_after_commission_pct"] is None
    assert rep["break_even_price_after_commission"] is None
    assert rep["distance_to_break_even_rub"] is None
    assert rep["commission_drag_rub"] is None
    assert any("комисси" in w.lower() or "отток" in w.lower()
               for w in rep["warnings"])


def test_missing_current_price_pnl_null(tmp_path):
    f44 = _f44(current_price=None)
    rep = _run(tmp_path, f44=f44)
    assert rep["new_fill_current_value"] is None
    assert rep["new_fill_gross_unrealized_pnl"] is None
    assert rep["new_fill_net_unrealized_pnl_after_commission"] is None
    assert rep["distance_to_break_even_rub"] is None
    # безубыток (не зависит от текущей цены) всё ещё считается
    assert rep["break_even_price_after_commission"] == Decimal("276.22")
    assert any("цена" in w.lower() for w in rep["warnings"])


def test_income_not_guessed_when_unavailable(tmp_path):
    rep = _run(tmp_path)
    assert rep["base_monthly_living_basket_rub"] == 150000
    assert rep["estimated_income_contribution_rub_monthly"] is None
    assert rep["estimated_income_contribution_rub_yearly"] is None
    assert rep["income_target_coverage_pct"] is None
    assert rep["income_data_source"] is None
    assert any("доход" in w.lower() or "не угадыва" in w.lower()
               for w in rep["warnings"])


# ─── missing F4.4 / token / network ───────────────────────────────────────────

def test_missing_f44_clean_failure_no_network(tmp_path):
    calls = {"n": 0}

    def watchdog(*a, **k):
        calls["n"] += 1
        raise AssertionError("network must not be called")

    rep = ilfe.run(
        ticker="T", order_id=ORDER_ID, live_account_id=ACCOUNT,
        f44_report=str(tmp_path / "no44.json"),
        f43_report=str(tmp_path / "no43.json"),
        f42_report=str(tmp_path / "no42.json"),
        f41_report=str(tmp_path / "no41.json"),
        output_json=str(tmp_path / "fe.json"), output_md=str(tmp_path / "fe.md"),
        client=None, price_provider=watchdog, read_token_present=False)
    assert calls["n"] == 0
    assert rep["_exit_code"] == 1
    assert any("f4.4" in e.lower() for e in rep["errors"])
    assert Path(rep["_output_json"]).exists()


def test_no_token_sufficient_reports_succeeds_no_network(tmp_path):
    calls = {"n": 0}

    def watchdog(*a, **k):
        calls["n"] += 1
        raise AssertionError("network must not be called")

    rep = _run(tmp_path, client=None, price_provider=watchdog,
               read_token_present=False)
    assert calls["n"] == 0
    assert rep["_exit_code"] == 0
    assert rep["new_fill_net_unrealized_pnl_after_commission"] == Decimal("-7.96")
    assert rep["token_policy"]["read_only_token_present"] is False


def test_price_refresh_only_when_missing(tmp_path):
    # current_price нет в отчётах → провайдер вызывается ровно один раз.
    calls = {"n": 0}

    def provider(uid_, figi_):
        calls["n"] += 1
        return {"currency": "rub", "units": "268", "nano": 260000000}  # 268.26

    rep = _run(tmp_path, f44=_f44(current_price=None),
               f43=None, price_provider=provider)
    assert calls["n"] == 1
    assert rep["current_price"] == Decimal("268.26")
    assert rep["new_fill_gross_unrealized_pnl"] == Decimal("-7.82")
    assert rep["token_policy"]["read_only_token_used_for"]


# ─── безопасность / токены / guards ───────────────────────────────────────────

def test_account_masked(tmp_path):
    rep = _run(tmp_path)
    assert rep["live_account_id_masked"] and rep["live_account_id_masked"] != ACCOUNT
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    assert ACCOUNT not in js


def test_no_token_value_leaks(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKOFF_TOKEN", "READ-SECRET")
    monkeypatch.setenv("TINKOFF_LIVE_TRADING_TOKEN", "LIVE-SECRET")
    monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "SANDBOX-SECRET")
    rep = _run(tmp_path)
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    for secret in ("READ-SECRET", "LIVE-SECRET", "SANDBOX-SECRET"):
        assert secret not in js and secret not in md


def test_guards_all_safe(tmp_path):
    rep = _run(tmp_path)
    g = rep["guards"]
    assert g[ilfe.GUARD_LIVE_ORDER_SENT] is False
    assert g["post_order_called"] is False
    assert g[ilfe.GUARD_CANCEL_CALLED] is False
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
    rep = _run(tmp_path)
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
        with pytest.raises(ilfe.FillEconomicsError):
            ilfe.run(**kw)


def test_reports_created_and_fields(tmp_path):
    rep = _run(tmp_path)
    assert Path(rep["_output_json"]).exists()
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "F4.5" in md
    assert "Read-only economics" in md
    data = json.loads(Path(rep["_output_json"]).read_text(encoding="utf-8"))
    for key in (
        "stage", "mode", "ticker", "order_id", "live_account_id_masked",
        "fill_attribution_confidence", "attribution_method",
        "fill_quantity_units", "fill_quantity_lots", "fill_price",
        "fill_gross_amount", "fill_commission_raw", "fill_commission_abs",
        "fill_cash_outflow", "fill_currency", "current_price",
        "current_total_position_units", "current_total_position_lots",
        "current_average_position_price", "current_total_position_value",
        "current_total_unrealized_pnl", "new_fill_current_value",
        "new_fill_gross_unrealized_pnl", "new_fill_gross_unrealized_pnl_pct",
        "new_fill_net_unrealized_pnl_after_commission",
        "new_fill_net_unrealized_pnl_after_commission_pct", "commission_drag_rub",
        "commission_drag_pct_of_gross_amount", "break_even_price_after_commission",
        "distance_to_break_even_rub", "distance_to_break_even_pct",
        "new_fill_weight_in_total_position_pct", "total_position_pnl_kept_separate",
        "previous_position_estimated_units",
        "previous_position_estimated_average_price",
        "previous_position_estimation_warning", "base_monthly_living_basket_rub",
        "estimated_income_contribution_rub_monthly",
        "estimated_income_contribution_rub_yearly", "income_target_coverage_pct",
        "income_data_source", "income_estimation_warning", "checked_at",
        "warnings", "errors", "guards", "token_policy",
    ):
        assert key in data, key
    assert data["stage"] == "F4_5_LIVE_FILL_ECONOMICS_READ_ONLY"
    assert data["mode"] == "FILL_ECONOMICS_READ_ONLY"
    assert data["total_position_pnl_kept_separate"] is True


def test_default_output_paths():
    assert ilfe.DEFAULT_OUTPUT_JSON == \
        "data/reports/income_live_fill_economics_report.json"
    assert ilfe.DEFAULT_OUTPUT_MD == \
        "data/reports/income_live_fill_economics_report.md"


# ─── статическая проверка: нет запрещённых литералов ──────────────────────────

def test_module_source_has_no_forbidden_literals():
    src = Path(ilfe.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service", "post" "Order(", "cancel" "Order(",
        "place" "_order", "submit" "_order", "cancel" "_order",
        "live" "_order", "order" "_client", "place" "_limit_" "order",
    )
    for tok in forbidden:
        assert tok not in src, tok
