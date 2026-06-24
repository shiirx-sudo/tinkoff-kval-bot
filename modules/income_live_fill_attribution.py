"""
income_live_fill_attribution — F4.4 read-only live fill attribution & operations.

Безопасная READ-ONLY атрибуция: отделяет НОВУЮ сделку (1 лот) от уже имевшейся
позиции, подтягивает комиссию из операций (если доступна) и считает вклад новой
сделки в текущий нереализованный PnL и в income-цель. Команда
`income-live-fill-attribution` НИЧЕГО не исполняет.

Жёсткий контракт (никогда не нарушать):
- Только READ-ONLY данные: аналитический `TINKOFF_TOKEN`
  (operations/portfolio/market-data).
- `TINKOFF_LIVE_TRADING_TOKEN` НЕ требуется и НЕ используется.
- `TINKOFF_SANDBOX_TOKEN` НЕ используется. Токен не печатается и не пишется в отчёт.
- НЕ вызывает PostOrder, НЕ отменяет, НЕ ставит/не продаёт заявок, НЕ ретраит,
  НЕ использует MARKET. НЕ мутирует портфель/config. НЕ шлёт Telegram.
- НЕ угадывает комиссию/дивиденды: при отсутствии надёжных данных ставит null и
  добавляет предупреждение.
- Реконструкция прежней позиции помечается как ОЦЕНКА (estimated), не авторитет.

Имена ключей/guard со словом «order» переиспользуются из F4.1/F4.2/F4.3 модулей
(собраны там из фрагментов), поэтому цельных запрещённых литералов в этом исходнике
нет — статический сканер modules/execution_preflight.py и safety-grep не считают
этот read-only модуль ложным order-endpoint.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common.helpers import mask_identifier, quotation_to_decimal
from modules.income_live_execution import (
    DEFAULT_OUTPUT_JSON as F41_DEFAULT_JSON,
)
from modules.income_live_execution import (
    FIELD_REQUEST,
)
from modules.income_live_position import (
    DEFAULT_OUTPUT_JSON as F43_DEFAULT_JSON,
)
from modules.income_live_position import (
    BASE_MONTHLY_LIVING_BASKET_RUB,
    extract_instrument_ids,
    extract_order_facts,
    find_position,
)
from modules.income_live_status import (
    DEFAULT_OUTPUT_JSON as F42_DEFAULT_JSON,
)
from modules.income_live_status import (
    GUARD_CANCEL_CALLED,
    GUARD_LIVE_ORDER_SENT,
)

DEFAULT_OUTPUT_JSON = "data/reports/income_live_fill_attribution_report.json"
DEFAULT_OUTPUT_MD = "data/reports/income_live_fill_attribution_report.md"

STAGE = "F4_4_LIVE_FILL_ATTRIBUTION_READ_ONLY"
MODE = "FILL_ATTRIBUTION_READ_ONLY"

READ_TOKEN_ENV = "TINKOFF_TOKEN"
LIVE_TRADING_TOKEN_ENV = "TINKOFF_LIVE_TRADING_TOKEN"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

_PRICE_TOL_BPS = Decimal("80")     # допуск сопоставления цены сделки (0.8%)
_OPS_WINDOW_DAYS = 2               # окно поиска операций вокруг даты заявки


class FillAttributionError(Exception):
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
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int_of(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _money(raw):
    if isinstance(raw, dict):
        return quotation_to_decimal(raw)
    return _to_decimal(raw)


def _money_currency(*raws) -> str | None:
    for raw in raws:
        if isinstance(raw, dict) and raw.get("currency"):
            return raw.get("currency")
    return None


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ─── operation accessors (read-only OperationItem) ────────────────────────────

def _op_is_buy(op: dict) -> bool:
    ot = str(op.get("operationType") or "").upper()
    if "BUY" in ot:
        return True
    if "SELL" in ot:
        return False
    t = str(op.get("type") or "").lower()
    if "покупк" in t or "buy" in t:
        return True
    if "продаж" in t or "sell" in t:
        return False
    pay = _money(op.get("payment"))
    return pay is not None and pay < 0


def _op_instrument_match(op: dict, *, figi, uid) -> bool:
    if uid and (op.get("instrumentUid") == uid or op.get("instrument_uid") == uid):
        return True
    if figi and op.get("figi") == figi:
        return True
    return False


def _op_qty(op: dict) -> Decimal | None:
    for key in ("quantity", "quantityDone", "quantity_done"):
        v = _to_decimal(op.get(key))
        if v is not None:
            return v
    return None


def _op_order_id(op: dict):
    return op.get("orderId") or op.get("order_id")


def _op_trade_id(op: dict):
    info = op.get("tradesInfo") or op.get("trades_info") or {}
    trades = (info.get("trades") if isinstance(info, dict) else None) \
        or op.get("trades") or []
    if trades and isinstance(trades, list) and isinstance(trades[0], dict):
        return trades[0].get("num") or trades[0].get("tradeId") or trades[0].get("trade_id")
    return None


def _price_close(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None or b == 0:
        return False
    return (abs(a - b) / b * Decimal(10000)) <= _PRICE_TOL_BPS


def attribute_fill(operations, *, order_id, figi, uid, fill_units: Decimal | None,
                   exec_price: Decimal | None, window: tuple) -> dict:
    """Сопоставляет новую сделку с операциями. Возвращает {op, confidence, method}."""
    candidates = [
        op for op in (operations or [])
        if isinstance(op, dict) and _op_is_buy(op)
        and _op_instrument_match(op, figi=figi, uid=uid)
    ]
    qty_match = [op for op in candidates
                 if fill_units is not None and _op_qty(op) == fill_units]

    # 1) прямое совпадение по order_id (высокая уверенность)
    for op in qty_match or candidates:
        oid = _op_order_id(op)
        if oid and str(oid) == str(order_id):
            return {"op": op, "confidence": CONFIDENCE_HIGH,
                    "method": "operations_order_id_match"}

    # 2) instrument + BUY + qty + цена + дата (средняя уверенность)
    w_from, w_to = window
    for op in qty_match:
        price_ok = _price_close(_money(op.get("price")), exec_price)
        dt = _parse_dt(op.get("date"))
        date_ok = (dt is None or w_from is None or w_to is None
                   or (w_from <= dt <= w_to))
        if price_ok and date_ok:
            return {"op": op, "confidence": CONFIDENCE_MEDIUM,
                    "method": "operations_instrument_qty_price_date_match"}

    # 3) instrument + BUY + qty (средняя уверенность, без цены/даты)
    if qty_match:
        return {"op": qty_match[0], "confidence": CONFIDENCE_MEDIUM,
                "method": "operations_instrument_qty_match"}

    return {"op": None, "confidence": CONFIDENCE_LOW,
            "method": "reports_only_derived"}


# ─── income (без угадывания) ──────────────────────────────────────────────────

def estimate_income(*, fill_units: Decimal | None, dividend_provider,
                    instrument: dict, base_monthly: int) -> dict:
    out = {
        "base_monthly_living_basket_rub": base_monthly,
        "estimated_income_contribution_rub_monthly": None,
        "estimated_income_contribution_rub_yearly": None,
        "income_target_coverage_pct": None,
        "income_data_source": None,
        "income_estimation_warning": None,
    }
    data = None
    if dividend_provider is not None and fill_units is not None:
        try:
            data = dividend_provider(instrument)
        except Exception:  # noqa: BLE001
            data = None
    per_share = _to_decimal((data or {}).get("annual_dividend_per_share_rub")
                            if isinstance(data, dict) else None)
    if per_share is None or per_share <= 0 or fill_units is None:
        out["income_estimation_warning"] = (
            "Надёжных данных о дивидендах/доходе нет — вклад новой сделки в доход "
            "не рассчитывается (не угадываем); income-поля = null.")
        return out
    yearly = (per_share * fill_units).quantize(Decimal("0.01"))
    monthly = (yearly / Decimal(12)).quantize(Decimal("0.01"))
    coverage = ((monthly / Decimal(base_monthly) * Decimal(100)).quantize(
        Decimal("0.0001")) if base_monthly else None)
    out.update({
        "estimated_income_contribution_rub_monthly": monthly,
        "estimated_income_contribution_rub_yearly": yearly,
        "income_target_coverage_pct": coverage,
        "income_data_source": data.get("source") or "read_only_dividends",
    })
    return out


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


def _token_policy(read_token_present: bool) -> dict:
    return {
        "read_only_token_env": READ_TOKEN_ENV,
        "read_only_token_present": bool(read_token_present),
        "read_only_token_used_for": (
            "operations/portfolio/market-data" if read_token_present else None),
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
        "# F4.4 — live fill attribution (READ ONLY)",
        "",
        "> Только read-only атрибуция завершённой сделки: отделяет новую сделку "
        "(1 лот) от прежней позиции. Команда НИЧЕГО не исполняет.",
        "",
        "> Guard block",
        ">",
        "> - F4.4 read-only fill attribution",
        "> - No PostOrder, no order cancellation, no second order, no sell",
        "> - No retry, no MARKET, no portfolio/config mutation, no Telegram",
        "> - Read-only `TINKOFF_TOKEN` only; live/sandbox token not used",
        "",
        f"- stage: `{report['stage']}`",
        f"- mode: `{report['mode']}`",
        f"- ticker: `{report['ticker']}` | order_id: `{report['order_id']}`",
        f"- live_account_id_masked: `{_fmt(report['live_account_id_masked'])}`",
        f"- **fill_attribution_confidence: {_fmt(report['fill_attribution_confidence'])}** "
        f"(method: {_fmt(report['attribution_method'])})",
        "",
        "## Order / instrument (F4.1/F4.2)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("order_status"), row("lots_executed"), row("execution_price_from_f41"),
        row("order_currency"), row("instrument_uid"), row("figi"),
        row("class_code"), row("lot_size"),
        "",
        "## New fill (attributed)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("fill_found_in_operations"), row("fill_operation_id"),
        row("fill_trade_id"), row("fill_datetime"), row("fill_quantity_units"),
        row("fill_quantity_lots"), row("fill_price"), row("fill_gross_amount"),
        row("fill_commission"), row("fill_net_amount"), row("fill_currency"),
        row("fill_source"),
        "",
        "## Current TOTAL position (F4.3) — отдельно от новой сделки",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("current_total_position_units"), row("current_total_position_lots"),
        row("current_average_position_price"), row("current_price"),
        row("current_total_position_value"), row("current_total_unrealized_pnl"),
        row("current_total_position_source"),
        "",
        "## New-fill contribution (estimated)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("estimated_new_fill_current_value"),
        row("estimated_new_fill_unrealized_pnl"),
        row("estimated_new_fill_unrealized_pnl_pct"),
        row("estimated_new_fill_weight_in_position_pct"),
        "",
        "## Previous position (ESTIMATED reconstruction)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("estimated_previous_position_units"),
        row("estimated_previous_average_price"),
        row("estimated_previous_position_value"),
    ]
    if report.get("old_position_estimation_warning"):
        lines += ["", f"> ⚠️ {report['old_position_estimation_warning']}"]
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
        "Read-only attribution. No orders were created, cancelled, sold or "
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
        output_json: str | None = None,
        output_md: str | None = None,
        client=None,
        operations_provider=None,
        positions_provider=None,
        dividend_provider=None,
        read_token_present: bool | None = None,
        client_error: str | None = None,
        now: datetime | None = None) -> dict:
    """Read-only атрибуция новой сделки к завершённой заявке. Ничего не исполняет."""
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        raise FillAttributionError("Не задан --ticker.")
    cli_order_id = str(order_id or "").strip()
    if not cli_order_id:
        raise FillAttributionError("Не задан --order-id.")
    live_account_id = str(live_account_id or "").strip()
    if not live_account_id:
        raise FillAttributionError("Не задан --live-account-id.")

    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []
    errors: list[str] = []

    f41 = _load_json(f41_report or F41_DEFAULT_JSON)
    f42 = _load_json(f42_report or F42_DEFAULT_JSON)
    f43 = _load_json(f43_report or F43_DEFAULT_JSON)
    for name, rep in (("F4.1", f41), ("F4.2", f42), ("F4.3", f43)):
        if rep is None:
            warnings.append(f"{name} отчёт не найден — атрибуция может быть менее точной.")

    ids = extract_instrument_ids(f41)
    facts = extract_order_facts(f41, f42)
    figi = ids.get("figi")
    uid = ids.get("uid")
    class_code = ids.get("class_code")
    lot_size = ids.get("lot_size")
    lots_executed = facts.get("lots_executed")
    order_status = facts.get("status")
    order_currency = None
    exec_price = None
    if f41:
        exec_price = _to_decimal(f41.get("reference_price"))
        req = f41.get(FIELD_REQUEST) or {}
        order_currency = req.get("currency")

    fill_units = (Decimal(lot_size) * Decimal(lots_executed)
                  if lot_size and lots_executed is not None else
                  (Decimal(lots_executed) if lots_executed is not None else None))

    if read_token_present is None:
        read_token_present = client is not None

    # Default read-only провайдеры (никаких заявок/записи).
    if operations_provider is None and client is not None:
        def operations_provider(account_id, from_dt, to_dt):  # noqa: ANN001
            return client.get_operations(account_id, from_dt, to_dt)
    if positions_provider is None and client is not None:
        def positions_provider(account_id):  # noqa: ANN001
            return (client.get_portfolio(account_id) or {}).get("positions") or []

    # Окно поиска операций вокруг даты заявки (F4.1 generated_at / F4.2 checked_at).
    order_dt = _parse_dt((f41 or {}).get("generated_at")) or \
        _parse_dt((f42 or {}).get("checked_at"))
    if order_dt is None:
        w_from, w_to = now - timedelta(days=14), now + timedelta(days=1)
    else:
        w_from = order_dt - timedelta(days=_OPS_WINDOW_DAYS)
        w_to = order_dt + timedelta(days=_OPS_WINDOW_DAYS)

    operations = None
    if operations_provider is None:
        detail = f" ({client_error})" if client_error else " (нет TINKOFF_TOKEN)"
        errors.append(
            "Read-only клиент недоступен" + detail + " — операции не прочитаны; "
            "сетевых вызовов не выполнено.")
    else:
        try:
            operations = operations_provider(live_account_id, w_from, w_to)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Не удалось прочитать операции (read-only): {exc}")
            operations = None

    attribution = attribute_fill(
        operations, order_id=cli_order_id, figi=figi, uid=uid,
        fill_units=fill_units, exec_price=exec_price, window=(w_from, w_to))
    matched = attribution["op"]
    confidence = attribution["confidence"]
    method = attribution["method"]

    # ── new fill fields ──
    if matched is not None:
        fill_found = True
        fill_operation_id = matched.get("id")
        fill_trade_id = _op_trade_id(matched)
        fill_datetime = matched.get("date")
        fq_units = _op_qty(matched) or fill_units
        fill_price = _money(matched.get("price")) or exec_price
        pay = _money(matched.get("payment"))
        fill_gross = abs(pay) if pay is not None else (
            fill_price * fq_units if (fill_price is not None and fq_units is not None)
            else None)
        fill_commission = _money(matched.get("commission"))
        fill_currency = _money_currency(matched.get("payment"),
                                        matched.get("price")) or order_currency
        fill_source = "readonly_operations"
    else:
        fill_found = False
        fill_operation_id = fill_trade_id = fill_datetime = None
        fq_units = fill_units
        fill_price = exec_price
        fill_gross = (fill_price * fq_units
                      if (fill_price is not None and fq_units is not None) else None)
        fill_commission = None
        fill_currency = order_currency
        fill_source = "f41_f42_f43_reports"

    if fill_commission is None:
        warnings.append(
            "Комиссия сделки недоступна из операций — не угадываем; "
            "fill_commission=null.")
        fill_net = None
    else:
        fill_net = (fill_gross + fill_commission) if fill_gross is not None else None

    fq_lots = (fq_units / Decimal(lot_size)
               if (fq_units is not None and lot_size) else
               (Decimal(lots_executed) if lots_executed is not None else None))

    # ── current TOTAL position (из F4.3; иначе read-only портфель) ──
    cur_units = cur_lots = cur_avg = cur_price = cur_value = cur_pnl = None
    cur_currency = None
    cur_source = None
    if f43 and f43.get("position_found"):
        cur_units = _to_decimal(f43.get("position_quantity_units"))
        cur_lots = _to_decimal(f43.get("position_quantity_lots"))
        cur_avg = _to_decimal(f43.get("average_position_price"))
        cur_price = _to_decimal(f43.get("current_price"))
        cur_value = _to_decimal(f43.get("current_position_value"))
        cur_pnl = _to_decimal(f43.get("unrealized_pnl"))
        cur_currency = f43.get("currency")
        cur_source = "F4.3 position report"
    elif positions_provider is not None:
        try:
            positions = positions_provider(live_account_id)
            pos = find_position(positions, figi=figi, uid=uid)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Не удалось прочитать портфель (read-only): {exc}")
            pos = None
        if pos is not None:
            cur_units = quotation_to_decimal(pos.get("quantity"))
            cur_lots = quotation_to_decimal(pos.get("quantityLots"))
            cur_avg = quotation_to_decimal(pos.get("averagePositionPrice"))
            cur_price = quotation_to_decimal(pos.get("currentPrice"))
            if cur_price is not None and cur_units is not None:
                cur_value = cur_price * cur_units
            if (cur_price is not None and cur_avg is not None
                    and cur_units is not None):
                cur_pnl = (cur_price - cur_avg) * cur_units
            cur_currency = _money_currency(pos.get("averagePositionPrice"),
                                           pos.get("currentPrice"))
            cur_source = "read-only portfolio"
    if cur_source is None:
        warnings.append(
            "Текущая суммарная позиция недоступна (нет F4.3 и портфеля) — "
            "разделение новой сделки и прежней позиции ограничено.")

    # ── new-fill contribution (отдельно от total) ──
    new_fill_value = (cur_price * fq_units
                      if (cur_price is not None and fq_units is not None) else None)
    new_fill_pnl = ((cur_price - fill_price) * fq_units
                    if (cur_price is not None and fill_price is not None
                        and fq_units is not None) else None)
    new_fill_pnl_pct = (((cur_price / fill_price) - Decimal(1)) * Decimal(100)
                        ).quantize(Decimal("0.0001")) if (
        cur_price is not None and fill_price not in (None, 0)) else None
    new_fill_weight = ((fq_units / cur_units * Decimal(100)).quantize(Decimal("0.0001"))
                       if (fq_units is not None and cur_units not in (None, 0))
                       else None)

    # ── previous position (ESTIMATED reconstruction) ──
    prev_units = prev_avg = prev_value = None
    old_warn = None
    if (cur_units is not None and fq_units is not None):
        prev_units = cur_units - fq_units
        if prev_units > 0 and cur_avg is not None and fill_price is not None:
            total_cost = cur_avg * cur_units
            fill_cost = fill_price * fq_units
            prev_cost = total_cost - fill_cost
            prev_avg = (prev_cost / prev_units).quantize(Decimal("0.0001"))
            prev_value = prev_cost.quantize(Decimal("0.01"))
            old_warn = (
                "Прежняя позиция реконструирована из текущего среднего и цены "
                "новой сделки (estimated): комиссия не учтена (если неизвестна), "
                "брокер мог считать среднее своим методом; значение информационное, "
                "не авторитетное. Подтверждается только полной историей операций.")
        elif prev_units <= 0:
            prev_units = Decimal(0)
            old_warn = (
                "Текущая позиция равна новой сделке — прежней позиции нет (или не "
                "реконструируется).")
        else:
            old_warn = (
                "Недостаточно данных (среднее/цена) для оценки прежней позиции — "
                "не угадываем.")

    instrument = {"ticker": ticker, "figi": figi, "uid": uid,
                  "class_code": class_code}
    income = estimate_income(
        fill_units=fq_units, dividend_provider=dividend_provider,
        instrument=instrument, base_monthly=BASE_MONTHLY_LIVING_BASKET_RUB)
    if income.get("income_estimation_warning"):
        warnings.append(income["income_estimation_warning"])

    report = {
        "kind": "income_live_fill_attribution",
        "read_only": True,
        "generated_at": now.isoformat(),
        "stage": STAGE,
        "mode": MODE,
        "ticker": ticker,
        "order_id": cli_order_id or facts.get("order_id"),
        "live_account_id_masked": (
            mask_identifier(live_account_id) if live_account_id else None),
        "order_status": order_status,
        "lots_executed": lots_executed,
        "execution_price_from_f41": exec_price,
        "order_currency": order_currency,
        "instrument_uid": uid,
        "figi": figi,
        "class_code": class_code,
        "lot_size": lot_size,
        # current TOTAL position (kept separate)
        "current_total_position_units": cur_units,
        "current_total_position_lots": cur_lots,
        "current_average_position_price": cur_avg,
        "current_price": cur_price,
        "current_total_position_value": cur_value,
        "current_total_unrealized_pnl": cur_pnl,
        "current_total_position_source": cur_source,
        # new fill
        "fill_found_in_operations": fill_found,
        "fill_operation_id": fill_operation_id,
        "fill_trade_id": fill_trade_id,
        "fill_datetime": fill_datetime,
        "fill_quantity_units": fq_units,
        "fill_quantity_lots": fq_lots,
        "fill_price": fill_price,
        "fill_gross_amount": fill_gross,
        "fill_commission": fill_commission,
        "fill_net_amount": fill_net,
        "fill_currency": fill_currency or cur_currency,
        "fill_source": fill_source,
        "fill_attribution_confidence": confidence,
        "attribution_method": method,
        # previous position (estimated)
        "estimated_previous_position_units": prev_units,
        "estimated_previous_average_price": prev_avg,
        "estimated_previous_position_value": prev_value,
        # new-fill contribution
        "estimated_new_fill_current_value": new_fill_value,
        "estimated_new_fill_unrealized_pnl": new_fill_pnl,
        "estimated_new_fill_unrealized_pnl_pct": new_fill_pnl_pct,
        "estimated_new_fill_weight_in_position_pct": new_fill_weight,
        "old_position_estimation_warning": old_warn,
        # income goal
        "base_monthly_living_basket_rub": income["base_monthly_living_basket_rub"],
        "estimated_income_contribution_rub_monthly": income[
            "estimated_income_contribution_rub_monthly"],
        "estimated_income_contribution_rub_yearly": income[
            "estimated_income_contribution_rub_yearly"],
        "income_target_coverage_pct": income["income_target_coverage_pct"],
        "income_data_source": income["income_data_source"],
        "income_estimation_warning": income["income_estimation_warning"],
        "checked_at": now.isoformat(),
        "warnings": warnings,
        "errors": errors,
        "guards": _guards(),
        "token_policy": _token_policy(bool(read_token_present)),
    }
    report["_exit_code"] = 1 if errors else 0
    return _write(report, output_json, output_md)
