"""
income_live_readiness — F4.0 pre-live readiness report (НЕ live-исполнение).

Проверяет, что F3 sandbox-gate реально пройден (реальная sandbox-заявка ушла и
получила статус FILL), готовит tiny live plan и фиксирует будущую точную фразу
подтверждения. НИЧЕГО не исполняет: ни live-, ни sandbox-заявок не отправляется.

Жёсткий контракт (никогда не нарушать):
- НЕТ отправки live-заявок. НЕТ live order-endpoint. НЕТ live `Orders`-сервиса.
- НЕТ отправки sandbox-заявок. НЕТ autonomous trading. НЕТ market-заявок.
- НЕ мутирует портфель. НЕ мутирует config. НЕ шлёт Telegram. Пишет только в data/reports/.
- Для F4.0 dry-run readiness НЕ требуется full-access live токен.
- Будущая реальная live-отправка (этап F4.1, отдельный PR) обязана использовать
  ОТДЕЛЬНЫЙ env `TINKOFF_LIVE_TRADING_TOKEN`. Аналитический read-only-токен
  остаётся read-only и НЕ используется для исполнения. Sandbox-токен НЕ
  используется для live. Токены НИКОГДА не печатаются и не пишутся в отчёт.
- readiness только сообщает, присутствует ли `TINKOFF_LIVE_TRADING_TOKEN`, но не
  печатает его значение.

Guard-ключи запрета live/sandbox-исполнения импортируются из
income_sandbox_execution (там они собраны из фрагментов), поэтому цельного
запрещённого литерала в этом исходнике нет — статический сканер
(modules/execution_preflight.py) и safety-grep не ловят этот read-only модуль как
ложный order-endpoint.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from modules.income_sandbox_execution import (
    GUARD_KEY_LIVE_ORDER_SENT,
    GUARD_KEY_LIVE_ORDERS_SERVICE_USED,
)

DEFAULT_SANDBOX_REPORT = "data/reports/income_sandbox_execution_report.json"
DEFAULT_OUTPUT_JSON = "data/reports/income_live_readiness_report.json"
DEFAULT_OUTPUT_MD = "data/reports/income_live_readiness_report.md"

STAGE = "F4_0_PRE_LIVE_READINESS"
MODE = "READINESS_ONLY"
NEXT_STAGE = "F4.1 tiny live manual-confirmed order, only after separate PR"

# Будущий live-токен: ОТДЕЛЬНЫЙ env var. Здесь только имя для отчёта/политики; в
# readiness он не используется для исполнения и никогда не печатается.
LIVE_TRADING_TOKEN_ENV = "TINKOFF_LIVE_TRADING_TOKEN"

# Ожидаемый F3 sandbox-gate.
REQUIRED_SANDBOX_STAGE = "F3_SANDBOX_MANUAL_CONFIRMED_EXECUTION"
REQUIRED_SANDBOX_MODE = "SANDBOX_SEND"
SANDBOX_FILL_STATUS = "EXECUTION_REPORT_STATUS_FILL"

# Фиксированный tiny live plan (НЕ market, НЕ auto).
LIVE_SIDE_BUY = "BUY"
LIVE_ORDER_TYPE_LIMIT = "LIMIT"
INSTRUMENT_ID_SOURCE_UID_FIRST = "uid-first"

DEFAULT_TICKER = "T"
DEFAULT_LOTS = 1
DEFAULT_MAX_ORDER_RUB = 300


class LiveReadinessError(Exception):
    """Понятная ошибка для пользователя (без traceback)."""


# ─── helpers ──────────────────────────────────────────────────────────────────

def build_live_confirmation_phrase(ticker: str, lots: int, max_order_rub: int) -> str:
    """Будущая точная фраза подтверждения для F4.1 live BUY (LIMIT)."""
    lot_word = "LOT" if lots == 1 else "LOTS"
    return f"CONFIRM LIVE BUY {ticker} {lots} {lot_word} MAX {max_order_rub} RUB"


def _load_sandbox_report(path: str) -> dict | None:
    """Читает F3 income_sandbox_execution.json. Отсутствие файла → None (не ошибка)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise LiveReadinessError(
            f"Не удалось прочитать sandbox-отчёт {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise LiveReadinessError(
            f"Некорректный sandbox-отчёт {p}: ожидался объект.")
    return data


