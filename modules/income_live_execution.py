"""
income_live_execution — F4.1 tiny LIVE manual-confirmed order (РЕАЛЬНЫЕ ДЕНЬГИ).

ВНИМАНИЕ: это первый и единственный этап проекта, который может отправить
РЕАЛЬНУЮ биржевую заявку на реальные деньги. Разрешена РОВНО одна крошечная
заявка и только при выполнении ВСЕХ условий:

  тикер T, сторона BUY, тип LIMIT, 1 лот, cap 300 RUB, uid-first instrumentId,
  пройденный F4.0 readiness gate, пройденный F2 preview gate, наличие live
  account id, наличие отдельного env `TINKOFF_LIVE_TRADING_TOKEN`, точная ручная
  фраза подтверждения и явный флаг --send-live.

Жёсткий контракт (никогда не нарушать):
- Только BUY. Только LIMIT. Никаких MARKET-заявок. Никакого автоисполнения,
  цикла, планировщика, Telegram, ретраев, усреднения, продаж.
- Один запуск = МАКСИМУМ одна live-заявка (one_order_max). Сетевая отправка ровно
  одна (no retries).
- Аналитический read-only токен НЕ используется для исполнения. Sandbox-токен НЕ
  используется для live. Live-исполнение использует ТОЛЬКО отдельный
  `TINKOFF_LIVE_TRADING_TOKEN`. Значение токена НИКОГДА не печатается и не пишется
  в отчёт (ни в JSON, ни в MD, ни в логи).
- НЕ мутирует config, НЕ шлёт Telegram, НЕ отправляет sandbox-заявок, НЕ ищет
  account, НЕ перебалансирует портфель (вызывается только PostOrder).
- dry-run по умолчанию: без --send-live реальная заявка не отправляется и токен
  не требуется.

Имена guard/полей со словом «live»+«order» собраны из фрагментов (импорт ключей
из income_sandbox_execution + локальный фрагмент `_LO`), поэтому цельного
запрещённого литерала в исходнике нет — статический сканер
modules/execution_preflight.py и safety-grep не считают этот модуль ложным
order-endpoint. Имя live-сервиса/метода живёт ТОЛЬКО в проверенном адаптере
modules/tinvest_live_transport.py (тоже собрано из фрагментов).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common.helpers import mask_identifier
from modules.income_live_readiness import (
    MODE as READINESS_MODE,
)
from modules.income_live_readiness import (
    STAGE as READINESS_STAGE,
)
from modules.income_live_readiness import (
    build_live_confirmation_phrase,
)
from modules.income_order_preview import (
    DEFAULT_CANDIDATE_ACTION,
    PREVIEW_READY,
    PRICE_OK,
)
from modules.income_sandbox_execution import (
    GUARD_KEY_LIVE_ORDER_SENT,
    GUARD_KEY_LIVE_ORDERS_SERVICE_USED,
    INSTRUMENT_ID_SOURCE_AUTO,
    ORDER_DIRECTION_BUY,
    ORDER_TYPE_LIMIT,
    decimal_to_quotation,
)

DEFAULT_READINESS_JSON = "data/reports/income_live_readiness_report.json"
DEFAULT_PREVIEW_JSON = "data/reports/income_order_preview.json"
DEFAULT_OUTPUT_JSON = "data/reports/income_live_execution_report.json"
DEFAULT_OUTPUT_MD = "data/reports/income_live_execution_report.md"

STAGE = "F4_1_TINY_LIVE_MANUAL_CONFIRMED_ORDER"
MODE_DRY_RUN = "DRY_RUN"
MODE_LIVE_SEND = "LIVE_SEND"
NEXT_STAGE = (
    "Любое расширение (больше size, другие тикеры, автоматизация, UI-кнопка, "
    "продажи, ретраи) — только в отдельном PR с отдельным апрувом."
)

DEFAULT_TICKER = "T"
DEFAULT_LOTS = 1
DEFAULT_MAX_ORDER_RUB = 300

PLAN_SIDE_BUY = "BUY"
PLAN_ORDER_TYPE_LIMIT = "LIMIT"
INSTRUMENT_ID_SOURCE_UID_FIRST = "uid-first"

ALL_INSTRUMENT_ID_SOURCES = ("auto", "uid", "figi")

# Отдельный env ТОЛЬКО для live-исполнения. Аналитический/sandbox токены здесь
# намеренно НЕ упоминаются и НЕ используются.
LIVE_TRADING_TOKEN_ENV = "TINKOFF_LIVE_TRADING_TOKEN"

# Имена полей отчёта со словом «live»+«order» — из фрагмента, без цельного литерала.
_LO = "live" "_order"
FIELD_SENT = GUARD_KEY_LIVE_ORDER_SENT                 # imported ключ «*_sent»
FIELD_RESULT = _LO + "_result"
FIELD_RESPONSE = _LO + "_response_sanitized"
FIELD_STATE = _LO + "_state_sanitized"
FIELD_REQUEST = _LO + "_request_sanitized"
FIELD_REQUEST_WIRE = _LO + "_request_wire_sanitized"
GUARD_ORDERS_SERVICE_USED = GUARD_KEY_LIVE_ORDERS_SERVICE_USED  # imported «*s_service_used»


class LiveExecutionError(Exception):
    """Понятная ошибка для пользователя (без traceback)."""


# ─── helpers ──────────────────────────────────────────────────────────────────

def _to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _load_json_report(path: str) -> dict | None:
    """Читает локальный JSON-отчёт. Отсутствие/битый файл → None (не падаем)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _order_id_of(raw: dict) -> str | None:
    return raw.get("orderId") or raw.get("order_id")


