"""
Тесты F3.1 verified sandbox transport (modules/tinvest_sandbox_transport).

Без реальной сети: транспорт инъектируется как callable. Проверяем, что REST-
payload использует подтверждённые поля proto-контракта, что MARKET-заявки и
не-BUY hard-fail, и что токен передаётся только адаптеру (не печатается).
"""
from __future__ import annotations

import pytest

from modules import income_sandbox_execution as ise
from modules import tinvest_sandbox_transport as tx


def _request(*, order_type=ise.ORDER_TYPE_LIMIT, direction=ise.ORDER_DIRECTION_BUY,
             figi="FIGI-T", uid="uid-T", lots=3, price=("275", 500000000),
             client_order_id="sandbox-f3-T-x"):
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
    assert p["price"] == {"units": "275", "nano": 500000000}
    assert p["direction"] == "ORDER_DIRECTION_BUY"
    assert p["accountId"] == "sandbox-acc-007"
    assert p["orderType"] == "ORDER_TYPE_LIMIT"
    assert p["orderId"] == "sandbox-f3-T-x"
    assert p["instrumentId"] == "FIGI-T"
    # никакого live-token-поля или секрета в payload
    assert "token" not in p
    assert "Authorization" not in p


def test_instrument_id_falls_back_to_uid_when_no_figi():
    rec = _Recorder()
    adapter = tx.VerifiedSandboxRestAdapter(transport=rec)
    adapter.post_sandbox_order(
        request=_request(figi=None), account_id="acc", token="t")
    assert rec.calls[0]["payload"]["instrumentId"] == "uid-T"


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
