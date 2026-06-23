"""
Тесты F3 income_sandbox_execution — sandbox-only manual-confirmed execution.

Без реального API: read-only клиент и sandbox-адаптер мокаются на уровне фасада.
Реальные live order-endpoints не импортируются и не используются.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from modules import income_sandbox_execution as ise
from modules import tinvest_sandbox_transport as tx
from tests.conftest import quotation


# ─── фикстуры/билдеры ─────────────────────────────────────────────────────────

def _preview_row(ticker="T", *, preview_lots=3, reference_price="275.5",
                 reference_price_status="OK", estimated_total_rub="826.5",
                 preview_status="PREVIEW_READY",
                 source_proposed_action="BUY_CANDIDATE", figi=None, **over):
    row = {
        "ticker": ticker,
        "name": f"{ticker} name",
        "figi": figi if figi is not None else f"FIGI-{ticker}",
        "uid": f"uid-{ticker}",
        "class_code": "TQBR",
        "source_proposed_action": source_proposed_action,
        "lot_size": 1,
        "min_lots": 1,
        "preview_lots": preview_lots,
        "preview_quantity": preview_lots,
        "max_order_rub": 1000,
        "reference_price": reference_price,
        "reference_price_source": "decision_report.reference_price",
        "reference_price_time": "2026-06-22T10:00:00Z",
        "reference_price_status": reference_price_status,
        "estimated_notional_rub": estimated_total_rub,
        "estimated_total_rub": estimated_total_rub,
        "preview_status": preview_status,
        "preview_blockers": [],
        "manual_confirmation_required": True,
        "order_send_allowed": False,
        "auto_execution_allowed": False,
        "full_access_token_required": False,
        "orders_service_allowed": False,
    }
    row.update(over)
    return row


def _write_preview(tmp_path: Path, rows) -> Path:
    payload = {"kind": "income_order_preview", "read_only": True, "previews": rows}
    p = tmp_path / "income_order_preview.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _run(tmp_path: Path, rows, *, ticker="T", **kw):
    preview = _write_preview(tmp_path, rows)
    return ise.run(
        ticker=ticker,
        preview_json=str(preview),
        output_json=str(tmp_path / "f3.json"),
        output_md=str(tmp_path / "f3.md"),
        **kw,
    )


class _FakeClient:
    """Read-only фасад только с методами, нужными для preflight-цены."""

    def __init__(self, price):
        self._price = price
        self.calls = []

    def find_instrument(self, ticker, class_code):
        self.calls.append(("find_instrument", ticker, class_code))
        return {"figi": f"FIGI-{ticker}"}

    def get_last_price(self, instrument_id):
        self.calls.append(("get_last_price", instrument_id))
        return {"price": quotation(self._price), "time": "2026-06-22T10:00:00Z"}


class _FakeSandboxAdapter(ise.SandboxOrderAdapter):
    """Sandbox-only адаптер для тестов: фиксирует запрос, возвращает sandbox-ответ."""

    def __init__(self, *, state_raises=False):
        self.received = None
        self.post_calls = 0
        self.state_calls = 0
        self._state_raises = state_raises

    def post_sandbox_order(self, *, request, account_id, token):
        self.post_calls += 1
        self.received = {"request": request, "account_id": account_id, "token": token}
        return {
            "orderId": "sb-order-1",
            "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
            "lotsRequested": 3,
            "lotsExecuted": 3,
            "totalOrderAmount": {"currency": "rub", "units": "826", "nano": 500000000},
            "secretToken": "SHOULD-NOT-LEAK",
        }

    def get_sandbox_order_state(self, *, account_id, order_id, token):
        self.state_calls += 1
        if self._state_raises:
            raise RuntimeError("sandbox state read failed")
        return {"order_state": "EXECUTION_REPORT_STATUS_FILL"}


class _RecorderTx:
    """Фейковый transport callable для VerifiedSandboxRestAdapter (без сети)."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, payload, token):
        self.calls.append({"method": method, "payload": payload, "token": token})
        if method == "PostSandboxOrder":
            return {"orderId": "sb-1",
                    "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW",
                    "lotsRequested": 3}
        return {"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL"}