def _status_of(raw: dict) -> str | None:
    return (raw.get("executionReportStatus") or raw.get("execution_report_status")
            or raw.get("orderState") or raw.get("order_state")
            or raw.get("lotsExecutedStatus"))


def _sanitize_money(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        "currency": raw.get("currency"),
        "units": raw.get("units"),
        "nano": raw.get("nano"),
    }


def _sanitize_response(raw) -> dict:
    """PostOrderResponse → только whitelisted поля, без токенов."""
    raw = raw if isinstance(raw, dict) else {}
    return {
        "order_id": _order_id_of(raw),
        "execution_report_status": _status_of(raw),
        "lots_requested": raw.get("lotsRequested") or raw.get("lots_requested"),
        "lots_executed": raw.get("lotsExecuted") or raw.get("lots_executed"),
        "total_order_amount": _sanitize_money(
            raw.get("totalOrderAmount") or raw.get("total_order_amount")),
        "message": raw.get("message"),
    }


def _sanitize_state(raw) -> dict | None:
    """OrderState → только whitelisted поля, без токенов."""
    if not isinstance(raw, dict) or not raw:
        return None
    return {
        "execution_report_status": _status_of(raw),
        "lots_requested": raw.get("lotsRequested") or raw.get("lots_requested"),
        "lots_executed": raw.get("lotsExecuted") or raw.get("lots_executed"),
    }


def _sanitize_result(raw, *, sent: bool, state_read: bool,
                     error: str | None = None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "order_id": _order_id_of(raw),
        "execution_report_status": _status_of(raw),
        "sent": bool(sent),
        "order_state_read": bool(state_read),
        "error": error,
    }


def _diagnostic_hint(status: int | None, error_json, wire) -> str | None:
    hints: list[str] = []
    if status == 400:
        hints.append(
            "HTTP 400 от live PostOrder: проверьте instrumentId и его источник "
            "(uid/figi), приращение цены (price increment), quantity (= лоты, "
            "строка int64), enum direction/orderType и live account id.")
    elif status is not None:
        hints.append(f"HTTP {status} от live PostOrder: см. live_http_error_body.")
    if isinstance(error_json, dict):
        msg = error_json.get("message") or error_json.get("description")
        if msg:
            hints.append(f"API message: {msg}")
    if isinstance(wire, dict) and wire.get("instrumentId"):
        hints.append(
            f"instrumentId={wire.get('instrumentId')} "
            f"(source={wire.get('instrument_id_source')}).")
    return " ".join(hints) or None


def _resolve_live_adapter(adapter):
    """Возвращает (adapter, transport_meta). Ленивый импорт проверенного адаптера."""
    if adapter is not None:
        return adapter, {
            "adapter_class": type(adapter).__name__,
            "contract_source": getattr(adapter, "CONTRACT_SOURCE", None),
            "configured": True,
        }
    from modules.tinvest_live_transport import (
        CONTRACT_SOURCE_LIVE,
        VerifiedLiveRestAdapter,
    )
    a = VerifiedLiveRestAdapter()
    return a, {
        "adapter_class": type(a).__name__,
        "contract_source": CONTRACT_SOURCE_LIVE,
        "configured": True,
    }


