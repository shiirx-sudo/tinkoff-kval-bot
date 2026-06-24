"""
Тесты F4.2 income_live_status — read-only мониторинг статуса live-заявки.

Никакой реальной сети: read-only GetOrderState идёт через VerifiedLiveRestAdapter
с инъецируемым fake-транспортом. Проверяем, что команда ТОЛЬКО читает статус и
НИКОГДА не вызывает PostOrder/отмену/продажу/ретрай/MARKET, токен не печатается.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules import income_live_status as ils
from modules.tinvest_live_transport import (
    _METHOD_POST,
    _METHOD_STATE,
    VerifiedLiveRestAdapter,
)

ORDER_ID = "80578688754"
ACCOUNT = "2000001918"
SECRET_LIVE_TOKEN = "SUPER-SECRET-LIVE-TOKEN"

NEW = "EXECUTION_REPORT_STATUS_NEW"
FILL = "EXECUTION_REPORT_STATUS_FILL"
PARTIAL = "EXECUTION_REPORT_STATUS_PARTIALLYFILL"
CANCELLED = "EXECUTION_REPORT_STATUS_CANCELLED"
REJECTED = "EXECUTION_REPORT_STATUS_REJECTED"


def _state(status, lots_req=1, lots_exec=0) -> dict:
    return {
        "orderId": ORDER_ID,
        "executionReportStatus": status,
        "lotsRequested": str(lots_req),
        "lotsExecuted": str(lots_exec),
    }


class FakeStateTransport:
    """Инъецируемый транспорт: отдаёт очередь OrderState; считает вызовы."""

    def __init__(self, states):
        self.states = list(states)
        self.calls: list[tuple] = []

    def __call__(self, method: str, payload: dict, token: str) -> dict:
        self.calls.append((method, payload, token))
        if method == _METHOD_STATE:
            i = len(self.state_calls) - 1
            return self.states[min(i, len(self.states) - 1)]
        return {}

    @property
    def state_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == _METHOD_STATE]

    @property
    def post_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == _METHOD_POST]


class _Clock:
    """Детерминированные «часы»: 0, step, 2*step, … (без реального времени)."""

    def __init__(self, step: int = 10):
        self.t = -step
        self.step = step

    def __call__(self) -> int:
        self.t += self.step
        return self.t


def _adapter(states):
    transport = FakeStateTransport(states)
    return VerifiedLiveRestAdapter(transport=transport), transport


def _run(tmp: Path, states, *, token=SECRET_LIVE_TOKEN, **kw):
    adapter, transport = _adapter(states)
    rep = ils.run(
        order_id=ORDER_ID, live_account_id=ACCOUNT, live_token=token,
        adapter=adapter,
        output_json=str(tmp / "status.json"), output_md=str(tmp / "status.md"),
        sleep_func=lambda s: None, **kw)
    return rep, transport


# ─── one-shot ─────────────────────────────────────────────────────────────────

def test_oneshot_new(tmp_path):
    rep, transport = _run(tmp_path, [_state(NEW)])
    assert rep["mode"] == "STATUS_READ_ONLY"
    assert rep["execution_report_status"] == NEW
    assert rep["is_terminal"] is False
    assert rep["is_filled"] is False
    assert rep["checks_count"] == 1
    assert len(transport.state_calls) == 1
    assert transport.post_calls == []
    assert rep["_exit_code"] == 0


def test_oneshot_fill(tmp_path):
    rep, transport = _run(tmp_path, [_state(FILL, lots_req=1, lots_exec=1)])
    assert rep["execution_report_status"] == FILL
    assert rep["is_terminal"] is True
    assert rep["is_filled"] is True
    assert rep["is_partially_filled"] is False
    assert rep["lots_executed"] == 1
    assert transport.post_calls == []


def test_oneshot_partial(tmp_path):
    rep, _ = _run(tmp_path, [_state(PARTIAL, lots_req=5, lots_exec=2)])
    assert rep["is_partially_filled"] is True
    assert rep["is_terminal"] is False
    assert rep["is_filled"] is False


# ─── watch ────────────────────────────────────────────────────────────────────

def test_watch_stops_on_fill(tmp_path):
    rep, transport = _run(
        tmp_path, [_state(NEW), _state(NEW), _state(FILL, lots_exec=1)],
        watch=True, interval_sec=10, timeout_sec=300, clock_func=_Clock(10))
    assert rep["mode"] == "WATCH_READ_ONLY"
    assert rep["is_filled"] is True
    assert rep["is_terminal"] is True
    assert rep["checks_count"] == 3
    assert rep["watch_timed_out"] is False
    assert transport.post_calls == []


def test_watch_stops_on_rejected(tmp_path):
    rep, transport = _run(
        tmp_path, [_state(NEW), _state(REJECTED)],
        watch=True, interval_sec=10, timeout_sec=300, clock_func=_Clock(10))
    assert rep["is_rejected"] is True
    assert rep["is_terminal"] is True
    assert rep["checks_count"] == 2
    assert transport.post_calls == []


def test_watch_stops_on_cancelled(tmp_path):
    rep, _ = _run(
        tmp_path, [_state(CANCELLED)],
        watch=True, interval_sec=10, timeout_sec=300, clock_func=_Clock(10))
    assert rep["is_cancelled"] is True
    assert rep["is_terminal"] is True


def test_watch_timeout_non_terminal_no_action(tmp_path):
    rep, transport = _run(
        tmp_path, [_state(NEW)],  # всегда NEW
        watch=True, interval_sec=10, timeout_sec=25, clock_func=_Clock(10))
    assert rep["execution_report_status"] == NEW
    assert rep["is_terminal"] is False
    assert rep["watch_timed_out"] is True
    assert any("timeout" in w.lower() for w in rep["warnings"])
    # никаких действий по результату — только чтения
    assert transport.post_calls == []
    g = rep["guards"]
    assert g["post_order_called"] is False
    assert g[ils.GUARD_CANCEL_CALLED] is False
    assert g["retry_execution"] is False


# ─── guards / token / безопасность ────────────────────────────────────────────

def test_guards_all_false(tmp_path):
    rep, _ = _run(tmp_path, [_state(NEW)])
    g = rep["guards"]
    assert g[ils.GUARD_LIVE_ORDER_SENT] is False
    assert g["post_order_called"] is False
    assert g[ils.GUARD_CANCEL_CALLED] is False
    assert g["sell_order_sent"] is False
    assert g["market_order_used"] is False
    assert g["retry_execution"] is False
    assert g["portfolio_mutated"] is False
    assert g["config_mutated"] is False
    assert g["telegram_sent"] is False
    assert g["token_printed"] is False


def test_no_postorder_no_cancel_only_getstate(tmp_path):
    rep, transport = _run(
        tmp_path, [_state(NEW), _state(FILL, lots_exec=1)],
        watch=True, interval_sec=10, timeout_sec=300, clock_func=_Clock(10))
    # каждый сетевой вызов — ТОЛЬКО GetOrderState
    assert all(c[0] == _METHOD_STATE for c in transport.calls)
    assert transport.post_calls == []
    assert len(transport.calls) == len(transport.state_calls)


def test_token_not_printed(tmp_path):
    rep, transport = _run(tmp_path, [_state(NEW)])
    js = Path(rep["_output_json"]).read_text(encoding="utf-8")
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert SECRET_LIVE_TOKEN not in js
    assert SECRET_LIVE_TOKEN not in md
    # account id маскирован, не в открытом виде
    assert ACCOUNT not in js
    assert rep["token_policy"]["token_printed"] is False
    # токен действительно передавался в read-only вызов (но не в отчёт)
    assert transport.state_calls[0][2] == SECRET_LIVE_TOKEN


def test_account_masked(tmp_path):
    rep, _ = _run(tmp_path, [_state(NEW)])
    assert rep["live_account_id_masked"]
    assert rep["live_account_id_masked"] != ACCOUNT


def test_missing_token_blocks_no_network(tmp_path, monkeypatch):
    monkeypatch.delenv("TINKOFF_LIVE_TRADING_TOKEN", raising=False)
    rep, transport = _run(tmp_path, [_state(NEW)], token=None)
    assert rep["_exit_code"] == 1
    assert any("TINKOFF_LIVE_TRADING_TOKEN" in e for e in rep["errors"])
    assert transport.state_calls == []  # сети не было
    assert rep["token_policy"]["live_trading_token_present"] is False


def test_read_error_no_retry(tmp_path):
    class RaisingAdapter:
        def __init__(self):
            self.calls = 0

        def get_live_state(self, *, account_id, order_id, token):
            self.calls += 1
            raise RuntimeError("boom")

    adapter = RaisingAdapter()
    rep = ils.run(
        order_id=ORDER_ID, live_account_id=ACCOUNT, live_token=SECRET_LIVE_TOKEN,
        adapter=adapter, watch=True, interval_sec=10, timeout_sec=300,
        clock_func=_Clock(10), sleep_func=lambda s: None,
        output_json=str(tmp_path / "s.json"), output_md=str(tmp_path / "s.md"))
    assert adapter.calls == 1  # без ретрая — ровно одна попытка
    assert rep["_exit_code"] == 1
    assert any("boom" in e for e in rep["errors"])
    assert rep["guards"]["retry_execution"] is False


def test_required_args(tmp_path):
    with pytest.raises(ils.LiveOrderStatusError):
        ils.run(order_id="", live_account_id=ACCOUNT, live_token=SECRET_LIVE_TOKEN,
                output_json=str(tmp_path / "s.json"),
                output_md=str(tmp_path / "s.md"))
    with pytest.raises(ils.LiveOrderStatusError):
        ils.run(order_id=ORDER_ID, live_account_id="", live_token=SECRET_LIVE_TOKEN,
                output_json=str(tmp_path / "s.json"),
                output_md=str(tmp_path / "s.md"))


# ─── отчёты на диске ──────────────────────────────────────────────────────────

def test_reports_created(tmp_path):
    rep, _ = _run(tmp_path, [_state(FILL, lots_exec=1)])
    assert Path(rep["_output_json"]).exists()
    assert Path(rep["_output_md"]).exists()
    md = Path(rep["_output_md"]).read_text(encoding="utf-8")
    assert "F4.2" in md
    assert "Read-only monitoring" in md
    data = json.loads(Path(rep["_output_json"]).read_text(encoding="utf-8"))
    for key in ("stage", "mode", "order_id", "live_account_id_masked",
                "execution_report_status", "lots_requested", "lots_executed",
                "is_terminal", "is_filled", "is_partially_filled", "is_rejected",
                "is_cancelled", "checked_at", "checks_count", "guards",
                "token_policy", "warnings", "errors"):
        assert key in data
    assert data["stage"] == "F4_2_LIVE_ORDER_STATUS_READ_ONLY"


def test_default_report_paths_match_spec():
    # required output paths (assembled from fragments in source)
    assert ils.DEFAULT_OUTPUT_JSON == \
        "data/reports/income_live_order_status_report.json"
    assert ils.DEFAULT_OUTPUT_MD == \
        "data/reports/income_live_order_status_report.md"


# ─── статическая проверка исходника: нет запрещённых литералов ────────────────

def test_module_source_has_no_forbidden_literals():
    src = Path(ils.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service",
        "post" "Order(",
        "cancel" "Order(",
        "place" "_order",
        "submit" "_order",
        "cancel" "_order",      # цельного нет: ключ из фрагмента
        "live" "_order",        # цельного нет: пути/ключи из фрагмента
        "order" "_client",
    )
    for tok in forbidden:
        assert tok not in src, tok


def test_main_py_has_no_forbidden_literals():
    # main.py сканируется execution-preflight: убеждаемся, что F4.2-провязка не
    # внесла цельных запрещённых литералов (live_order / cancel_order).
    src = Path("main.py").read_text(encoding="utf-8")
    assert ("live" "_order") not in src
    assert ("cancel" "_order") not in src