class _HttpErrorAdapter(ise.SandboxOrderAdapter):
    """Адаптер, имитирующий HTTP 400 от PostSandboxOrder (с wire-диагностикой)."""

    def __init__(self):
        self.last_wire_sanitized = {
            "instrumentId": "uid-T", "instrument_id_source": "uid",
            "quantity": "3", "quantity_type": "str",
            "accountId_masked": "sa****07"}
        self.last_instrument_id_source = "uid"

    def post_sandbox_order(self, *, request, account_id, token):
        raise tx.SandboxTransportHttpError(
            method="PostSandboxOrder", status_code=400,
            safe_response_body='{"code":3,"message":"bad instrument"}',
            safe_response_json={"code": 3, "message": "bad instrument"},
            safe_request_payload={"instrumentId": "uid-T", "accountId": "sa****07"},
            url="https://invest-public-api.tinkoff.ru/rest/Service/PostSandboxOrder")


_SEND_KW = dict(
    send_sandbox=True,
    sandbox_account_id="sandbox-acc-007",
    sandbox_token="sbx-secret-token",
    confirm="CONFIRM SANDBOX BUY T 3 LOTS MAX 1000 RUB",
)


# ─── dry-run по умолчанию ─────────────────────────────────────────────────────

def test_dry_run_default_sends_no_sandbox_order(tmp_path):
    rep = _run(tmp_path, [_preview_row()])
    assert rep["mode"] == ise.MODE_DRY_RUN
    assert rep["guards"]["sandbox_order_sent"] is False
    assert rep["guards"]["dry_run"] is True
    assert rep["sandbox_order_request"] is None
    assert rep["_exit_code"] == 0


def test_dry_run_emits_required_confirmation_phrase(tmp_path):
    rep = _run(tmp_path, [_preview_row()])
    assert rep["required_confirmation_phrase"] == \
        "CONFIRM SANDBOX BUY T 3 LOTS MAX 1000 RUB"


def test_dry_run_vtbr_phrase(tmp_path):
    rep = _run(tmp_path, [_preview_row("VTBR", preview_lots=14,
                                       estimated_total_rub="983")],
               ticker="VTBR")
    assert rep["required_confirmation_phrase"] == \
        "CONFIRM SANDBOX BUY VTBR 14 LOTS MAX 1000 RUB"


# ─── send-gate'ы ──────────────────────────────────────────────────────────────

def test_send_without_confirm_blocks(tmp_path):
    kw = dict(_SEND_KW)
    kw["confirm"] = None
    rep = _run(tmp_path, [_preview_row()], **kw)
    assert rep["mode"] == ise.MODE_SANDBOX_SEND
    assert rep["guards"]["sandbox_order_sent"] is False
    assert rep["confirmation_matched"] is False
    assert rep["required_confirmation_phrase"]
    assert rep["_exit_code"] == 1


def test_send_wrong_confirm_blocks(tmp_path):
    kw = dict(_SEND_KW)
    kw["confirm"] = "CONFIRM SANDBOX BUY T 99 LOTS MAX 1000 RUB"
    rep = _run(tmp_path, [_preview_row()], adapter=_FakeSandboxAdapter(), **kw)
    assert rep["confirmation_matched"] is False
    assert rep["guards"]["sandbox_order_sent"] is False
    assert rep["_exit_code"] == 1


def test_send_exact_confirm_invokes_adapter(tmp_path):
    adapter = _FakeSandboxAdapter()
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    assert adapter.received is not None
    assert rep["guards"]["sandbox_order_sent"] is True
    assert rep["_exit_code"] == 0


def test_send_missing_sandbox_account_blocks(tmp_path):
    kw = dict(_SEND_KW)
    kw["sandbox_account_id"] = None
    rep = _run(tmp_path, [_preview_row()], adapter=_FakeSandboxAdapter(), **kw)
    assert rep["preflight"]["checks"]["sandbox_account_present"] is False
    assert rep["guards"]["sandbox_order_sent"] is False
    assert rep["_exit_code"] == 1


