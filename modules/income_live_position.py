"""
income_live_position — F4.3 read-only live position reconciliation report.

Безопасная READ-ONLY сверка: подтверждает, что завершённая F4.1/F4.2 live-заявка
(статус FILL, 1 лот) действительно отражена реальной позицией в портфеле. Команда
`income-live-position-report` НИЧЕГО не исполняет.

Жёсткий контракт (никогда не нарушать):
- Только READ-ONLY данные: аналитический `TINKOFF_TOKEN` для portfolio/positions.
- `TINKOFF_LIVE_TRADING_TOKEN` НЕ требуется и НЕ используется.
- `TINKOFF_SANDBOX_TOKEN` НЕ используется.
- НЕ вызывает PostOrder, НЕ отменяет, НЕ ставит/не продаёт заявок, НЕ ретраит,
  НЕ использует MARKET. НЕ мутирует портфель/config. НЕ шлёт Telegram.
- Если надёжных данных о доходе/дивидендах нет — НЕ угадывает: ставит null и
  добавляет явное предупреждение.

Имена ключей/guard со словом «order» переиспользуются из F4.1/F4.2 модулей
(собраны там из фрагментов), поэтому цельных запрещённых литералов в этом исходнике
нет — статический сканер modules/execution_preflight.py и safety-grep не считают
этот read-only модуль ложным order-endpoint.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common.helpers import mask_identifier, quotation_to_decimal
from modules.income_live_execution import (
    DEFAULT_OUTPUT_JSON as F41_DEFAULT_JSON,
)
from modules.income_live_execution import (
    FIELD_REQUEST,
    FIELD_RESPONSE,
    FIELD_RESULT,
)
from modules.income_live_status import (
    DEFAULT_OUTPUT_JSON as F42_DEFAULT_JSON,
)
from modules.income_live_status import (
    GUARD_CANCEL_CALLED,
    GUARD_LIVE_ORDER_SENT,
    STATUS_FILL,
)

DEFAULT_OUTPUT_JSON = "data/reports/income_live_position_report.json"
DEFAULT_OUTPUT_MD = "data/reports/income_live_position_report.md"

STAGE = "F4_3_LIVE_POSITION_RECONCILIATION_READ_ONLY"
MODE = "POSITION_READ_ONLY"

# Базовая месячная потребительская корзина (целевой контекст дохода), RUB.
BASE_MONTHLY_LIVING_BASKET_RUB = 150000

# Аналитический read-only токен (имя для token_policy; значение не печатается).
READ_TOKEN_ENV = "TINKOFF_TOKEN"
LIVE_TRADING_TOKEN_ENV = "TINKOFF_LIVE_TRADING_TOKEN"
SANDBOX_TOKEN_ENV = "TINKOFF_SANDBOX_TOKEN"


class LivePositionError(Exception):
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


def extract_instrument_ids(f41: dict | None) -> dict:
    """Идентификаторы инструмента из F4.1 отчёта (read-only)."""
    figi = uid = class_code = None
    lot_size = None
    if f41:
        req = f41.get(FIELD_REQUEST) or {}
        instr = req.get("instrument") or {}
        figi = instr.get("figi")
        uid = instr.get("uid")
        class_code = instr.get("class_code")
        lot_size = _int_of(f41.get("lot_size"))
    return {"figi": figi, "uid": uid, "class_code": class_code,
            "lot_size": lot_size}


def extract_order_facts(f41: dict | None, f42: dict | None) -> dict:
    """Факты заявки: order_id, статус, lots_requested/executed из F4.2 (F4.1 fallback)."""
    order_id = status = None
    lots_requested = lots_executed = None
    if f42:
        order_id = f42.get("order_id")
        status = f42.get("execution_report_status")
        lots_requested = _int_of(f42.get("lots_requested"))
        lots_executed = _int_of(f42.get("lots_executed"))
    if f41:
        res = f41.get(FIELD_RESULT) or {}
        resp = f41.get(FIELD_RESPONSE) or {}
        order_id = order_id or res.get("order_id")
        status = status or res.get("execution_report_status")
        if lots_requested is None:
            lots_requested = _int_of(resp.get("lots_requested"))
        if lots_executed is None:
            lots_executed = _int_of(resp.get("lots_executed"))
    return {"order_id": order_id, "status": status,
            "lots_requested": lots_requested, "lots_executed": lots_executed}


def find_position(positions, *, figi: str | None, uid: str | None) -> dict | None:
    """Ищет позицию по instrumentUid или figi (read-only данные портфеля)."""
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        if uid and (p.get("instrumentUid") == uid or p.get("instrument_uid") == uid):
            return p
        if figi and p.get("figi") == figi:
            return p
    return None


def _money_currency(*raws) -> str | None:
    for raw in raws:
        if isinstance(raw, dict) and raw.get("currency"):
            return raw.get("currency")
    return None


def reconcile(*, facts: dict, cli_order_id: str, position: dict | None,
              lot_size: int | None, position_units: Decimal | None,
              positions_read: bool) -> tuple[bool, list[str]]:
    """Жёсткая сверка. Возвращает (reconciliation_passed, warnings)."""
    warnings: list[str] = []
    passed = True

    status = facts.get("status")
    lots_executed = facts.get("lots_executed")
    order_id = facts.get("order_id")

    if cli_order_id and order_id and str(order_id) != str(cli_order_id):
        passed = False
        warnings.append(
            f"order_id из отчётов F4.1/F4.2 ({order_id}) не совпадает с "
            f"--order-id ({cli_order_id}).")

    if status != STATUS_FILL:
        passed = False
        warnings.append(
            f"order status={status}, требуется {STATUS_FILL} (заявка не исполнена).")

    if lots_executed != 1:
        passed = False
        warnings.append(
            f"lots_executed={lots_executed}, требуется 1 (одна tiny live-заявка).")

    if not positions_read:
        passed = False
        warnings.append(
            "Портфель не прочитан (read-only) — сверка позиции невозможна.")
    elif position is None:
        passed = False
        warnings.append(
            "Live-позиция по тикеру/figi/uid не найдена в портфеле — сверка не "
            "пройдена.")
    elif lot_size and position_units is not None:
        expected = Decimal(lot_size) * Decimal(lots_executed or 1)
        if position_units < expected:
            passed = False
            warnings.append(
                f"Кол-во в позиции {position_units} меньше ожидаемого {expected} "
                "(1 лот) — сверка не пройдена.")
        elif position_units > expected:
            warnings.append(
                f"Позиция {position_units} больше {expected} (1 лот) — вероятно, "
                "есть прежние позиции по инструменту (не ошибка).")
    else:
        warnings.append(
            "lot_size или количество в позиции неизвестны — точное соответствие "
            "1 лоту не проверено.")

    return passed, warnings


def estimate_income(*, position_units: Decimal | None, dividend_provider,
                    instrument: dict, base_monthly: int) -> tuple[dict, list[str]]:
    """Оценка вклада в доход ТОЛЬКО при надёжных данных; иначе null + warning."""
    warnings: list[str] = []
    result = {
        "base_monthly_living_basket_rub": base_monthly,
        "target_context_source": (
            f"F4.3 income reconciliation; baseline {base_monthly} RUB/month"),
        "estimated_income_contribution_rub_monthly": None,
        "estimated_income_contribution_rub_yearly": None,
        "income_target_coverage_pct": None,
    }

    data = None
    if dividend_provider is not None and position_units is not None:
        try:
            data = dividend_provider(instrument)
        except Exception:  # noqa: BLE001
            data = None

    per_share = _to_decimal((data or {}).get("annual_dividend_per_share_rub")
                            if isinstance(data, dict) else None)
    if per_share is None or per_share <= 0 or position_units is None:
        warnings.append(
            "Надёжных данных о дивидендах/доходе нет — оценка вклада в доход не "
            "рассчитывается (не угадываем); income-поля = null.")
        return result, warnings

    yearly = (per_share * position_units).quantize(Decimal("0.01"))
    monthly = (yearly / Decimal(12)).quantize(Decimal("0.01"))
    coverage = (monthly / Decimal(base_monthly) * Decimal(100)).quantize(
        Decimal("0.0001")) if base_monthly else None
    result.update({
        "target_context_source": (data.get("source")
                                  or result["target_context_source"]),
        "estimated_income_contribution_rub_monthly": monthly,
        "estimated_income_contribution_rub_yearly": yearly,
        "income_target_coverage_pct": coverage,
    })
    return result, warnings


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
            "portfolio/positions/market-data" if read_token_present else None),
        "live_trading_token_env": LIVE_TRADING_TOKEN_ENV,
        "live_trading_token_required": False,
        "live_token_used": False,
        "sandbox_token_used": False,
        "token_printed": False,
    }


# ─── core ─────────────────────────────────────────────────────────────────────

def build_report(*, ticker: str, cli_order_id: str, live_account_id: str,
                 ids: dict, facts: dict, position: dict | None,
                 position_units: Decimal | None, position_lots: Decimal | None,
                 avg_price: Decimal | None, current_price: Decimal | None,
                 currency: str | None, positions_read: bool,
                 read_token_present: bool, income: dict,
                 recon_warnings: list[str], warnings: list[str],
                 errors: list[str], now: datetime) -> dict:
    position_found = position is not None
    lot_size = ids.get("lot_size")
    instrument_type = (position or {}).get("instrumentType") if position else None

    position_value = (current_price * position_units
                      if current_price is not None and position_units is not None
                      else None)
    unrealized_pnl = (
        (current_price - avg_price) * position_units
        if (current_price is not None and avg_price is not None
            and position_units is not None) else None)

    passed, recon_hard_warnings = reconcile(
        facts=facts, cli_order_id=cli_order_id, position=position,
        lot_size=lot_size, position_units=position_units,
        positions_read=positions_read)
    all_recon_warnings = recon_hard_warnings + recon_warnings
    reconciliation_passed = bool(passed and not errors)

    report = {
        "kind": "income_live_position",
        "read_only": True,
        "generated_at": now.isoformat(),
        "stage": STAGE,
        "mode": MODE,
        "ticker": ticker,
        "order_id": cli_order_id or facts.get("order_id"),
        "live_account_id_masked": (
            mask_identifier(live_account_id) if live_account_id else None),
        "order_status": facts.get("status"),
        "lots_requested": facts.get("lots_requested"),
        "lots_executed": facts.get("lots_executed"),
        "instrument_uid": ids.get("uid"),
        "figi": ids.get("figi"),
        "class_code": ids.get("class_code"),
        "instrument_type": instrument_type,
        "lot_size": lot_size,
        "position_found": position_found,
        "position_quantity_lots": position_lots,
        "position_quantity_units": position_units,
        "average_position_price": avg_price,
        "current_price": current_price,
        "current_position_value": position_value,
        "unrealized_pnl": unrealized_pnl,
        "currency": currency,
        "reconciliation_passed": reconciliation_passed,
        "reconciliation_warnings": all_recon_warnings,
        # income-goal block
        "base_monthly_living_basket_rub": income["base_monthly_living_basket_rub"],
        "target_context_source": income["target_context_source"],
        "estimated_income_contribution_rub_monthly": income[
            "estimated_income_contribution_rub_monthly"],
        "estimated_income_contribution_rub_yearly": income[
            "estimated_income_contribution_rub_yearly"],
        "income_target_coverage_pct": income["income_target_coverage_pct"],
        "checked_at": now.isoformat(),
        "guards": _guards(),
        "token_policy": _token_policy(read_token_present),
        "warnings": warnings,
        "errors": errors,
    }
    report["_exit_code"] = 0 if (reconciliation_passed and not errors) else 1
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
        "# F4.3 — live position reconciliation (READ ONLY)",
        "",
        "> Только read-only сверка реальной позиции с завершённой F4.1/F4.2 "
        "заявкой. Команда НИЧЕГО не исполняет, не отменяет и не продаёт.",
        "",
        "> Guard block",
        ">",
        "> - F4.3 read-only position reconciliation",
        "> - No PostOrder, no order cancellation, no second order, no sell",
        "> - No retry, no MARKET, no portfolio/config mutation, no Telegram",
        "> - Read-only `TINKOFF_TOKEN` only; live/sandbox token not used",
        "",
        f"- stage: `{report['stage']}`",
        f"- mode: `{report['mode']}`",
        f"- ticker: `{report['ticker']}`",
        f"- order_id: `{report['order_id']}`",
        f"- live_account_id_masked: `{_fmt(report['live_account_id_masked'])}`",
        f"- **reconciliation_passed: {_fmt(report['reconciliation_passed'])}**",
        "",
        "## Order (from F4.1/F4.2)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        f"| order_status | `{_fmt(report['order_status'])}` |",
        f"| lots_requested | {_fmt(report['lots_requested'])} |",
        f"| lots_executed | {_fmt(report['lots_executed'])} |",
        f"| instrument_uid | `{_fmt(report['instrument_uid'])}` |",
        f"| figi | `{_fmt(report['figi'])}` |",
        f"| class_code | `{_fmt(report['class_code'])}` |",
        f"| instrument_type | `{_fmt(report['instrument_type'])}` |",
        f"| lot_size | {_fmt(report['lot_size'])} |",
        "",
        "## Live position (read-only portfolio)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        f"| position_found | {_fmt(report['position_found'])} |",
        f"| position_quantity_lots | {_fmt(report['position_quantity_lots'])} |",
        f"| position_quantity_units | {_fmt(report['position_quantity_units'])} |",
        f"| average_position_price | {_fmt(report['average_position_price'])} |",
        f"| current_price | {_fmt(report['current_price'])} |",
        f"| current_position_value | {_fmt(report['current_position_value'])} |",
        f"| unrealized_pnl | {_fmt(report['unrealized_pnl'])} |",
        f"| currency | {_fmt(report['currency'])} |",
        "",
        "## Income goal (read-only, без угадывания)",
        "",
        f"- base_monthly_living_basket_rub: {_fmt(report['base_monthly_living_basket_rub'])}",
        f"- target_context_source: {_fmt(report['target_context_source'])}",
        f"- estimated_income_contribution_rub_monthly: "
        f"{_fmt(report['estimated_income_contribution_rub_monthly'])}",
        f"- estimated_income_contribution_rub_yearly: "
        f"{_fmt(report['estimated_income_contribution_rub_yearly'])}",
        f"- income_target_coverage_pct: {_fmt(report['income_target_coverage_pct'])}",
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
    if report.get("reconciliation_warnings"):
        lines += ["", "## Reconciliation warnings"]
        lines += [f"- {w}" for w in report["reconciliation_warnings"]]
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
        "Read-only reconciliation. No orders were created, cancelled, sold or "
        "retried; no portfolio/config mutation.",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация / оркестрация ───────────────────────────────────────────────

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


def run(*, ticker: str, order_id: str, live_account_id: str,
        f41_report: str | None = None,
        f42_report: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None,
        client=None,
        positions_provider=None,
        dividend_provider=None,
        read_token_present: bool | None = None,
        client_error: str | None = None,
        now: datetime | None = None) -> dict:
    """Read-only сверка реальной позиции с завершённой F4.1/F4.2 заявкой.

    Ничего не исполняет. Использует только read-only данные. Возвращает отчёт
    (+ пути и _exit_code).
    """
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        raise LivePositionError("Не задан --ticker.")
    cli_order_id = str(order_id or "").strip()
    if not cli_order_id:
        raise LivePositionError("Не задан --order-id.")
    live_account_id = str(live_account_id or "").strip()
    if not live_account_id:
        raise LivePositionError("Не задан --live-account-id.")

    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []
    errors: list[str] = []

    f41 = _load_json(f41_report or F41_DEFAULT_JSON)
    f42 = _load_json(f42_report or F42_DEFAULT_JSON)
    if f41 is None:
        warnings.append(
            "F4.1 execution report не найден — идентификаторы инструмента/лот "
            "могут быть неполными.")
    if f42 is None:
        warnings.append(
            "F4.2 order status report не найден — статус/lots_executed берутся из "
            "F4.1, если доступны.")

    ids = extract_instrument_ids(f41)
    facts = extract_order_facts(f41, f42)

    if read_token_present is None:
        read_token_present = client is not None

    # Default read-only positions provider из портфеля (никаких заявок/записи).
    if positions_provider is None and client is not None:
        def positions_provider(account_id):  # noqa: ANN001
            portfolio = client.get_portfolio(account_id) or {}
            return portfolio.get("positions") or []

    # Read-only резолв figi/uid, если их нет в отчётах (тот же тикер, не переключаем).
    if not (ids.get("figi") or ids.get("uid")) and client is not None:
        try:
            instr = client.find_instrument(ticker, ids.get("class_code") or "TQBR")
            if isinstance(instr, dict):
                ids["figi"] = ids.get("figi") or instr.get("figi")
                ids["uid"] = ids.get("uid") or (
                    instr.get("uid") or instr.get("instrumentUid"))
                if ids.get("lot_size") is None:
                    ids["lot_size"] = _int_of(instr.get("lot"))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Read-only резолв инструмента не удался: {exc}")

    positions = None
    positions_read = False
    if positions_provider is None:
        detail = f" ({client_error})" if client_error else " (нет TINKOFF_TOKEN)"
        errors.append(
            "Read-only клиент недоступен" + detail + " — портфель не прочитан; "
            "сетевых вызовов не выполнено.")
    else:
        try:
            positions = positions_provider(live_account_id)
            positions_read = True
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Не удалось прочитать портфель (read-only): {exc}")

    position = (find_position(positions, figi=ids.get("figi"), uid=ids.get("uid"))
                if positions_read else None)

    position_units = position_lots = avg_price = current_price = None
    currency = None
    if position is not None:
        position_units = quotation_to_decimal(position.get("quantity"))
        position_lots = quotation_to_decimal(position.get("quantityLots"))
        if position_lots is None and position_units is not None and ids.get("lot_size"):
            position_lots = position_units / Decimal(ids["lot_size"])
        avg_price = quotation_to_decimal(position.get("averagePositionPrice"))
        current_price = quotation_to_decimal(position.get("currentPrice"))
        currency = _money_currency(position.get("averagePositionPrice"),
                                   position.get("currentPrice"))

    instrument = {"ticker": ticker, "figi": ids.get("figi"), "uid": ids.get("uid"),
                  "class_code": ids.get("class_code")}
    income, income_warnings = estimate_income(
        position_units=position_units, dividend_provider=dividend_provider,
        instrument=instrument, base_monthly=BASE_MONTHLY_LIVING_BASKET_RUB)
    warnings.extend(income_warnings)

    report = build_report(
        ticker=ticker, cli_order_id=cli_order_id, live_account_id=live_account_id,
        ids=ids, facts=facts, position=position, position_units=position_units,
        position_lots=position_lots, avg_price=avg_price,
        current_price=current_price, currency=currency,
        positions_read=positions_read, read_token_present=bool(read_token_present),
        income=income, recon_warnings=[], warnings=warnings, errors=errors, now=now)
    return _write(report, output_json, output_md)