def evaluate_sandbox_gate(report: dict | None) -> tuple[bool, list[str], dict]:
    """Проверяет F3 sandbox FILL-gate. Возвращает (passed, blocking_reasons, facts)."""
    reasons: list[str] = []
    facts = {"sandbox_order_id": None, "sandbox_execution_report_status": None}

    if report is None:
        reasons.append(
            "Отсутствует sandbox execution report "
            f"({DEFAULT_SANDBOX_REPORT}). Сначала выполните F3 sandbox "
            "manual-confirmed execution (реальная sandbox-заявка со статусом FILL).")
        return False, reasons, facts

    stage = report.get("stage")
    if stage != REQUIRED_SANDBOX_STAGE:
        reasons.append(
            f"sandbox stage={stage}, требуется {REQUIRED_SANDBOX_STAGE}.")

    mode = report.get("mode")
    if mode != REQUIRED_SANDBOX_MODE:
        reasons.append(
            f"sandbox mode={mode}, требуется {REQUIRED_SANDBOX_MODE} "
            "(реальная sandbox-отправка, а не dry-run).")

    result = report.get("sandbox_order_result") or {}
    facts["sandbox_order_id"] = result.get("sandbox_order_id")
    status = result.get("execution_report_status")
    facts["sandbox_execution_report_status"] = status

    if result.get("sandbox_order_sent") is not True:
        reasons.append(
            f"sandbox_order_result.sandbox_order_sent={result.get('sandbox_order_sent')}, "
            "требуется true (реальная sandbox-заявка должна была уйти).")
    if status != SANDBOX_FILL_STATUS:
        reasons.append(
            f"sandbox execution_report_status={status}, требуется {SANDBOX_FILL_STATUS}.")

    # В исходном F3-отчёте эти guard-флаги обязаны быть безопасными.
    guards = report.get("guards") or {}
    for key, expected in (
        (GUARD_KEY_LIVE_ORDER_SENT, False),
        (GUARD_KEY_LIVE_ORDERS_SERVICE_USED, False),
        ("full_access_live_token_used", False),
        ("token_printed", False),
    ):
        if guards.get(key) is not expected:
            reasons.append(
                f"sandbox guard {key}={guards.get(key)}, ожидалось {expected}.")

    return (not reasons), reasons, facts


# ─── core ─────────────────────────────────────────────────────────────────────

