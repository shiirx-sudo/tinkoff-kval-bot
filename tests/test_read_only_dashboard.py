"""
Тесты F4.7 read_only_dashboard — локальный read-only просмотрщик отчётов.

Чистые функции (load_dashboard_state/build_dashboard_html/sanitize) тестируются без
сервера. Маршрутизация и привязка к 127.0.0.1 проверяются через loopback-сервер на
порту 0 (без интернета). Проверяем: агрегацию F4.1–F4.6, отсутствие падений при
нехватке отчётов, раздельный PnL, income-поля, безопасные guard-флаги и BLOCKED при
небезопасном флаге, маскирование account, редактирование токенов, отсутствие
брокер-клиента/токенов/POST/действий.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from modules import read_only_dashboard as dash

ACCOUNT = "2000001918"
MASKED = "***1918"


def _safe_guards():
    return {g: False for g in dash.EXPECTED_GUARDS}


def _safe_tp():
    return {
        "read_only_token_env": "TINKOFF_TOKEN",
        "read_only_token_present": True,
        "read_only_token_used_for": None,
        "live_trading_token_required": False,
        "live_token_used": False,
        "sandbox_token_used": False,
        "token_printed": False,
    }


def _f41():
    return {"stage": "F4_1", "mode": "X", "generated_at": "2026-06-24T06:00:00+00:00",
            "ticker": "T", "live_account_id_masked": MASKED, "blocking_reasons": [],
            "warnings": [], "guards": _safe_guards(), "token_policy": _safe_tp(),
            "_exit_code": 0}


def _f42():
    return {"stage": "F4_2_LIVE_ORDER_STATUS_READ_ONLY", "mode": "X",
            "generated_at": "2026-06-24T06:01:00+00:00", "order_id": "80578688754",
            "live_account_id_masked": MASKED, "execution_report_status": "FILL",
            "warnings": [], "errors": [], "guards": _safe_guards(),
            "token_policy": _safe_tp(), "_exit_code": 0}


def _f43():
    return {"stage": "F4_3", "mode": "X", "generated_at": "2026-06-24T06:02:00+00:00",
            "ticker": "T", "order_id": "80578688754", "live_account_id_masked": MASKED,
            "position_quantity_units": 27, "average_position_price": 304.02,
            "current_price": 268.26, "current_position_value": 7243.02,
            "unrealized_pnl": -965.52, "currency": "rub",
            "reconciliation_passed": True, "warnings": [], "errors": [],
            "guards": _safe_guards(), "token_policy": _safe_tp(), "_exit_code": 0}


def _f44(**over):
    base = {"stage": "F4_4_LIVE_FILL_ATTRIBUTION_READ_ONLY", "mode": "X",
            "generated_at": "2026-06-24T06:03:00+00:00", "ticker": "T",
            "order_id": "80578688754", "live_account_id_masked": MASKED,
            "fill_quantity_units": 1.0, "fill_price": 276.08,
            "fill_gross_amount": 276.08, "fill_commission_raw": -0.14,
            "fill_commission_abs": 0.14, "fill_cash_outflow": 276.22,
            "fill_attribution_confidence": "medium",
            "attribution_method": "operations_instrument_qty_price_date_match",
            "current_total_position_units": 27.0,
            "current_average_position_price": 304.02, "current_price": 268.26,
            "current_total_position_value": 7243.02,
            "current_total_unrealized_pnl": -965.52, "fill_currency": "rub",
            "warnings": [], "errors": [], "guards": _safe_guards(),
            "token_policy": _safe_tp(), "_exit_code": 0}
    base.update(over)
    return base


def _f45():
    return {"stage": "F4_5_LIVE_FILL_ECONOMICS_READ_ONLY", "mode": "X",
            "generated_at": "2026-06-24T06:04:00+00:00",
            "new_fill_gross_unrealized_pnl": -7.82,
            "new_fill_net_unrealized_pnl_after_commission": -7.96,
            "commission_drag_rub": 0.14, "break_even_price_after_commission": 276.22,
            "distance_to_break_even_rub": -7.96,
            "new_fill_weight_in_total_position_pct": 3.7037,
            "current_total_unrealized_pnl": -965.52,
            "total_position_pnl_kept_separate": True, "current_price": 268.26,
            "current_total_position_units": 27.0,
            "current_total_position_value": 7243.02, "fill_quantity_units": 1.0,
            "fill_cash_outflow": 276.22, "fill_currency": "rub", "warnings": [],
            "errors": [], "guards": _safe_guards(), "token_policy": _safe_tp(),
            "_exit_code": 0}


def _f46():
    return {"stage": "F4_6_LIVE_INCOME_VALIDATION_READ_ONLY", "mode": "X",
            "generated_at": "2026-06-25T19:32:49+00:00",
            "income_data_checked": True, "reliable_income_data_found": True,
            "income_data_confidence": "high", "income_data_source": "api_known_future",
            "expected_dividend_per_unit_rub": 4.6,
            "expected_income_rub_yearly_new_fill": 4.6,
            "expected_income_rub_monthly_new_fill": 0.38,
            "expected_income_rub_yearly_total_position": 124.2,
            "expected_income_rub_monthly_total_position": 10.35,
            "income_target_coverage_pct_new_fill": 0.0003,
            "income_target_coverage_pct_total_position": 0.0069,
            "base_monthly_living_basket_rub": 150000,
            "next_known_income_event_date": "2026-08-24",
            "next_known_income_event_type": "dividend",
            "next_known_income_event_amount_per_unit": 4.6,
            "withholding_tax_assumption": None, "income_validation_passed": True,
            "income_validation_blocking_reasons": [], "warnings": [], "errors": [],
            "guards": _safe_guards(), "token_policy": _safe_tp(), "_exit_code": 0}


_ALL = {"f41": _f41, "f42": _f42, "f43": _f43, "f44": _f44, "f45": _f45, "f46": _f46}
_FILES = {key: filename for key, filename, _ in dash.REPORTS}


def _write_reports(tmp: Path, data: dict) -> str:
    for key, payload in data.items():
        (tmp / _FILES[key]).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(tmp)


def _all_reports():
    return {key: fn() for key, fn in _ALL.items()}


# ─── load + build ─────────────────────────────────────────────────────────────

def test_loads_all_reports_and_builds_state(tmp_path):
    d = _write_reports(tmp_path, _all_reports())
    state = dash.load_dashboard_state(d)
    assert set(state["reports_loaded"]) == {"f41", "f42", "f43", "f44", "f45", "f46"}
    assert state["reports_missing"] == []
    assert state["kind"] == "read_only_dashboard"
    assert state["stage"] == "F4_7_READ_ONLY_WEB_DASHBOARD"
    assert state["mode"] == "DASHBOARD_READ_ONLY"
    assert state["trade_summary"]["order_id"] == "80578688754"
    assert state["latest_generated_at"]
    html = dash.build_dashboard_html(state)
    assert "F4.7" in html and "<html" in html.lower()


def test_missing_reports_do_not_crash(tmp_path):
    state = dash.load_dashboard_state(str(tmp_path))  # пустой каталог
    assert set(state["reports_missing"]) == {"f41", "f42", "f43", "f44", "f45", "f46"}
    assert state["reports_loaded"] == []
    assert state["overall_status"] == dash.STATUS_WARN
    assert state["warnings"]
    html = dash.build_dashboard_html(state)  # не падает
    assert "F4.7" in html


def test_new_fill_and_total_pnl_displayed_separately(tmp_path):
    d = _write_reports(tmp_path, _all_reports())
    state = dash.load_dashboard_state(d)
    econ = state["economics_summary"]
    pos = state["position_summary"]
    assert econ["gross_pnl_before_commission"] == -7.82
    assert econ["net_pnl_after_commission"] == -7.96
    assert econ["total_position_pnl_kept_separate"] is True
    assert pos["total_unrealized_pnl"] == -965.52
    # разные величины
    assert econ["net_pnl_after_commission"] != pos["total_unrealized_pnl"]
    html = dash.build_dashboard_html(state)
    assert "-7.82" in html and "-7.96" in html and "-965.52" in html


def test_income_validation_fields_displayed(tmp_path):
    d = _write_reports(tmp_path, _all_reports())
    state = dash.load_dashboard_state(d)
    inc = state["income_summary"]
    assert inc["reliable_income_data_found"] is True
    assert inc["confidence"] == "high"
    assert inc["source"] == "api_known_future"
    assert inc["expected_income_rub_yearly_total_position"] == 124.2
    assert inc["next_known_income_event_date"] == "2026-08-24"
    assert inc["tax_note"]  # gross/net предупреждение
    html = dash.build_dashboard_html(state)
    assert "api_known_future" in html and "2026-08-24" in html


# ─── safety ───────────────────────────────────────────────────────────────────

def test_safety_all_safe(tmp_path):
    d = _write_reports(tmp_path, _all_reports())
    state = dash.load_dashboard_state(d)
    safety = state["safety_summary"]
    assert safety["any_unsafe"] is False
    assert safety["unsafe_flags"] == []
    assert state["overall_status"] == dash.STATUS_OK
    for key in dash.EXPECTED_GUARDS:
        assert safety["guards_summary"][key] is False


def test_execution_stage_f41_flags_not_unsafe(tmp_path):
    # F4.1 — стадия исполнения: один manual-confirmed ордер БЫЛ отправлен.
    # Её флаги live_order_sent/live_token_used=true ожидаемы и НЕ делают дашборд
    # небезопасным (вердикт считается по read-only стадиям F4.2–F4.6).
    reps = _all_reports()
    f41 = _f41()
    f41["guards"] = {**_safe_guards(), dash.GUARD_LIVE_ORDER_SENT: True,
                     "live_token_used": True}
    f41["token_policy"] = {**_safe_tp(), "live_token_used": True,
                           "read_only_token_used_for": None}
    reps["f41"] = f41
    d = _write_reports(tmp_path, reps)
    state = dash.load_dashboard_state(d)
    assert state["safety_summary"]["any_unsafe"] is False
    assert state["overall_status"] == dash.STATUS_OK
    es = state["safety_summary"]["execution_stage"]
    assert es["order_was_sent"] is True
    assert es["live_token_used"] is True
    # token_policy сводки берётся из read-only стадий, а не из F4.1
    assert state["safety_summary"]["token_policy_summary"]["live_token_used"] is False
    html = dash.build_dashboard_html(state)
    assert "execution stage" in html.lower()


def test_unsafe_guard_blocks(tmp_path):
    reps = _all_reports()
    reps["f44"] = _f44(guards={**_safe_guards(), "post_order_called": True})
    d = _write_reports(tmp_path, reps)
    state = dash.load_dashboard_state(d)
    assert state["safety_summary"]["any_unsafe"] is True
    assert "post_order_called" in state["safety_summary"]["unsafe_flags"]
    assert state["overall_status"] == dash.STATUS_BLOCKED
    html = dash.build_dashboard_html(state)
    assert "BLOCKED_UNSAFE" in html


# ─── masking / redaction ──────────────────────────────────────────────────────

def test_account_id_masked(tmp_path):
    reps = _all_reports()
    reps["f44"] = _f44(account_id=ACCOUNT)  # сырой account id (защитное маскирование)
    d = _write_reports(tmp_path, reps)
    state = dash.load_dashboard_state(d)
    js = json.dumps(state, ensure_ascii=False, default=str)
    html = dash.build_dashboard_html(state)
    assert ACCOUNT not in js
    assert ACCOUNT not in html
    assert state["raw_reports"]["f44"]["account_id"] == MASKED


def test_token_like_values_redacted(tmp_path):
    leak = "t.abcdEFGH1234567890_klmnopqrstuvwxyz"
    reps = _all_reports()
    reps["f44"] = _f44(some_leak=leak)
    d = _write_reports(tmp_path, reps)
    state = dash.load_dashboard_state(d)
    js = json.dumps(state, ensure_ascii=False, default=str)
    html = dash.build_dashboard_html(state)
    assert leak not in js
    assert leak not in html
    assert "***REDACTED***" in state["raw_reports"]["f44"]["some_leak"]


def test_sanitize_is_idempotent_and_pure(tmp_path):
    state = {"a": "t.SECRETSECRETSECRET1234567", "b": {"account_xyz": ACCOUNT}}
    s1 = dash.sanitize_dashboard_state(state)
    s2 = dash.sanitize_dashboard_state(s1)
    assert "t.SECRET" not in json.dumps(s1)
    assert s1 == s2


def test_no_env_token_read(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKOFF_TOKEN", "READ-SECRET")
    monkeypatch.setenv("TINKOFF_LIVE_TRADING_TOKEN", "LIVE-SECRET")
    monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "SANDBOX-SECRET")
    d = _write_reports(tmp_path, _all_reports())
    state = dash.load_dashboard_state(d)
    html = dash.build_dashboard_html(state)
    for secret in ("READ-SECRET", "LIVE-SECRET", "SANDBOX-SECRET"):
        assert secret not in html and secret not in json.dumps(state, default=str)


# ─── source-level safety scans ────────────────────────────────────────────────

def test_module_does_not_initialize_broker_or_read_tokens():
    src = Path(dash.__file__).read_text(encoding="utf-8")
    assert "ReadOnlyClient" not in src
    assert "TINKOFF_TOKEN" not in src
    assert "TINKOFF_LIVE_TRADING_TOKEN" not in src
    assert "TINKOFF_SANDBOX_TOKEN" not in src
    assert "import requests" not in src
    assert "rest_client" not in src


def test_no_post_or_action_handlers():
    handler = dash.make_handler("data/reports", dash.DEFAULT_HOST)
    assert not hasattr(handler, "do_POST")
    assert not hasattr(handler, "do_PUT")
    assert not hasattr(handler, "do_DELETE")


def test_module_source_has_no_forbidden_literals():
    src = Path(dash.__file__).read_text(encoding="utf-8")
    forbidden = (
        "Orders" "Service", "post" "Order(", "cancel" "Order(",
        "place" "_order", "submit" "_order", "cancel" "_order",
        "live" "_order", "order" "_client", "place" "_limit_" "order",
    )
    for tok in forbidden:
        assert tok not in src, tok


def test_html_has_no_external_resources(tmp_path):
    d = _write_reports(tmp_path, _all_reports())
    html = dash.build_dashboard_html(dash.load_dashboard_state(d))
    assert "https://" not in html
    assert "<script src" not in html.lower()
    assert "cdn" not in html.lower()
    assert "//ajax" not in html.lower()


# ─── stale ────────────────────────────────────────────────────────────────────

def test_stale_report_flagged_when_now_given(tmp_path):
    d = _write_reports(tmp_path, _all_reports())
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)  # сильно позже отчётов
    state = dash.load_dashboard_state(d, now=now, stale_after_hours=48)
    assert state["reports_stale_or_invalid"]
    assert any("устарел" in w for w in state["warnings"])


# ─── loopback server (только GET, 127.0.0.1) ──────────────────────────────────

@contextmanager
def _running_server(reports_dir):
    handler = dash.make_handler(reports_dir, dash.DEFAULT_HOST)
    httpd = ThreadingHTTPServer((dash.DEFAULT_HOST, 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_server_binds_localhost_by_default(tmp_path):
    httpd = dash.serve(host=dash.DEFAULT_HOST, port=0, reports_dir=str(tmp_path))
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_get_root_and_state_ok(tmp_path):
    d = _write_reports(tmp_path, _all_reports())
    with _running_server(d) as base:
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert r.status == 200
            body = r.read().decode("utf-8")
            assert "F4.7" in body and "<html" in body.lower()
        with urllib.request.urlopen(base + "/state.json", timeout=5) as r:
            assert r.status == 200
            data = json.loads(r.read().decode("utf-8"))
            assert data["kind"] == "read_only_dashboard"


@pytest.mark.parametrize("path", ["/order", "/buy", "/sell", "/cancel",
                                  "/execute", "/retry", "/anything"])
def test_action_endpoints_return_404(tmp_path, path):
    d = _write_reports(tmp_path, _all_reports())
    with _running_server(d) as base:
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                assert r.status == 404
        except urllib.error.HTTPError as exc:
            assert exc.code == 404


def test_post_request_not_allowed(tmp_path):
    d = _write_reports(tmp_path, _all_reports())
    with _running_server(d) as base:
        req = urllib.request.Request(base + "/", data=b"x", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code in (404, 501)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_registers_dashboard():
    import main
    args = main._parse_args(["dashboard"])
    assert args.command == "dashboard"
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert "dashboard" in main._HANDLERS


# ─── F4.7.1 UX / HTML well-formedness ─────────────────────────────────────────

from html.parser import HTMLParser  # noqa: E402


class _CardNestingParser(HTMLParser):
    """Стек div-ов: отслеживает, не вложена ли одна card в другую, и баланс div."""

    def __init__(self):
        super().__init__()
        self.stack = []            # для каждого открытого <div>: True если это card
        self.max_card_depth = 0
        self.div_balance = 0
        self.nested_card = False

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        classes = dict(attrs).get("class", "") or ""
        is_card = "card" in classes.split()
        if is_card and any(self.stack):
            self.nested_card = True
        self.stack.append(is_card)
        self.div_balance += 1
        self.max_card_depth = max(self.max_card_depth, sum(self.stack))

    def handle_endtag(self, tag):
        if tag != "div":
            return
        self.div_balance -= 1
        if self.stack:
            self.stack.pop()


def _html_all_reports(tmp_path):
    return dash.build_dashboard_html(
        dash.load_dashboard_state(_write_reports(tmp_path, _all_reports())))


def test_html_well_formed_no_nested_cards(tmp_path):
    html = _html_all_reports(tmp_path)
    # баланс table-тегов (регрессия незакрытого <table> в income-card)
    assert html.count("<table") == html.count("</table>")
    p = _CardNestingParser()
    p.feed(html)
    assert p.div_balance == 0, "несбалансированные <div>"
    assert p.nested_card is False, "card вложена в другую card"
    assert p.max_card_depth == 1


def test_safety_card_not_inside_income_card(tmp_path):
    html = _html_all_reports(tmp_path)
    inc_idx = html.find("5 · Валидация дохода")
    saf_idx = html.find("6 · Безопасность")
    assert inc_idx != -1 and saf_idx != -1
    # income раньше safety
    assert inc_idx < saf_idx
    # между заголовком income и заголовком safety таблица income закрыта
    between = html[inc_idx:saf_idx]
    assert between.count("<table") == between.count("</table>")


def test_income_card_closes_table_regression(tmp_path):
    # Точная регрессия: income-card должна закрывать </table> перед tax-note/закрытием.
    html = _html_all_reports(tmp_path)
    inc_idx = html.find("5 · Валидация дохода")
    saf_idx = html.find("6 · Безопасность")
    income_html = html[inc_idx:saf_idx]
    assert "<table" in income_html and "</table>" in income_html
    assert income_html.index("</table>") > income_html.index("<table")


def test_kpi_strip_present(tmp_path):
    html = _html_all_reports(tmp_path)
    assert 'class="kpis"' in html
    for label in ("Текущая цена", "PnL всей позиции",
                  "PnL новой сделки (с комиссией)", "Покрытие цели 150 000 ₽/мес.",
                  "Безопасность"):
        assert label in html


def test_russian_business_labels(tmp_path):
    html = _html_all_reports(tmp_path)
    for label in ("Тикер", "ID заявки", "Цена сделки",
                  "Фактический расход с комиссией", "PnL без комиссии",
                  "PnL с учётом комиссии", "Цена безубытка", "До безубытка",
                  "Всего бумаг", "Средняя цена позиции", "Текущая цена",
                  "PnL всей позиции", "Ожидаемый дивиденд на 1 шт.",
                  "Покрытие цели 150 000 ₽/мес."):
        assert label in html, label
    # технические имена не должны оставаться видимыми ярлыками
    assert "income_data_checked" not in html.split("debug")[0]


def test_interpretation_text_for_coverage(tmp_path):
    html = _html_all_reports(tmp_path)
    assert "Что сейчас" in html
    assert "150 000" in html
    assert "покрывает только" in html
    assert "0.0069%" in html  # покрытие цели всей позицией


def test_units_and_currency_formatting(tmp_path):
    html = _html_all_reports(tmp_path)
    assert "шт." in html        # единицы
    assert "₽" in html          # рубли
    assert "0.0069%" in html    # проценты


def test_raw_reports_collapsed_by_default(tmp_path):
    html = _html_all_reports(tmp_path)
    assert "Технические JSON-отчёты" in html
    # details без атрибута open → свёрнуто по умолчанию
    assert "<details open" not in html
    assert "<details>" in html


def test_freshness_badge_stale_not_unsafe(tmp_path):
    d = _write_reports(tmp_path, _all_reports())
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)  # отчёты сильно устарели
    state = dash.load_dashboard_state(d, now=now, stale_after_hours=48)
    assert state["safety_summary"]["any_unsafe"] is False
    html = dash.build_dashboard_html(state)
    assert "STALE" in html              # бейдж свежести
    assert "BLOCKED_UNSAFE" not in html  # устаревание ≠ небезопасно


def test_missing_reports_freshness_partial(tmp_path):
    # часть отчётов отсутствует → PARTIAL, но не падает
    reps = _all_reports()
    del reps["f41"]
    d = _write_reports(tmp_path, reps)
    html = dash.build_dashboard_html(dash.load_dashboard_state(d))
    assert "PARTIAL" in html