def test_send_missing_sandbox_token_blocks(tmp_path):
    kw = dict(_SEND_KW)
    kw["sandbox_token"] = ""
    rep = _run(tmp_path, [_preview_row()], adapter=_FakeSandboxAdapter(), **kw)
    assert rep["preflight"]["checks"]["sandbox_token_present"] is False
    assert rep["guards"]["sandbox_order_sent"] is False
    assert rep["_exit_code"] == 1


def test_send_price_deviation_above_max_blocks(tmp_path):
    # свежая цена 400 против preview 275.5 → отклонение >> 100 bps
    rep = _run(tmp_path, [_preview_row()], adapter=_FakeSandboxAdapter(),
               client=_FakeClient("400"), max_price_deviation_bps=100, **_SEND_KW)
    assert rep["preflight"]["checks"]["price_deviation_ok"] is False
    assert rep["guards"]["sandbox_order_sent"] is False
    assert rep["_exit_code"] == 1


def test_send_price_deviation_within_max_passes(tmp_path):
    rep = _run(tmp_path, [_preview_row()], adapter=_FakeSandboxAdapter(),
               client=_FakeClient("275.5"), **_SEND_KW)
    assert rep["preflight"]["checks"]["price_deviation_ok"] is True
    assert rep["guards"]["sandbox_order_sent"] is True


def test_send_with_unconfigured_adapter_blocks_and_reports_wrapper(tmp_path):
    # без инъекции адаптера используется UnconfiguredSandboxAdapter → not wired
    rep = _run(tmp_path, [_preview_row()], client=None, **_SEND_KW)
    assert rep["guards"]["sandbox_order_sent"] is False
    assert rep["_exit_code"] == 1
    assert any("F3.1" in w for w in rep["warnings"])
    assert rep["sandbox_order_result"]["sandbox_order_sent"] is False


# ─── жёсткая валидация входа ───────────────────────────────────────────────────

def test_ticker_not_found_blocks(tmp_path):
    with pytest.raises(ise.SandboxExecutionError):
        _run(tmp_path, [_preview_row("VTBR")], ticker="SBER")


def test_preview_not_ready_blocks(tmp_path):
    with pytest.raises(ise.SandboxExecutionError):
        _run(tmp_path, [_preview_row(preview_status="NEEDS_PRICE")])


def test_preview_total_over_cap_blocks(tmp_path):
    with pytest.raises(ise.SandboxExecutionError):
        _run(tmp_path, [_preview_row(estimated_total_rub="1500")],
             max_order_rub=1000)


def test_unsafe_f2_flag_blocks(tmp_path):
    with pytest.raises(ise.SandboxExecutionError):
        _run(tmp_path, [_preview_row(order_send_allowed=True)])


def test_non_buy_candidate_blocks(tmp_path):
    with pytest.raises(ise.SandboxExecutionError):
        _run(tmp_path, [_preview_row(source_proposed_action="WAIT")])


def test_missing_preview_file_blocks(tmp_path):
    with pytest.raises(ise.SandboxExecutionError):
        ise.run(ticker="T", preview_json=str(tmp_path / "nope.json"),
                output_json=str(tmp_path / "o.json"),
                output_md=str(tmp_path / "o.md"))


def test_bad_lots_blocks(tmp_path):
    with pytest.raises(ise.SandboxExecutionError):
        _run(tmp_path, [_preview_row(preview_lots=0)])


# ─── только LIMIT/BUY, никаких market-заявок ──────────────────────────────────

def test_only_limit_buy_no_market_order(tmp_path):
    adapter = _FakeSandboxAdapter()
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    req = adapter.received["request"]
    assert req["direction"] == ise.ORDER_DIRECTION_BUY
    assert req["order_type"] == ise.ORDER_TYPE_LIMIT
    assert "MARKET" not in req["order_type"]
    assert rep["preflight"]["checks"]["no_market_order"] is True


