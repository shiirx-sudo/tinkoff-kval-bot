"""
Тесты F3.2 sandbox account bootstrap (modules/income_sandbox_account).

Без реальной сети: sandbox-транспорт инъектируется как callable (тот же приём,
что в test_tinvest_sandbox_transport). Проверяем, что status — чистая инспекция,
list — read-only, open/pay-in мутируют sandbox только при точной фразе, токен
никогда не печатается/не пишется в отчёт, и не вызываются заявки/live-методы.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules import income_sandbox_account as isa
from modules.tinvest_sandbox_transport import VerifiedSandboxRestAdapter

ALLOWED_METHODS = {"GetSandboxAccounts", "OpenSandboxAccount", "SandboxPayIn"}


class _Recorder:
    """Записывает (method, payload, token); возвращает каноничные sandbox-ответы."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, payload, token):
        self.calls.append({"method": method, "payload": payload, "token": token})
        if method == "GetSandboxAccounts":
            return {"accounts": [{
                "id": "sb-acc-1",
                "type": "ACCOUNT_TYPE_TINKOFF",
                "name": "Sandbox",
                "status": "ACCOUNT_STATUS_OPEN",
                "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS",
                "openedDate": "2026-06-01T00:00:00Z",
            }]}
        if method == "OpenSandboxAccount":
            return {"accountId": "sb-acc-new-007"}
        if method == "SandboxPayIn":
            return {"balance": {"currency": "rub", "units": "100000", "nano": 0}}
        return {}


def _adapter():
    rec = _Recorder()
    return VerifiedSandboxRestAdapter(transport=rec), rec


def _run(tmp_path: Path, **kw):
    kw.setdefault("output_json", str(tmp_path / "rep.json"))
    kw.setdefault("output_md", str(tmp_path / "rep.md"))
    return isa.run(**kw)


# ─── status ────────────────────────────────────────────────────────────────────

def test_status_dry_run_writes_safe_report(tmp_path):
    rep = _run(tmp_path, action="status", sandbox_token="")
    assert rep["stage"] == "F3_2_SANDBOX_ACCOUNT_BOOTSTRAP"
    assert rep["action"] == "status"
    assert rep["mode"] == "DRY_RUN"
    assert rep["_exit_code"] == 0
    g = rep["guards"]
    assert g["live_order_sent"] is False
    assert g["sandbox_order_sent"] is False
    assert g["live_orders_service_used"] is False
    assert g["full_access_live_token_used"] is False
    assert g["live_token_used"] is False
    assert g["token_printed"] is False
    assert g["portfolio_mutated"] is False
    assert g["config_mutated"] is False
    assert g["telegram_sent"] is False
    assert g["no_live_execution"] is True
    assert g["no_order_execution"] is True
    assert g["sandbox_token_used"] is False
    assert Path(rep["_output_json"]).exists()
    assert Path(rep["_output_md"]).exists()


def test_status_does_not_call_api(tmp_path):
    adapter, rec = _adapter()
    _run(tmp_path, action="status", adapter=adapter, sandbox_token="t")
    assert rec.calls == []


# ─── list ────────────────────────────────────────────────────────────────────

def test_list_accounts_mocked_success(tmp_path):
    adapter, rec = _adapter()
    rep = _run(tmp_path, action="list", adapter=adapter, sandbox_token="sbx-token")
    assert rep["mode"] == "SANDBOX_ACCOUNT_LIST"
    assert rep["_exit_code"] == 0
    assert len(rec.calls) == 1
    assert rec.calls[0]["method"] == "GetSandboxAccounts"
    assert rec.calls[0]["token"] == "sbx-token"
    assert rep["sandbox_accounts"][0]["id"] == "sb-acc-1"
    assert rep["selected_sandbox_account_id"] == "sb-acc-1"
    assert rep["guards"]["sandbox_token_used"] is True


def test_list_requires_token(tmp_path):
    adapter, rec = _adapter()
    rep = _run(tmp_path, action="list", adapter=adapter, sandbox_token="")
    assert rep["_exit_code"] == 1
    assert rec.calls == []
    assert any("TINKOFF_SANDBOX_TOKEN" in e for e in rep["errors"])


