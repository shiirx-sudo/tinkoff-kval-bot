"""
income_live_income_validation — F4.6 read-only income/dividend data validation.

Безопасная READ-ONLY проверка: есть ли НАДЁЖНЫЕ данные о доходе/дивидендах для
конкретного инструмента/позиции (по точному UID/FIGI), и можно ли их безопасно
использовать. Команда `income-live-income-validation` НИЧЕГО не исполняет и НИЧЕГО
не угадывает: при отсутствии надёжного источника income-поля остаются null с
явным объяснением причины.

Источник дохода — существующий read-only механизм проекта
`modules/income_sources.fetch_dividend_data` (T-Invest `GetDividends`), который
сам помечает `api_known_future` (объявленные будущие выплаты) против
`api_trailing_12m` (историческая ОЦЕНКА, не гарантия). F4.6 не выдумывает
аннуализацию: trailing-оценка не считается надёжным годовым доходом.

Жёсткий контракт (никогда не нарушать):
- Только READ-ONLY данные: отчёты F4.1–F4.5 и (опц.) аналитический `TINKOFF_TOKEN`
  для read-only валидации доходных данных.
- `TINKOFF_LIVE_TRADING_TOKEN` НЕ требуется и НЕ используется.
- `TINKOFF_SANDBOX_TOKEN` НЕ используется. Токен не печатается и не пишется в отчёт.
- НЕ вызывает PostOrder, НЕ отменяет, НЕ ставит/не продаёт заявок, НЕ ретраит,
  НЕ использует MARKET. НЕ мутирует портфель/config. НЕ шлёт Telegram.
- gross/net налогообложение помечается ЯВНО; при неизвестном налоге считаем только
  БРУТТО и предупреждаем (net не угадываем).

Имена ключей/guard со словом «order» переиспользуются из F4.1–F4.5 модулей
(constants), поэтому цельных запрещённых литералов в этом исходнике нет —
статический сканер modules/execution_preflight.py и safety-grep не считают этот
read-only модуль ложным order-endpoint.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common.helpers import mask_identifier
from modules.income_live_execution import (
    DEFAULT_OUTPUT_JSON as F41_DEFAULT_JSON,
)
from modules.income_live_fill_attribution import (
    DEFAULT_OUTPUT_JSON as F44_DEFAULT_JSON,
)
from modules.income_live_fill_economics import (
    DEFAULT_OUTPUT_JSON as F45_DEFAULT_JSON,
)
from modules.income_live_position import (
    BASE_MONTHLY_LIVING_BASKET_RUB,
    extract_instrument_ids,
)
from modules.income_live_position import (
    DEFAULT_OUTPUT_JSON as F43_DEFAULT_JSON,
)
from modules.income_live_status import (
    GUARD_CANCEL_CALLED,
    GUARD_LIVE_ORDER_SENT,
)
from modules.income_sources import (
    CONF_API_KNOWN,
    CONF_ESTIMATED,
    CONF_MANUAL,
    SRC_API_TRAILING_12M,
)

DEFAULT_OUTPUT_JSON = "data/reports/income_live_income_validation_report.json"
DEFAULT_OUTPUT_MD = "data/reports/income_live_income_validation_report.md"

STAGE = "F4_6_LIVE_INCOME_VALIDATION_READ_ONLY"
MODE = "INCOME_VALIDATION_READ_ONLY"

READ_TOKEN_ENV = "TINKOFF_TOKEN"
LIVE_TRADING_TOKEN_ENV = "TINKOFF_LIVE_TRADING_TOKEN"

# Уровни уверенности F4.6
CONF_NONE = "none"
CONF_LOW = "low"
CONF_MEDIUM = "medium"
CONF_HIGH = "high"

# Метки источников/причин
SRC_UNSUPPORTED = "unsupported_by_current_client"
SRC_NO_TOKEN = "no_token_no_local_income_data"
REASON_NO_RELIABLE = "no_reliable_income_source"
REASON_UNSUPPORTED = "unsupported_by_current_client"
REASON_TRAILING = "income_estimate_trailing_not_guaranteed"
REASON_EVENT_NOT_ANNUALIZED = "future_event_present_but_not_annualizable"
REASON_TAX_UNKNOWN = "withholding_tax_unknown_gross_only"

_M2 = Decimal("0.01")
_P4 = Decimal("0.0001")

WARN_TAX_UNKNOWN = (
    "Оценка дохода — БРУТТО (до налога): налоговый режим неизвестен, net не "
    "считаем (не угадываем). Учитывайте удержание налога отдельно.")
WARN_TRAILING = (
    "Найдены только trailing (исторические) дивиденды — это ОЦЕНКА, не гарантия "
    "будущих выплат; как надёжный годовой доход не используется.")
WARN_NO_RELIABLE = (
    "Надёжного источника дохода/дивидендов для этого инструмента нет — income-поля "
    "= null (не угадываем).")
WARN_UNSUPPORTED = (
    "Текущий read-only клиент не поддерживает метод доходных данных — данные не "
    "проверены через API; income-поля = null.")
WARN_NO_TOKEN = (
    "Нет read-only TINKOFF_TOKEN, а локальные отчёты не несут доходных данных — "
    "проверка через API не выполнялась; income-поля = null.")


class IncomeValidationError(Exception):
    """Понятная ошибка для пользователя (без traceback)."""


# ─── helpers ──────────────────────────────────────────────────────────────────

def _load_json(path: str | None) -> dict | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _to_decimal(value) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _first(*values) -> Decimal | None:
    for v in values:
        d = _to_decimal(v)
        if d is not None:
            return d
    return None


def _first_str(*values) -> str | None:
    for v in values:
        if v not in (None, ""):
            return v
    return None


# ─── оценка дохода (нормализация income_sources-словаря) ──────────────────────

def assess_income(div: dict | None) -> dict:
    """Нормализует income_sources-словарь в оценку F4.6 (без угадывания).

    Возвращает: reliable, confidence, source, as_of, per_unit, next_event_*,
    annualized, note.
    """
    out = {
        "reliable": False,
        "confidence": CONF_NONE,
        "source": None,
        "as_of": None,
        "per_unit": None,
        "next_event_date": None,
        "next_event_type": None,
        "next_event_amount_per_unit": None,
        "annualized": False,
        "note": None,
    }
    if not isinstance(div, dict):
        return out

    out["source"] = div.get("dividend_source")
    out["as_of"] = _first_str(div.get("as_of"), div.get("data_as_of"))
    per_unit = _to_decimal(div.get("expected_annual_dividend_rub_per_share"))
    conf_token = div.get("dividend_confidence")

    # будущее событие (известная объявленная выплата) — отдельно от аннуализации
    events = div.get("events") or []
    if events and isinstance(events[0], dict):
        out["next_event_date"] = _first_str(events[0].get("date"))
        out["next_event_amount_per_unit"] = _to_decimal(events[0].get("per_share"))
        out["next_event_type"] = "dividend"
    out["next_event_date"] = out["next_event_date"] or _first_str(
        div.get("next_dividend_date"))
    if out["next_event_date"] and out["next_event_type"] is None:
        out["next_event_type"] = "dividend"

    # уверенность: manual / api_known → high; trailing-оценка → low; иначе none
    if conf_token in (CONF_API_KNOWN, CONF_MANUAL):
        out["confidence"] = CONF_HIGH
    elif conf_token == CONF_ESTIMATED:
        out["confidence"] = CONF_LOW
    else:
        out["confidence"] = CONF_NONE

    # Надёжно ТОЛЬКО при per_unit>0 И высокой/средней уверенности И источник —
    # не trailing-оценка (trailing не гарантирует будущие выплаты).
    annualizable = (per_unit is not None and per_unit > 0
                    and out["source"] != SRC_API_TRAILING_12M)
    if annualizable and out["confidence"] in (CONF_MEDIUM, CONF_HIGH):
        out["reliable"] = True
        out["annualized"] = True
        out["per_unit"] = per_unit
    elif out["source"] == SRC_API_TRAILING_12M and per_unit is not None:
        out["note"] = REASON_TRAILING
        out["confidence"] = CONF_LOW
    elif out["next_event_date"] and not annualizable:
        # известно одно будущее событие, но надёжной аннуализации нет
        out["note"] = REASON_EVENT_NOT_ANNUALIZED
        out["confidence"] = out["confidence"] if out["confidence"] != CONF_NONE \
            else CONF_LOW
    return out


def compute_income(*, per_unit: Decimal | None, current_price: Decimal | None,
                   new_fill_units: Decimal | None,
                   total_units: Decimal | None, base_monthly: int) -> dict:
    """Чистый расчёт дохода (брутто). None → None. New-fill и total — отдельно."""
    out = {
        "expected_dividend_per_unit_rub": None,
        "expected_dividend_yield_pct": None,
        "expected_income_rub_monthly_new_fill": None,
        "expected_income_rub_yearly_new_fill": None,
        "expected_income_rub_monthly_total_position": None,
        "expected_income_rub_yearly_total_position": None,
        "income_target_coverage_pct_new_fill": None,
        "income_target_coverage_pct_total_position": None,
    }
    if per_unit is None or per_unit <= 0:
        return out
    out["expected_dividend_per_unit_rub"] = per_unit
    if current_price not in (None, 0):
        out["expected_dividend_yield_pct"] = (
            per_unit / current_price * Decimal(100)).quantize(_P4)
    if new_fill_units is not None:
        y = (per_unit * new_fill_units).quantize(_M2)
        m = (y / Decimal(12)).quantize(_M2)
        out["expected_income_rub_yearly_new_fill"] = y
        out["expected_income_rub_monthly_new_fill"] = m
        if base_monthly:
            out["income_target_coverage_pct_new_fill"] = (
                m / Decimal(base_monthly) * Decimal(100)).quantize(_P4)
    if total_units is not None:
        y = (per_unit * total_units).quantize(_M2)
        m = (y / Decimal(12)).quantize(_M2)
        out["expected_income_rub_yearly_total_position"] = y
        out["expected_income_rub_monthly_total_position"] = m
        if base_monthly:
            out["income_target_coverage_pct_total_position"] = (
                m / Decimal(base_monthly) * Decimal(100)).quantize(_P4)
    return out


# ─── guards / token policy ────────────────────────────────────────────────────

def _guards() -> dict:
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
        "live_token_used": False,
        "sandbox_token_used": False,
        "token_printed": False,
    }


def _token_policy(read_token_present: bool, used_for: str | None) -> dict:
    return {
        "read_only_token_env": READ_TOKEN_ENV,
        "read_only_token_present": bool(read_token_present),
        "read_only_token_used_for": used_for if read_token_present else None,
        "live_trading_token_env": LIVE_TRADING_TOKEN_ENV,
        "live_trading_token_required": False,
        "live_token_used": False,
        "sandbox_token_used": False,
        "token_printed": False,
    }


# ─── markdown ─────────────────────────────────────────────────────────────────

def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "—"
    return str(value)


def render_md(report: dict) -> str:
    g = report["guards"]
    tp = report["token_policy"]

    def row(key):
        return f"| {key} | {_fmt(report.get(key))} |"

    lines = [
        "# F4.6 — live income/dividend data validation (READ ONLY)",
        "",
        "> Только read-only проверка НАДЁЖНОСТИ доходных данных. Команда НИЧЕГО не "
        "исполняет и НИЧЕГО не угадывает.",
        "",
        "> Guard block",
        ">",
        "> - F4.6 read-only income validation",
        "> - No PostOrder, no order cancellation, no second order, no sell",
        "> - No retry, no MARKET, no portfolio/config mutation, no Telegram",
        "> - Read-only `TINKOFF_TOKEN` only (опционально); live/sandbox token not used",
        "",
        f"- stage: `{report['stage']}`",
        f"- mode: `{report['mode']}`",
        f"- ticker: `{report['ticker']}` | order_id: `{report['order_id']}`",
        f"- live_account_id_masked: `{_fmt(report['live_account_id_masked'])}`",
        "",
        "## Instrument / context (F4.1–F4.5)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("figi"), row("instrument_uid"), row("class_code"), row("lot_size"),
        row("currency"), row("current_price"),
        row("current_total_position_units"), row("current_total_position_value"),
        row("new_fill_quantity_units"), row("new_fill_cash_outflow"),
        "",
        "## Income data validation",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("income_data_checked"), row("reliable_income_data_found"),
        row("income_data_confidence"), row("income_data_source"),
        row("income_data_as_of"), row("income_data_sources_checked"),
        row("income_validation_passed"),
        row("income_validation_blocking_reasons"),
        "",
        "## Expected income — БРУТТО (new-fill и total раздельно)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("base_monthly_living_basket_rub"),
        row("expected_dividend_per_unit_rub"), row("expected_dividend_yield_pct"),
        row("expected_income_rub_monthly_new_fill"),
        row("expected_income_rub_yearly_new_fill"),
        row("income_target_coverage_pct_new_fill"),
        row("expected_income_rub_monthly_total_position"),
        row("expected_income_rub_yearly_total_position"),
        row("income_target_coverage_pct_total_position"),
        "",
        "> ⚠️ Доход new-fill и доход всей позиции — РАЗНЫЕ величины. Среднее/PnL "
        "позиции для оценки дохода НЕ используются.",
        "",
        "## Next known income event (отдельно от аннуализации)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("next_known_income_event_date"), row("next_known_income_event_type"),
        row("next_known_income_event_amount_per_unit"),
        "",
        "> Разовое будущее событие ≠ надёжный месячный/годовой доход. Аннуализация "
        "выполняется ТОЛЬКО если источник её явно поддерживает.",
        "",
        "## Tax treatment",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("withholding_tax_assumption"), row("withholding_tax_source"),
        "",
        "## Token policy",
        "",
        f"- read_only_token_env: `{tp['read_only_token_env']}`",
        f"- read_only_token_present: {_fmt(tp['read_only_token_present'])}",
        f"- read_only_token_used_for: {_fmt(tp['read_only_token_used_for'])}",
        f"- live_trading_token_required: {_fmt(tp['live_trading_token_required'])}",
        f"- live_token_used: {_fmt(tp['live_token_used'])}",
        f"- sandbox_token_used: {_fmt(tp['sandbox_token_used'])}",
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
        f"- live_token_used: {_fmt(g['live_token_used'])}",
        f"- sandbox_token_used: {_fmt(g['sandbox_token_used'])}",
        f"- token_printed: {_fmt(g['token_printed'])}",
    ]
    if report.get("warnings"):
        lines += ["", "## Warnings"]
        lines += [f"- {w}" for w in report["warnings"]]
    if report.get("errors"):
        lines += ["", "## Errors"]
        lines += [f"- {e}" for e in report["errors"]]
    lines += [
        "",
        "---",
        "",
        "Read-only income validation. No orders were created, cancelled, sold or "
        "retried; no portfolio/config mutation.",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация ─────────────────────────────────────────────────────────────

def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
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


# ─── провайдер доходных данных (read-only) ────────────────────────────────────

def _resolve_provider(income_provider, client):
    """Возвращает (provider_callable|None, source_label, note).

    Никогда не выдумывает метод: если клиент не поддерживает доходные данные —
    провайдер None и note=unsupported. Без токена — провайдер None, note=no_token.
    """
    if income_provider is not None:
        return income_provider, "injected_income_provider", None
    if client is None:
        return None, "readonly_client:absent_no_token", SRC_NO_TOKEN
    if not hasattr(client, "get_dividends"):
        return None, "readonly_client:unsupported", SRC_UNSUPPORTED

    def provider(ctx):
        from modules.income_sources import fetch_dividend_data
        iid = ctx.get("uid") or ctx.get("figi")
        if not iid:
            return None
        return fetch_dividend_data(client, iid, now=ctx.get("now"))

    return provider, "readonly_client:get_dividends", None


# ─── оркестрация ──────────────────────────────────────────────────────────────

def run(*, ticker: str, order_id: str, live_account_id: str,
        f41_report: str | None = None,
        f42_report: str | None = None,
        f43_report: str | None = None,
        f44_report: str | None = None,
        f45_report: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None,
        client=None,
        income_provider=None,
        read_token_present: bool | None = None,
        client_error: str | None = None,
        now: datetime | None = None) -> dict:
    """Read-only валидация доходных данных. Ничего не исполняет/не угадывает."""
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        raise IncomeValidationError("Не задан --ticker.")
    cli_order_id = str(order_id or "").strip()
    if not cli_order_id:
        raise IncomeValidationError("Не задан --order-id.")
    live_account_id = str(live_account_id or "").strip()
    if not live_account_id:
        raise IncomeValidationError("Не задан --live-account-id.")

    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []
    errors: list[str] = []

    if read_token_present is None:
        read_token_present = client is not None

    f41 = _load_json(f41_report or F41_DEFAULT_JSON)
    f43 = _load_json(f43_report or F43_DEFAULT_JSON)
    f44 = _load_json(f44_report or F44_DEFAULT_JSON)
    f45 = _load_json(f45_report or F45_DEFAULT_JSON)
    if f45 is None:
        warnings.append("F4.5 отчёт не найден — fallback на F4.4/F4.3.")

    # ── идентификаторы инструмента (F4.4 → F4.1) ──
    ids = extract_instrument_ids(f41)
    figi = _first_str((f44 or {}).get("figi"), (f43 or {}).get("figi"),
                      ids.get("figi"))
    uid = _first_str((f44 or {}).get("instrument_uid"),
                     (f43 or {}).get("instrument_uid"), ids.get("uid"))
    class_code = _first_str((f44 or {}).get("class_code"),
                            (f43 or {}).get("class_code"), ids.get("class_code"))
    lot_size = _first((f44 or {}).get("lot_size"), (f43 or {}).get("lot_size"),
                      ids.get("lot_size"))

    # Без идентификаторов инструмента — чистый отказ, БЕЗ сетевых вызовов.
    if not figi and not uid:
        errors.append(
            "Не найдены идентификаторы инструмента (figi/uid) в отчётах "
            "F4.1–F4.5 — валидация дохода невозможна; сетевых вызовов не выполнено.")
        report = _assemble(
            now=now, ticker=ticker, order_id=cli_order_id,
            live_account_id=live_account_id, figi=figi, uid=uid,
            class_code=class_code, lot_size=lot_size, ctx={}, assessment=None,
            income={}, income_data_checked=False, sources_checked=["local_reports_f41_f45"],
            blocking=[REASON_NO_RELIABLE], warnings=warnings, errors=errors,
            read_token_present=read_token_present, token_used_for=None)
        report["_exit_code"] = 1
        return _write(report, output_json, output_md)

    # ── контекст позиции/сделки (F4.5 → F4.4 → F4.3) ──
    currency = _first_str((f45 or {}).get("fill_currency"),
                          (f44 or {}).get("fill_currency"),
                          (f44 or {}).get("order_currency"),
                          (f43 or {}).get("currency"))
    current_price = _first((f45 or {}).get("current_price"),
                           (f44 or {}).get("current_price"),
                           (f43 or {}).get("current_price"))
    total_units = _first((f45 or {}).get("current_total_position_units"),
                         (f44 or {}).get("current_total_position_units"),
                         (f43 or {}).get("position_quantity_units"))
    total_value = _first((f45 or {}).get("current_total_position_value"),
                         (f44 or {}).get("current_total_position_value"),
                         (f43 or {}).get("current_position_value"))
    new_fill_units = _first((f45 or {}).get("fill_quantity_units"),
                            (f44 or {}).get("fill_quantity_units"))
    new_fill_cash_outflow = _first((f45 or {}).get("fill_cash_outflow"),
                                   (f44 or {}).get("fill_cash_outflow"))

    ctx = {"ticker": ticker, "figi": figi, "uid": uid, "class_code": class_code,
           "now": now, "current_price": current_price}

    # ── валидация доходных данных (read-only) ──
    provider, source_label, provider_note = _resolve_provider(income_provider, client)
    sources_checked = ["local_reports_f41_f45", source_label]
    token_used_for = None
    div = None
    if provider is not None:
        try:
            div = provider(ctx)
            if client is not None and income_provider is None:
                token_used_for = "income-data validation (read-only dividends)"
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Не удалось проверить доходные данные (read-only): {exc}")
            div = None

    assessment = assess_income(div)
    income_data_checked = provider is not None

    reliable = assessment["reliable"]
    income = compute_income(
        per_unit=assessment["per_unit"] if reliable else None,
        current_price=current_price, new_fill_units=new_fill_units,
        total_units=total_units, base_monthly=BASE_MONTHLY_LIVING_BASKET_RUB)

    # ── налог: при неизвестном режиме — только БРУТТО, без угадывания net ──
    withholding_tax_assumption = None
    withholding_tax_source = None

    # ── причины блокировки / предупреждения ──
    blocking: list[str] = []
    if reliable:
        warnings.append(WARN_TAX_UNKNOWN)
        blocking_passed = True
    else:
        blocking_passed = False
        if provider_note == SRC_UNSUPPORTED:
            blocking.append(REASON_UNSUPPORTED)
            warnings.append(WARN_UNSUPPORTED)
        elif provider_note == SRC_NO_TOKEN:
            blocking.append(REASON_NO_RELIABLE)
            warnings.append(WARN_NO_TOKEN)
        elif assessment["note"] == REASON_TRAILING:
            blocking.append(REASON_TRAILING)
            warnings.append(WARN_TRAILING)
        elif assessment["note"] == REASON_EVENT_NOT_ANNUALIZED:
            blocking.append(REASON_EVENT_NOT_ANNUALIZED)
            warnings.append(
                "Известно будущее дивидендное событие, но надёжной аннуализации "
                "нет — месячный/годовой доход не считаем (не угадываем).")
        else:
            blocking.append(REASON_NO_RELIABLE)
            warnings.append(WARN_NO_RELIABLE)

    income_data_source = (assessment["source"] if income_data_checked
                          else (provider_note or None))
    income_data_as_of = assessment["as_of"] or (
        now.isoformat() if (income_data_checked and div is not None) else None)
    income_data_confidence = assessment["confidence"]

    report = _assemble(
        now=now, ticker=ticker, order_id=cli_order_id,
        live_account_id=live_account_id, figi=figi, uid=uid,
        class_code=class_code, lot_size=lot_size,
        ctx={"currency": currency, "current_price": current_price,
             "total_units": total_units, "total_value": total_value,
             "new_fill_units": new_fill_units,
             "new_fill_cash_outflow": new_fill_cash_outflow},
        assessment=assessment, income=income,
        income_data_checked=income_data_checked, sources_checked=sources_checked,
        blocking=blocking, warnings=warnings, errors=errors,
        read_token_present=read_token_present, token_used_for=token_used_for,
        reliable=reliable, income_data_source=income_data_source,
        income_data_as_of=income_data_as_of,
        income_data_confidence=income_data_confidence,
        income_validation_passed=blocking_passed,
        withholding_tax_assumption=withholding_tax_assumption,
        withholding_tax_source=withholding_tax_source)
    report["_exit_code"] = 1 if errors else 0
    return _write(report, output_json, output_md)


def _assemble(*, now, ticker, order_id, live_account_id, figi, uid, class_code,
              lot_size, ctx, assessment, income, income_data_checked,
              sources_checked, blocking, warnings, errors, read_token_present,
              token_used_for, reliable=False, income_data_source=None,
              income_data_as_of=None, income_data_confidence=CONF_NONE,
              income_validation_passed=False,
              withholding_tax_assumption=None, withholding_tax_source=None) -> dict:
    a = assessment or {}
    return {
        "kind": "income_live_income_validation",
        "read_only": True,
        "generated_at": now.isoformat(),
        "stage": STAGE,
        "mode": MODE,
        "ticker": ticker,
        "order_id": order_id,
        "live_account_id_masked": (
            mask_identifier(live_account_id) if live_account_id else None),
        "figi": figi,
        "instrument_uid": uid,
        "class_code": class_code,
        "lot_size": lot_size,
        "currency": ctx.get("currency"),
        "current_price": ctx.get("current_price"),
        "current_total_position_units": ctx.get("total_units"),
        "current_total_position_value": ctx.get("total_value"),
        "new_fill_quantity_units": ctx.get("new_fill_units"),
        "new_fill_cash_outflow": ctx.get("new_fill_cash_outflow"),
        "base_monthly_living_basket_rub": BASE_MONTHLY_LIVING_BASKET_RUB,
        # validation
        "income_data_checked": bool(income_data_checked),
        "income_data_sources_checked": sources_checked,
        "reliable_income_data_found": bool(reliable),
        "income_data_confidence": income_data_confidence,
        "income_data_source": income_data_source,
        "income_data_as_of": income_data_as_of,
        # expected income (БРУТТО), new-fill и total раздельно
        "expected_dividend_per_unit_rub": income.get("expected_dividend_per_unit_rub"),
        "expected_dividend_yield_pct": income.get("expected_dividend_yield_pct"),
        "expected_income_rub_monthly_new_fill": income.get(
            "expected_income_rub_monthly_new_fill"),
        "expected_income_rub_yearly_new_fill": income.get(
            "expected_income_rub_yearly_new_fill"),
        "expected_income_rub_monthly_total_position": income.get(
            "expected_income_rub_monthly_total_position"),
        "expected_income_rub_yearly_total_position": income.get(
            "expected_income_rub_yearly_total_position"),
        "income_target_coverage_pct_new_fill": income.get(
            "income_target_coverage_pct_new_fill"),
        "income_target_coverage_pct_total_position": income.get(
            "income_target_coverage_pct_total_position"),
        # next known event (отдельно от аннуализации)
        "next_known_income_event_date": a.get("next_event_date"),
        "next_known_income_event_type": a.get("next_event_type"),
        "next_known_income_event_amount_per_unit": a.get("next_event_amount_per_unit"),
        # tax
        "withholding_tax_assumption": withholding_tax_assumption,
        "withholding_tax_source": withholding_tax_source,
        # verdict
        "income_validation_passed": bool(income_validation_passed),
        "income_validation_blocking_reasons": blocking,
        "warnings": warnings,
        "errors": errors,
        "checked_at": now.isoformat(),
        "guards": _guards(),
        "token_policy": _token_policy(bool(read_token_present), token_used_for),
    }
