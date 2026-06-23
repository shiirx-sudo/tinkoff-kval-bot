"""
Тесты F4.0 income_live_readiness — pre-live readiness report (НЕ live-исполнение).

Никакой сети, никаких заявок: модуль только читает F3 sandbox-отчёт, проверяет
gate и пишет readiness json/md. Live order-endpoints не импортируются и не
используются; токены не читаются по значению и не печатаются.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules import income_live_readiness as ilr


# ─── фикстуры ─────────────────────────────────────────────────────────────────

def _sandbox_report(*, stage="F3_SANDBOX_MANUAL_CONFIRMED_EXECUTION",
                    mode="SANDBOX_SEND", sent=True,
                    status="EXECUTION_REPORT_STATUS_FILL",
                    order_id="078f7639-05c1-402e-8432-cb1720603352",
                    **guard_over):
    guards = {
        "live_order_sent": False,
        "sandbox_order_sent": True,
        "live_orders_service_used": False,
        "full_access_live_token_used": False,
        "token_printed": False,
    }
    guards.update(guard_over)
    return {
        "kind": "income_sandbox_execution",
        "stage": stage,
        "mode": mode,
        "ticker": "T",
        "sandbox_order_result": {
            "sandbox_order_id": order_id,
            "execution_report_status": status,
            "sandbox_order_sent": sent,
            "sandbox_order_state_read": True,
            "error": None,
        },
        "guards": guards,
    }


def _write_sandbox(tmp_path: Path, report: dict | None) -> Path:
    p = tmp_path / "income_sandbox_execution_report.json"
    if report is not None:
        p.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return p


def _run(tmp_path: Path, sandbox_report: dict | None, **kw):
    sb = _write_sandbox(tmp_path, sandbox_report)
    return ilr.run(
        sandbox_report=str(sb),
        output_json=str(tmp_path / "f40.json"),
        output_md=str(tmp_path / "f40.md"),
        live_token_present=kw.pop("live_token_present", False),
        **kw,
    )


# ─── gate: блокировки ─────────────────────────────────────────────────────────

def test_missing_sandbox_report_blocks(tmp_path):
    rep = _run(tmp_path, None)  # файл не создаётся
    assert rep["ready_for_f4_live_manual_order"] is False
    assert rep["sandbox_gate_passed"] is False
    assert any("sandbox execution report" in r.lower()
               for r in rep["blocking_reasons"])
    assert rep["_exit_code"] == 1


def test_sandbox_order_not_sent_blocks(tmp_path):
    rep = _run(tmp_path, _sandbox_report(sent=False, status=None))
    assert rep["ready_for_f4_live_manual_order"] is False
    assert any("sandbox_order_sent" in r for r in rep["blocking_reasons"])
    assert rep["_exit_code"] == 1


def test_execution_status_not_fill_blocks(tmp_path):
    rep = _run(tmp_path, _sandbox_report(status="EXECUTION_REPORT_STATUS_NEW"))
    assert rep["ready_for_f4_live_manual_order"] is False
    assert any("execution_report_status" in r for r in rep["blocking_reasons"])
    assert rep["_exit_code"] == 1


def test_wrong_stage_blocks(tmp_path):
    rep = _run(tmp_path, _sandbox_report(stage="SOMETHING_ELSE"))
    assert rep["ready_for_f4_live_manual_order"] is False
    assert any("stage" in r for r in rep["blocking_reasons"])


def test_dry_run_sandbox_mode_blocks(tmp_path):
    rep = _run(tmp_path, _sandbox_report(mode="DRY_RUN"))
    assert rep["ready_for_f4_live_manual_order"] is False
    assert any("mode" in r for r in rep["blocking_reasons"])


def test_unsafe_sandbox_guard_blocks(tmp_path):
    rep = _run(tmp_path, _sandbox_report(live_order_sent=True))
    assert rep["ready_for_f4_live_manual_order"] is False
    assert any("guard" in r for r in rep["blocking_reasons"])


# ─── gate: успех ──────────────────────────────────────────────────────────────

def test_good_sandbox_report_ready_true(tmp_path):
    rep = _run(tmp_path, _sandbox_report())
    assert rep["sandbox_gate_passed"] is True
    assert rep["ready_for_f4_live_manual_order"] is True
    assert rep["blocking_reasons"] == []
    assert rep["_exit_code"] == 0
    assert rep["sandbox_order_id"] == "078f7639-05c1-402e-8432-cb1720603352"
    assert rep["sandbox_execution_report_status"] == "EXECUTION_REPORT_STATUS_FILL"


# ─── tiny live plan ───────────────────────────────────────────────────────────

def test_live_plan_fixed_defaults(tmp_path):
    rep = _run(tmp_path, _sandbox_report())
    lp = rep["live_plan"]
    assert lp["ticker"] == "T"
    assert lp["side"] == "BUY"
    assert lp["order_type"] == "LIMIT"
    assert lp["lots"] == 1
    assert lp["max_order_rub"] == 300
    assert lp["instrument_id_source"] == "uid-first"


def test_future_confirmation_phrase_exact(tmp_path):
    rep = _run(tmp_path, _sandbox_report())
    assert rep["required_future_confirmation_phrase"] == \
        "CONFIRM LIVE BUY T 1 LOT MAX 300 RUB"
    assert rep["live_plan"]["required_future_confirmation_phrase"] == \
        "CONFIRM LIVE BUY T 1 LOT MAX 300 RUB"


def test_phrase_plural_for_multiple_lots(tmp_path):
    rep = _run(tmp_path, _sandbox_report(), lots=3, max_order_rub=900)
    assert rep["required_future_confirmation_phrase"] == \
        "CONFIRM LIVE BUY T 3 LOTS MAX 900 RUB"


def test_bad_lots_blocks(tmp_path):
    with pytest.raises(ilr.LiveReadinessError):
        _run(tmp_path, _sandbox_report(), lots=0)


def test_bad_max_order_rub_blocks(tmp_path):
    with pytest.raises(ilr.LiveReadinessError):
        _run(tmp_path, _sandbox_report(), max_order_rub=0)


# ─── token policy / без утечки токена ─────────────────────────────────────────

def test_token_policy_present_flag(tmp_path):
    rep = _run(tmp_path, _sandbox_report(), live_token_present=True)
    tp = rep["token_policy"]
    assert tp["live_trading_token_env"] == "TINKOFF_LIVE_TRADING_TOKEN"
    assert tp["live_trading_token_present"] is True
    assert tp["tinkoff_token_used_for_execution"] is False
    assert tp["sandbox_token_used_for_live"] is False
    assert tp["token_printed"] is False


def test_token_presence_read_from_env_not_value(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKOFF_LIVE_TRADING_TOKEN", "super-secret-live-token")
    sb = _write_sandbox(tmp_path, _sandbox_report())
    rep = ilr.run(sandbox_report=str(sb),
                  output_json=str(tmp_path / "f40.json"),
                  output_md=str(tmp_path / "f40.md"))
    assert rep["token_policy"]["live_trading_token_present"] is True
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "super-secret-live-token" not in js
    assert "super-secret-live-token" not in md


def test_no_token_value_leak_in_reports(tmp_path, monkeypatch):
    # любые токены в окружении не должны попадать в отчёт
    monkeypatch.setenv("TINKOFF_TOKEN", "LIVE-READ-SECRET")
    monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "SANDBOX-SECRET")
    rep = _run(tmp_path, _sandbox_report())
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    for secret in ("LIVE-READ-SECRET", "SANDBOX-SECRET"):
        assert secret not in js
        assert secret not in md


# ─── guards ───────────────────────────────────────────────────────────────────

def test_guards_locked_safe(tmp_path):
    rep = _run(tmp_path, _sandbox_report())
    g = rep["guards"]
    assert g["live_order_sent"] is False
    assert g["sandbox_order_sent"] is False
    assert g["live_orders_service_used"] is False
    assert g["full_access_live_token_used"] is False
    assert g["live_token_used"] is False
    assert g["sandbox_token_used"] is False
    assert g["token_printed"] is False
    assert g["portfolio_mutated"] is False
    assert g["config_mutated"] is False
    assert g["telegram_sent"] is False
    assert g["no_live_execution"] is True
    assert g["no_order_execution"] is True


def test_stage_and_mode(tmp_path):
    rep = _run(tmp_path, _sandbox_report())
    assert rep["stage"] == "F4_0_PRE_LIVE_READINESS"
    assert rep["mode"] == "READINESS_ONLY"
    assert rep["next_stage"].startswith("F4.1")


# ─── отчёты на диске ──────────────────────────────────────────────────────────

def test_json_and_md_created(tmp_path):
    rep = _run(tmp_path, _sandbox_report())
    assert Path(rep["_output_json"]).exists()
    assert Path(rep["_output_md"]).exists()
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "No live orders were sent." in md
    assert "No sandbox orders were sent." in md
    assert "F4.0 pre-live readiness only" in md
    assert "CONFIRM LIVE BUY T 1 LOT MAX 300 RUB" in md


# ─── статическая проверка: нет live order-exec API ────────────────────────────

def test_module_source_has_no_live_order_execution_apis():
    src = Path(ilr.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service",
        "post" "Order(",
        "cancel" "Order(",
        "place" "_order",
        "submit" "_order",
        "place" "_limit_" "order",
        "order" "_client",
        "LIVE_" "EXECUTION",
        "live" "_order",  # цельного литерала нет: guard-ключи импортируются
        "TINKOFF" "_TOKEN",
        "TINKOFF" "_SANDBOX_TOKEN",
    )
    for tok in forbidden:
        assert tok not in src, tok