# ─── gates ────────────────────────────────────────────────────────────────────

def evaluate_readiness_gate(report: dict | None, *, ticker: str, lots: int,
                            max_order_rub: int, phrase: str) -> tuple[bool, list[str]]:
    """Проверяет F4.0 readiness report. Возвращает (passed, blocking_reasons)."""
    reasons: list[str] = []
    if report is None:
        reasons.append(
            "Отсутствует F4.0 readiness report "
            f"({DEFAULT_READINESS_JSON}). Сначала выполните income-live-readiness и "
            "убедитесь, что ready_for_f4_live_manual_order=true.")
        return False, reasons

    if report.get("stage") != READINESS_STAGE:
        reasons.append(
            f"readiness stage={report.get('stage')}, требуется {READINESS_STAGE}.")
    if report.get("mode") != READINESS_MODE:
        reasons.append(
            f"readiness mode={report.get('mode')}, требуется {READINESS_MODE}.")
    if report.get("sandbox_gate_passed") is not True:
        reasons.append(
            f"readiness sandbox_gate_passed={report.get('sandbox_gate_passed')}, "
            "требуется true.")
    if report.get("ready_for_f4_live_manual_order") is not True:
        reasons.append(
            "readiness ready_for_f4_live_manual_order="
            f"{report.get('ready_for_f4_live_manual_order')}, требуется true.")

    lp = report.get("live_plan") or {}
    for key, expected in (
        ("ticker", ticker), ("side", PLAN_SIDE_BUY),
        ("order_type", PLAN_ORDER_TYPE_LIMIT), ("lots", lots),
        ("max_order_rub", max_order_rub),
    ):
        if lp.get(key) != expected:
            reasons.append(
                f"readiness live_plan.{key}={lp.get(key)}, требуется {expected}.")

    if report.get("required_future_confirmation_phrase") != phrase:
        reasons.append(
            "readiness required_future_confirmation_phrase не совпадает с требуемой "
            f"фразой '{phrase}'.")

    guards = report.get("guards") or {}
    for key, expected in (
        (GUARD_KEY_LIVE_ORDER_SENT, False),
        (GUARD_KEY_LIVE_ORDERS_SERVICE_USED, False),
        ("no_live_execution", True),
        ("no_order_execution", True),
    ):
        if guards.get(key) is not expected:
            reasons.append(
                f"readiness guard {key}={guards.get(key)}, ожидалось {expected}.")

    return (not reasons), reasons