def test_adapter_receives_correct_ticker_lots_price(tmp_path):
    adapter = _FakeSandboxAdapter()
    _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    req = adapter.received["request"]
    assert req["instrument"]["ticker"] == "T"
    assert req["lots"] == 3
    assert req["limit_price"] == Decimal("275.5")
    # client_order_id == wire orderId: ТОЛЬКО UUID v4, без семантического префикса.
    parsed = uuid.UUID(req["client_order_id"])
    assert parsed.version == 4
    assert not req["client_order_id"].startswith("sandbox-f3-")
    # семантический контекст хранится отдельным полем
    assert req["order_trace_label"].startswith("sandbox-f3-T-")


# ─── санитизация ответа / отсутствие утечки токена ────────────────────────────

def test_sandbox_response_sanitized(tmp_path):
    rep = _run(tmp_path, [_preview_row()], adapter=_FakeSandboxAdapter(),
               client=None, **_SEND_KW)
    blob = json.dumps(rep, default=str)
    assert "SHOULD-NOT-LEAK" not in blob
    res = rep["sandbox_order_result"]
    assert res["sandbox_order_id"] == "sb-order-1"
    assert res["sandbox_order_sent"] is True
    assert res["sandbox_order_state_read"] is True


def test_no_token_in_report_or_md(tmp_path):
    rep = _run(tmp_path, [_preview_row()], adapter=_FakeSandboxAdapter(),
               client=None, **_SEND_KW)
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    assert "sbx-secret-token" not in md
    assert "sbx-secret-token" not in js
    # account id в отчёте только маскированный
    assert "sandbox-acc-007" not in js


# ─── guards в JSON-отчёте ─────────────────────────────────────────────────────

def test_guards_live_flags_locked_false(tmp_path):
    rep = _run(tmp_path, [_preview_row()])
    g = rep["guards"]
    assert g["live_order_sent"] is False
    assert g["live_orders_service_used"] is False
    assert g["full_access_live_token_used"] is False
    assert g["auto_execution_allowed"] is False
    assert g["order_send_allowed"] is False
    assert g["manual_confirmation_required"] is True
    assert g["portfolio_mutated"] is False
    assert g["config_mutated"] is False
    assert g["telegram_sent"] is False
    assert g["next_stage"].startswith("F4")


def test_stage_and_dry_run_guard(tmp_path):
    rep = _run(tmp_path, [_preview_row()])
    assert rep["stage"] == "F3_SANDBOX_MANUAL_CONFIRMED_EXECUTION"
    assert rep["guards"]["dry_run"] is True
    assert rep["guards"]["sandbox_order_sent"] is False


def test_send_mocked_guards_sandbox_used(tmp_path):
    rep = _run(tmp_path, [_preview_row()], adapter=_FakeSandboxAdapter(),
               client=None, **_SEND_KW)
    g = rep["guards"]
    assert g["sandbox_order_sent"] is True
    assert g["sandbox_service_used"] is True
    assert g["sandbox_token_used"] is True
    assert g["live_order_sent"] is False
    assert g["full_access_live_token_used"] is False


# ─── markdown guard phrases ───────────────────────────────────────────────────

def test_markdown_contains_guard_phrases(tmp_path):
    rep = _run(tmp_path, [_preview_row()])
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    for phrase in (
        "F3 sandbox manual-confirmed execution",
        "LIVE orders are forbidden",
        "Sandbox only",
        "No live " + "Orders" "Service",
        "No full-access live token",
        "Manual confirmation required",
        "No autonomous execution",
        "No live orders were sent.",
        "No portfolio/config mutation.",
        "F4 tiny live requires separate PR and separate approval.",
        "CONFIRM SANDBOX BUY T 3 LOTS MAX 1000 RUB",
    ):
        assert phrase in md, phrase


def test_markdown_no_recommendation_wording(tmp_path):
    rep = _run(tmp_path, [_preview_row()])
    md = Path(rep["_output_md"]).read_text(encoding="utf-8").lower()
    for bad in ("купить сейчас", "продать сейчас", "гарантированная доходность",
                "guaranteed income", "safe profit"):
        assert bad not in md


# ─── статическая проверка: нет live order-exec API ────────────────────────────

