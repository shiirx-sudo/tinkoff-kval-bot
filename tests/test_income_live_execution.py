"""
Тесты F4.1 income_live_execution — tiny LIVE manual-confirmed order.

Никакой реальной сети: live-адаптер VerifiedLiveRestAdapter получает инъецируемый
fake-транспорт, поэтому ни одна реальная live-заявка не отправляется. Проверяем
все gate'ы, политику токена (только TINKOFF_LIVE_TRADING_TOKEN, значение не течёт),
ровно одну отправку, отсутствие ретраев/market/sell, UUID orderId, uid-first
instrumentId и quantity-строку.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from modules import income_live_execution as ile
from modules.tinvest_live_transport import (
    _METHOD_POST,
    _METHOD_STATE,
    VerifiedLiveRestAdapter,
)

PHRASE = "CONFIRM LIVE BUY T 1 LOT MAX 300 RUB"
LIVE_ACCOUNT = "2000123456"
SECRET_LIVE_TOKEN = "SUPER-SECRET-LIVE-TOKEN"


# ─── фикстуры ─────────────────────────────────────────────────────────────────

def _readiness(**over) -> dict:
    rep = {
        "kind": "income_live_readiness",
        "stage": "F4_0_PRE_LIVE_READINESS",
        "mode": "READINESS_ONLY",
        "ticker": "T",
        "sandbox_gate_passed": True,
        "ready_for_f4_live_manual_order": True,
        "required_future_confirmation_phrase": PHRASE,
        "live_plan": {
            "ticker": "T", "side": "BUY", "order_type": "LIMIT",
            "lots": 1, "max_order_rub": 300,
            "instrument_id_source": "uid-first",
            "required_future_confirmation_phrase": PHRASE,
        },
        "guards": {
            "live_order_sent": False,
            "live_orders_service_used": False,
            "no_live_execution": True,
            "no_order_execution": True,
        },
    }
    rep.update(over)
    return rep


def _preview_row(**over) -> dict:
    row = {
        "ticker": "T",
        "figi": "BBG004730N88",
        "uid": "87db07bc-0e02-4e29-90bb-05e8ef791d7b",
        "class_code": "TQBR",
        "source_proposed_action": "BUY_CANDIDATE",
        "preview_status": "PREVIEW_READY",
        "preview_lots": 1,
        "preview_quantity": 1,
        "lot_size": 1,
        "reference_price": 150.0,
        "reference_price_status": "OK",
        "estimated_total_rub": 150.0,
        "manual_confirmation_required": True,
        "order_send_allowed": False,
        "auto_execution_allowed": False,
        "full_access_token_required": False,
        "orders_service_allowed": False,
    }
    row.update(over)
    return row


def _preview(rows=None) -> dict:
    return {
        "kind": "income_order_preview",
        "previews": rows if rows is not None else [_preview_row()],
    }


class FakeTransport:
    """Инъецируемый транспорт: считает вызовы, реальной сети нет."""

    def __init__(self, *, post_response=None, raise_on_post: Exception | None = None):
        self.calls: list[dict] = []
        self._post_response = post_response or {
            "orderId": str(uuid.uuid4()),
            "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
            "lotsRequested": "1",
            "lotsExecuted": "1",
            "totalOrderAmount": {"currency": "rub", "units": "150", "nano": 0},
        }
        self._raise_on_post = raise_on_post

    def __call__(self, method: str, payload: dict, token: str) -> dict:
        self.calls.append({"method": method, "payload": payload, "token": token})
        if method == _METHOD_POST:
            if self._raise_on_post is not None:
                raise self._raise_on_post
            return self._post_response
        if method == _METHOD_STATE:
            return {"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                    "lotsExecuted": "1"}
        return {}

    @property
    def post_calls(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == _METHOD_POST]


def _write(tmp: Path, name: str, data: dict | None) -> str:
    p = tmp / name
    if data is not None:
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def trad_available(**over):
    """Read-only tradability-провайдер: инструмент доступен для BUY LIMIT."""
    base = {
        "trading_status": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
        "api_trade_available_flag": True,
        "buy_available_flag": True,
        "for_qual_investor_flag": False,
        "exchange": "MOEX",
        "class_code": "TQBR",
        "min_price_increment": {"units": "0", "nano": 20000000},
        "source": "test_provider",
    }
    base.update(over)
    return lambda instrument: dict(base)


def trad_unavailable(**over):
    """Read-only tradability-провайдер: инструмент НЕ доступен (как 30079)."""
    return trad_available(
        trading_status="SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING",
        api_trade_available_flag=False, **over)


_UNSET = object()


def _run(tmp: Path, *, readiness=None, preview=None, transport=None,
         tradability=_UNSET, **kw) -> dict:
    rp = _write(tmp, "readiness.json", readiness)
    pp = _write(tmp, "preview.json", preview)
    adapter = None
    if transport is not None:
        adapter = VerifiedLiveRestAdapter(transport=transport)
    # По умолчанию tradability проходит (изоляция от сети). Тесты, проверяющие сам
    # tradability-gate, передают tradability=None (не проверено) или trad_unavailable().
    provider = trad_available() if tradability is _UNSET else tradability
    return ile.run(
        readiness_report=rp,
        preview_report=pp,
        output_json=str(tmp / "exec.json"),
        output_md=str(tmp / "exec.md"),
        adapter=adapter,
        tradability_provider=provider,
        **kw,
    )


# ─── dry-run ──────────────────────────────────────────────────────────────────

def test_dry_run_no_token_required(tmp_path, monkeypatch):
    monkeypatch.delenv("TINKOFF_LIVE_TRADING_TOKEN", raising=False)
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview())
    assert rep["mode"] == "DRY_RUN"
    assert rep[ile.FIELD_SENT] is False
    assert rep["guards"][ile.GUARD_KEY_LIVE_ORDER_SENT] is False
    assert rep["_exit_code"] == 0


def test_dry_run_sends_no_order(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport)
    assert rep["mode"] == "DRY_RUN"
    assert transport.post_calls == []
    assert rep[ile.FIELD_SENT] is False


def test_dry_run_builds_wire_preview_uid_first(tmp_path):
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview())
    wire = rep[ile.FIELD_REQUEST_WIRE]
    assert wire is not None
    assert wire["instrument_id_source"] == "uid"
    assert wire["instrumentId"] == "87db07bc-0e02-4e29-90bb-05e8ef791d7b"
    assert wire["orderId_is_uuid"] is True
    assert wire["orderId_version"] == 4
    assert isinstance(wire["quantity"], str)


def test_dry_run_reports_created(tmp_path):
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview())
    assert Path(rep["_output_json"]).exists()
    assert Path(rep["_output_md"]).exists()
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "F4.1" in md
    assert "No live orders were sent." in md
    assert PHRASE in md


def test_strict_dry_run_contract(tmp_path):
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview())
    g = rep["guards"]
    assert rep["stage"] == "F4_1_TINY_LIVE_MANUAL_CONFIRMED_ORDER"
    assert rep["mode"] == "DRY_RUN"
    assert rep["ticker"] == "T"
    assert rep[ile.FIELD_SENT] is False
    assert g["sandbox_order_sent"] is False
    assert g["tinkoff_token_used_for_execution"] is False
    assert g["sandbox_token_used_for_live"] is False
    assert g["token_printed"] is False
    assert g["market_order_used"] is False
    assert g["manual_confirmation_required"] is True
    assert g["no_retries"] is True
    assert g["one_order_max"] is True
    lp = rep["live_plan"]
    assert (lp["ticker"], lp["side"], lp["order_type"], lp["lots"],
            lp["max_order_rub"]) == ("T", "BUY", "LIMIT", 1, 300)
    assert rep["required_confirmation_phrase"] == PHRASE


# ─── readiness gate ───────────────────────────────────────────────────────────

def test_missing_readiness_blocks_send(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=None, preview=_preview(), transport=transport,
               send_live=True, confirm=PHRASE, live_account_id=LIVE_ACCOUNT,
               live_token=SECRET_LIVE_TOKEN)
    assert rep[ile.FIELD_SENT] is False
    assert rep["readiness_gate_passed"] is False
    assert transport.post_calls == []
    assert rep["_exit_code"] == 1


def test_readiness_not_ready_blocks_send(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(ready_for_f4_live_manual_order=False),
               preview=_preview(), transport=transport, send_live=True,
               confirm=PHRASE, live_account_id=LIVE_ACCOUNT,
               live_token=SECRET_LIVE_TOKEN)
    assert rep["readiness_gate_passed"] is False
    assert rep[ile.FIELD_SENT] is False
    assert transport.post_calls == []
    assert rep["_exit_code"] == 1


def test_readiness_wrong_stage_blocks(tmp_path):
    rep = _run(tmp_path, readiness=_readiness(stage="SOMETHING"),
               preview=_preview())
    assert rep["readiness_gate_passed"] is False
    assert any("stage" in r for r in rep["blocking_reasons"])


# ─── preview gate ─────────────────────────────────────────────────────────────

def test_missing_preview_blocks_send(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=None, transport=transport,
               send_live=True, confirm=PHRASE, live_account_id=LIVE_ACCOUNT,
               live_token=SECRET_LIVE_TOKEN)
    assert rep["preview_gate_passed"] is False
    assert rep[ile.FIELD_SENT] is False
    assert transport.post_calls == []
    assert rep["_exit_code"] == 1


def test_preview_not_ready_blocks_send(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(),
               preview=_preview([_preview_row(preview_status="NEEDS_PRICE")]),
               transport=transport, send_live=True, confirm=PHRASE,
               live_account_id=LIVE_ACCOUNT, live_token=SECRET_LIVE_TOKEN)
    assert rep["preview_gate_passed"] is False
    assert transport.post_calls == []
    assert rep["_exit_code"] == 1


def test_cap_exceeded_blocks_send(tmp_path):
    transport = FakeTransport()
    # цена 500 за 1 лот > cap 300
    rep = _run(tmp_path, readiness=_readiness(),
               preview=_preview([_preview_row(
                   reference_price=500.0, estimated_total_rub=500.0)]),
               transport=transport, send_live=True, confirm=PHRASE,
               live_account_id=LIVE_ACCOUNT, live_token=SECRET_LIVE_TOKEN)
    assert rep["preview_gate_passed"] is False
    assert transport.post_calls == []
    assert any("превышает cap" in r for r in rep["blocking_reasons"])
    assert rep["_exit_code"] == 1


# ─── confirmation gate ────────────────────────────────────────────────────────

def test_no_confirmation_blocks(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, send_live=True, confirm=None,
               live_account_id=LIVE_ACCOUNT, live_token=SECRET_LIVE_TOKEN)
    assert rep["confirmation_matched"] is False
    assert rep[ile.FIELD_SENT] is False
    assert transport.post_calls == []
    assert rep["_exit_code"] == 1


def test_wrong_confirmation_blocks(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, send_live=True,
               confirm="CONFIRM LIVE BUY T 2 LOTS MAX 300 RUB",
               live_account_id=LIVE_ACCOUNT, live_token=SECRET_LIVE_TOKEN)
    assert rep["confirmation_matched"] is False
    assert transport.post_calls == []
    assert rep["_exit_code"] == 1


# ─── account / token gates ────────────────────────────────────────────────────

def test_missing_live_account_blocks_send(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, send_live=True, confirm=PHRASE,
               live_account_id=None, live_token=SECRET_LIVE_TOKEN)
    assert rep[ile.FIELD_SENT] is False
    assert transport.post_calls == []
    assert any("account" in r.lower() for r in rep["blocking_reasons"])
    assert rep["_exit_code"] == 1


def test_missing_live_token_blocks_send(tmp_path, monkeypatch):
    monkeypatch.delenv("TINKOFF_LIVE_TRADING_TOKEN", raising=False)
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, send_live=True, confirm=PHRASE,
               live_account_id=LIVE_ACCOUNT, live_token=None)
    assert rep[ile.FIELD_SENT] is False
    assert rep["token_policy"]["live_trading_token_present"] is False
    assert transport.post_calls == []
    assert rep["_exit_code"] == 1


def test_tinkoff_token_not_used_for_live(tmp_path, monkeypatch):
    # аналитический TINKOFF_TOKEN присутствует, но НЕ используется для live-отправки
    monkeypatch.setenv("TINKOFF_TOKEN", "ANALYTICS-READ-SECRET")
    monkeypatch.delenv("TINKOFF_LIVE_TRADING_TOKEN", raising=False)
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, send_live=True, confirm=PHRASE,
               live_account_id=LIVE_ACCOUNT, live_token=None)
    assert rep[ile.FIELD_SENT] is False  # нет live-токена → блок
    assert transport.post_calls == []
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    assert "ANALYTICS-READ-SECRET" not in js
    assert rep["guards"]["tinkoff_token_used_for_execution"] is False


def test_sandbox_token_not_used_for_live(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "SANDBOX-SECRET")
    monkeypatch.delenv("TINKOFF_LIVE_TRADING_TOKEN", raising=False)
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, send_live=True, confirm=PHRASE,
               live_account_id=LIVE_ACCOUNT, live_token=None)
    assert rep[ile.FIELD_SENT] is False
    assert transport.post_calls == []
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    assert "SANDBOX-SECRET" not in js
    assert rep["guards"]["sandbox_token_used_for_live"] is False


# ─── успешная live-отправка (mocked) ──────────────────────────────────────────

def test_live_send_calls_exactly_one_post_order(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, send_live=True, confirm=PHRASE,
               live_account_id=LIVE_ACCOUNT, live_token=SECRET_LIVE_TOKEN)
    assert rep["mode"] == "LIVE_SEND"
    assert rep[ile.FIELD_SENT] is True
    assert len(transport.post_calls) == 1  # ровно одна live-заявка
    g = rep["guards"]
    assert g[ile.GUARD_KEY_LIVE_ORDER_SENT] is True
    assert g[ile.GUARD_ORDERS_SERVICE_USED] is True
    assert g["live_token_used"] is True
    assert g["full_access_live_token_used"] is True
    assert rep["_exit_code"] == 0


def test_live_send_wire_contract(tmp_path):
    transport = FakeTransport()
    _run(tmp_path, readiness=_readiness(), preview=_preview(),
         transport=transport, send_live=True, confirm=PHRASE,
         live_account_id=LIVE_ACCOUNT, live_token=SECRET_LIVE_TOKEN)
    payload = transport.post_calls[0]["payload"]
    # uid-first instrumentId
    assert payload["instrumentId"] == "87db07bc-0e02-4e29-90bb-05e8ef791d7b"
    # quantity — строка int64 (= число лотов)
    assert payload["quantity"] == "1"
    assert isinstance(payload["quantity"], str)
    # только LIMIT BUY, никаких market
    assert payload["orderType"] == "ORDER_TYPE_LIMIT"
    assert payload["direction"] == "ORDER_DIRECTION_BUY"
    # orderId — валидный UUID v4
    assert uuid.UUID(payload["orderId"]).version == 4
    # live account id передан как есть в wire (в отчёте — маскирован)
    assert payload["accountId"] == LIVE_ACCOUNT


def test_live_send_passes_only_live_token_to_transport(tmp_path):
    transport = FakeTransport()
    _run(tmp_path, readiness=_readiness(), preview=_preview(),
         transport=transport, send_live=True, confirm=PHRASE,
         live_account_id=LIVE_ACCOUNT, live_token=SECRET_LIVE_TOKEN)
    assert transport.post_calls[0]["token"] == SECRET_LIVE_TOKEN


def test_live_token_never_leaks_into_reports(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, send_live=True, confirm=PHRASE,
               live_account_id=LIVE_ACCOUNT, live_token=SECRET_LIVE_TOKEN)
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert SECRET_LIVE_TOKEN not in js
    assert SECRET_LIVE_TOKEN not in md
    # account id в отчёте маскирован, не в открытом виде
    assert LIVE_ACCOUNT not in js
    assert rep["live_account_id_masked"] and rep["live_account_id_masked"] != LIVE_ACCOUNT


def test_live_send_account_masked_in_json(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, send_live=True, confirm=PHRASE,
               live_account_id=LIVE_ACCOUNT, live_token=SECRET_LIVE_TOKEN)
    wire = rep[ile.FIELD_REQUEST_WIRE]
    assert wire["accountId_masked"] != LIVE_ACCOUNT


def test_live_send_http_error_no_retry_one_call(tmp_path):
    from modules.tinvest_live_transport import LiveTransportHttpError
    err = LiveTransportHttpError(
        method=_METHOD_POST, status_code=400,
        safe_response_body='{"message":"bad"}', safe_response_json={"message": "bad"},
        safe_request_payload={"accountId": "***"}, url="https://x/y")
    transport = FakeTransport(raise_on_post=err)
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, send_live=True, confirm=PHRASE,
               live_account_id=LIVE_ACCOUNT, live_token=SECRET_LIVE_TOKEN)
    assert rep[ile.FIELD_SENT] is False
    assert len(transport.post_calls) == 1  # без ретраев — ровно одна попытка
    assert rep["live_http_status"] == 400
    assert rep["_exit_code"] == 1
    assert rep["guards"]["no_retries"] is True


# ─── валидация аргументов ─────────────────────────────────────────────────────

def test_bad_lots_raises(tmp_path):
    with pytest.raises(ile.LiveExecutionError):
        _run(tmp_path, readiness=_readiness(), preview=_preview(), lots=0)


def test_bad_max_order_rub_raises(tmp_path):
    with pytest.raises(ile.LiveExecutionError):
        _run(tmp_path, readiness=_readiness(), preview=_preview(), max_order_rub=0)


# ─── статическая проверка исходника: нет запрещённых литералов ────────────────

# ─── current-order notional cap gate (F4.1 safety refinement) ─────────────────

def test_notional_gate_preview_lots_mismatch_passes_with_warning(tmp_path):
    # старый preview сайзил 3 лота (828.42), но заявка --lots 1 (276.14) ≤ 300
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(),
               preview=_preview([_preview_row(
                   preview_lots=3, reference_price=276.14, lot_size=1,
                   estimated_total_rub=828.42)]),
               transport=transport, lots=1, max_order_rub=300)
    assert rep["preview_gate_passed"] is True
    assert rep["preview_lots"] == 3
    assert rep["cli_lots"] == 1
    assert rep["preview_lots_matches_cli_lots"] is False
    assert rep["current_order_estimated_total_rub"] == Decimal("276.14")
    assert rep["current_order_cap_passed"] is True
    assert rep["preview_lots_mismatch_warning"]
    assert any("preview_lots=3" in w for w in rep["warnings"])
    # transparency: preview est_total всё ещё в отчёте, но НЕ блокирует
    assert rep["preview_estimated_total_rub"] == Decimal("828.42")
    assert rep[ile.FIELD_SENT] is False  # dry-run
    assert transport.post_calls == []


def test_notional_gate_match_no_warning(tmp_path):
    rep = _run(tmp_path, readiness=_readiness(),
               preview=_preview([_preview_row(
                   preview_lots=1, reference_price=276.14, lot_size=1,
                   estimated_total_rub=276.14)]),
               lots=1, max_order_rub=300)
    assert rep["preview_gate_passed"] is True
    assert rep["preview_lots_matches_cli_lots"] is True
    assert rep["preview_lots_mismatch_warning"] is None
    assert rep["current_order_estimated_total_rub"] == Decimal("276.14")
    assert rep["current_order_cap_passed"] is True
    assert not any("preview_lots" in w for w in rep["warnings"])


def test_notional_gate_price_over_cap_blocks(tmp_path):
    rep = _run(tmp_path, readiness=_readiness(),
               preview=_preview([_preview_row(
                   preview_lots=1, reference_price=301, lot_size=1,
                   estimated_total_rub=301)]),
               lots=1, max_order_rub=300)
    assert rep["preview_gate_passed"] is False
    assert rep["current_order_cap_passed"] is False
    assert rep["current_order_estimated_total_rub"] == Decimal("301.00")
    assert any("превышает cap" in r for r in rep["blocking_reasons"])


def test_notional_gate_lot_size_pushes_over_cap_blocks(tmp_path):
    # 1 лот, но lot_size=3 → 276.14*3 = 828.42 > 300
    rep = _run(tmp_path, readiness=_readiness(),
               preview=_preview([_preview_row(
                   preview_lots=1, reference_price=276.14, lot_size=3,
                   estimated_total_rub=276.14)]),
               lots=1, max_order_rub=300)
    assert rep["preview_gate_passed"] is False
    assert rep["current_order_cap_passed"] is False
    assert rep["current_order_estimated_total_rub"] == Decimal("828.42")
    assert any("превышает cap" in r for r in rep["blocking_reasons"])


def test_notional_gate_dry_run_no_send_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKOFF_TOKEN", "ANALYTICS-SECRET")
    monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "SANDBOX-SECRET")
    monkeypatch.delenv("TINKOFF_LIVE_TRADING_TOKEN", raising=False)
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(),
               preview=_preview([_preview_row(
                   preview_lots=3, reference_price=276.14, lot_size=1,
                   estimated_total_rub=828.42)]),
               transport=transport, lots=1, max_order_rub=300)
    g = rep["guards"]
    assert rep["mode"] == "DRY_RUN"
    assert rep[ile.FIELD_SENT] is False
    assert transport.post_calls == []
    assert g["live_token_used"] is False
    assert g["tinkoff_token_used_for_execution"] is False
    assert g["sandbox_token_used_for_live"] is False
    assert rep["token_policy"]["live_trading_token_present"] is False
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    assert "ANALYTICS-SECRET" not in js and "SANDBOX-SECRET" not in js


# ─── live tradability preflight (F4.1 safety gate before PostOrder) ───────────

def test_tradability_available_dry_run_unchanged(tmp_path):
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               tradability=trad_available())
    assert rep["mode"] == "DRY_RUN"
    assert rep["live_tradability_checked"] is True
    assert rep["live_tradability_passed"] is True
    assert rep["live_trading_status"] == "SECURITY_TRADING_STATUS_NORMAL_TRADING"
    assert rep["live_api_trade_available_flag"] is True
    assert rep["live_buy_available_flag"] is True
    assert rep["live_postorder_blocked_before_call"] is False
    assert rep[ile.FIELD_SENT] is False


def test_tradability_unavailable_blocks_before_postorder(tmp_path):
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, tradability=trad_unavailable(),
               send_live=True, confirm=PHRASE, live_account_id=LIVE_ACCOUNT,
               live_token=SECRET_LIVE_TOKEN)
    # blocked before any PostOrder
    assert transport.post_calls == []
    assert rep[ile.FIELD_SENT] is False
    assert rep["guards"][ile.GUARD_ORDERS_SERVICE_USED] is False  # service not used
    assert rep["guards"][ile.GUARD_KEY_LIVE_ORDER_SENT] is False
    assert rep["guards"]["no_retries"] is True
    assert rep["guards"]["market_order_used"] is False
    assert rep["live_tradability_checked"] is True
    assert rep["live_tradability_passed"] is False
    assert rep["live_postorder_blocked_before_call"] is True
    assert rep["live_tradability_blocking_reason"]
    assert rep["_exit_code"] == 1
    # token must not be used to call any service, and must not leak
    assert rep["guards"]["live_token_used"] is False
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    assert SECRET_LIVE_TOKEN not in js


def test_send_blocked_when_tradability_unchecked(tmp_path):
    # provider=None → tradability not verified → live send must be blocked
    transport = FakeTransport()
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, tradability=None,
               send_live=True, confirm=PHRASE, live_account_id=LIVE_ACCOUNT,
               live_token=SECRET_LIVE_TOKEN)
    assert transport.post_calls == []
    assert rep[ile.FIELD_SENT] is False
    assert rep["live_tradability_checked"] is False
    assert rep["guards"][ile.GUARD_ORDERS_SERVICE_USED] is False
    assert rep["live_postorder_blocked_before_call"] is True
    assert any("tradability" in r.lower() for r in rep["blocking_reasons"])
    assert rep["_exit_code"] == 1


def test_tradability_unavailable_individual_flags(tmp_path):
    # buy_available_flag false alone must block
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               tradability=trad_available(buy_available_flag=False),
               send_live=True, confirm=PHRASE, live_account_id=LIVE_ACCOUNT,
               live_token=SECRET_LIVE_TOKEN)
    assert rep["live_tradability_passed"] is False
    assert "buy_available_flag" in rep["live_tradability_blocking_reason"]
    assert rep[ile.FIELD_SENT] is False


def test_http_400_30079_classified(tmp_path):
    # if PostOrder still returns 30079 (tradability passed but broker rejects),
    # classify as INSTRUMENT_NOT_AVAILABLE_FOR_TRADING
    from modules.tinvest_live_transport import LiveTransportHttpError
    body = ('{"code":3,"message":"Instrument is not available for trading",'
            '"description":"30079"}')
    err = LiveTransportHttpError(
        method=_METHOD_POST, status_code=400,
        safe_response_body=body,
        safe_response_json={"code": 3,
                            "message": "Instrument is not available for trading",
                            "description": "30079"},
        safe_request_payload={"accountId": "***"}, url="https://x/y")
    transport = FakeTransport(raise_on_post=err)
    rep = _run(tmp_path, readiness=_readiness(), preview=_preview(),
               transport=transport, tradability=trad_available(),
               send_live=True, confirm=PHRASE, live_account_id=LIVE_ACCOUNT,
               live_token=SECRET_LIVE_TOKEN)
    assert len(transport.post_calls) == 1  # tradability passed → call attempted once
    assert rep[ile.FIELD_SENT] is False
    assert rep["live_http_status"] == 400
    assert rep["live_http_error_classification"] == \
        "INSTRUMENT_NOT_AVAILABLE_FOR_TRADING"
    # raw sanitized body preserved
    assert "30079" in (rep["live_http_error_body"] or "")
    assert "INSTRUMENT_NOT_AVAILABLE_FOR_TRADING" in (rep["diagnostic_hint"] or "")
    assert rep["guards"]["market_order_used"] is False
    assert rep["guards"]["no_retries"] is True


def test_classify_live_http_error_unit():
    assert ile.classify_live_http_error(
        400, {"description": "30079"}, None) == "INSTRUMENT_NOT_AVAILABLE_FOR_TRADING"
    assert ile.classify_live_http_error(
        400, {"message": "Instrument is not available for trading"}, None) == \
        "INSTRUMENT_NOT_AVAILABLE_FOR_TRADING"
    assert ile.classify_live_http_error(400, {"message": "bad price"}, None) is None
    assert ile.classify_live_http_error(None, None, None) is None


def test_module_source_has_no_forbidden_literals():
    src = Path(ile.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service",
        "post" "Order(",
        "cancel" "Order(",
        "place" "_order",
        "submit" "_order",
        "place" "_limit_" "order",
        "order" "_client",
        "LIVE_" "EXECUTION_" "ENABLED",
        "live" "_order",  # цельного нет: ключи из фрагмента/импорта
        "TINKOFF" "_TOKEN",
        "TINKOFF" "_SANDBOX_TOKEN",
    )
    for tok in forbidden:
        assert tok not in src, tok
    # но dedicated live env var присутствует
    assert "TINKOFF_LIVE_TRADING_TOKEN" in src


def test_transport_source_has_no_forbidden_literals():
    from modules import tinvest_live_transport as tlt
    src = Path(tlt.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service",
        "Post" "Order(",
        "post" "Order(",
        "place" "_order",
        "submit" "_order",
        "live" "_order",
        "TINKOFF" "_TOKEN",
        "TINKOFF" "_SANDBOX_TOKEN",
    )
    for tok in forbidden:
        assert tok not in src, tok