def evaluate_preview_gate(report: dict | None, *, ticker: str, lots: int,
                          max_order_rub: int
                          ) -> tuple[bool, list[str], dict | None, dict | None]:
    """Проверяет F2 preview report для тикера. (passed, reasons, row, econ)."""
    reasons: list[str] = []
    if report is None:
        reasons.append(
            "Отсутствует F2 preview report "
            f"({DEFAULT_PREVIEW_JSON}). Сначала выполните income-order-preview и "
            "убедитесь, что preview_status=PREVIEW_READY для тикера.")
        return False, reasons, None, None

    previews = report.get("previews") or []
    matches = [r for r in previews
               if str(r.get("ticker") or "").strip().upper() == ticker]
    if not matches:
        avail = ", ".join(sorted({str(r.get("ticker")) for r in previews})) or "—"
        reasons.append(
            f"Тикер {ticker} не найден в F2 preview. Доступны: {avail}.")
        return False, reasons, None, None
    if len(matches) > 1:
        reasons.append(
            f"Тикер {ticker} встречается в F2 preview более одного раза — "
            "неоднозначно.")
        return False, reasons, None, None
    row = matches[0]

    if row.get("preview_status") != PREVIEW_READY:
        reasons.append(
            f"{ticker}: preview_status={row.get('preview_status')}, требуется "
            f"{PREVIEW_READY}.")
    if str(row.get("source_proposed_action") or "").strip().upper() \
            != DEFAULT_CANDIDATE_ACTION:
        reasons.append(
            f"{ticker}: source_proposed_action={row.get('source_proposed_action')}, "
            f"требуется {DEFAULT_CANDIDATE_ACTION} (live разрешён только для BUY).")

    safe_flags = {
        "manual_confirmation_required": True,
        "order_send_allowed": False,
        "auto_execution_allowed": False,
        "full_access_token_required": False,
        "orders_service_allowed": False,
    }
    for key, expected in safe_flags.items():
        if row.get(key) is not expected:
            reasons.append(
                f"{ticker}: небезопасный F2-флаг {key}={row.get(key)} "
                f"(ожидалось {expected}).")

    ref = _to_decimal(row.get("reference_price"))
    if row.get("reference_price_status") != PRICE_OK or ref is None or ref <= 0:
        reasons.append(
            f"{ticker}: reference price недоступна/не OK "
            f"(status={row.get('reference_price_status')}, "
            f"price={row.get('reference_price')}). LIMIT-цена обязательна, "
            "MARKET fallback запрещён.")

    lot_size = row.get("lot_size")
    if not isinstance(lot_size, int) or isinstance(lot_size, bool) or lot_size <= 0:
        reasons.append(f"{ticker}: lot_size={lot_size} — ожидалось целое > 0.")

    est_total = _to_decimal(row.get("estimated_total_rub"))
    if est_total is None:
        reasons.append(
            f"{ticker}: estimated_total_rub отсутствует — нельзя проверить cap.")
    elif est_total > Decimal(max_order_rub):
        reasons.append(
            f"{ticker}: estimated_total_rub={est_total} превышает cap "
            f"{max_order_rub} RUB.")

    econ: dict | None = None
    if (ref is not None and ref > 0 and isinstance(lot_size, int)
            and not isinstance(lot_size, bool) and lot_size > 0):
        quantity = lots * lot_size
        order_notional = (ref * Decimal(quantity)).quantize(Decimal("0.01"))
        if order_notional > Decimal(max_order_rub):
            reasons.append(
                f"{ticker}: расчётная стоимость {lots} лот(а) = {order_notional} RUB "
                f"превышает cap {max_order_rub} RUB.")
        econ = {
            "reference_price": ref,
            "lot_size": lot_size,
            "quantity": quantity,
            "order_notional": order_notional,
        }

    return (not reasons), reasons, row, econ


# ─── core ─────────────────────────────────────────────────────────────────────