def test_list_unconfigured_transport_blocks(tmp_path):
    rep = _run(tmp_path, action="list", sandbox_transport="unconfigured",
               sandbox_token="t")
    assert rep["_exit_code"] == 1
    assert any("SANDBOX_TRANSPORT_UNCONFIGURED" in e for e in rep["errors"])


# ─── open ────────────────────────────────────────────────────────────────────

def test_open_without_confirmation_blocks(tmp_path):
    adapter, rec = _adapter()
    rep = _run(tmp_path, action="open", adapter=adapter, sandbox_token="t",
               confirm=None)
    assert rep["_exit_code"] == 1
    assert rep["sandbox_account_opened"] is False
    assert rep["required_confirmation_phrase"] == "CONFIRM SANDBOX ACCOUNT OPEN"
    assert rec.calls == []


def test_open_wrong_confirmation_blocks(tmp_path):
    adapter, rec = _adapter()
    rep = _run(tmp_path, action="open", adapter=adapter, sandbox_token="t",
               confirm="CONFIRM SANDBOX ACCOUNT open")
    assert rep["_exit_code"] == 1
    assert rep["confirmation_matched"] is False
    assert rep["sandbox_account_opened"] is False
    assert rec.calls == []


def test_open_exact_confirmation_calls_open_once(tmp_path):
    adapter, rec = _adapter()
    rep = _run(tmp_path, action="open", adapter=adapter, sandbox_token="sbx-token",
               confirm="CONFIRM SANDBOX ACCOUNT OPEN")
    assert rep["_exit_code"] == 0
    assert rep["confirmation_matched"] is True
    assert rep["mode"] == "SANDBOX_ACCOUNT_OPEN"
    assert rep["sandbox_account_opened"] is True
    assert rep["selected_sandbox_account_id"] == "sb-acc-new-007"
    assert len(rec.calls) == 1
    assert rec.calls[0]["method"] == "OpenSandboxAccount"
    assert rep["guards"]["sandbox_token_used"] is True


def test_open_requires_token_even_with_confirmation(tmp_path):
    adapter, rec = _adapter()
    rep = _run(tmp_path, action="open", adapter=adapter, sandbox_token="",
               confirm="CONFIRM SANDBOX ACCOUNT OPEN")
    assert rep["_exit_code"] == 1
    assert rep["sandbox_account_opened"] is False
    assert rec.calls == []
    assert any("TINKOFF_SANDBOX_TOKEN" in e for e in rep["errors"])


# ─── pay-in ────────────────────────────────────────────────────────────────────

def test_payin_without_account_id_blocks(tmp_path):
    adapter, rec = _adapter()
    rep = _run(tmp_path, action="pay-in", adapter=adapter, sandbox_token="t",
               pay_in_rub=100000,
               confirm="CONFIRM SANDBOX PAYIN 100000 RUB")
    assert rep["_exit_code"] == 1
    assert rep["sandbox_payin_done"] is False
    assert rec.calls == []


def test_payin_without_amount_blocks(tmp_path):
    adapter, rec = _adapter()
    rep = _run(tmp_path, action="pay-in", adapter=adapter, sandbox_token="t",
               sandbox_account_id="sb-acc-1", pay_in_rub=None)
    assert rep["_exit_code"] == 1
    assert rep["sandbox_payin_done"] is False
    assert rec.calls == []


def test_payin_wrong_confirmation_blocks(tmp_path):
    adapter, rec = _adapter()
    rep = _run(tmp_path, action="pay-in", adapter=adapter, sandbox_token="t",
               sandbox_account_id="sb-acc-1", pay_in_rub=100000,
               confirm="CONFIRM SANDBOX PAYIN 999 RUB")
    assert rep["_exit_code"] == 1
    assert rep["confirmation_matched"] is False
    assert rep["sandbox_payin_done"] is False
    assert rec.calls == []


