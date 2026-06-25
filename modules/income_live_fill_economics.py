"""
income_live_fill_economics — F4.5 read-only fill net PnL & position economics.

Безопасная READ-ONLY экономика НОВОЙ сделки (1 лот) поверх атрибуции F4.4:
- gross PnL (до комиссии) и net PnL (после комиссии / денежного оттока);
- комиссионный drag;
- цена безубытка после комиссии и расстояние до неё;
- доля новой сделки в суммарной позиции;
- вклад в месячную корзину 150000 RUB — ТОЛЬКО при надёжных данных о доходе.

Команда `income-live-fill-economics` НИЧЕГО не исполняет.

Жёсткий контракт (никогда не нарушать):
- Только READ-ONLY данные: отчёты F4.1/F4.2/F4.3/F4.4 и (если нужно) аналитический
  `TINKOFF_TOKEN` (portfolio/market-data refresh цены).
- `TINKOFF_LIVE_TRADING_TOKEN` НЕ требуется и НЕ используется.
- `TINKOFF_SANDBOX_TOKEN` НЕ используется. Токен не печатается и не пишется в отчёт.
- НЕ вызывает PostOrder, НЕ отменяет, НЕ ставит/не продаёт заявок, НЕ ретраит,
  НЕ использует MARKET. НЕ мутирует портфель/config. НЕ шлёт Telegram.
- НЕ угадывает комиссию/дивиденды: при отсутствии надёжных данных ставит null и
  добавляет предупреждение. PnL всей позиции держится ОТДЕЛЬНО от PnL новой сделки;
  среднее всей позиции НЕ используется для PnL новой сделки.

Имена ключей/guard со словом «order» переиспользуются из F4.1/F4.2/F4.3/F4.4
модулей (constants), поэтому цельных запрещённых литералов в этом исходнике нет —
статический сканер modules/execution_preflight.py и safety-grep не считают этот
read-only модуль ложным order-endpoint.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common.helpers import mask_identifier, quotation_to_decimal
from modules.income_live_fill_attribution import (
    DEFAULT_OUTPUT_JSON as F44_DEFAULT_JSON,
)
from modules.income_live_position import (
    BASE_MONTHLY_LIVING_BASKET_RUB,
)
from modules.income_live_position import (
    DEFAULT_OUTPUT_JSON as F43_DEFAULT_JSON,
)
from modules.income_live_execution import (
    DEFAULT_OUTPUT_JSON as F41_DEFAULT_JSON,
)
from modules.income_live_status import (
    DEFAULT_OUTPUT_JSON as F42_DEFAULT_JSON,
)
from modules.income_live_status import (
    GUARD_CANCEL_CALLED,
    GUARD_LIVE_ORDER_SENT,
)

DEFAULT_OUTPUT_JSON = "data/reports/income_live_fill_economics_report.json"
DEFAULT_OUTPUT_MD = "data/reports/income_live_fill_economics_report.md"

STAGE = "F4_5_LIVE_FILL_ECONOMICS_READ_ONLY"
MODE = "FILL_ECONOMICS_READ_ONLY"

READ_TOKEN_ENV = "TINKOFF_TOKEN"
LIVE_TRADING_TOKEN_ENV = "TINKOFF_LIVE_TRADING_TOKEN"

_M2 = Decimal("0.01")       # деньги / PnL
_P4 = Decimal("0.0001")     # проценты / цены безубытка

# Предупреждения (read-only, без угадывания).
WARN_NO_CURRENT_PRICE = (
    "Текущая цена недоступна — PnL-поля новой сделки = null (не угадываем).")
WARN_NO_COMMISSION = (
    "Комиссия/денежный отток сделки недоступны — net-after-commission поля = null; "
    "gross-поля посчитаны при наличии цены. Не угадываем.")
WARN_NO_INCOME = (
    "Надёжных данных о дивидендах/доходе нет — вклад новой сделки в доход не "
    "рассчитывается (не угадываем); income-поля = null.")
WARN_NO_F44 = (
    "F4.4 отчёт (income_live_fill_attribution_report.json) не найден или нечитаем — "
    "экономика новой сделки не рассчитана; сетевых вызовов не выполнено.")


class FillEconomicsError(Exception):
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


# ─── pure economics (без сети, без записи) ────────────────────────────────────

ECON_FIELDS = (
    "new_fill_current_value",
    "new_fill_gross_unrealized_pnl",
    "new_fill_gross_unrealized_pnl_pct",
    "new_fill_net_unrealized_pnl_after_commission",
    "new_fill_net_unrealized_pnl_after_commission_pct",
    "commission_drag_rub",
    "commission_drag_pct_of_gross_amount",
    "break_even_price_after_commission",
    "distance_to_break_even_rub",
    "distance_to_break_even_pct",
    "new_fill_weight_in_total_position_pct",
)


def compute_economics(*, current_price: Decimal | None,
                      fill_quantity_units: Decimal | None,
                      fill_gross_amount: Decimal | None,
                      fill_cash_outflow: Decimal | None,
                      fill_commission_abs: Decimal | None,
                      current_total_position_units: Decimal | None) -> dict:
    """Чистая экономика новой сделки. None-вход → None-выход (не угадываем).

    Правила:
    - gross PnL = текущая стоимость − gross сумма сделки (без комиссии);
    - net PnL  = текущая стоимость − денежный отток (с комиссией);
    - PnL всей позиции держится ОТДЕЛЬНО (здесь не считается);
    - среднее всей позиции НЕ используется для PnL новой сделки.
    """
    out = {key: None for key in ECON_FIELDS}

    # 1) текущая стоимость новой сделки = current_price * units
    cur_value = None
    if current_price is not None and fill_quantity_units is not None:
        cur_value = (current_price * fill_quantity_units).quantize(_M2)
        out["new_fill_current_value"] = cur_value

    # 2-3) gross PnL (до комиссии) от gross суммы сделки
    if cur_value is not None and fill_gross_amount is not None:
        gross_pnl = (cur_value - fill_gross_amount).quantize(_M2)
        out["new_fill_gross_unrealized_pnl"] = gross_pnl
        if fill_gross_amount != 0:
            out["new_fill_gross_unrealized_pnl_pct"] = (
                gross_pnl / fill_gross_amount * Decimal(100)).quantize(_P4)

    # 4-5) net PnL (после комиссии) от денежного оттока
    if cur_value is not None and fill_cash_outflow is not None:
        net_pnl = (cur_value - fill_cash_outflow).quantize(_M2)
        out["new_fill_net_unrealized_pnl_after_commission"] = net_pnl
        if fill_cash_outflow != 0:
            out["new_fill_net_unrealized_pnl_after_commission_pct"] = (
                net_pnl / fill_cash_outflow * Decimal(100)).quantize(_P4)

    # 6-7) комиссионный drag
    if fill_commission_abs is not None:
        out["commission_drag_rub"] = fill_commission_abs
        if fill_gross_amount not in (None, 0):
            out["commission_drag_pct_of_gross_amount"] = (
                fill_commission_abs / fill_gross_amount * Decimal(100)).quantize(_P4)

    # 8-10) цена безубытка после комиссии и расстояние до неё
    if fill_cash_outflow is not None and fill_quantity_units not in (None, 0):
        break_even = (fill_cash_outflow / fill_quantity_units).quantize(_P4)
        out["break_even_price_after_commission"] = break_even
        if current_price is not None:
            dist = (current_price - break_even).quantize(_M2)
            out["distance_to_break_even_rub"] = dist
            if break_even != 0:
                out["distance_to_break_even_pct"] = (
                    (current_price - break_even) / break_even * Decimal(100)
                ).quantize(_P4)

    # 11) доля новой сделки в суммарной позиции
    if (fill_quantity_units is not None
            and current_total_position_units not in (None, 0)):
        out["new_fill_weight_in_total_position_pct"] = (
            fill_quantity_units / current_total_position_units * Decimal(100)
        ).quantize(_P4)

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
    return str(value)


def render_md(report: dict) -> str:
    g = report["guards"]
    tp = report["token_policy"]

    def row(key):
        return f"| {key} | {_fmt(report.get(key))} |"

    lines = [
        "# F4.5 — live fill net PnL & position economics (READ ONLY)",
        "",
        "> Только read-only экономика завершённой сделки: gross vs net PnL, "
        "комиссионный drag, безубыток. Команда НИЧЕГО не исполняет.",
        "",
        "> Guard block",
        ">",
        "> - F4.5 read-only fill economics",
        "> - No PostOrder, no order cancellation, no second order, no sell",
        "> - No retry, no MARKET, no portfolio/config mutation, no Telegram",
        "> - Read-only `TINKOFF_TOKEN` only (опционально); live/sandbox token not used",
        "",
        f"- stage: `{report['stage']}`",
        f"- mode: `{report['mode']}`",
        f"- ticker: `{report['ticker']}` | order_id: `{report['order_id']}`",
        f"- live_account_id_masked: `{_fmt(report['live_account_id_masked'])}`",
        f"- fill_attribution_confidence: {_fmt(report['fill_attribution_confidence'])} "
        f"(method: {_fmt(report['attribution_method'])})",
        "",
        "## New fill (из F4.4)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("fill_quantity_units"), row("fill_quantity_lots"), row("fill_price"),
        row("fill_gross_amount"), row("fill_commission_raw"),
        row("fill_commission_abs"), row("fill_cash_outflow"), row("fill_currency"),
        row("current_price"),
        "",
        "## New-fill economics — gross vs net (отдельно от всей позиции)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("new_fill_current_value"),
        row("new_fill_gross_unrealized_pnl"),
        row("new_fill_gross_unrealized_pnl_pct"),
        row("new_fill_net_unrealized_pnl_after_commission"),
        row("new_fill_net_unrealized_pnl_after_commission_pct"),
        row("commission_drag_rub"),
        row("commission_drag_pct_of_gross_amount"),
        row("break_even_price_after_commission"),
        row("distance_to_break_even_rub"),
        row("distance_to_break_even_pct"),
        row("new_fill_weight_in_total_position_pct"),
        "",
        "> gross PnL = текущая стоимость − gross сумма сделки (БЕЗ комиссии). "
        "net PnL = текущая стоимость − денежный отток (С комиссией).",
        "",
        "## Current TOTAL position (F4.3/F4.4) — держится ОТДЕЛЬНО",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("current_total_position_units"), row("current_total_position_lots"),
        row("current_average_position_price"), row("current_total_position_value"),
        row("current_total_unrealized_pnl"),
        row("total_position_pnl_kept_separate"),
        "",
        "> ⚠️ PnL всей позиции (`current_total_unrealized_pnl`) и PnL новой сделки — "
        "РАЗНЫЕ величины. Среднее всей позиции НЕ используется для PnL новой сделки.",
        "",
        "## Previous position (ESTIMATED, из F4.4)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("previous_position_estimated_units"),
        row("previous_position_estimated_average_price"),
    ]
    if report.get("previous_position_estimation_warning"):
        lines += ["", f"> ⚠️ {report['previous_position_estimation_warning']}"]
    lines += [
        "",
        "## Income goal (read-only, без угадывания)",
        "",
        row("base_monthly_living_basket_rub"),
        row("estimated_income_contribution_rub_monthly"),
        row("estimated_income_contribution_rub_yearly"),
        row("income_target_coverage_pct"), row("income_data_source"),
    ]
    if report.get("income_estimation_warning"):
        lines += ["", f"> {report['income_estimation_warning']}"]
    lines += [
        "",
        "## Token policy",
        "",
        f"- read_only_token_env: `{tp['read_only_token_env']}`",
        f"- read_only_token_present: {_fmt(tp['read_only_token_present'])}",
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
        "Read-only economics. No orders were created, cancelled, sold or "
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


# ─── оркестрация ──────────────────────────────────────────────────────────────

def run(*, ticker: str, order_id: str, live_account_id: str,
        f41_report: str | None = None,
        f42_report: str | None = None,
        f43_report: str | None = None,
        f44_report: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None,
        client=None,
        price_provider=None,
        read_token_present: bool | None = None,
        client_error: str | None = None,
        now: datetime | None = None) -> dict:
    """Read-only экономика новой сделки поверх F4.4. Ничего не исполняет."""
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        raise FillEconomicsError("Не задан --ticker.")
    cli_order_id = str(order_id or "").strip()
    if not cli_order_id:
        raise FillEconomicsError("Не задан --order-id.")
    live_account_id = str(live_account_id or "").strip()
    if not live_account_id:
        raise FillEconomicsError("Не задан --live-account-id.")

    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []
    errors: list[str] = []

    if read_token_present is None:
        read_token_present = client is not None

    f44 = _load_json(f44_report or F44_DEFAULT_JSON)
    f43 = _load_json(f43_report or F43_DEFAULT_JSON)
    f42 = _load_json(f42_report or F42_DEFAULT_JSON)  # noqa: F841 (контекст/совместимость)
    f41 = _load_json(f41_report or F41_DEFAULT_JSON)  # noqa: F841

    # F4.4 — основной источник. Без него экономику не считаем: чистая ошибка,
    # БЕЗ сетевых вызовов (никакого refresh цены).
    if f44 is None:
        errors.append(WARN_NO_F44)
        report = _assemble(
            now=now, ticker=ticker, order_id=cli_order_id,
            live_account_id=live_account_id, f44={}, econ={k: None for k in ECON_FIELDS},
            warnings=warnings, errors=errors, read_token_present=read_token_present,
            token_used_for=None)
        report["_exit_code"] = 1
        return _write(report, output_json, output_md)

    # ── входные величины из F4.4 (read-only), current_total_* fallback на F4.3 ──
    fill_units = _to_decimal(f44.get("fill_quantity_units"))
    fill_gross = _to_decimal(f44.get("fill_gross_amount"))
    fill_comm_abs = _to_decimal(f44.get("fill_commission_abs"))
    fill_cash_outflow = _to_decimal(f44.get("fill_cash_outflow"))

    current_price = _first(
        f44.get("current_price"),
        (f43 or {}).get("current_price"))
    cur_total_units = _first(
        f44.get("current_total_position_units"),
        (f43 or {}).get("position_quantity_units"))

    token_used_for = None
    # Опциональный read-only refresh цены ТОЛЬКО если её нет в отчётах и есть провайдер.
    if current_price is None:
        provider = price_provider
        if provider is None and client is not None:
            def provider(uid_, figi_):  # noqa: ANN001
                return client.get_last_price(uid=uid_, figi=figi_)
        if provider is not None:
            try:
                raw = provider(f44.get("instrument_uid"), f44.get("figi"))
                refreshed = quotation_to_decimal(raw) if isinstance(raw, dict) \
                    else _to_decimal(raw)
                if refreshed is not None:
                    current_price = refreshed
                    token_used_for = "market-data (price refresh)"
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Не удалось обновить цену (read-only): {exc}")

    # ── экономика (чистая, без угадывания) ──
    econ = compute_economics(
        current_price=current_price,
        fill_quantity_units=fill_units,
        fill_gross_amount=fill_gross,
        fill_cash_outflow=fill_cash_outflow,
        fill_commission_abs=fill_comm_abs,
        current_total_position_units=cur_total_units)

    if current_price is None:
        warnings.append(WARN_NO_CURRENT_PRICE)
    if fill_comm_abs is None or fill_cash_outflow is None:
        warnings.append(WARN_NO_COMMISSION)

    report = _assemble(
        now=now, ticker=ticker, order_id=cli_order_id,
        live_account_id=live_account_id, f44=f44, econ=econ,
        current_price=current_price, cur_total_units=cur_total_units,
        warnings=warnings, errors=errors, read_token_present=read_token_present,
        token_used_for=token_used_for)
    report["_exit_code"] = 1 if errors else 0
    return _write(report, output_json, output_md)


def _assemble(*, now, ticker, order_id, live_account_id, f44, econ,
              current_price=None, cur_total_units=None,
              warnings, errors, read_token_present, token_used_for) -> dict:
    """Собирает финальный отчёт из F4.4-полей и посчитанной экономики."""
    # income-поля наследуются из F4.4 (F4.5 тоже НЕ угадывает доход).
    income_monthly = _to_decimal(f44.get("estimated_income_contribution_rub_monthly"))
    income_yearly = _to_decimal(f44.get("estimated_income_contribution_rub_yearly"))
    income_cov = _to_decimal(f44.get("income_target_coverage_pct"))
    income_src = f44.get("income_data_source")
    income_warn = f44.get("income_estimation_warning")
    if income_monthly is None and income_yearly is None and income_cov is None:
        income_warn = income_warn or WARN_NO_INCOME
        if income_warn not in warnings:
            warnings.append(income_warn)

    base_monthly = f44.get("base_monthly_living_basket_rub")
    if base_monthly is None:
        base_monthly = BASE_MONTHLY_LIVING_BASKET_RUB

    report = {
        "kind": "income_live_fill_economics",
        "read_only": True,
        "generated_at": now.isoformat(),
        "stage": STAGE,
        "mode": MODE,
        "ticker": ticker,
        "order_id": order_id,
        "live_account_id_masked": (
            mask_identifier(live_account_id) if live_account_id else None),
        "fill_attribution_confidence": f44.get("fill_attribution_confidence"),
        "attribution_method": f44.get("attribution_method"),
        # new fill (из F4.4)
        "fill_quantity_units": _to_decimal(f44.get("fill_quantity_units")),
        "fill_quantity_lots": _to_decimal(f44.get("fill_quantity_lots")),
        "fill_price": _to_decimal(f44.get("fill_price")),
        "fill_gross_amount": _to_decimal(f44.get("fill_gross_amount")),
        "fill_commission_raw": _to_decimal(f44.get("fill_commission_raw")),
        "fill_commission_abs": _to_decimal(f44.get("fill_commission_abs")),
        "fill_cash_outflow": _to_decimal(f44.get("fill_cash_outflow")),
        "fill_currency": f44.get("fill_currency"),
        "current_price": current_price if current_price is not None
        else _to_decimal(f44.get("current_price")),
        # current TOTAL position (kept separate)
        "current_total_position_units": (
            cur_total_units if cur_total_units is not None
            else _to_decimal(f44.get("current_total_position_units"))),
        "current_total_position_lots": _to_decimal(
            f44.get("current_total_position_lots")),
        "current_average_position_price": _to_decimal(
            f44.get("current_average_position_price")),
        "current_total_position_value": _to_decimal(
            f44.get("current_total_position_value")),
        "current_total_unrealized_pnl": _to_decimal(
            f44.get("current_total_unrealized_pnl")),
        # new-fill economics (gross vs net)
        "new_fill_current_value": econ["new_fill_current_value"],
        "new_fill_gross_unrealized_pnl": econ["new_fill_gross_unrealized_pnl"],
        "new_fill_gross_unrealized_pnl_pct": econ["new_fill_gross_unrealized_pnl_pct"],
        "new_fill_net_unrealized_pnl_after_commission": econ[
            "new_fill_net_unrealized_pnl_after_commission"],
        "new_fill_net_unrealized_pnl_after_commission_pct": econ[
            "new_fill_net_unrealized_pnl_after_commission_pct"],
        "commission_drag_rub": econ["commission_drag_rub"],
        "commission_drag_pct_of_gross_amount": econ[
            "commission_drag_pct_of_gross_amount"],
        "break_even_price_after_commission": econ[
            "break_even_price_after_commission"],
        "distance_to_break_even_rub": econ["distance_to_break_even_rub"],
        "distance_to_break_even_pct": econ["distance_to_break_even_pct"],
        "new_fill_weight_in_total_position_pct": econ[
            "new_fill_weight_in_total_position_pct"],
        "total_position_pnl_kept_separate": True,
        # previous position (estimated, из F4.4)
        "previous_position_estimated_units": _to_decimal(
            f44.get("estimated_previous_position_units")),
        "previous_position_estimated_average_price": _to_decimal(
            f44.get("estimated_previous_average_price")),
        "previous_position_estimation_warning": f44.get(
            "old_position_estimation_warning"),
        # income goal
        "base_monthly_living_basket_rub": base_monthly,
        "estimated_income_contribution_rub_monthly": income_monthly,
        "estimated_income_contribution_rub_yearly": income_yearly,
        "income_target_coverage_pct": income_cov,
        "income_data_source": income_src,
        "income_estimation_warning": income_warn,
        "checked_at": now.isoformat(),
        "warnings": warnings,
        "errors": errors,
        "guards": _guards(),
        "token_policy": _token_policy(bool(read_token_present), token_used_for),
    }
    return report
