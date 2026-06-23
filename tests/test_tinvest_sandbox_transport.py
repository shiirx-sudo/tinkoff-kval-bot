"""
Тесты F3.1 verified sandbox transport (modules/tinvest_sandbox_transport).

Без реальной сети: транспорт инъектируется как callable. Проверяем, что REST-
payload использует подтверждённые поля proto-контракта, что MARKET-заявки и
не-BUY hard-fail, и что токен передаётся только адаптеру (не печатается).
"""
from __future__ import annotations

import json
import sys
import types
import uuid

import pytest

from modules import income_sandbox_execution as ise
from modules import tinvest_sandbox_transport as tx

# Валидный UUID v4 для wire orderId (контракт API PostSandboxOrder).
_UUID_ORDER_ID = "f3a1b2c3-d4e5-4f6a-8b9c-0d1e2f3a4b5c"


def _request(*, order_type=ise.ORDER_TYPE_LIMIT, direction=ise.ORDER_DIRECTION_BUY,
             figi="FIGI-T", uid="uid-T", lots=3, price=("275", 500000000),
             client_order_id=_UUID_ORDER_ID):
    units, nano = price
    return {
        "direction": direction,
        "order_type": order_type,
        "instrument": {"ticker": "T", "figi": figi, "uid": uid, "class_code": "TQBR"},
        "lots": lots,
        "quantity": lots,
        "limit_price": "275.5",
        "limit_price_quotation": {"units": units, "nano": nano},
        "currency": "rub",
        "client_order_id": client_order_id,
        "sandbox_account_id_masked": "sa****07",
    }