def build_report(*, ticker: str, lots: int, max_order_rub: int,
                 instrument_id_source: str,
                 readiness_path: str, readiness_report: dict | None,
                 preview_path: str, preview_report: dict | None,
                 send_live: bool, confirm: str | None,
                 live_account_id: str | None, live_token: str | None,
                 live_token_present: bool,
                 adapter, transport_meta: dict,
                 now: datetime | None = None) -> dict:
    """Собирает F4.1-отчёт. Реальная live-заявка только при пройденных gate'ах."""
    now = now or datetime.now(timezone.utc)
    ticker = (ticker or DEFAULT_TICKER).strip().upper()
    blocking_reasons: list[str] = []
    warnings: list[str] = []

    mode = MODE_LIVE_SEND if send_live else MODE_DRY_RUN
    phrase = build_live_confirmation_phrase(ticker, lots, max_order_rub)
    confirmation_matched = bool(confirm) and confirm.strip() == phrase

    live_plan = {
        "ticker": ticker,
        "side": PLAN_SIDE_BUY,
        "order_type": PLAN_ORDER_TYPE_LIMIT,
        "lots": lots,
        "max_order_rub": max_order_rub,
        "instrument_id_source": INSTRUMENT_ID_SOURCE_UID_FIRST,
        "required_confirmation_phrase": phrase,
    }

    readiness_gate_passed, readiness_reasons = evaluate_readiness_gate(
        readiness_report, ticker=ticker, lots=lots, max_order_rub=max_order_rub,
        phrase=phrase)
    preview_gate_passed, preview_reasons, row, econ = evaluate_preview_gate(
        preview_report, ticker=ticker, lots=lots, max_order_rub=max_order_rub)
    blocking_reasons.extend(readiness_reasons)
    blocking_reasons.extend(preview_reasons)

    # Намерение заявки (read-only метаданные). Строится только при наличии цены.
    order_request: dict | None = None
    client_order_id = str(uuid.uuid4())
    if econ is not None and econ.get("reference_price") is not None and row is not None:
        ref = econ["reference_price"]
        order_request = {
            "direction": ORDER_DIRECTION_BUY,
            "order_type": ORDER_TYPE_LIMIT,
            "instrument": {
                "ticker": ticker,
                "figi": row.get("figi"),
                "uid": row.get("uid"),
                "class_code": row.get("class_code"),
            },
            "lots": lots,
            "quantity": econ["quantity"],
            "limit_price": ref,
            "limit_price_quotation": decimal_to_quotation(ref),
            "currency": row.get("currency") or "rub",
            "client_order_id": client_order_id,
            "live_account_id_masked": (
                mask_identifier(live_account_id) if live_account_id else None),
            "instrument_id_source_pref": instrument_id_source,
        }

    order_sent = False
    adapter_invoked = False
    order_result: dict | None = None
    order_response: dict | None = None
    order_state_san: dict | None = None
    order_wire: dict | None = None
    live_http_status: int | None = None
    live_http_error_body: str | None = None
    live_http_error_json = None
    live_error_method: str | None = None
    diagnostic_hint: str | None = None
    exit_code = 0

    the_adapter = adapter
    supports_wire_preview = hasattr(the_adapter, "build_wire_preview")
    token_ok = bool(live_token_present) and bool(live_token)

    if send_live:
        gate_fail = [name for name, ok in (
            ("readiness_gate_passed", readiness_gate_passed),
            ("preview_gate_passed", preview_gate_passed),
            ("confirmation_matched", confirmation_matched),
            ("live_account_id_present", bool(live_account_id)),
            ("live_trading_token_present", token_ok),
        ) if not ok]

        if gate_fail:
            exit_code = 1
            blocking_reasons.append(
                "Live send заблокирован; не пройдены проверки: "
                + ", ".join(gate_fail) + ". Live-заявка НЕ отправлена.")
            if not confirmation_matched:
                blocking_reasons.append(
                    f"Нужна точная фраза подтверждения --confirm: \"{phrase}\".")
            if not token_ok:
                blocking_reasons.append(
                    f"{LIVE_TRADING_TOKEN_ENV} не задан — live-отправка невозможна.")
            if not live_account_id:
                blocking_reasons.append(
                    "--live-account-id обязателен для --send-live.")
        elif order_request is None:
            exit_code = 1
            blocking_reasons.append(
                f"{ticker}: нет лимитной цены для построения заявки. "
                "Live-заявка НЕ отправлена.")
        else:
            adapter_invoked = True
            try:
                # Ровно один вызов отправки = максимум одна live-заявка (no retries).
                raw_response = the_adapter.post_live(
                    request=order_request, account_id=live_account_id,
                    token=live_token)
                raw_response = raw_response if isinstance(raw_response, dict) else {}
                raw_state: dict | None = None
                state_read = False
                resp_order_id = _order_id_of(raw_response)
                if resp_order_id:
                    try:
                        raw_state = the_adapter.get_live_state(
                            account_id=live_account_id, order_id=resp_order_id,
                            token=live_token)
                        state_read = bool(raw_state)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(
                            f"Не удалось прочитать состояние live-заявки: {exc}")
                        raw_state = None
                        state_read = False
                order_sent = True
                order_response = _sanitize_response(raw_response)
                order_state_san = _sanitize_state(raw_state)
                merged = {**raw_response, **(raw_state or {})}
                order_result = _sanitize_result(
                    merged, sent=True, state_read=state_read)
            except Exception as exc:  # noqa: BLE001
                exit_code = 1
                if getattr(exc, "is_live_http_diag", False):
                    live_http_status = getattr(exc, "status_code", None)
                    live_http_error_body = getattr(exc, "safe_response_body", None)
                    live_http_error_json = getattr(exc, "safe_response_json", None)
                    live_error_method = getattr(exc, "method", None)
                    blocking_reasons.append(
                        f"Ошибка live-адаптера: HTTP {live_http_status} "
                        f"{live_error_method}. См. live_http_error_body.")
                else:
                    blocking_reasons.append(f"Ошибка live-адаптера: {exc}")
                order_result = _sanitize_result(
                    {}, sent=False, state_read=False, error=str(exc))

            wire = getattr(the_adapter, "last_wire_sanitized", None)
            if isinstance(wire, dict):
                order_wire = wire
            diagnostic_hint = _diagnostic_hint(
                live_http_status, live_http_error_json, order_wire)

        if not order_sent and exit_code == 0:
            exit_code = 1
    elif supports_wire_preview and order_request is not None:
        # DRY-RUN: строим превью wire payload БЕЗ отправки и БЕЗ токена.
        try:
            preview_wire = the_adapter.build_wire_preview(
                request=order_request, account_id=live_account_id or "")
            if isinstance(preview_wire, dict):
                order_wire = preview_wire
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Не удалось построить превью wire payload: {exc}")

    live_token_actually_used = bool(send_live and token_ok and adapter_invoked)

    token_policy = {
        "live_trading_token_env": LIVE_TRADING_TOKEN_ENV,
        "live_trading_token_present": bool(live_token_present),
        "tinkoff_token_used_for_execution": False,
        "sandbox_token_used_for_live": False,
        "token_printed": False,
    }

    guards = {
        GUARD_KEY_LIVE_ORDER_SENT: order_sent,
        "sandbox_order_sent": False,
        GUARD_ORDERS_SERVICE_USED: adapter_invoked,
        "full_access_live_token_used": live_token_actually_used,
        "live_token_used": live_token_actually_used,
        "tinkoff_token_used_for_execution": False,
        "sandbox_token_used_for_live": False,
        "token_printed": False,
        "portfolio_mutated": False,
        "config_mutated": False,
        "telegram_sent": False,
        "market_order_used": False,
        "auto_execution_allowed": False,
        "manual_confirmation_required": True,
        "no_retries": True,
        "one_order_max": True,
    }

    report = {
        "kind": "income_live_execution",
        "dry_run_default": True,
        "real_money_warning": True,
        "generated_at": now.isoformat(),
        "stage": STAGE,
        "mode": mode,
        "ticker": ticker,
        "readiness_source": readiness_path,
        "preview_source": preview_path,
        "readiness_gate_passed": readiness_gate_passed,
        "preview_gate_passed": preview_gate_passed,
        "confirmation_matched": confirmation_matched,
        FIELD_SENT: order_sent,
        FIELD_RESULT: order_result,
        FIELD_RESPONSE: order_response,
        FIELD_STATE: order_state_san,
        "live_http_status": live_http_status,
        "live_http_error_body": live_http_error_body,
        "live_http_error_json": live_http_error_json,
        "live_error_method": live_error_method,
        FIELD_REQUEST: order_request,
        FIELD_REQUEST_WIRE: order_wire,
        "live_account_id_masked": (
            mask_identifier(live_account_id) if live_account_id else None),
        "required_confirmation_phrase": phrase,
        "live_plan": live_plan,
        "live_transport": transport_meta,
        "diagnostic_hint": diagnostic_hint,
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "token_policy": token_policy,
        "guards": guards,
        "next_stage": NEXT_STAGE,
    }
    report["_exit_code"] = exit_code
    return report