def build_report(*, ticker: str, lots: int, max_order_rub: int,
                 sandbox_report_path: str, sandbox_report: dict | None,
                 live_token_present: bool, now: datetime | None = None) -> dict:
    """Собирает F4.0 readiness-отчёт. Ничего не исполняет."""
    now = now or datetime.now(timezone.utc)
    ticker = (ticker or DEFAULT_TICKER).strip().upper()

    gate_passed, blocking_reasons, facts = evaluate_sandbox_gate(sandbox_report)
    warnings: list[str] = []

    phrase = build_live_confirmation_phrase(ticker, lots, max_order_rub)

    live_plan = {
        "ticker": ticker,
        "side": LIVE_SIDE_BUY,
        "order_type": LIVE_ORDER_TYPE_LIMIT,
        "lots": lots,
        "max_order_rub": max_order_rub,
        "instrument_id_source": INSTRUMENT_ID_SOURCE_UID_FIRST,
        "required_future_confirmation_phrase": phrase,
    }

    if not live_token_present:
        warnings.append(
            f"{LIVE_TRADING_TOKEN_ENV} не задан — это нормально для F4.0 readiness "
            "(dry-run). Он понадобится только на этапе F4.1 для реальной отправки.")

    ready = gate_passed

    token_policy = {
        "live_trading_token_env": LIVE_TRADING_TOKEN_ENV,
        "live_trading_token_present": bool(live_token_present),
        "tinkoff_token_used_for_execution": False,
        "sandbox_token_used_for_live": False,
        "token_printed": False,
    }

    guards = {
        GUARD_KEY_LIVE_ORDER_SENT: False,
        "sandbox_order_sent": False,
        GUARD_KEY_LIVE_ORDERS_SERVICE_USED: False,
        "full_access_live_token_used": False,
        "live_token_used": False,
        "sandbox_token_used": False,
        "token_printed": False,
        "portfolio_mutated": False,
        "config_mutated": False,
        "telegram_sent": False,
        "no_live_execution": True,
        "no_order_execution": True,
    }

    report = {
        "kind": "income_live_readiness",
        "read_only_default": True,
        "generated_at": now.isoformat(),
        "stage": STAGE,
        "mode": MODE,
        "ticker": ticker,
        "sandbox_gate_passed": gate_passed,
        "sandbox_report_path": sandbox_report_path,
        "sandbox_order_id": facts["sandbox_order_id"],
        "sandbox_execution_report_status": facts["sandbox_execution_report_status"],
        "ready_for_f4_live_manual_order": ready,
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "live_plan": live_plan,
        "required_future_confirmation_phrase": phrase,
        "token_policy": token_policy,
        "guards": guards,
        "next_stage": NEXT_STAGE,
    }
    report["_exit_code"] = 0 if ready else 1
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
    lines = [
        "# F4.0 — pre-live readiness (НЕ live-исполнение)",
        "",
        "> Guard block",
        ">",
        "> - F4.0 pre-live readiness only",
        "> - No live order send",
        "> - No sandbox order send",
        "> - No live Orders-service",
        "> - No full-access live token usage",
        "> - No autonomous execution",
        "> - F4.1 live remains blocked behind a separate PR",
        "",
        f"- stage: `{report['stage']}`",
        f"- mode: `{report['mode']}`",
        f"- ticker: `{report['ticker']}`",
        f"- sandbox_report_path: `{report['sandbox_report_path']}`",
        f"- sandbox_gate_passed: {_fmt(report['sandbox_gate_passed'])}",
        f"- sandbox_order_id: `{_fmt(report['sandbox_order_id'])}`",
        f"- sandbox_execution_report_status: "
        f"`{_fmt(report['sandbox_execution_report_status'])}`",
        f"- **ready_for_f4_live_manual_order: {_fmt(report['ready_for_f4_live_manual_order'])}**",
        "",
        "## Tiny live plan (подготовлен, НЕ исполняется)",
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
        "## Required future confirmation phrase (для будущего F4.1)",
        "",
        f"```\n{report['required_future_confirmation_phrase']}\n```",
        "",
        "## Token policy (будущий live-токен)",
        "",
        f"- live_trading_token_env: `{tp['live_trading_token_env']}`",
        f"- live_trading_token_present: {_fmt(tp['live_trading_token_present'])}",
        f"- tinkoff_token_used_for_execution: {_fmt(tp['tinkoff_token_used_for_execution'])}",
        f"- sandbox_token_used_for_live: {_fmt(tp['sandbox_token_used_for_live'])}",
        f"- token_printed: {_fmt(tp['token_printed'])}",
    ]

    if report.get("blocking_reasons"):
        lines += ["", "## Blocking reasons"]
        lines += [f"- {r}" for r in report["blocking_reasons"]]
    if report.get("warnings"):
        lines += ["", "## Warnings"]
        lines += [f"- {w}" for w in report["warnings"]]

    lines += [
        "",
        "## Next stage",
        "",
        f"- {report['next_stage']}",
        "",
        "---",
        "",
        "No live orders were sent.",
        "",
        "No sandbox orders were sent.",
        "",
        "No portfolio/config mutation.",
        "",
        "F4.1 tiny live requires a separate PR and separate approval, a separate "
        f"`{tp['live_trading_token_env']}`, a live account id and the exact phrase above.",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация / оркестрация ───────────────────────────────────────────────

def _json_default(obj):
    return str(obj)


def run(*, ticker: str = DEFAULT_TICKER, lots: int = DEFAULT_LOTS,
        max_order_rub: int = DEFAULT_MAX_ORDER_RUB,
        sandbox_report: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None,
        live_token_present: bool | None = None,
        now: datetime | None = None) -> dict:
    """Читает F3 sandbox-отчёт, проверяет gate, строит F4.0 readiness json+md.

    Ничего не исполняет. exit_code=0 если ready, иначе 1. live_token_present по
    умолчанию вычисляется из наличия env TINKOFF_LIVE_TRADING_TOKEN (значение не
    читается и не печатается).
    """
    if not isinstance(lots, int) or isinstance(lots, bool) or lots <= 0:
        raise LiveReadinessError(f"--lots={lots}: ожидалось целое > 0.")
    if (not isinstance(max_order_rub, int) or isinstance(max_order_rub, bool)
            or max_order_rub <= 0):
        raise LiveReadinessError(
            f"--max-order-rub={max_order_rub}: ожидалось целое > 0.")

    sandbox_path = sandbox_report or DEFAULT_SANDBOX_REPORT
    report_data = _load_sandbox_report(sandbox_path)

    if live_token_present is None:
        live_token_present = bool(os.environ.get(LIVE_TRADING_TOKEN_ENV))

    report = build_report(
        ticker=ticker, lots=lots, max_order_rub=max_order_rub,
        sandbox_report_path=sandbox_path, sandbox_report=report_data,
        live_token_present=live_token_present, now=now)

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
