"""Тесты read-only REST-клиента (HTTP замокан, реальных запросов нет)."""
from __future__ import annotations

from brokers.tinkoff.rest_client import TinkoffReadOnlyClient


def test_get_broker_accounts_includes_broker_and_iis():
    c = TinkoffReadOnlyClient(token="t")
    c._post = lambda s, m, p: {"accounts": [  # type: ignore[assignment]
        {"id": "1", "name": "Брокерский", "type": "ACCOUNT_TYPE_TINKOFF"},
        {"id": "2", "name": "ИИС", "type": "ACCOUNT_TYPE_TINKOFF_IIS"},
        {"id": "3", "name": "Копилка", "type": "ACCOUNT_TYPE_INVEST_BOX"},
        {"id": "4", "name": "?", "type": "ACCOUNT_TYPE_UNSPECIFIED"},
    ]}
    accs = c.get_broker_accounts()
    ids = {a["id"] for a in accs}
    assert ids == {"1", "2"}  # брокерский + ИИС учитываются, копилка/unspecified — нет


def test_is_turnover_account_and_label():
    from brokers.tinkoff.rest_client import account_type_label, is_turnover_account
    assert is_turnover_account({"type": "ACCOUNT_TYPE_TINKOFF"}) is True
    assert is_turnover_account({"type": "ACCOUNT_TYPE_TINKOFF_IIS"}) is True
    assert is_turnover_account({"type": "ACCOUNT_TYPE_INVEST_BOX"}) is False
    assert account_type_label("ACCOUNT_TYPE_TINKOFF") == "broker"
    assert account_type_label("ACCOUNT_TYPE_TINKOFF_IIS") == "iis"


def test_iter_operations_paginates():
    c = TinkoffReadOnlyClient(token="t")
    pages = [
        {"items": [{"id": "a"}, {"id": "b"}], "hasNext": True, "nextCursor": "C1"},
        {"items": [{"id": "c"}], "hasNext": False, "nextCursor": ""},
    ]
    state = {"i": 0}

    def fake_post(service, method, payload):
        assert method == "GetOperationsByCursor"
        page = pages[state["i"]]
        state["i"] += 1
        return page

    c._post = fake_post  # type: ignore[assignment]
    ops = c.get_operations("acc-1", "2025-04-01T00:00:00Z", "2026-03-31T23:59:59Z")
    assert [o["id"] for o in ops] == ["a", "b", "c"]
    assert state["i"] == 2


def test_iter_operations_passes_filter():
    c = TinkoffReadOnlyClient(token="t")
    seen = {}

    def fake_post(service, method, payload):
        seen.update(payload)
        return {"items": [], "hasNext": False}

    c._post = fake_post  # type: ignore[assignment]
    c.get_operations("acc-1", "2025-04-01T00:00:00Z", "2026-03-31T23:59:59Z",
                     operation_types=["OPERATION_TYPE_BUY"])
    assert seen["operationTypes"] == ["OPERATION_TYPE_BUY"]
    assert seen["state"] == "OPERATION_STATE_EXECUTED"
    assert seen["withoutTrades"] is False