class _Recorder:
    """Записывает (method, payload, token); возвращает каноничный sandbox-ответ."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, payload, token):
        self.calls.append({"method": method, "payload": payload, "token": token})
        if method == "PostSandboxOrder":
            return {
                "orderId": "sb-1",
                "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW",
                "lotsRequested": 3,
            }
        return {"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL"}


def test_contract_source_mentions_proto():
    assert "proto" in tx.CONTRACT_SOURCE
    assert "SandboxService" in tx.CONTRACT_SOURCE


def test_post_payload_uses_confirmed_contract_fields():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    resp = adapter.post_sandbox_order(
        request=_request(), account_id="sandbox-acc-007", token="sbx-token")
    assert resp["orderId"] == "sb-1"
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["method"] == "PostSandboxOrder"
    assert call["token"] == "sbx-token"
    p = call["payload"]
    # подтверждённые поля PostOrderRequest (camelCase JSON)
    assert p["quantity"] == "3"            # int64 → строка, КОЛИЧЕСТВО ЛОТОВ
    assert isinstance(p["quantity"], str)  # quantity в wire payload — строка
    assert p["price"] == {"units": "275", "nano": 500000000}
    assert p["direction"] == "ORDER_DIRECTION_BUY"
    assert p["accountId"] == "sandbox-acc-007"
    assert p["orderType"] == "ORDER_TYPE_LIMIT"
    # orderId == client_order_id и обязан быть валидным UUID (требование API)
    assert p["orderId"] == _UUID_ORDER_ID
    assert uuid.UUID(p["orderId"]).version == 4
    # UID-first: при наличии и uid, и figi берётся uid (поле instrumentId)
    assert p["instrumentId"] == "uid-T"
    # никакого live-token-поля или секрета в payload
    assert "token" not in p
    assert "Authorization" not in p


def test_instrument_id_uses_uid_before_figi_when_both_exist():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    adapter.post_sandbox_order(
        request=_request(figi="FIGI-T", uid="uid-T"), account_id="acc", token="t")
    assert rec.calls[0]["payload"]["instrumentId"] == "uid-T"
    assert adapter.last_instrument_id_source == "uid"


def test_instrument_id_falls_back_to_figi_when_uid_missing():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    adapter.post_sandbox_order(
        request=_request(uid=None), account_id="acc", token="t")
    assert rec.calls[0]["payload"]["instrumentId"] == "FIGI-T"
    assert adapter.last_instrument_id_source == "figi"


def test_instrument_id_missing_both_hard_fails():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    with pytest.raises(tx.SandboxTransportError):
        adapter.post_sandbox_order(
            request=_request(figi=None, uid=None), account_id="acc", token="t")
    assert rec.calls == []


def test_instrument_id_source_force_uid_requires_uid():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    req = _request(figi="FIGI-T", uid=None)
    req["instrument_id_source_pref"] = "uid"
    with pytest.raises(tx.SandboxTransportError):
        adapter.post_sandbox_order(request=req, account_id="acc", token="t")
    assert rec.calls == []


def test_instrument_id_source_force_figi_uses_figi_even_with_uid():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    req = _request(figi="FIGI-T", uid="uid-T")
    req["instrument_id_source_pref"] = "figi"
    adapter.post_sandbox_order(request=req, account_id="acc", token="t")
    assert rec.calls[0]["payload"]["instrumentId"] == "FIGI-T"
    assert adapter.last_instrument_id_source == "figi"


def test_wire_payload_sanitized_uses_uid_and_string_quantity():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    adapter.post_sandbox_order(
        request=_request(figi="FIGI-T", uid="uid-T"),
        account_id="sandbox-acc-007", token="sbx-token")
    wire = adapter.last_wire_sanitized
    assert wire is not None
    assert wire["instrumentId"] == "uid-T"
    assert wire["instrument_id_source"] == "uid"
    assert wire["quantity"] == "3"
    assert wire["quantity_type"] == "str"
    # цена-quotation сохранена, enums сохранены
    assert wire["price"] == {"units": "275", "nano": 500000000}
    assert wire["direction"] == "ORDER_DIRECTION_BUY"
    assert wire["orderType"] == "ORDER_TYPE_LIMIT"
    # accountId маскирован, токен не включён
    assert wire["accountId_masked"] != "sandbox-acc-007"
    blob = json.dumps(wire)
    assert "sbx-token" not in blob


def test_wire_payload_order_id_uuid_flags():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    adapter.post_sandbox_order(
        request=_request(), account_id="sandbox-acc-007", token="sbx-token")
    wire = adapter.last_wire_sanitized
    assert wire["orderId"] == _UUID_ORDER_ID
    assert wire["orderId_is_uuid"] is True
    assert wire["orderId_version"] == 4


def test_non_uuid_order_id_hard_fails():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    req = _request(client_order_id="sandbox-f3-T-20260623T141714Z-d8ee4fd8")
    with pytest.raises(tx.SandboxTransportError):
        adapter.post_sandbox_order(request=req, account_id="acc", token="t")
    assert rec.calls == []  # семантический id не уходит в сеть


def test_build_wire_preview_no_send_uuid_order_id():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    wire = adapter.build_wire_preview(
        request=_request(), account_id="sandbox-acc-007")
    # превью НЕ отправляет ничего в сеть
    assert rec.calls == []
    assert wire["instrumentId"] == "uid-T"
    assert wire["quantity"] == "3"
    assert uuid.UUID(wire["orderId"]).version == 4
    assert wire["orderId_is_uuid"] is True
    # токена в превью нет (он и не передавался)
    assert "token" not in json.dumps(wire)


def test_build_wire_preview_rejects_non_uuid():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    req = _request(client_order_id="not-a-uuid")
    with pytest.raises(tx.SandboxTransportError):
        adapter.build_wire_preview(request=req, account_id="acc")
    assert rec.calls == []


def test_market_order_hard_fails():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    with pytest.raises(tx.SandboxTransportError):
        adapter.post_sandbox_order(
            request=_request(order_type="ORDER_TYPE_MARKET"),
            account_id="acc", token="t")
    assert rec.calls == []  # ничего не отправлено


def test_sell_direction_hard_fails():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    with pytest.raises(tx.SandboxTransportError):
        adapter.post_sandbox_order(
            request=_request(direction="ORDER_DIRECTION_SELL"),
            account_id="acc", token="t")
    assert rec.calls == []


def test_missing_token_hard_fails():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    with pytest.raises(tx.SandboxTransportError):
        adapter.post_sandbox_order(request=_request(), account_id="acc", token="")
    assert rec.calls == []


def test_missing_account_hard_fails():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    with pytest.raises(tx.SandboxTransportError):
        adapter.post_sandbox_order(request=_request(), account_id="", token="t")


def test_missing_price_quotation_hard_fails():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    req = _request()
    req["limit_price_quotation"] = None
    with pytest.raises(tx.SandboxTransportError):
        adapter.post_sandbox_order(request=req, account_id="acc", token="t")


def test_get_order_state_uses_confirmed_fields():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    state = adapter.get_sandbox_order_state(
        account_id="sandbox-acc-007", order_id="sb-1", token="sbx-token")
    assert state["executionReportStatus"] == "EXECUTION_REPORT_STATUS_FILL"
    call = rec.calls[0]
    assert call["method"] == "GetSandboxOrderState"
    assert call["payload"] == {"accountId": "sandbox-acc-007", "orderId": "sb-1"}


def test_get_order_state_returns_none_without_ids():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    assert adapter.get_sandbox_order_state(
        account_id="", order_id="sb-1", token="t") is None
    assert rec.calls == []


def _make_fake_requests(*, status_code, json_data, text, json_raises=False):
    """Минимальный фейковый requests-модуль для проверки захвата HTTP-ошибки."""
    mod = types.ModuleType("requests")

    class HTTPError(Exception):
        pass

    class FakeResp:
        def __init__(self):
            self.status_code = status_code
            self.text = text

        def json(self):
            if json_raises:
                raise ValueError("no json")
            return json_data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise HTTPError(f"{self.status_code} Client Error for url …")

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def post(self, url, json=None, timeout=None):
            return FakeResp()

    mod.HTTPError = HTTPError
    mod.Session = FakeSession
    return mod


def test_http_400_captures_sanitized_response_body(monkeypatch):
    fake = _make_fake_requests(
        status_code=400,
        json_data={"code": 3, "message": "bad instrument"},
        text='{"code":3,"message":"bad instrument"}')
    monkeypatch.setitem(sys.modules, "requests", fake)
    adapter = tx.VerifiedSandboxRestAdapter(transport=None, max_retries=1)
    with pytest.raises(tx.SandboxTransportHttpError) as ei:
        adapter.post_sandbox_order(
            request=_request(), account_id="sandbox-acc-007", token="sbx-secret")
    exc = ei.value
    assert exc.status_code == 400
    assert exc.method == "PostSandboxOrder"
    assert "bad instrument" in exc.safe_response_body
    assert exc.safe_response_json["message"] == "bad instrument"
    assert exc.safe_request_payload["instrumentId"] == "uid-T"
    # accountId маскирован; токен/Authorization нигде не появляются
    assert exc.safe_request_payload["accountId"] != "sandbox-acc-007"
    blob = json.dumps({
        "body": exc.safe_response_body, "json": exc.safe_response_json,
        "payload": exc.safe_request_payload, "url": exc.url, "msg": str(exc)})
    assert "sbx-secret" not in blob
    assert "Authorization" not in blob


def test_http_400_non_json_body_captured(monkeypatch):
    fake = _make_fake_requests(
        status_code=400, json_data=None, text="Bad Request plain text",
        json_raises=True)
    monkeypatch.setitem(sys.modules, "requests", fake)
    adapter = tx.VerifiedSandboxRestAdapter(transport=None, max_retries=1)
    with pytest.raises(tx.SandboxTransportHttpError) as ei:
        adapter.post_sandbox_order(request=_request(), account_id="acc", token="t")
    exc = ei.value
    assert exc.safe_response_json is None
    assert "Bad Request" in exc.safe_response_body


def test_wire_payload_recorded_before_http_error(monkeypatch):
    fake = _make_fake_requests(
        status_code=400, json_data={"message": "x"}, text="{}")
    monkeypatch.setitem(sys.modules, "requests", fake)
    adapter = tx.VerifiedSandboxRestAdapter(transport=None, max_retries=1)
    with pytest.raises(tx.SandboxTransportHttpError):
        adapter.post_sandbox_order(request=_request(), account_id="acc", token="t")
    assert adapter.last_wire_sanitized["instrumentId"] == "uid-T"
    assert adapter.last_instrument_id_source == "uid"


def test_module_source_has_no_live_order_execution_apis():
    from pathlib import Path
    src = Path(tx.__file__).read_text(encoding="utf-8")
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
    )
    for tok in forbidden:
        assert tok not in src, tok
