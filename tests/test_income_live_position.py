"""
Тесты F4.3 income_live_position — read-only сверка позиции с завершённой заявкой.

Никакой сети: portfolio positions инъектируются провайдером. Проверяем сверку
(FILL / 1 лот / позиция найдена), отсутствие угадывания дохода, маскирование
account, отсутствие любых исполняющих вызовов и неиспользование live/sandbox токена.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from modules import income_live_position as ilp

ORDER_ID = "80578688754"
ACCOUNT = "2000001918"
FIGI = "BBG004730N88"
UID = "87db07bc-0e02-4e29-90bb-05e8ef791d7b"
FILL = "EXECUTION_REPORT_STATUS_FILL"
NEW = "EXECUTION_REPORT_STATUS_NEW"


def _f41(*, figi=FIGI, uid=UID, class_code="TQBR", lot_size=1,
         order_id=ORDER_ID, status=FILL, lots_exec=1, lots_req=1) -> dict:
    return {
        "live_order_request_sanitized": {
            "instrument": {"ticker": "T", "figi": figi, "uid": uid,
                           "class_code": class_code}},
        "lot_size": lot_size,
        "live_order_result": {"order_id": order_id,
                              "execution_report_status": status},
        "live_order_response_sanitized": {"lots_requested": lots_req,
                                          "lots_executed": lots_exec},
    }


def _f42(*, order_id=ORDER_ID, status=FILL, lots_exec=1, lots_req=1) -> dict:
    return {"order_id": order_id, "execution_report_status": status,
            "lots_executed": lots_exec, "lots_requested": lots_req}


def _pos(*, units=1, lots=1, figi=FIGI, uid=UID) -> dict:
    return {
        "figi": figi,
        "instrumentUid": uid,
        "instrumentType": "share",
        "quantity": {"units": str(units), "nano": 0},
        "quantityLots": {"units": str(lots), "nano": 0},
        "averagePositionPrice": {"currency": "rub", "units": "276",
                                 "nano": 140000000},
        "currentPrice": {"currency": "rub", "units": "280", "nano": 0},
    }


def _write(tmp: Path, name: str, data) -> str:
    p = tmp / name
    if data is not None:
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


_SENTINEL = object()


def _run(tmp: Path, *, f41=_SENTINEL, f42=_SENTINEL, positions=_SENTINEL,
         dividend_provider=None, **kw):
    f41v = _f41() if f41 is _SENTINEL else f41
    f42v = _f42() if f42 is _SENTINEL else f42
    posv = [_pos()] if positions is _SENTINEL else positions
    provider = (lambda acc: posv) if posv is not None else None
    return ilp.run(
        ticker="T", order_id=ORDER_ID, live_account_id=ACCOUNT,
        f41_report=_write(tmp, "f41.json", f41v),
        f42_report=_write(tmp, "f42.json", f42v),
        output_json=str(tmp / "pos.json"), output_md=str(tmp / "pos.md"),
        positions_provider=provider, dividend_provider=dividend_provider,
        read_token_present=True, **kw)


# ─── успешная сверка ──────────────────────────────────────────────────────────

def test_reconcile_filled_with_matching_position(tmp_path):
    rep = _run(tmp_path)
    assert rep["reconciliation_passed"] is True
    assert rep["order_status"] == FILL
    assert rep["lots_executed"] == 1
    assert rep["position_found"] is True
    assert rep["position_quantity_units"] == Decimal("1")
    assert rep["position_quantity_lots"] == Decimal("1")
    assert rep["figi"] == FIGI
    assert rep["instrument_uid"] == UID
    assert rep["lot_size"] == 1
    assert rep["average_position_price"] == Decimal("276.14")
    assert rep["current_price"] == Decimal("280")
    assert rep["unrealized_pnl"] == Decimal("3.86")
    assert rep["current_position_value"] == Decimal("280")
    assert rep["currency"] == "rub"
    assert rep["_exit_code"] == 0


def test_match_by_figi_when_uid_absent(tmp_path):
    rep = _run(tmp_path, positions=[_pos(uid=None)])
    assert rep["position_found"] is True
    assert rep["reconciliation_passed"] is True


# ─── провалы сверки ───────────────────────────────────────────────────────────

def test_fail_when_status_not_fill(tmp_path):
    rep = _run(tmp_path, f41=_f41(status=NEW), f42=_f42(status=NEW))
    assert rep["reconciliation_passed"] is False
    assert any("status" in w for w in rep["reconciliation_warnings"])
    assert rep["_exit_code"] == 1


def test_fail_when_lots_executed_not_one(tmp_path):
    rep = _run(tmp_path, f41=_f41(lots_exec=2), f42=_f42(lots_exec=2),
               positions=[_pos(units=2, lots=2)])
    assert rep["reconciliation_passed"] is False
    assert any("lots_executed" in w for w in rep["reconciliation_warnings"])


def test_warn_when_position_missing(tmp_path):
    rep = _run(tmp_path, positions=[])  # портфель прочитан, позиции нет
    assert rep["position_found"] is False
    assert rep["reconciliation_passed"] is False
    assert any("не найден" in w.lower() for w in rep["reconciliation_warnings"])


def test_order_id_mismatch_fails(tmp_path):
    rep = _run(tmp_path, f42=_f42(order_id="999999"))
    assert rep["reconciliation_passed"] is False
    assert any("order_id" in w for w in rep["reconciliation_warnings"])


def test_position_larger_than_one_lot_warns_not_fail(tmp_path):
    rep = _run(tmp_path, positions=[_pos(units=3, lots=3)])
    assert rep["reconciliation_passed"] is True  # куплен ≥1 лот присутствует
    assert any("больше" in w for w in rep["reconciliation_warnings"])


# ─── income goal: без угадывания ──────────────────────────────────────────────

def test_income_unavailable_sets_null_and_warns(tmp_path):
    rep = _run(tmp_path, dividend_provider=None)
    assert rep["base_monthly_living_basket_rub"] == 150000
    assert rep["estimated_income_contribution_rub_monthly"] is None
    assert rep["estimated_income_contribution_rub_yearly"] is None
    assert rep["income_target_coverage_pct"] is None
    assert any("не угадыва" in w.lower() or "надёжных данных" in w.lower()
               for w in rep["warnings"])


def test_income_computed_when_reliable(tmp_path):
    def prov(instr):
        return {"annual_dividend_per_share_rub": "30", "source": "test_dividends"}
    rep = _run(tmp_path, dividend_provider=prov)
    # 1 unit * 30 = 30 yearly; monthly 2.5; coverage 2.5/150000*100
    assert rep["estimated_income_contribution_rub_yearly"] == Decimal("30.00")
    assert rep["estimated_income_contribution_rub_monthly"] == Decimal("2.50")
    assert rep["income_target_coverage_pct"] == Decimal("0.0017")


# ─── безопасность / токены / guards ───────────────────────────────────────────

def test_account_masked(tmp_path):
    rep = _run(tmp_path)
    assert rep["live_account_id_masked"]
    assert rep["live_account_id_masked"] != ACCOUNT
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
        assert secret not in js
        assert secret not in md


def test_guards_all_safe(tmp_path):
    rep = _run(tmp_path)
    g = rep["guards"]
    assert g[ilp.GUARD_LIVE_ORDER_SENT] is False
    assert g["post_order_called"] is False
    assert g[ilp.GUARD_CANCEL_CALLED] is False
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
    assert tp["live_trading_token_required"] is False
    assert tp["live_token_used"] is False
    assert tp["sandbox_token_used"] is False
    assert tp["token_printed"] is False
    assert tp["read_only_token_env"] == "TINKOFF_TOKEN"


def test_no_token_fails_cleanly_no_network(tmp_path):
    # client=None и positions_provider=None → нет провайдера → нет сетевого вызова
    rep = ilp.run(
        ticker="T", order_id=ORDER_ID, live_account_id=ACCOUNT,
        f41_report=str(tmp_path / "missing41.json"),
        f42_report=str(tmp_path / "missing42.json"),
        output_json=str(tmp_path / "pos.json"),
        output_md=str(tmp_path / "pos.md"),
        client=None, positions_provider=None,
        client_error="EnvironmentError: TINKOFF_TOKEN не задана",
        read_token_present=False)
    assert rep["reconciliation_passed"] is False
    assert any("клиент недоступен" in e.lower() for e in rep["errors"])
    assert rep["position_found"] is False
    assert rep["_exit_code"] == 1
    assert rep["token_policy"]["read_only_token_present"] is False


def test_required_args(tmp_path):
    for bad in ({"ticker": ""}, {"order_id": ""}, {"live_account_id": ""}):
        kw = {"ticker": "T", "order_id": ORDER_ID, "live_account_id": ACCOUNT,
              "output_json": str(tmp_path / "p.json"),
              "output_md": str(tmp_path / "p.md"),
              "positions_provider": lambda acc: [_pos()]}
        kw.update(bad)
        with pytest.raises(ilp.LivePositionError):
            ilp.run(**kw)


# ─── отчёты на диске ──────────────────────────────────────────────────────────

def test_reports_created(tmp_path):
    rep = _run(tmp_path)
    assert Path(rep["_output_json"]).exists()
    assert Path(rep["_output_md"]).exists()
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "F4.3" in md
    assert "Read-only reconciliation" in md
    data = json.loads(Path(rep["_output_json"]).read_text(encoding="utf-8"))
    for key in ("stage", "mode", "ticker", "order_id", "live_account_id_masked",
                "order_status", "lots_requested", "lots_executed", "instrument_uid",
                "figi", "class_code", "lot_size", "position_found",
                "reconciliation_passed", "reconciliation_warnings",
                "base_monthly_living_basket_rub", "checked_at", "guards",
                "token_policy"):
        assert key in data
    assert data["stage"] == "F4_3_LIVE_POSITION_RECONCILIATION_READ_ONLY"
    assert data["mode"] == "POSITION_READ_ONLY"


def test_default_output_paths():
    assert ilp.DEFAULT_OUTPUT_JSON == "data/reports/income_live_position_report.json"
    assert ilp.DEFAULT_OUTPUT_MD == "data/reports/income_live_position_report.md"


# ─── статическая проверка: нет запрещённых литералов ──────────────────────────

def test_module_source_has_no_forbidden_literals():
    src = Path(ilp.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service",
        "post" "Order(",
        "cancel" "Order(",
        "place" "_order",
        "submit" "_order",
        "cancel" "_order",
        "live" "_order",
        "order" "_client",
    )
    for tok in forbidden:
        assert tok not in src, tok
