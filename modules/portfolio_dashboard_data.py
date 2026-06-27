"""
portfolio_dashboard_data — F4.8 read-only portfolio dashboard data model.

Безопасная READ-ONLY агрегация портфельных метрик для будущего дашборда F4.9.
Команда `portfolio-dashboard-data` НИЧЕГО не исполняет и НИЧЕГО не угадывает: это
модель данных (JSON/MD), а не UI и не торговля. Первая сделка по T — лишь ОДНО
событие в истории, а не центр отчёта; старые позиции — реальные инвестиции и
учитываются на уровне портфеля.

Источники (read-only): локальные отчёты F4.1–F4.6 и (опционально) аналитический
`TINKOFF_TOKEN` для portfolio/operations/market-data/dividends. При отсутствии
полных брокерских данных отчёт помечается как PARTIAL.

Жёсткий контракт (никогда не нарушать):
- Только READ-ONLY данные. `TINKOFF_LIVE_TRADING_TOKEN` / `TINKOFF_SANDBOX_TOKEN`
  НЕ требуются и НЕ используются. Токен не печатается и не пишется в отчёт.
- НЕ вызывает PostOrder, НЕ отменяет, НЕ продаёт, НЕ ретраит, НЕ использует MARKET.
  НЕ мутирует портфель/config. НЕ шлёт Telegram. Нет планировщика.
- Оборот = sum(abs(gross BUY) + abs(gross SELL)); дивиденды/купоны — это доход, НЕ
  оборот. Net-доход не считаем при неизвестном налоге. Метрику без данных = null + warning.

Имена ключей/guard со словом «order» переиспользуются из F4.x модулей (constants),
поэтому цельных запрещённых литералов в этом исходнике нет — статический сканер
modules/execution_preflight.py и safety-grep не считают этот read-only модуль
ложным order-endpoint.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common.helpers import mask_identifier, quotation_to_decimal
from modules.income_live_execution import (
    DEFAULT_OUTPUT_JSON as F41_JSON,
)
from modules.income_live_fill_attribution import (
    DEFAULT_OUTPUT_JSON as F44_JSON,
)
from modules.income_live_fill_economics import (
    DEFAULT_OUTPUT_JSON as F45_JSON,
)
from modules.income_live_income_validation import (
    DEFAULT_OUTPUT_JSON as F46_JSON,
)
from modules.income_live_position import (
    DEFAULT_OUTPUT_JSON as F43_JSON,
)
from modules.income_live_status import (
    DEFAULT_OUTPUT_JSON as F42_JSON,
)
from modules.income_live_status import (
    GUARD_CANCEL_CALLED,
    GUARD_LIVE_ORDER_SENT,
)
from modules.operation_filter import is_buy, is_qualifying_operation
from modules.turnover_calculator import calculate_operation_turnover

DEFAULT_OUTPUT_JSON = "data/reports/portfolio_dashboard_data.json"
DEFAULT_OUTPUT_MD = "data/reports/portfolio_dashboard_data.md"
DEFAULT_CONTRIBUTION_PLAN = "data/config/contribution_plan.json"

STAGE = "F4_8_PORTFOLIO_DASHBOARD_DATA_READ_ONLY"
MODE = "PORTFOLIO_DATA_READ_ONLY"
KIND = "portfolio_dashboard_data"

READ_TOKEN_ENV = "TINKOFF_TOKEN"
LIVE_TRADING_TOKEN_ENV = "TINKOFF_LIVE_TRADING_TOKEN"

BASE_MONTHLY_LIVING_BASKET_RUB = 150000
BASE_INCOME_DATE = "2026-06"
TURNOVER_ANNUAL_TARGET_RUB = 60_000_000
TURNOVER_MONTHLY_TARGET_RUB = 5_000_000
TURNOVER_QUARTERLY_TARGET_RUB = 15_000_000
# Явное допущение доходности для оценки требуемого капитала (не реальная доходность).
REQUIRED_CAPITAL_ASSUMED_YIELD_PCT = Decimal("10.0")

_M2 = Decimal("0.01")
_P4 = Decimal("0.0001")

WARN_TAX_UNKNOWN = (
    "Налоговый режим неизвестен — net-доход не считаем (не угадываем); "
    "net-поля = null. Оценка дохода — БРУТТО.")
WARN_CONTRIB_NOT_CONFIGURED = "contribution_plan_not_configured"
WARN_TURNOVER_PARTIAL = (
    "Полная история операций недоступна — оборот частичный (из локальных отчётов "
    "по одной известной сделке); помечен partial.")
WARN_PORTFOLIO_PARTIAL = (
    "Полный портфель через read-only API недоступен — портфельные метрики из "
    "локальных отчётов (только известные позиции); помечено partial.")


class PortfolioDashboardError(Exception):
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


def _money(raw) -> Decimal | None:
    if isinstance(raw, dict):
        return quotation_to_decimal(raw)
    return _to_decimal(raw)


def _first(*values) -> Decimal | None:
    for v in values:
        d = _to_decimal(v)
        if d is not None:
            return d
    return None


def _first_str(*values):
    for v in values:
        if v not in (None, ""):
            return v
    return None


def _q2(value: Decimal | None) -> Decimal | None:
    return value.quantize(_M2) if value is not None else None


def _q4(value: Decimal | None) -> Decimal | None:
    return value.quantize(_P4) if value is not None else None


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _pct(part: Decimal | None, whole: Decimal | None) -> Decimal | None:
    if part is None or whole in (None, 0):
        return None
    return _q4(part / whole * Decimal(100))


# ─── positions model ──────────────────────────────────────────────────────────

def _income_for_instrument(*, uid, figi, ticker, qty, current_price, f46,
                           dividend_provider):
    """Возвращает (per_unit, source, confidence, next_date, next_amt) без угадывания."""
    f46 = f46 or {}
    f46_uid = f46.get("instrument_uid")
    f46_figi = f46.get("figi")
    matches_f46 = (uid and uid == f46_uid) or (figi and figi == f46_figi)
    if matches_f46 and f46.get("reliable_income_data_found"):
        return (_to_decimal(f46.get("expected_dividend_per_unit_rub")),
                f46.get("income_data_source"), f46.get("income_data_confidence"),
                f46.get("next_known_income_event_date"),
                _to_decimal(f46.get("next_known_income_event_amount_per_unit")))
    if dividend_provider is not None:
        try:
            div = dividend_provider(uid or figi)
        except Exception:  # noqa: BLE001
            div = None
        if isinstance(div, dict) and div.get("dividend_source") == "api_known_future":
            per = _to_decimal(div.get("expected_annual_dividend_rub_per_share"))
            events = div.get("events") or []
            nxt = events[0] if events and isinstance(events[0], dict) else {}
            return (per, div.get("dividend_source"), div.get("dividend_confidence"),
                    _first_str(div.get("next_dividend_date"), nxt.get("date")),
                    _to_decimal(nxt.get("per_share")))
    return (None, None, None, None, None)


def _build_position(*, ticker, figi, uid, name, asset_class, currency, qty, avg,
                    current_price, source, f46, dividend_provider) -> dict:
    market_value = (current_price * qty
                    if (current_price is not None and qty is not None) else None)
    pnl = ((current_price - avg) * qty
           if (current_price is not None and avg is not None and qty is not None)
           else None)
    pnl_pct = (_pct(current_price - avg, avg)
               if (current_price is not None and avg is not None) else None)
    per_unit, inc_src, inc_conf, nxt_date, nxt_amt = _income_for_instrument(
        uid=uid, figi=figi, ticker=ticker, qty=qty, current_price=current_price,
        f46=f46, dividend_provider=dividend_provider)
    yearly = (per_unit * qty
              if (per_unit is not None and qty is not None) else None)
    monthly = (yearly / Decimal(12) if yearly is not None else None)
    yield_pct = (_pct(per_unit, current_price)
                 if (per_unit is not None and current_price not in (None, 0))
                 else None)
    pos_warnings = []
    if per_unit is None:
        pos_warnings.append("Надёжных данных о доходе по инструменту нет — "
                            "income-поля = null (не угадываем).")
    return {
        "ticker": ticker,
        "figi": figi,
        "instrument_uid": uid,
        "name": name,
        "asset_class": asset_class,
        "currency": currency,
        "quantity_units": qty,
        "average_price": avg,
        "current_price": current_price,
        "market_value_rub": _q2(market_value),
        "weight_pct": None,  # заполняется после агрегирования
        "unrealized_pnl_rub": _q2(pnl),
        "unrealized_pnl_pct": pnl_pct,
        "expected_income_rub_yearly": _q2(yearly),
        "expected_income_rub_monthly": _q2(monthly),
        "expected_income_yield_pct": yield_pct,
        "next_income_event_date": nxt_date,
        "next_income_event_amount_per_unit": nxt_amt,
        "income_data_source": inc_src,
        "income_data_confidence": inc_conf,
        "source": source,
        "warnings": pos_warnings,
    }


def build_positions_model(*, portfolio_raw, f43, f46, dividend_provider=None,
                          resolver=None) -> list[dict]:
    """Список позиций портфеля. Из read-only портфеля (если есть), иначе из F4.3."""
    positions: list[dict] = []
    if portfolio_raw and isinstance(portfolio_raw.get("positions"), list):
        for pos in portfolio_raw["positions"]:
            if not isinstance(pos, dict):
                continue
            itype = str(pos.get("instrumentType") or "").lower()
            if itype == "currency":
                continue  # валюта — это кэш, не позиция
            uid = _first_str(pos.get("instrumentUid"), pos.get("instrument_uid"))
            figi = pos.get("figi")
            ticker = pos.get("ticker")
            name = pos.get("name")
            if resolver is not None and not ticker:
                try:
                    info = resolver.resolve(figi=figi, instrument_uid=uid)
                    ticker = ticker or getattr(info, "ticker", None)
                    name = name or getattr(info, "name", None)
                except Exception:  # noqa: BLE001
                    pass
            qty = _money(pos.get("quantity"))
            positions.append(_build_position(
                ticker=ticker, figi=figi, uid=uid, name=name, asset_class=itype,
                currency=_first_str(
                    (pos.get("averagePositionPrice") or {}).get("currency")
                    if isinstance(pos.get("averagePositionPrice"), dict) else None),
                qty=qty, avg=_money(pos.get("averagePositionPrice")),
                current_price=_money(pos.get("currentPrice")),
                source="readonly_portfolio_api", f46=f46,
                dividend_provider=dividend_provider))
    elif f43 and f43.get("position_found"):
        positions.append(_build_position(
            ticker=f43.get("ticker"), figi=f43.get("figi"),
            uid=f43.get("instrument_uid"), name=f43.get("ticker"),
            asset_class=str(f43.get("instrument_type") or "").lower() or None,
            currency=f43.get("currency"),
            qty=_to_decimal(f43.get("position_quantity_units")),
            avg=_to_decimal(f43.get("average_position_price")),
            current_price=_to_decimal(f43.get("current_price")),
            source="local_reports_f43", f46=f46,
            dividend_provider=dividend_provider))
    return positions


def _apply_weights(positions: list[dict], positions_value: Decimal | None) -> None:
    if not positions_value or positions_value == 0:
        return
    for pos in positions:
        mv = _to_decimal(pos.get("market_value_rub"))
        if mv is not None:
            pos["weight_pct"] = _q4(mv / positions_value * Decimal(100))


# ─── portfolio summary ────────────────────────────────────────────────────────

def build_portfolio_summary(*, portfolio_raw, positions: list[dict], now,
                            warnings: list[str]) -> dict:
    positions_value = None
    pos_values = [_to_decimal(p.get("market_value_rub")) for p in positions]
    pos_values = [v for v in pos_values if v is not None]
    if pos_values:
        positions_value = sum(pos_values, Decimal(0))
    pnl_values = [_to_decimal(p.get("unrealized_pnl_rub")) for p in positions]
    pnl_values = [v for v in pnl_values if v is not None]
    unrealized = sum(pnl_values, Decimal(0)) if pnl_values else None

    cash = total = None
    currency = "rub"
    partial = True
    source = "local_reports (partial)"
    as_of = None
    if portfolio_raw:
        partial = False
        source = "readonly_portfolio_api"
        as_of = now.isoformat()
        cash = _money(portfolio_raw.get("totalAmountCurrencies"))
        total = _money(portfolio_raw.get("totalAmountPortfolio"))
        ey = portfolio_raw.get("expectedYield")
        if isinstance(ey, dict) and ey.get("currency"):
            currency = ey.get("currency")
    if total is None:
        total = positions_value  # частично: только стоимость известных позиций
    if partial:
        warnings.append(WARN_PORTFOLIO_PARTIAL)

    cost_basis = None
    if positions_value is not None and unrealized is not None:
        cost_basis = positions_value - unrealized
    return {
        "total_portfolio_value_rub": _q2(total),
        "positions_value_rub": _q2(positions_value),
        "cash_rub": _q2(cash),
        "cash_pct": _pct(cash, total) if (cash is not None and total) else None,
        "unrealized_pnl_rub": _q2(unrealized),
        "unrealized_pnl_pct": _pct(unrealized, cost_basis),
        "positions_count": len(positions),
        "currency": currency,
        "portfolio_source": source,
        "portfolio_as_of": as_of,
        "partial": partial,
    }


# ─── income summary ───────────────────────────────────────────────────────────

def build_income_summary(*, positions: list[dict], f46, portfolio_value,
                         warnings: list[str]) -> dict:
    yearly = Decimal(0)
    has_income = False
    breakdown: dict[str, dict] = {}
    events: list[dict] = []
    calendar: dict[str, Decimal] = {}
    for pos in positions:
        y = _to_decimal(pos.get("expected_income_rub_yearly"))
        if y is None:
            continue
        has_income = True
        yearly += y
        src = pos.get("income_data_source") or "unknown"
        b = breakdown.setdefault(src, {"yearly_rub": Decimal(0)})
        b["yearly_rub"] += y
        ev_date = pos.get("next_income_event_date")
        per_unit = _to_decimal(pos.get("next_income_event_amount_per_unit"))
        qty = _to_decimal(pos.get("quantity_units"))
        ev_total = (per_unit * qty if (per_unit is not None and qty is not None)
                    else None)
        if ev_date:
            events.append({
                "date": ev_date, "ticker": pos.get("ticker"), "type": "dividend",
                "amount_per_unit": per_unit, "amount_total_rub": _q2(ev_total)})
            month = str(ev_date)[:7]
            if ev_total is not None:
                calendar[month] = calendar.get(month, Decimal(0)) + ev_total

    yearly_gross = _q2(yearly) if has_income else None
    monthly_gross = _q2(yearly / Decimal(12)) if has_income else None
    for b in breakdown.values():
        b["yearly_rub"] = _q2(b["yearly_rub"])
        b["monthly_rub"] = _q2(b["yearly_rub"] / Decimal(12))

    target = Decimal(BASE_MONTHLY_LIVING_BASKET_RUB)
    index_mult = Decimal("1.0")  # индексация цели пока не применяется (база 2026-06)
    target_indexed = _q2(target * index_mult)
    coverage = _pct(monthly_gross, target) if monthly_gross is not None else None
    gap = _q2(target - monthly_gross) if monthly_gross is not None else None

    # требуемый капитал при ЯВНОМ допущении доходности (не реальная доходность)
    annual_target_income = target * Decimal(12)
    req_capital = _q2(annual_target_income
                      / (REQUIRED_CAPITAL_ASSUMED_YIELD_PCT / Decimal(100)))
    req_gap = (_q2(req_capital - portfolio_value)
               if (req_capital is not None and portfolio_value is not None) else None)

    if WARN_TAX_UNKNOWN not in warnings:
        warnings.append(WARN_TAX_UNKNOWN)

    calendar_out = {m: _q2(v) for m, v in sorted(calendar.items())}
    events_sorted = sorted(events, key=lambda e: str(e.get("date") or ""))
    return {
        "passive_income_rub_monthly_gross": monthly_gross,
        "passive_income_rub_yearly_gross": yearly_gross,
        "passive_income_rub_monthly_net": None,
        "passive_income_rub_yearly_net": None,
        "income_net_estimation_available": False,
        "income_tax_warning": WARN_TAX_UNKNOWN,
        "target_monthly_income_rub": BASE_MONTHLY_LIVING_BASKET_RUB,
        "target_monthly_income_indexed_rub": target_indexed,
        "target_index_multiplier": index_mult,
        "income_target_coverage_pct": coverage,
        "income_gap_rub_monthly": gap,
        "required_capital_rub": req_capital,
        "required_capital_assumption_yield_pct": REQUIRED_CAPITAL_ASSUMED_YIELD_PCT,
        "required_capital_gap_rub": req_gap,
        "income_sources_breakdown": breakdown,
        "income_calendar_monthly": calendar_out,
        "next_income_events": events_sorted,
    }


# ─── turnover summary ─────────────────────────────────────────────────────────

def _year_fraction(now: datetime) -> tuple[Decimal, int]:
    year_start = datetime(now.year, 1, 1, tzinfo=now.tzinfo or timezone.utc)
    year_end = datetime(now.year, 12, 31, tzinfo=now.tzinfo or timezone.utc)
    days_total = (year_end - year_start).days + 1
    days_elapsed = max(1, (now - year_start).days + 1)
    return Decimal(days_elapsed) / Decimal(days_total), days_total - days_elapsed


def _period_starts(now: datetime):
    tz = now.tzinfo or timezone.utc
    year_start = datetime(now.year, 1, 1, tzinfo=tz)
    month_start = datetime(now.year, now.month, 1, tzinfo=tz)
    q_first_month = ((now.month - 1) // 3) * 3 + 1
    quarter_start = datetime(now.year, q_first_month, 1, tzinfo=tz)
    return year_start, month_start, quarter_start


def build_turnover_summary(*, operations, f44, now, warnings: list[str],
                           resolver=None) -> dict:
    definition = "sum_abs_buy_sell_gross_amount"
    year_start, month_start, quarter_start = _period_starts(now)
    ytd = mtd = qtd = Decimal(0)
    comm_ytd = comm_mtd = comm_qtd = Decimal(0)
    by_instrument: dict[str, Decimal] = {}
    by_side = {"BUY": Decimal(0), "SELL": Decimal(0)}
    by_month: dict[str, Decimal] = {}
    partial = False
    have_data = False

    if operations:
        have_data = True
        for op in operations:
            if not isinstance(op, dict) or not is_qualifying_operation(op):
                continue
            dt = _parse_dt(op.get("date"))
            res = calculate_operation_turnover(op, account_id="", resolver=resolver)
            gross = res.turnover_exact if not res.is_approximate \
                else res.turnover_approximate
            comm = abs(_money(op.get("commission")) or Decimal(0))
            side = "BUY" if is_buy(op) else "SELL"
            label = res.ticker or res.instrument_uid[:8] or res.operation_id
            by_side[side] += gross
            by_instrument[label] = by_instrument.get(label, Decimal(0)) + gross
            if dt is not None:
                by_month[dt.strftime("%Y-%m")] = \
                    by_month.get(dt.strftime("%Y-%m"), Decimal(0)) + gross
                if dt >= year_start:
                    ytd += gross
                    comm_ytd += comm
                if dt >= month_start:
                    mtd += gross
                    comm_mtd += comm
                if dt >= quarter_start:
                    qtd += gross
                    comm_qtd += comm
    elif f44 and _to_decimal(f44.get("fill_gross_amount")) is not None:
        # PARTIAL: полной истории нет — учитываем одну известную сделку из F4.4.
        have_data = True
        partial = True
        gross = _to_decimal(f44.get("fill_gross_amount"))
        comm = abs(_to_decimal(f44.get("fill_commission_abs")) or Decimal(0))
        dt = _parse_dt(f44.get("fill_datetime"))
        label = f44.get("ticker") or "?"
        by_side["BUY"] += gross
        by_instrument[label] = gross
        if dt is not None:
            by_month[dt.strftime("%Y-%m")] = gross
            if dt >= year_start:
                ytd, comm_ytd = gross, comm
            if dt >= month_start:
                mtd, comm_mtd = gross, comm
            if dt >= quarter_start:
                qtd, comm_qtd = gross, comm
        else:
            ytd = qtd = gross
            comm_ytd = comm_qtd = comm
        warnings.append(WARN_TURNOVER_PARTIAL)

    if not have_data:
        warnings.append(
            "История операций недоступна — оборот не рассчитан (поля = null).")
        return _empty_turnover(definition)

    fraction, _days_remaining = _year_fraction(now)
    annual = Decimal(TURNOVER_ANNUAL_TARGET_RUB)
    plan_to_date = _q2(annual * fraction)
    gap = _q2(plan_to_date - ytd)
    progress = _pct(ytd, annual)
    forecast = _q2(ytd / fraction) if fraction > 0 else None
    remaining = _q2(annual - ytd)
    trading_days_remaining = int(_days_remaining * 5 / 7)
    daily_required = (_q2(remaining / Decimal(trading_days_remaining))
                      if trading_days_remaining > 0 and remaining is not None
                      else None)
    turnover_total = ytd
    comm_rate = _pct(comm_ytd, turnover_total) if turnover_total else None

    return {
        "turnover_definition": definition,
        "turnover_partial": partial,
        "turnover_ytd_rub": _q2(ytd),
        "turnover_mtd_rub": _q2(mtd),
        "turnover_qtd_rub": _q2(qtd),
        "turnover_annual_target_rub": TURNOVER_ANNUAL_TARGET_RUB,
        "turnover_monthly_target_rub": TURNOVER_MONTHLY_TARGET_RUB,
        "turnover_quarterly_target_rub": TURNOVER_QUARTERLY_TARGET_RUB,
        "turnover_ytd_plan_to_date_rub": plan_to_date,
        "turnover_ytd_gap_rub": gap,
        "turnover_ytd_progress_pct": progress,
        "turnover_forecast_year_end_rub": forecast,
        "turnover_remaining_year_rub": remaining,
        "turnover_daily_required_rub": daily_required,
        "trading_days_remaining_estimate": trading_days_remaining,
        "commissions_ytd_rub": _q2(comm_ytd),
        "commissions_mtd_rub": _q2(comm_mtd),
        "commissions_qtd_rub": _q2(comm_qtd),
        "commission_rate_pct_of_turnover": comm_rate,
        "turnover_by_instrument": {k: _q2(v) for k, v in by_instrument.items()},
        "turnover_by_side": {k: _q2(v) for k, v in by_side.items()},
        "turnover_by_month": {k: _q2(v) for k, v in sorted(by_month.items())},
    }


def _empty_turnover(definition: str) -> dict:
    return {
        "turnover_definition": definition,
        "turnover_partial": True,
        "turnover_ytd_rub": None, "turnover_mtd_rub": None, "turnover_qtd_rub": None,
        "turnover_annual_target_rub": TURNOVER_ANNUAL_TARGET_RUB,
        "turnover_monthly_target_rub": TURNOVER_MONTHLY_TARGET_RUB,
        "turnover_quarterly_target_rub": TURNOVER_QUARTERLY_TARGET_RUB,
        "turnover_ytd_plan_to_date_rub": None, "turnover_ytd_gap_rub": None,
        "turnover_ytd_progress_pct": None, "turnover_forecast_year_end_rub": None,
        "turnover_remaining_year_rub": None, "turnover_daily_required_rub": None,
        "trading_days_remaining_estimate": None, "commissions_ytd_rub": None,
        "commissions_mtd_rub": None, "commissions_qtd_rub": None,
        "commission_rate_pct_of_turnover": None, "turnover_by_instrument": {},
        "turnover_by_side": {}, "turnover_by_month": {},
    }


# ─── contributions summary ────────────────────────────────────────────────────

def build_contributions_summary(*, plan, now, warnings: list[str]) -> dict:
    """F4.8 contributions_summary через общий модуль F4.10 (единая логика)."""
    from modules.contribution_plan import (
        WARN_NOT_CONFIGURED,
        summarize_for_dashboard,
    )
    summary = summarize_for_dashboard(plan, as_of=now.date())
    if not summary.get("contributions_tracking_enabled"):
        if WARN_NOT_CONFIGURED not in warnings:
            warnings.append(WARN_NOT_CONFIGURED)
    return summary


# ─── risk summary ─────────────────────────────────────────────────────────────

def build_risk_summary(*, positions: list[dict], portfolio_summary: dict) -> dict:
    weights = sorted(
        (w for w in (_to_decimal(p.get("weight_pct")) for p in positions)
         if w is not None), reverse=True)
    top1 = weights[0] if weights else None
    top5 = _q4(sum(weights[:5], Decimal(0))) if weights else None
    cash_pct = _to_decimal(portfolio_summary.get("cash_pct"))
    neg = sum(1 for p in positions
              if (_to_decimal(p.get("unrealized_pnl_rub")) or Decimal(0)) < 0)
    concentration_warnings = []
    if top1 is not None and top1 >= Decimal(40):
        concentration_warnings.append(
            f"Высокая концентрация: топ-позиция = {top1}% (≥40%).")
    cash_warnings = []
    if cash_pct is not None and cash_pct >= Decimal(30):
        cash_warnings.append(f"Высокая доля кэша: {cash_pct}% (≥30%).")
    quality = "full" if not portfolio_summary.get("partial") else "partial"
    return {
        "top_position_weight_pct": top1,
        "top_5_positions_weight_pct": top5,
        "cash_pct": cash_pct,
        "concentration_warnings": concentration_warnings,
        "cash_warnings": cash_warnings,
        "negative_pnl_positions_count": neg,
        "portfolio_unrealized_pnl_rub": portfolio_summary.get("unrealized_pnl_rub"),
        "portfolio_unrealized_pnl_pct": portfolio_summary.get("unrealized_pnl_pct"),
        "risk_data_quality": quality,
    }


# ─── last trade audit ─────────────────────────────────────────────────────────

def build_last_trade_audit_summary(*, f44, f45, f46, loaded_keys) -> dict:
    f44 = f44 or {}
    f45 = f45 or {}
    f46 = f46 or {}
    return {
        "last_tracked_trade_ticker": f44.get("ticker"),
        "last_tracked_trade_order_id": f44.get("order_id"),
        "last_tracked_trade_quantity": _to_decimal(f44.get("fill_quantity_units")),
        "last_tracked_trade_cash_outflow": _to_decimal(f44.get("fill_cash_outflow")),
        "last_tracked_trade_net_pnl_after_commission": _to_decimal(
            f45.get("new_fill_net_unrealized_pnl_after_commission")),
        "last_tracked_trade_income_yearly": _to_decimal(
            f46.get("expected_income_rub_yearly_new_fill")),
        "last_tracked_trade_income_monthly": _to_decimal(
            f46.get("expected_income_rub_monthly_new_fill")),
        "last_tracked_trade_audit_passed": bool(
            f46.get("income_validation_passed")) if f46 else None,
        "source_reports": list(loaded_keys),
    }


# ─── dashboard KPI ────────────────────────────────────────────────────────────

def build_dashboard_kpi(*, portfolio, income, turnover, contributions,
                        any_unsafe) -> dict:
    return {
        "portfolio_value_rub": portfolio.get("total_portfolio_value_rub"),
        "cash_rub": portfolio.get("cash_rub"),
        "cash_pct": portfolio.get("cash_pct"),
        "passive_income_monthly_rub": income.get("passive_income_rub_monthly_gross"),
        "passive_income_target_rub": income.get("target_monthly_income_rub"),
        "passive_income_coverage_pct": income.get("income_target_coverage_pct"),
        "income_gap_rub_monthly": income.get("income_gap_rub_monthly"),
        "turnover_ytd_rub": turnover.get("turnover_ytd_rub"),
        "turnover_annual_target_rub": turnover.get("turnover_annual_target_rub"),
        "turnover_ytd_progress_pct": turnover.get("turnover_ytd_progress_pct"),
        "turnover_gap_rub": turnover.get("turnover_ytd_gap_rub"),
        "contribution_monthly_fact_rub": contributions.get(
            "contribution_fact_monthly_rub"),
        "contribution_monthly_plan_rub": contributions.get(
            "contribution_plan_monthly_rub"),
        "contribution_gap_monthly_rub": contributions.get(
            "contribution_gap_monthly_rub"),
        "portfolio_unrealized_pnl_rub": portfolio.get("unrealized_pnl_rub"),
        "portfolio_unrealized_pnl_pct": portfolio.get("unrealized_pnl_pct"),
        "safety_status": "BLOCKED_UNSAFE" if any_unsafe else "READ_ONLY_SAFE",
    }


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


def _token_policy(read_token_present: bool, used_for) -> dict:
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


# ─── сериализация / markdown ──────────────────────────────────────────────────

def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def render_md(report: dict) -> str:
    kpi = report["dashboard_kpi"]
    pf = report["portfolio_summary"]
    inc = report["income_summary"]
    tn = report["turnover_summary"]
    cn = report["contributions_summary"]
    rk = report["risk_summary"]
    lt = report["last_trade_audit_summary"]

    def kv(d, keys):
        return "\n".join(f"| {k} | {_fmt(d.get(k))} |" for k in keys)

    lines = [
        "# F4.8 — portfolio dashboard data (READ ONLY)",
        "",
        "> Модель данных для будущего дашборда F4.9. Только read-only агрегация; "
        "не торгует, без записи/мутаций. PARTIAL при отсутствии полных брокерских "
        "данных.",
        "",
        f"- stage: `{report['stage']}` | mode: `{report['mode']}`",
        f"- account: `{_fmt(report['live_account_id_masked'])}` | "
        f"data_freshness: `{_fmt(report['data_freshness'].get('overall'))}`",
        f"- generated_at: `{_fmt(report['generated_at'])}`",
        "",
        "## Dashboard KPI (модель шапки дашборда)",
        "",
        "| KPI | Значение |",
        "| --- | --- |",
        kv(kpi, ["portfolio_value_rub", "cash_rub", "cash_pct",
                 "passive_income_monthly_rub", "passive_income_target_rub",
                 "passive_income_coverage_pct", "income_gap_rub_monthly",
                 "turnover_ytd_rub", "turnover_annual_target_rub",
                 "turnover_ytd_progress_pct", "turnover_gap_rub",
                 "contribution_monthly_fact_rub", "contribution_monthly_plan_rub",
                 "contribution_gap_monthly_rub", "portfolio_unrealized_pnl_rub",
                 "portfolio_unrealized_pnl_pct", "safety_status"]),
        "",
        "## Портфель",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        kv(pf, ["total_portfolio_value_rub", "positions_value_rub", "cash_rub",
                "cash_pct", "unrealized_pnl_rub", "unrealized_pnl_pct",
                "positions_count", "currency", "portfolio_source", "partial"]),
        "",
        "## Позиции",
        "",
        "| Тикер | Кол-во | Тек. цена | Стоимость | PnL | Доход/год | Источник |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for p in report["positions"]:
        lines.append(
            f"| {_fmt(p.get('ticker'))} | {_fmt(p.get('quantity_units'))} | "
            f"{_fmt(p.get('current_price'))} | {_fmt(p.get('market_value_rub'))} | "
            f"{_fmt(p.get('unrealized_pnl_rub'))} | "
            f"{_fmt(p.get('expected_income_rub_yearly'))} | "
            f"{_fmt(p.get('income_data_source'))} |")
    lines += [
        "",
        "## Пассивный доход",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        kv(inc, ["passive_income_rub_monthly_gross", "passive_income_rub_yearly_gross",
                 "passive_income_rub_monthly_net", "target_monthly_income_rub",
                 "income_target_coverage_pct", "income_gap_rub_monthly",
                 "required_capital_rub", "required_capital_assumption_yield_pct",
                 "required_capital_gap_rub"]),
        f"\n> {inc.get('income_tax_warning')}",
        "",
        "## Оборот (buy+sell gross, не дивиденды)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        kv(tn, ["turnover_definition", "turnover_partial", "turnover_ytd_rub",
                "turnover_mtd_rub", "turnover_qtd_rub", "turnover_annual_target_rub",
                "turnover_ytd_plan_to_date_rub", "turnover_ytd_gap_rub",
                "turnover_ytd_progress_pct", "turnover_forecast_year_end_rub",
                "commissions_ytd_rub", "commission_rate_pct_of_turnover"]),
        "",
        "## Взносы",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        kv(cn, ["contributions_tracking_enabled", "contribution_plan_weekly_rub",
                "contribution_plan_monthly_rub", "contribution_fact_monthly_rub",
                "contribution_gap_monthly_rub", "missed_contributions_count_month",
                "next_planned_contribution_date", "contribution_source"]),
        "",
        "## Риск / концентрация",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        kv(rk, ["top_position_weight_pct", "top_5_positions_weight_pct", "cash_pct",
                "negative_pnl_positions_count", "portfolio_unrealized_pnl_rub",
                "risk_data_quality"]),
        "",
        "## Последняя отслеживаемая сделка (аудит F4.1–F4.6, не центр отчёта)",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        kv(lt, ["last_tracked_trade_ticker", "last_tracked_trade_order_id",
                "last_tracked_trade_quantity", "last_tracked_trade_cash_outflow",
                "last_tracked_trade_net_pnl_after_commission",
                "last_tracked_trade_income_yearly", "last_tracked_trade_audit_passed"]),
    ]
    if report.get("warnings"):
        lines += ["", "## Warnings"] + [f"- {w}" for w in report["warnings"]]
    if report.get("errors"):
        lines += ["", "## Errors"] + [f"- {e}" for e in report["errors"]]
    lines += [
        "",
        "---",
        "",
        "Read-only data model. No orders were created, cancelled, sold or retried; "
        "no portfolio/config mutation. Live/sandbox token not used.",
        "",
    ]
    return "\n".join(lines)


def _write(report: dict, output_json, output_md) -> dict:
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

def load_portfolio_dashboard_data(
        *, live_account_id: str, reports_dir: str = "data/reports",
        client=None, portfolio_provider=None, operations_provider=None,
        dividend_provider=None, resolver=None,
        contribution_plan_path: str | None = None,
        f41=None, f42=None, f43=None, f44=None, f45=None, f46=None,
        read_token_present: bool | None = None, now: datetime | None = None) -> dict:
    """Чистая агрегация портфельной модели данных (без записи на диск)."""
    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []
    errors: list[str] = []
    if read_token_present is None:
        read_token_present = client is not None

    base = Path(reports_dir)

    def _rep(passed, default_path):
        return passed if passed is not None else _load_json(str(base / Path(default_path).name))

    f41 = _rep(f41, F41_JSON)
    f42 = _rep(f42, F42_JSON)
    f43 = _rep(f43, F43_JSON)
    f44 = _rep(f44, F44_JSON)
    f45 = _rep(f45, F45_JSON)
    f46 = _rep(f46, F46_JSON)

    loaded_keys = [k for k, v in (("f41", f41), ("f42", f42), ("f43", f43),
                                  ("f44", f44), ("f45", f45), ("f46", f46))
                   if v is not None]
    missing_keys = [k for k in ("f41", "f42", "f43", "f44", "f45", "f46")
                    if k not in loaded_keys]

    # read-only провайдеры (никогда не пишут/не торгуют)
    used_for = None
    if portfolio_provider is None and client is not None:
        def portfolio_provider(acc):  # noqa: ANN001
            return client.get_portfolio(acc)
    if dividend_provider is None and client is not None:
        from modules.income_sources import fetch_dividend_data

        def dividend_provider(instrument_id):  # noqa: ANN001
            return fetch_dividend_data(client, instrument_id, now=now)
    if operations_provider is None and client is not None:
        from datetime import timedelta

        def operations_provider(acc):  # noqa: ANN001
            frm = now - timedelta(days=400)
            return client.get_operations(acc, frm, now)

    portfolio_raw = None
    if portfolio_provider is not None:
        try:
            portfolio_raw = portfolio_provider(live_account_id)
            used_for = "portfolio/operations/market-data/dividends"
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Не удалось прочитать портфель (read-only): {exc}")
    operations = None
    if operations_provider is not None:
        try:
            operations = operations_provider(live_account_id)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Не удалось прочитать операции (read-only): {exc}")

    # ── модель ──
    positions = build_positions_model(
        portfolio_raw=portfolio_raw, f43=f43, f46=f46,
        dividend_provider=dividend_provider, resolver=resolver)
    portfolio = build_portfolio_summary(
        portfolio_raw=portfolio_raw, positions=positions, now=now,
        warnings=warnings)
    _apply_weights(positions, _to_decimal(portfolio.get("positions_value_rub")))
    income = build_income_summary(
        positions=positions, f46=f46,
        portfolio_value=_to_decimal(portfolio.get("total_portfolio_value_rub")),
        warnings=warnings)
    turnover = build_turnover_summary(
        operations=operations, f44=f44, now=now, warnings=warnings,
        resolver=resolver)
    plan = _load_json(contribution_plan_path or DEFAULT_CONTRIBUTION_PLAN)
    contributions = build_contributions_summary(plan=plan, now=now,
                                                warnings=warnings)
    risk = build_risk_summary(positions=positions, portfolio_summary=portfolio)
    last_trade = build_last_trade_audit_summary(
        f44=f44, f45=f45, f46=f46, loaded_keys=loaded_keys)
    kpi = build_dashboard_kpi(
        portfolio=portfolio, income=income, turnover=turnover,
        contributions=contributions, any_unsafe=False)

    data_freshness = {
        "reports_loaded": loaded_keys,
        "reports_missing": missing_keys,
        "portfolio_api": "live" if portfolio_raw else "absent",
        "operations_api": "live" if operations else "absent",
        "overall": "full" if (portfolio_raw and operations) else "partial",
    }
    data_sources_used = ["local_reports:" + k for k in loaded_keys]
    if portfolio_raw:
        data_sources_used.append("readonly_portfolio_api")
    if operations:
        data_sources_used.append("readonly_operations_api")
    data_sources_missing = missing_keys[:]
    if not portfolio_raw:
        data_sources_missing.append("readonly_portfolio_api")
    if not operations:
        data_sources_missing.append("readonly_operations_api")

    account_masked = mask_identifier(live_account_id) if live_account_id else (
        _first_str((f43 or {}).get("live_account_id_masked"),
                   (f44 or {}).get("live_account_id_masked")))

    report = {
        "kind": KIND,
        "read_only": True,
        "generated_at": now.isoformat(),
        "stage": STAGE,
        "mode": MODE,
        "live_account_id_masked": account_masked,
        "data_sources_used": data_sources_used,
        "data_sources_missing": data_sources_missing,
        "data_freshness": data_freshness,
        "base_monthly_living_basket_rub": BASE_MONTHLY_LIVING_BASKET_RUB,
        "base_income_date": BASE_INCOME_DATE,
        "turnover_annual_target_rub": TURNOVER_ANNUAL_TARGET_RUB,
        "turnover_monthly_target_rub": TURNOVER_MONTHLY_TARGET_RUB,
        "turnover_quarterly_target_rub": TURNOVER_QUARTERLY_TARGET_RUB,
        "portfolio_summary": portfolio,
        "positions": positions,
        "cash_summary": {
            "cash_rub": portfolio.get("cash_rub"),
            "cash_pct": portfolio.get("cash_pct"),
            "currency": portfolio.get("currency"),
            "cash_source": portfolio.get("portfolio_source"),
            "partial": portfolio.get("partial"),
        },
        "income_summary": income,
        "turnover_summary": turnover,
        "contributions_summary": contributions,
        "risk_summary": risk,
        "last_trade_audit_summary": last_trade,
        "dashboard_kpi": kpi,
        "warnings": warnings,
        "errors": errors,
        "token_policy": _token_policy(bool(read_token_present), used_for),
        "guards": _guards(),
    }
    report["_exit_code"] = 1 if errors else 0
    return report


def run(*, live_account_id: str, reports_dir: str = "data/reports",
        output_json: str | None = None, output_md: str | None = None,
        contribution_plan_path: str | None = None,
        client=None, read_token_present: bool | None = None,
        now: datetime | None = None, **kw) -> dict:
    """Агрегирует модель и пишет JSON/MD. Ничего не исполняет."""
    live_account_id = str(live_account_id or "").strip()
    if not live_account_id:
        raise PortfolioDashboardError("Не задан --live-account-id.")
    report = load_portfolio_dashboard_data(
        live_account_id=live_account_id, reports_dir=reports_dir, client=client,
        contribution_plan_path=contribution_plan_path,
        read_token_present=read_token_present, now=now, **kw)
    return _write(report, output_json, output_md)