def test_payin_exact_confirmation_calls_payin_once(tmp_path):
    adapter, rec = _adapter()
    rep = _run(tmp_path, action="pay-in", adapter=adapter, sandbox_token="sbx-token",
               sandbox_account_id="sb-acc-1", pay_in_rub=100000,
               confirm="CONFIRM SANDBOX PAYIN 100000 RUB")
    assert rep["_exit_code"] == 0
    assert rep["confirmation_matched"] is True
    assert rep["mode"] == "SANDBOX_PAYIN"
    assert rep["sandbox_payin_done"] is True
    assert rep["selected_sandbox_account_id"] == "sb-acc-1"
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["method"] == "SandboxPayIn"
    # MoneyValue: units int64 → строка, currency rub
    assert call["payload"]["accountId"] == "sb-acc-1"
    assert call["payload"]["amount"] == {"currency": "rub", "units": "100000", "nano": 0}
    assert rep["guards"]["sandbox_token_used"] is True


# ─── token isolation / safety ───────────────────────────────────────────────────

def test_sandbox_token_required_for_real_action(tmp_path):
    # Без sandbox-токена реальные list/open/pay-in заблокированы.
    for kw in (
        {"action": "list"},
        {"action": "open", "confirm": "CONFIRM SANDBOX ACCOUNT OPEN"},
        {"action": "pay-in", "sandbox_account_id": "a", "pay_in_rub": 1000,
         "confirm": "CONFIRM SANDBOX PAYIN 1000 RUB"},
    ):
        adapter, rec = _adapter()
        rep = _run(tmp_path, adapter=adapter, sandbox_token="", **kw)
        assert rep["_exit_code"] == 1
        assert rec.calls == []


def test_tinkoff_live_token_not_read(tmp_path, monkeypatch):
    # Даже если live TINKOFF_TOKEN задан, модуль его не читает и не использует.
    monkeypatch.setenv("TINKOFF" "_TOKEN", "LIVE-FULL-ACCESS-SECRET")
    monkeypatch.delenv("TINKOFF_SANDBOX_TOKEN", raising=False)
    rep = _run(tmp_path, action="status")
    assert rep["guards"]["live_token_used"] is False
    assert rep["guards"]["full_access_live_token_used"] is False
    blob = json.dumps(rep, ensure_ascii=False)
    assert "LIVE-FULL-ACCESS-SECRET" not in blob


def test_token_never_appears_in_report(tmp_path):
    adapter, _ = _adapter()
    secret = "SECRET-SANDBOX-TOKEN-XYZ"
    rep = _run(tmp_path, action="open", adapter=adapter, sandbox_token=secret,
               confirm="CONFIRM SANDBOX ACCOUNT OPEN")
    blob = Path(rep["_output_json"]).read_text(encoding="utf-8")
    blob += Path(rep["_output_md"]).read_text(encoding="utf-8")
    blob += json.dumps(rep, ensure_ascii=False)
    assert secret not in blob


def test_account_id_appears_only_as_expected(tmp_path):
    adapter, _ = _adapter()
    rep = _run(tmp_path, action="open", adapter=adapter, sandbox_token="t",
               confirm="CONFIRM SANDBOX ACCOUNT OPEN")
    # account id показываем (он нужен владельцу), но только в selected/accounts.
    assert rep["selected_sandbox_account_id"] == "sb-acc-new-007"
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "sb-acc-new-007" in md


def test_only_sandbox_account_methods_called(tmp_path):
    for kw in (
        {"action": "list"},
        {"action": "open", "confirm": "CONFIRM SANDBOX ACCOUNT OPEN"},
        {"action": "pay-in", "sandbox_account_id": "a", "pay_in_rub": 1000,
         "confirm": "CONFIRM SANDBOX PAYIN 1000 RUB"},
    ):
        adapter, rec = _adapter()
        _run(tmp_path, adapter=adapter, sandbox_token="t", **kw)
        for call in rec.calls:
            assert call["method"] in ALLOWED_METHODS


def test_module_source_has_no_live_order_or_full_token_apis():
    src = Path(isa.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service",
        "post" "Order(",
        "cancel" "Order(",
        "place" "_order",
        "submit" "_order",
        "place" "_limit_" "order",
        "order" "_client",
        "LIVE_" "EXECUTION",
        "live" "_order",
        "TINKOFF" "_TOKEN",
        "FULL_" "ACCESS",
    )
    for tok in forbidden:
        assert tok not in src, tok


def test_unknown_action_raises(tmp_path):
    with pytest.raises(isa.SandboxAccountError):
        _run(tmp_path, action="close", sandbox_token="t")