def test_module_source_has_no_live_order_execution_apis():
    src = Path(ise.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service" + "(",
        "post" "Order(",
        "cancel" "Order(",
        "place" "_order(",
        "submit" "_order(",
        "place" "_limit_" "order(",
        "order" "_client",
        "LIVE_" "EXECUTION_" "ENABLED",
    )
    for tok in forbidden:
        assert tok not in src, tok


def test_decimal_to_quotation_roundtrip():
    q = ise.decimal_to_quotation(Decimal("275.5"))
    assert q["units"] == "275"
    assert q["nano"] == 500000000


# ─── F3.1 verified sandbox transport (оркестрация) ────────────────────────────

def test_send_with_unconfigured_transport_blocks_with_code(tmp_path):
    # без адаптера и transport=unconfigured → SANDBOX_TRANSPORT_UNCONFIGURED
    rep = _run(tmp_path, [_preview_row()], client=None,
               sandbox_transport=ise.TRANSPORT_UNCONFIGURED, **_SEND_KW)
    assert rep["guards"]["sandbox_order_sent"] is False
    assert rep["sandbox_transport"]["selected_transport"] == "unconfigured"
    assert rep["sandbox_transport"]["configured"] is False
    assert any("SANDBOX_TRANSPORT_UNCONFIGURED" in e for e in rep["errors"])
    assert rep["_exit_code"] == 1


def test_send_with_verified_sdk_unavailable_blocks(tmp_path):
    rep = _run(tmp_path, [_preview_row()], client=None,
               sandbox_transport=ise.TRANSPORT_VERIFIED_SDK, **_SEND_KW)
    assert rep["guards"]["sandbox_order_sent"] is False
    assert rep["sandbox_transport"]["selected_transport"] == "verified-sdk"
    assert rep["sandbox_transport"]["configured"] is False
    assert any("SANDBOX_SDK_NOT_AVAILABLE" in e for e in rep["errors"])
    assert rep["_exit_code"] == 1


def test_verified_rest_transport_metadata_in_report(tmp_path):
    # dry-run, но transport=verified-rest → metadata configured + contract_source
    rep = _run(tmp_path, [_preview_row()],
               sandbox_transport=ise.TRANSPORT_VERIFIED_REST)
    tr = rep["sandbox_transport"]
    assert tr["selected_transport"] == "verified-rest"
    assert tr["configured"] is True
    assert tr["adapter_class"] == "VerifiedSandboxRestAdapter"
    assert tr["contract_source"]
    assert "proto" in tr["contract_source"]


def test_send_exact_confirm_sends_exactly_one_order(tmp_path):
    adapter = _FakeSandboxAdapter()
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    assert adapter.post_calls == 1
    assert rep["guards"]["sandbox_order_sent"] is True


def test_price_deviation_above_cap_adapter_not_called(tmp_path):
    adapter = _FakeSandboxAdapter()
    rep = _run(tmp_path, [_preview_row()], adapter=adapter,
               client=_FakeClient("400"), max_price_deviation_bps=100, **_SEND_KW)
    assert adapter.post_calls == 0
    assert adapter.received is None
    assert rep["guards"]["sandbox_order_sent"] is False


def test_wrong_confirm_adapter_not_called(tmp_path):
    adapter = _FakeSandboxAdapter()
    kw = dict(_SEND_KW)
    kw["confirm"] = "CONFIRM SANDBOX BUY T 99 LOTS MAX 1000 RUB"
    _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **kw)
    assert adapter.post_calls == 0


def test_missing_account_adapter_not_called(tmp_path):
    adapter = _FakeSandboxAdapter()
    kw = dict(_SEND_KW)
    kw["sandbox_account_id"] = None
    _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **kw)
    assert adapter.post_calls == 0