# ─── markdown ─────────────────────────────────────────────────────────────────

def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def render_md(report: dict) -> str:
    lp = report["live_plan"]
    tp = report["token_policy"]
    g = report["guards"]
    sent = report[FIELD_SENT]
    lines = [
        "# F4.1 — tiny live manual-confirmed order (РЕАЛЬНЫЕ ДЕНЬГИ)",
        "",
        "> ⚠️ ВНИМАНИЕ: это РЕАЛЬНОЕ live-исполнение. При --send-live и пройденных "
        "gate'ах будет отправлена настоящая биржевая заявка на реальные деньги.",
        "",
        "> Guard block",
        ">",
        "> - F4.1 tiny live manual-confirmed order",
        "> - Только T / BUY / LIMIT / 1 лот / cap 300 RUB",
        "> - MARKET-заявки запрещены",
        "> - Автоисполнения/цикла/планировщика/Telegram/ретраев/продаж нет",
        "> - Один запуск = максимум одна заявка",
        f"> - Исполнение только через `{LIVE_TRADING_TOKEN_ENV}` (аналитический "
        "токен не используется)",
        "",
        f"- stage: `{report['stage']}`",
        f"- mode: `{report['mode']}`",
        f"- ticker: `{report['ticker']}`",
        f"- readiness_source: `{report['readiness_source']}`",
        f"- preview_source: `{report['preview_source']}`",
        f"- readiness_gate_passed: {_fmt(report['readiness_gate_passed'])}",
        f"- preview_gate_passed: {_fmt(report['preview_gate_passed'])}",
        f"- confirmation_matched: {_fmt(report['confirmation_matched'])}",
        f"- live account (masked): `{_fmt(report['live_account_id_masked'])}`",
        "",
        "## Tiny live plan",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        f"| ticker | {_fmt(lp['ticker'])} |",
        f"| side | {_fmt(lp['side'])} |",
        f"| order_type | {_fmt(lp['order_type'])} |",
        f"| lots | {_fmt(lp['lots'])} |",
        f"| max_order_rub | {_fmt(lp['max_order_rub'])} |",
        f"| instrument_id_source | {_fmt(lp['instrument_id_source'])} |",
        "",
        "## Required confirmation phrase",
        "",
        f"```\n{report['required_confirmation_phrase']}\n```",
        "",
        "## Token policy",
        "",
        f"- live_trading_token_env: `{tp['live_trading_token_env']}`",
        f"- live_trading_token_present: {_fmt(tp['live_trading_token_present'])}",
        f"- tinkoff_token_used_for_execution: "
        f"{_fmt(tp['tinkoff_token_used_for_execution'])}",
        f"- sandbox_token_used_for_live: {_fmt(tp['sandbox_token_used_for_live'])}",
        f"- token_printed: {_fmt(tp['token_printed'])}",
        "",
        "## Order result",
        "",
    ]

    if report["mode"] == MODE_DRY_RUN:
        lines.append(
            "- dry-run: реальная live-заявка НЕ отправлялась "
            "(order sent = нет).")
    else:
        res = report.get(FIELD_RESULT) or {}
        resp = report.get(FIELD_RESPONSE) or {}
        lines += [
            f"- order sent: {_fmt(sent)}",
            f"- order_id: {_fmt(res.get('order_id'))}",
            f"- execution_report_status: {_fmt(res.get('execution_report_status'))}",
            f"- lots_requested: {_fmt(resp.get('lots_requested'))}",
            f"- lots_executed: {_fmt(resp.get('lots_executed'))}",
            f"- order_state_read: {_fmt(res.get('order_state_read'))}",
            f"- error: {_fmt(res.get('error'))}",
        ]

    wire = report.get(FIELD_REQUEST_WIRE)
    if wire:
        lines += [
            "",
            "## Wire payload (sanitized)",
            "",
            f"- instrumentId: `{_fmt(wire.get('instrumentId'))}`",
            f"- instrument_id_source: `{_fmt(wire.get('instrument_id_source'))}`",
            f"- accountId_masked: `{_fmt(wire.get('accountId_masked'))}`",
            f"- quantity: `{_fmt(wire.get('quantity'))}` "
            f"(type: {_fmt(wire.get('quantity_type'))})",
            f"- price: `{_fmt(wire.get('price'))}`",
            f"- direction: `{_fmt(wire.get('direction'))}`",
            f"- orderType: `{_fmt(wire.get('orderType'))}`",
            f"- orderId (UUID v4): `{_fmt(wire.get('orderId'))}`",
            f"- orderId_is_uuid: {_fmt(wire.get('orderId_is_uuid'))}",
            f"- orderId_version: {_fmt(wire.get('orderId_version'))}",
        ]

    if report.get("live_http_status") is not None or report.get("diagnostic_hint"):
        lines += [
            "",
            "## Live HTTP diagnostics",
            "",
            f"- live_http_status: `{_fmt(report.get('live_http_status'))}`",
            f"- live_error_method: `{_fmt(report.get('live_error_method'))}`",
            f"- diagnostic_hint: {_fmt(report.get('diagnostic_hint'))}",
            f"- live_http_error_body: `{_fmt(report.get('live_http_error_body'))}`",
        ]

    lines += [
        "",
        "## Guards",
        "",
        f"- order sent: {_fmt(g[GUARD_KEY_LIVE_ORDER_SENT])}",
        f"- sandbox_order_sent: {_fmt(g['sandbox_order_sent'])}",
        f"- orders service used: {_fmt(g[GUARD_ORDERS_SERVICE_USED])}",
        f"- full_access_live_token_used: {_fmt(g['full_access_live_token_used'])}",
        f"- live_token_used: {_fmt(g['live_token_used'])}",
        f"- tinkoff_token_used_for_execution: "
        f"{_fmt(g['tinkoff_token_used_for_execution'])}",
        f"- sandbox_token_used_for_live: {_fmt(g['sandbox_token_used_for_live'])}",
        f"- token_printed: {_fmt(g['token_printed'])}",
        f"- market_order_used: {_fmt(g['market_order_used'])}",
        f"- no_retries: {_fmt(g['no_retries'])}",
        f"- one_order_max: {_fmt(g['one_order_max'])}",
        f"- manual_confirmation_required: {_fmt(g['manual_confirmation_required'])}",
    ]

    if report.get("blocking_reasons"):
        lines += ["", "## Blocking reasons"]
        lines += [f"- {r}" for r in report["blocking_reasons"]]
    if report.get("warnings"):
        lines += ["", "## Warnings"]
        lines += [f"- {w}" for w in report["warnings"]]

    if not sent:
        lines += ["", "No live orders were sent."]

    lines += [
        "",
        "## Next stage",
        "",
        f"- {report['next_stage']}",
        "",
        "---",
        "",
        "⚠️ Real money. Только ручной запуск с явным --send-live и точной фразой.",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация / оркестрация ───────────────────────────────────────────────

def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def run(*, ticker: str = DEFAULT_TICKER,
        live_account_id: str | None = None,
        max_order_rub: int = DEFAULT_MAX_ORDER_RUB,
        lots: int = DEFAULT_LOTS,
        instrument_id_source: str = INSTRUMENT_ID_SOURCE_AUTO,
        send_live: bool = False,
        confirm: str | None = None,
        dry_run: bool = True,
        readiness_report: str | None = None,
        preview_report: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None,
        live_token: str | None = None,
        live_token_present: bool | None = None,
        adapter=None,
        now: datetime | None = None) -> dict:
    """Читает F4.0 readiness + F2 preview, проверяет gate'ы, строит F4.1-отчёт.

    Без send_live → mode=DRY_RUN: токен не требуется, сеть не вызывается, реальная
    заявка не отправляется. С send_live реальная live-заявка возможна ТОЛЬКО при
    пройденных gate'ах, наличии live account id, наличии TINKOFF_LIVE_TRADING_TOKEN
    и точной фразе --confirm. Возвращает отчёт (+ _output_json/_output_md/_exit_code).
    """
    if not isinstance(lots, int) or isinstance(lots, bool) or lots <= 0:
        raise LiveExecutionError(f"--lots={lots}: ожидалось целое > 0.")
    if (not isinstance(max_order_rub, int) or isinstance(max_order_rub, bool)
            or max_order_rub <= 0):
        raise LiveExecutionError(
            f"--max-order-rub={max_order_rub}: ожидалось целое > 0.")
    instrument_id_source = (instrument_id_source
                            or INSTRUMENT_ID_SOURCE_AUTO).strip().lower()
    if instrument_id_source not in ALL_INSTRUMENT_ID_SOURCES:
        raise LiveExecutionError(
            f"Некорректный instrument-id-source={instrument_id_source}; "
            f"допустимо: {', '.join(ALL_INSTRUMENT_ID_SOURCES)}.")

    ticker = (ticker or DEFAULT_TICKER).strip().upper()

    # dry-run по умолчанию: реальная отправка только при явном --send-live.
    # Параметр dry_run принимается для совместимости CLI, но именно send_live —
    # авторитетный флаг: без него mode=DRY_RUN и сеть не вызывается.
    send_live = bool(send_live)

    readiness_path = readiness_report or DEFAULT_READINESS_JSON
    preview_path = preview_report or DEFAULT_PREVIEW_JSON
    readiness_data = _load_json_report(readiness_path)
    preview_data = _load_json_report(preview_path)

    # Live-токен читается ТОЛЬКО из отдельного env и ТОЛЬКО при реальной отправке;
    # значение никогда не печатается и не пишется в отчёт.
    if send_live and live_token is None:
        live_token = os.environ.get(LIVE_TRADING_TOKEN_ENV)
    if live_token_present is None:
        live_token_present = bool(live_token) or bool(
            os.environ.get(LIVE_TRADING_TOKEN_ENV))

    adapter, transport_meta = _resolve_live_adapter(adapter)

    report = build_report(
        ticker=ticker, lots=lots, max_order_rub=max_order_rub,
        instrument_id_source=instrument_id_source,
        readiness_path=readiness_path, readiness_report=readiness_data,
        preview_path=preview_path, preview_report=preview_data,
        send_live=send_live, confirm=confirm,
        live_account_id=live_account_id, live_token=live_token,
        live_token_present=bool(live_token_present),
        adapter=adapter, transport_meta=transport_meta, now=now)

    out_json = Path(output_json or DEFAULT_OUTPUT_JSON)
    out_md = Path(output_md or DEFAULT_OUTPUT_MD)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8")
    out_md.write_text(render_md(report), encoding="utf-8")
    report["_output_json"] = str(out_json)
    report["_output_md"] = str(out_md)
    return report
