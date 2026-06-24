"""
Тесты F4.4 income_live_fill_attribution — read-only атрибуция новой сделки.

Никакой сети: операции/портфель инъектируются провайдерами. Проверяем уровни
уверенности (high/medium/low), отделение новой сделки от прежней позиции, оценку
прежней позиции с warning, отсутствие угадывания комиссии/дохода, маскирование
account и неиспользование live/sandbox токена.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from modules import income_live_fill_attribution as ilfa

ORDER_ID = "80578688754"
ACCOUNT = "2000001918"
FIGI = "BBG004730N88"
UID = "87db07bc-0e02-4e29-90bb-05e8ef791d7b"
FILL = "EXECUTION_REPORT_STATUS_FILL"
NEW = "EXECUTION_REPORT_STATUS_NEW"
ORDER_DT = "2026-06-23T19:00:00+00:00"

PRICE_276 = {"currency": "rub", "units": "276", "nano": 140000000}     # 276.14
PAYMENT_276 = {"currency": "rub", "units": "-276", "nano": -140000000}  # -276.14
COMM_014 = {"currency": "rub", "units": "0", "nano": 140000000}        # 0.14
# Реальный кейс F4.4: цена 276.08, комиссия со знаком МИНУС (как у брокера).
PRICE_27608 = {"currency": "rub", "units": "276", "nano": 80000000}     # 276.08
PAYMENT_27608 = {"currency": "rub", "units": "-276", "nano": -80000000}  # -276.08
COMM_NEG_014 = {"currency": "rub", "units": "0", "nano": -140000000}    # -0.14
COMM_POS_014 = {"currency": "rub", "units": "0", "nano": 140000000}     # +0.14


def _f41():
    return {
        "live_order_request_sanitized": {
            "currency": "rub",
            "instrument": {"ticker": "T", "figi": FIGI, "uid": UID,
                           "class_code": "TQBR"}},
        "reference_price": 276.14,
        "lot_size": 1,
        "generated_at": ORDER_DT,
        "live_order_result": {"order_id": ORDER_ID,
                              "execution_report_status": FILL},
        "live_order_response_sanitized": {"lots_requested": 1, "lots_executed": 1},
    }


def _f42():
    return {"order_id": ORDER_ID, "execution_report_status": FILL,
            "lots_executed": 1, "lots_requested": 1,
            "checked_at": "2026-06-23T19:02:00+00:00"}


def _f43():
    return {
        "position_found": True,
        "position_quantity_units": 27,
        "position_quantity_lots": 27,
        "average_position_price": 304.02,
        "current_price": 268.26,
        "current_position_value": 7243.02,
        "unrealized_pnl": -965.52,
        "currency": "rub",
        "lot_size": 1,
    }


def _op(*, order_id=None, qty=1, with_commission=True, commission=None,
        price=None, payment=None, op_id="op1", date="2026-06-23T19:05:00Z"):
    op = {
        "id": op_id,
        "instrumentUid": UID,
        "figi": FIGI,
        "operationType": "OPERATION_TYPE_BUY",
        "type": "Покупка ЦБ",
        "date": date,
        "quantity": str(qty),
        "price": dict(price or PRICE_276),
        "payment": dict(payment or PAYMENT_276),
        "tradesInfo": {"trades": [{"num": "trade-1"}]},
    }
    if with_commission:
        op["commission"] = dict(commission or COMM_014)
    if order_id:
        op["orderId"] = order_id
    return op


def _write(tmp: Path, name: str, data) -> str:
    p = tmp / name
    if data is not None:
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


_S = object()


def _run(tmp: Path, *, f41=_S, f42=_S, f43=_S, operations=_S,
         operations_provider=_S, dividend_provider=None, **kw):
    f41v = _f41() if f41 is _S else f41
    f42v = _f42() if f42 is _S else f42
    f43v = _f43() if f43 is _S else f43
    if operations_provider is _S:
        ops = [_op(order_id=ORDER_ID)] if operations is _S else operations
        provider = ((lambda acc, a, b: ops) if ops is not None else None)
    else:
        provider = operations_provider
    return ilfa.run(
        ticker="T", order_id=ORDER_ID, live_account_id=ACCOUNT,
        f41_report=_write(tmp, "f41.json", f41v),
        f42_report=_write(tmp, "f42.json", f42v),
        f43_report=_write(tmp, "f43.json", f43v),
        output_json=str(tmp / "fa.json"), output_md=str(tmp / "fa.md"),
        operations_provider=provider, dividend_provider=dividend_provider,
        read_token_present=True, **kw)


# ─── confidence levels ────────────────────────────────────────────────────────

def test_high_confidence_order_id_match(tmp_path):
    rep = _run(tmp_path, operations=[_op(order_id=ORDER_ID)])
    assert rep["fill_attribution_confidence"] == "high"
    assert rep["attribution_method"] == "operations_order_id_match"
    assert rep["fill_found_in_operations"] is True
    assert rep["fill_operation_id"] == "op1"
    assert rep["fill_trade_id"] == "trade-1"
    assert rep["fill_quantity_units"] == Decimal("1")
    assert rep["fill_price"] == Decimal("276.14")
    # fill_commission (backward-compat) = raw signed; raw/abs explicit
    assert rep["fill_commission"] == Decimal("0.14")
    assert rep["fill_commission_raw"] == Decimal("0.14")
    assert rep["fill_commission_abs"] == Decimal("0.14")
    assert rep["fill_gross_amount"] == Decimal("276.14")
    assert rep["fill_cash_outflow"] == Decimal("276.28")
    assert rep["fill_net_amount"] == Decimal("276.28")  # == cash outflow
    assert rep["_exit_code"] == 0


def test_medium_confidence_instrument_qty_price_date(tmp_path):
    rep = _run(tmp_path, operations=[_op(order_id=None)])  # без orderId
    assert rep["fill_attribution_confidence"] == "medium"
    assert rep["attribution_method"] == \
        "operations_instrument_qty_price_date_match"
    assert rep["fill_found_in_operations"] is True


def test_low_confidence_no_operations(tmp_path):
    rep = _run(tmp_path, operations=[])  # операции прочитаны, совпадений нет
    assert rep["fill_attribution_confidence"] == "low"
    assert rep["attribution_method"] == "reports_only_derived"
    assert rep["fill_found_in_operations"] is False
    assert rep["fill_source"] == "f41_f42_f43_reports"
    # данные сделки из F4.1
    assert rep["fill_price"] == Decimal("276.14")
    assert rep["fill_quantity_units"] == Decimal("1")
    assert rep["_exit_code"] == 0


# ─── new-fill vs total separation ─────────────────────────────────────────────

def test_new_fill_pnl_from_fill_and_current_price(tmp_path):
    rep = _run(tmp_path)
    assert rep["estimated_new_fill_current_value"] == Decimal("268.26")
    assert rep["estimated_new_fill_unrealized_pnl"] == Decimal("-7.88")
    assert rep["estimated_new_fill_unrealized_pnl_pct"] == Decimal("-2.8536")
    assert rep["estimated_new_fill_weight_in_position_pct"] == Decimal("3.7037")


def test_total_pnl_kept_separate_from_new_fill(tmp_path):
    rep = _run(tmp_path)
    # total из F4.3 для всех 27 units
    assert rep["current_total_position_units"] == Decimal("27")
    assert rep["current_total_unrealized_pnl"] == Decimal("-965.52")
    assert rep["current_total_position_source"] == "F4.3 position report"
    # new-fill PnL — отдельная, маленькая величина, не равна total
    assert rep["estimated_new_fill_unrealized_pnl"] == Decimal("-7.88")
    assert rep["estimated_new_fill_unrealized_pnl"] != \
        rep["current_total_unrealized_pnl"]


# ─── previous position reconstruction (estimated) ─────────────────────────────

def test_previous_position_reconstruction_marked_estimated(tmp_path):
    rep = _run(tmp_path)
    assert rep["estimated_previous_position_units"] == Decimal("26")
    assert rep["estimated_previous_average_price"] == Decimal("305.0923")
    assert rep["estimated_previous_position_value"] == Decimal("7932.40")
    assert rep["old_position_estimation_warning"]
    assert "estimated" in rep["old_position_estimation_warning"].lower()


def test_no_previous_when_position_equals_fill(tmp_path):
    f43 = _f43()
    f43["position_quantity_units"] = 1
    f43["position_quantity_lots"] = 1
    rep = _run(tmp_path, f43=f43)
    assert rep["estimated_previous_position_units"] == Decimal("0")
    assert "прежней позиции нет" in rep["old_position_estimation_warning"].lower()


# ─── no guessing: commission / income ─────────────────────────────────────────

def test_commission_negative_sign_buy_cash_outflow(tmp_path):
    # Реальный кейс: брокер вернул комиссию -0.14 для BUY; отток = gross + |comm|.
    rep = _run(tmp_path, operations=[_op(
        order_id=ORDER_ID, commission=COMM_NEG_014,
        price=PRICE_27608, payment=PAYMENT_27608)])
    assert rep["fill_gross_amount"] == Decimal("276.08")
    assert rep["fill_commission_raw"] == Decimal("-0.14")
    assert rep["fill_commission_abs"] == Decimal("0.14")
    assert rep["fill_cash_outflow"] == Decimal("276.22")
    assert rep["fill_net_amount"] == Decimal("276.22")  # не 275.94
    assert rep["fill_cash_outflow_formula"]
    # отрицательная комиссия — нормальная конвенция, предупреждения о комиссии нет
    assert not any("комисси" in w.lower() for w in rep["warnings"])


def test_commission_positive_sign_same_cash_outflow(tmp_path):
    rep = _run(tmp_path, operations=[_op(
        order_id=ORDER_ID, commission=COMM_POS_014,
        price=PRICE_27608, payment=PAYMENT_27608)])
    assert rep["fill_commission_raw"] == Decimal("0.14")
    assert rep["fill_commission_abs"] == Decimal("0.14")
    assert rep["fill_cash_outflow"] == Decimal("276.22")
    assert rep["fill_net_amount"] == Decimal("276.22")


def test_commission_not_guessed_when_unavailable(tmp_path):
    rep = _run(tmp_path, operations=[_op(order_id=ORDER_ID, with_commission=False)])
    assert rep["fill_commission"] is None
    assert rep["fill_commission_raw"] is None
    assert rep["fill_commission_abs"] is None
    assert rep["fill_cash_outflow"] is None
    assert rep["fill_net_amount"] is None
    assert any("комисси" in w.lower() for w in rep["warnings"])


def test_income_not_guessed_when_unavailable(tmp_path):
    rep = _run(tmp_path, dividend_provider=None)
    assert rep["base_monthly_living_basket_rub"] == 150000
    assert rep["estimated_income_contribution_rub_monthly"] is None
    assert rep["estimated_income_contribution_rub_yearly"] is None
    assert rep["income_target_coverage_pct"] is None
    assert rep["income_data_source"] is None
    assert any("не угадыва" in w.lower() or "надёжных данных" in w.lower()
               for w in rep["warnings"])


def test_income_computed_when_reliable(tmp_path):
    def prov(instr):
        return {"annual_dividend_per_share_rub": "30", "source": "test_div"}
    rep = _run(tmp_path, dividend_provider=prov)
    assert rep["estimated_income_contribution_rub_yearly"] == Decimal("30.00")
    assert rep["estimated_income_contribution_rub_monthly"] == Decimal("2.50")
    assert rep["income_data_source"] == "test_div"


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
    assert g[ilfa.GUARD_LIVE_ORDER_SENT] is False
    assert g["post_order_called"] is False
    assert g[ilfa.GUARD_CANCEL_CALLED] is False
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
    assert tp["live_trading_token_required"] is False
    assert tp["live_token_used"] is False
    assert tp["sandbox_token_used"] is False
    assert tp["token_printed"] is False


def test_no_token_fails_cleanly_no_network(tmp_path):
    calls = {"n": 0}

    def watchdog(*a, **k):
        calls["n"] += 1
        raise AssertionError("network must not be called")

    rep = ilfa.run(
        ticker="T", order_id=ORDER_ID, live_account_id=ACCOUNT,
        f41_report=str(tmp_path / "no41.json"),
        f42_report=str(tmp_path / "no42.json"),
        f43_report=str(tmp_path / "no43.json"),
        output_json=str(tmp_path / "fa.json"), output_md=str(tmp_path / "fa.md"),
        client=None, operations_provider=None, positions_provider=None,
        client_error="EnvironmentError: TINKOFF_TOKEN не задана",
        read_token_present=False)
    assert calls["n"] == 0
    assert any("клиент недоступен" in e.lower() for e in rep["errors"])
    assert rep["_exit_code"] == 1
    assert rep["token_policy"]["read_only_token_present"] is False
    assert rep["guards"]["live_token_used"] is False


# ─── required args / отчёты ───────────────────────────────────────────────────

def test_required_args(tmp_path):
    for bad in ({"ticker": ""}, {"order_id": ""}, {"live_account_id": ""}):
        kw = {"ticker": "T", "order_id": ORDER_ID, "live_account_id": ACCOUNT,
              "output_json": str(tmp_path / "x.json"),
              "output_md": str(tmp_path / "x.md")}
        kw.update(bad)
        with pytest.raises(ilfa.FillAttributionError):
            ilfa.run(**kw)


def test_reports_created_and_fields(tmp_path):
    rep = _run(tmp_path)
    assert Path(rep["_output_json"]).exists()
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "F4.4" in md
    assert "Read-only attribution" in md
    data = json.loads(Path(rep["_output_json"]).read_text(encoding="utf-8"))
    for key in ("stage", "mode", "ticker", "order_id", "live_account_id_masked",
                "order_status", "lots_executed", "execution_price_from_f41",
                "instrument_uid", "figi", "lot_size", "current_total_position_units",
                "current_total_unrealized_pnl", "fill_found_in_operations",
                "fill_quantity_units", "fill_price", "fill_commission",
                "fill_commission_raw", "fill_commission_abs", "fill_cash_outflow",
                "fill_cash_outflow_formula", "fill_net_amount",
                "fill_attribution_confidence", "attribution_method",
                "estimated_previous_position_units", "estimated_new_fill_unrealized_pnl",
                "base_monthly_living_basket_rub", "checked_at", "guards",
                "token_policy", "warnings", "errors"):
        assert key in data
    assert data["stage"] == "F4_4_LIVE_FILL_ATTRIBUTION_READ_ONLY"
    assert data["mode"] == "FILL_ATTRIBUTION_READ_ONLY"


def test_default_output_paths():
    assert ilfa.DEFAULT_OUTPUT_JSON == \
        "data/reports/income_live_fill_attribution_report.json"
    assert ilfa.DEFAULT_OUTPUT_MD == \
        "data/reports/income_live_fill_attribution_report.md"


# ─── статическая проверка: нет запрещённых литералов ──────────────────────────

def test_module_source_has_no_forbidden_literals():
    src = Path(ilfa.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service", "post" "Order(", "cancel" "Order(",
        "place" "_order", "submit" "_order", "cancel" "_order",
        "live" "_order", "order" "_client",
    )
    for tok in forbidden:
        assert tok not in src, tok