def test_adapter_receives_sandbox_account_and_token_not_live(tmp_path, monkeypatch):
    # live-токен в окружении не должен использоваться адаптером
    monkeypatch.setenv("TINKOFF_TOKEN", "LIVE-SHOULD-NOT-BE-USED")
    monkeypatch.setenv("TINKOFF_READ_TOKEN", "READ-SHOULD-NOT-BE-USED")
    adapter = _FakeSandboxAdapter()
    _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    assert adapter.received["account_id"] == "sandbox-acc-007"
    assert adapter.received["token"] == "sbx-secret-token"
    assert adapter.received["token"] != "LIVE-SHOULD-NOT-BE-USED"
    assert adapter.received["token"] != "READ-SHOULD-NOT-BE-USED"
    inst = adapter.received["request"]["instrument"]
    assert inst["figi"] == "FIGI-T"


def test_adapter_receives_all_prepared_safe_params(tmp_path):
    adapter = _FakeSandboxAdapter()
    _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    req = adapter.received["request"]
    assert req["instrument"]["ticker"] == "T"
    assert req["lots"] == 3
    assert req["limit_price"] == Decimal("275.5")
    parsed = uuid.UUID(req["client_order_id"])
    assert parsed.version == 4
    assert req["order_trace_label"].startswith("sandbox-f3-T-")
    assert req["sandbox_account_id_masked"] != "sandbox-acc-007"


def test_state_read_failure_becomes_warning_no_retry_loop(tmp_path):
    adapter = _FakeSandboxAdapter(state_raises=True)
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    assert rep["guards"]["sandbox_order_sent"] is True
    assert adapter.state_calls == 1  # одна попытка, без retry-loop
    assert any("состояние sandbox-заявки" in w for w in rep["warnings"])
    assert rep["sandbox_order_state_sanitized"] is None


def test_sanitized_response_fields_present_and_no_secret(tmp_path):
    rep = _run(tmp_path, [_preview_row()], adapter=_FakeSandboxAdapter(),
               client=None, **_SEND_KW)
    resp = rep["sandbox_order_response_sanitized"]
    assert resp["sandbox_order_id"] == "sb-order-1"
    assert resp["lots_requested"] == 3
    assert resp["lots_executed"] == 3
    assert resp["total_order_amount"]["currency"] == "rub"
    assert rep["sandbox_order_state_sanitized"]["execution_report_status"]
    blob = json.dumps(rep, default=str)
    assert "SHOULD-NOT-LEAK" not in blob


def test_dry_run_sandbox_transport_present(tmp_path):
    rep = _run(tmp_path, [_preview_row()])
    assert rep["sandbox_transport"] is not None
    assert rep["guards"]["token_printed"] is False
    assert rep["guards"]["live_token_used"] is False


# ─── F3 wire payload + HTTP-диагностика (Задачи 1–4) ──────────────────────────

def test_report_wire_payload_uses_uid_first(tmp_path):
    adapter = tx.VerifiedSandboxRestAdapter(transport=_RecorderTx())
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    assert rep["guards"]["sandbox_order_sent"] is True
    wire = rep["sandbox_order_request_wire_sanitized"]
    assert wire["instrumentId"] == "uid-T"
    assert wire["instrument_id_source"] == "uid"
    assert wire["quantity"] == "3"
    assert wire["quantity_type"] == "str"
    assert wire["direction"] == ise.ORDER_DIRECTION_BUY
    assert wire["orderType"] == ise.ORDER_TYPE_LIMIT


def test_report_instrument_id_source_figi_flows_to_wire(tmp_path):
    adapter = tx.VerifiedSandboxRestAdapter(transport=_RecorderTx())
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None,
               instrument_id_source="figi", **_SEND_KW)
    wire = rep["sandbox_order_request_wire_sanitized"]
    assert wire["instrumentId"] == "FIGI-T"
    assert wire["instrument_id_source"] == "figi"


def test_invalid_instrument_id_source_blocks(tmp_path):
    with pytest.raises(ise.SandboxExecutionError):
        _run(tmp_path, [_preview_row()], instrument_id_source="bogus")


def test_http_error_body_captured_in_report(tmp_path):
    adapter = _HttpErrorAdapter()
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    assert rep["guards"]["sandbox_order_sent"] is False
    assert rep["sandbox_http_status"] == 400
    assert rep["sandbox_error_method"] == "PostSandboxOrder"
    assert "bad instrument" in rep["sandbox_http_error_body"]
    assert rep["sandbox_http_error_json"]["message"] == "bad instrument"
    assert rep["sandbox_order_request_wire_sanitized"]["instrumentId"] == "uid-T"
    assert rep["diagnostic_hint"]
    assert rep["_exit_code"] == 1


