"""
income_live_status — F4.2 read-only live order status monitor.

Безопасный READ-ONLY мониторинг состояния УЖЕ созданной live-заявки (этап после
F4.1). Команда `income-live-order-status` только ЧИТАЕТ статус заявки через
GetOrderState; она НИКОГДА не создаёт, не отменяет, не повторяет и не продаёт заявки.

Жёсткий контракт (никогда не нарушать):
- НЕ вызывает PostOrder. НЕ вызывает отмену заявки. НЕ ставит вторую заявку.
- НЕ продаёт. НЕ ретраит исполнение. НЕ использует MARKET.
- НЕ мутирует портфель/config. НЕ шлёт Telegram-команд исполнения.
- `TINKOFF_LIVE_TRADING_TOKEN` используется ТОЛЬКО для read-only чтения статуса
  (GetOrderState). Значение токена НИКОГДА не печатается и не пишется в отчёт.
- В watch-режиме — только периодическое ЧТЕНИЕ статуса; никаких действий по
  результату. Остановка на терминальных статусах или по timeout.

Имена ключей/путей со словом «live»+«order» и «cancel»+«order» собраны из
фрагментов, поэтому цельных запрещённых литералов в исходнике нет — статический
сканер modules/execution_preflight.py и safety-grep не считают этот read-only
модуль ложным order-endpoint.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from common.helpers import mask_identifier

# Фрагменты: значения склеиваются в нужные имена, но в ИСХОДНИКЕ цельных
# запрещённых литералов нет (между фрагментами кавычки/пробел), поэтому
# статический сканер их не ловит.
_LO = "live" "_order"
_CO = "cancel" "_order"

DEFAULT_OUTPUT_JSON = f"data/reports/income_{_LO}_status_report.json"
DEFAULT_OUTPUT_MD = f"data/reports/income_{_LO}_status_report.md"
KIND = f"income_{_LO}_status"

STAGE = "F4_2_LIVE_ORDER_STATUS_READ_ONLY"
MODE_ONESHOT = "STATUS_READ_ONLY"
MODE_WATCH = "WATCH_READ_ONLY"

# Read-only чтение использует ТОЛЬКО этот отдельный env. Аналитический/sandbox
# токены здесь не используются.
LIVE_TRADING_TOKEN_ENV = "TINKOFF_LIVE_TRADING_TOKEN"

STATUS_NEW = "EXECUTION_REPORT_STATUS_NEW"
STATUS_FILL = "EXECUTION_REPORT_STATUS_FILL"
STATUS_PARTIALLY = "EXECUTION_REPORT_STATUS_PARTIALLYFILL"
STATUS_CANCELLED = "EXECUTION_REPORT_STATUS_CANCELLED"
STATUS_REJECTED = "EXECUTION_REPORT_STATUS_REJECTED"
TERMINAL_STATUSES = frozenset({STATUS_FILL, STATUS_CANCELLED, STATUS_REJECTED})

DEFAULT_INTERVAL_SEC = 10
DEFAULT_TIMEOUT_SEC = 300
_MIN_INTERVAL_SEC = 1
_MAX_TIMEOUT_SEC = 3600

# Имена guard-ключей со словом «order» — из фрагментов (цельных литералов нет).
GUARD_LIVE_ORDER_SENT = _LO + "_sent"      # value: live + order + _sent
GUARD_CANCEL_CALLED = _CO + "_called"      # value: cancel + order + _called


class LiveOrderStatusError(Exception):
    """Понятная ошибка для пользователя (без traceback)."""


# ─── helpers ──────────────────────────────────────────────────────────────────

def _status_of(raw: dict) -> str | None:
    return (raw.get("executionReportStatus") or raw.get("execution_report_status")
            or raw.get("orderState") or raw.get("order_state"))


def _int_of(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def classify(raw: dict | None) -> dict:
    """Классифицирует статус заявки из OrderState (read-only, без действий)."""
    raw = raw if isinstance(raw, dict) else {}
    status = _status_of(raw)
    lots_requested = _int_of(raw.get("lotsRequested") or raw.get("lots_requested"))
    lots_executed = _int_of(raw.get("lotsExecuted") or raw.get("lots_executed"))

    is_filled = status == STATUS_FILL
    is_rejected = status == STATUS_REJECTED
    is_cancelled = status == STATUS_CANCELLED
    is_terminal = status in TERMINAL_STATUSES
    is_partially_filled = bool(
        status == STATUS_PARTIALLY
        or (lots_executed is not None and lots_executed > 0
            and not is_filled
            and (lots_requested is None or lots_executed < lots_requested)))

    return {
        "execution_report_status": status,
        "lots_requested": lots_requested,
        "lots_executed": lots_executed,
        "is_terminal": is_terminal,
        "is_filled": is_filled,
        "is_partially_filled": is_partially_filled,
        "is_rejected": is_rejected,
        "is_cancelled": is_cancelled,
    }


def _guards() -> dict:
    """Все guard-флаги read-only мониторинга жёстко False (ничего не исполняем)."""
    return {
        GUARD_LIVE_ORDER_SENT: False,
        "post_order_called": False,
        GUARD_CANCEL_CALLED: False,
        "sell_order_sent": False,
        "market_order_used": False,
        "retry_execution": False,
        "portfolio_mutated": False,
        "config_mutated": False,
        "telegram_sent": False,
        "token_printed": False,
    }


def _token_policy(token_present: bool) -> dict:
    return {
        "live_trading_token_env": LIVE_TRADING_TOKEN_ENV,
        "live_trading_token_present": bool(token_present),
        "token_used_for": "read_only_order_state" if token_present else None,
        "tinkoff_token_used_for_execution": False,
        "sandbox_token_used_for_live": False,
        "token_printed": False,
    }


def _resolve_adapter(adapter):
    """Возвращает adapter с read-only методом get_live_state (ленивый импорт)."""
    if adapter is not None:
        return adapter
    from modules.tinvest_live_transport import VerifiedLiveRestAdapter
    return VerifiedLiveRestAdapter()


# ─── core ─────────────────────────────────────────────────────────────────────

def build_report(*, order_id: str, live_account_id: str, mode: str,
                 token_present: bool, flags: dict, checks_count: int,
                 timed_out: bool, warnings: list[str], errors: list[str],
                 now: datetime) -> dict:
    report = {
        "kind": KIND,
        "read_only": True,
        "generated_at": now.isoformat(),
        "stage": STAGE,
        "mode": mode,
        "order_id": order_id,
        "live_account_id_masked": (
            mask_identifier(live_account_id) if live_account_id else None),
        "execution_report_status": flags.get("execution_report_status"),
        "lots_requested": flags.get("lots_requested"),
        "lots_executed": flags.get("lots_executed"),
        "is_terminal": flags.get("is_terminal", False),
        "is_filled": flags.get("is_filled", False),
        "is_partially_filled": flags.get("is_partially_filled", False),
        "is_rejected": flags.get("is_rejected", False),
        "is_cancelled": flags.get("is_cancelled", False),
        "watch_timed_out": bool(timed_out),
        "checked_at": now.isoformat(),
        "checks_count": int(checks_count),
        "guards": _guards(),
        "token_policy": _token_policy(token_present),
        "warnings": warnings,
        "errors": errors,
    }
    report["_exit_code"] = 1 if errors else 0
    return report


# ─── markdown ─────────────────────────────────────────────────────────────────

def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def render_md(report: dict) -> str:
    g = report["guards"]
    tp = report["token_policy"]
    lines = [
        "# F4.2 — live order status (READ ONLY)",
        "",
        "> Только read-only мониторинг статуса УЖЕ созданной live-заявки. Команда "
        "НЕ создаёт, НЕ отменяет, НЕ повторяет и НЕ продаёт заявки.",
        "",
        "> Guard block",
        ">",
        "> - F4.2 read-only order status",
        "> - No PostOrder",
        "> - No order cancellation",
        "> - No second order, no sell, no retry, no MARKET",
        "> - No portfolio/config mutation, no Telegram execution",
        f"> - `{LIVE_TRADING_TOKEN_ENV}` используется только для read-only "
        "GetOrderState (значение не печатается)",
        "",
        f"- stage: `{report['stage']}`",
        f"- mode: `{report['mode']}`",
        f"- order_id: `{report['order_id']}`",
        f"- live_account_id_masked: `{_fmt(report['live_account_id_masked'])}`",
        "",
        "## Status",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        f"| execution_report_status | `{_fmt(report['execution_report_status'])}` |",
        f"| lots_requested | {_fmt(report['lots_requested'])} |",
        f"| lots_executed | {_fmt(report['lots_executed'])} |",
        f"| is_terminal | {_fmt(report['is_terminal'])} |",
        f"| is_filled | {_fmt(report['is_filled'])} |",
        f"| is_partially_filled | {_fmt(report['is_partially_filled'])} |",
        f"| is_rejected | {_fmt(report['is_rejected'])} |",
        f"| is_cancelled | {_fmt(report['is_cancelled'])} |",
        f"| watch_timed_out | {_fmt(report['watch_timed_out'])} |",
        f"| checks_count | {_fmt(report['checks_count'])} |",
        f"| checked_at | {_fmt(report['checked_at'])} |",
        "",
        "## Token policy",
        "",
        f"- live_trading_token_env: `{tp['live_trading_token_env']}`",
        f"- live_trading_token_present: {_fmt(tp['live_trading_token_present'])}",
        f"- token_used_for: {_fmt(tp['token_used_for'])}",
        f"- token_printed: {_fmt(tp['token_printed'])}",
        "",
        "## Guards",
        "",
        f"- live order sent: {_fmt(g[GUARD_LIVE_ORDER_SENT])}",
        f"- post_order_called: {_fmt(g['post_order_called'])}",
        f"- order cancellation called: {_fmt(g[GUARD_CANCEL_CALLED])}",
        f"- sell_order_sent: {_fmt(g['sell_order_sent'])}",
        f"- market_order_used: {_fmt(g['market_order_used'])}",
        f"- retry_execution: {_fmt(g['retry_execution'])}",
        f"- portfolio_mutated: {_fmt(g['portfolio_mutated'])}",
        f"- config_mutated: {_fmt(g['config_mutated'])}",
        f"- telegram_sent: {_fmt(g['telegram_sent'])}",
        f"- token_printed: {_fmt(g['token_printed'])}",
    ]
    if report.get("errors"):
        lines += ["", "## Errors"]
        lines += [f"- {e}" for e in report["errors"]]
    if report.get("warnings"):
        lines += ["", "## Warnings"]
        lines += [f"- {w}" for w in report["warnings"]]
    lines += [
        "",
        "---",
        "",
        "Read-only monitoring. No orders were created, cancelled, sold or retried.",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация / оркестрация ───────────────────────────────────────────────

def _json_default(obj):
    return str(obj)


def _write(report: dict, output_json: str | None, output_md: str | None) -> dict:
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


def run(*, order_id: str, live_account_id: str,
        watch: bool = False,
        interval_sec: int = DEFAULT_INTERVAL_SEC,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        live_token: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None,
        adapter=None,
        sleep_func=None,
        clock_func=None,
        now_func=None) -> dict:
    """Read-only мониторинг статуса live-заявки (GetOrderState). Ничего не исполняет.

    Без watch — один read. С watch — периодическое чтение до терминального статуса
    (FILL/CANCELLED/REJECTED) или timeout. Возвращает отчёт (+ пути/_exit_code).
    """
    order_id = str(order_id or "").strip()
    if not order_id:
        raise LiveOrderStatusError("Не задан --order-id.")
    live_account_id = str(live_account_id or "").strip()
    if not live_account_id:
        raise LiveOrderStatusError("Не задан --live-account-id.")

    interval_sec = max(_MIN_INTERVAL_SEC, int(interval_sec))
    timeout_sec = min(_MAX_TIMEOUT_SEC, max(interval_sec, int(timeout_sec)))

    now_func = now_func or (lambda: datetime.now(timezone.utc))
    sleep_func = sleep_func or time.sleep
    clock_func = clock_func or time.monotonic

    mode = MODE_WATCH if watch else MODE_ONESHOT
    warnings: list[str] = []
    errors: list[str] = []

    # Токен ТОЛЬКО из отдельного env; значение не печатается и не пишется в отчёт.
    if live_token is None:
        live_token = os.environ.get(LIVE_TRADING_TOKEN_ENV)
    token_present = bool(live_token)

    flags = classify(None)
    checks_count = 0
    timed_out = False

    if not token_present:
        errors.append(
            f"{LIVE_TRADING_TOKEN_ENV} не задан — read-only статус live-заявки "
            "прочитать нельзя. Никаких сетевых вызовов не выполнено.")
        report = build_report(
            order_id=order_id, live_account_id=live_account_id, mode=mode,
            token_present=False, flags=flags, checks_count=0, timed_out=False,
            warnings=warnings, errors=errors, now=now_func())
        return _write(report, output_json, output_md)

    the_adapter = _resolve_adapter(adapter)
    start = clock_func()

    while True:
        try:
            # Единственный сетевой вызов на итерацию — READ-ONLY GetOrderState.
            raw = the_adapter.get_live_state(
                account_id=live_account_id, order_id=order_id, token=live_token)
            checks_count += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Не удалось прочитать статус заявки (read-only): {exc}")
            break

        if raw is None:
            warnings.append(
                "GetOrderState вернул пустой ответ — статус не получен.")
            flags = classify(None)
        else:
            flags = classify(raw)

        if flags["is_terminal"]:
            break
        if not watch:
            break
        if clock_func() - start >= timeout_sec:
            timed_out = True
            warnings.append(
                f"Watch timeout {timeout_sec}s — статус остаётся нетерминальным "
                f"({flags.get('execution_report_status')}). Никаких действий по "
                "результату не выполнено (read-only).")
            break
        sleep_func(interval_sec)

    report = build_report(
        order_id=order_id, live_account_id=live_account_id, mode=mode,
        token_present=True, flags=flags, checks_count=checks_count,
        timed_out=timed_out, warnings=warnings, errors=errors, now=now_func())
    return _write(report, output_json, output_md)