def test_http_error_no_token_leak_in_report_or_md(tmp_path):
    adapter = _HttpErrorAdapter()
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "sbx-secret-token" not in js
    assert "sbx-secret-token" not in md
    assert "Authorization" not in js
    # account id в отчёте только маскированный
    assert "sandbox-acc-007" not in js


def test_dry_run_wire_and_http_fields_present_as_none(tmp_path):
    rep = _run(tmp_path, [_preview_row()])
    assert rep["sandbox_order_request_wire_sanitized"] is None
    assert rep["sandbox_http_status"] is None
    assert rep["sandbox_http_error_body"] is None
    assert rep["sandbox_http_error_json"] is None
    assert rep["sandbox_error_method"] is None
    assert rep["diagnostic_hint"] is None


# ─── orderId UUID (Задачи 1–3) ────────────────────────────────────────────────

def test_generated_order_id_is_uuid_v4():
    oid = ise.build_sandbox_order_id()
    parsed = uuid.UUID(oid)
    assert parsed.version == 4
    assert not oid.startswith("sandbox-f3-")


def test_order_trace_label_is_semantic_not_uuid():
    from datetime import datetime, timezone
    now = datetime(2026, 6, 23, 14, 17, 14, tzinfo=timezone.utc)
    label = ise.build_order_trace_label("sandbox-f3", "T", now)
    assert label.startswith("sandbox-f3-T-")
    with pytest.raises(ValueError):
        uuid.UUID(label)


def test_send_wire_order_id_is_uuid_not_semantic(tmp_path):
    adapter = tx.VerifiedSandboxRestAdapter(transport=_RecorderTx())
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    wire = rep["sandbox_order_request_wire_sanitized"]
    assert wire is not None
    oid = wire["orderId"]
    assert uuid.UUID(oid).version == 4
    assert not oid.startswith("sandbox-f3-")
    assert wire["orderId_is_uuid"] is True
    assert wire["orderId_version"] == 4


def test_semantic_tag_preserved_separately_in_report(tmp_path):
    adapter = tx.VerifiedSandboxRestAdapter(transport=_RecorderTx())
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None, **_SEND_KW)
    # семантический трейс — отдельным полем, НЕ в wire orderId
    assert rep["order_trace_label"].startswith("sandbox-f3-T-")
    assert rep["client_order_tag"].startswith("sandbox-f3-T-")
    wire_oid = rep["sandbox_order_request_wire_sanitized"]["orderId"]
    assert rep["order_trace_label"] != wire_oid


def test_dry_run_verified_rest_builds_wire_preview_with_uuid(tmp_path):
    # dry-run + verified-rest транспорт: wire payload-превью строится БЕЗ отправки,
    # orderId — валидный UUID v4. Сеть/токен не нужны.
    adapter = tx.VerifiedSandboxRestAdapter(transport=_RecorderTx())
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None,
               sandbox_account_id="dummy-sandbox-account")
    assert rep["mode"] == ise.MODE_DRY_RUN
    assert rep["guards"]["sandbox_order_sent"] is False
    wire = rep["sandbox_order_request_wire_sanitized"]
    assert wire is not None
    oid = wire["orderId"]
    assert uuid.UUID(oid).version == 4
    assert not oid.startswith("sandbox-f3-")
    assert wire["orderId_is_uuid"] is True


def test_no_token_leak_with_verified_rest_dry_run(tmp_path):
    adapter = tx.VerifiedSandboxRestAdapter(transport=_RecorderTx())
    rep = _run(tmp_path, [_preview_row()], adapter=adapter, client=None,
               sandbox_account_id="dummy-sandbox-account",
               sandbox_token="sbx-secret-token")
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "sbx-secret-token" not in js
    assert "sbx-secret-token" not in md
    assert "dummy-sandbox-account" not in js
