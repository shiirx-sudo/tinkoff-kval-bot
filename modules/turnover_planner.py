"""
Read-only Manual Turnover Plan — расчётный план ручного набора оборота.

Ничего не покупает и не меняет портфель. Только читает уже созданные отчёты
(kval_plan.json, instrument_scan.json и др.) и считает, сколько оборота/сделок
ориентировочно нужно добрать вручную. Это НЕ инвестиционная рекомендация и НЕ
команда на сделку — фактическое засчитывание оборота сверяйте с брокером.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from math import ceil
from pathlib import Path

from loguru import logger

DISCLAIMER = (
    "Фактическое засчитывание оборота нужно сверять с брокерским отчётом. "
    "Финальное решение принимает брокер."
)
DISCLAIMER_FULL = (
    "Это не инвестиционная рекомендация и не команда на сделку. Это read-only "
    "расчётный план. Перед реальными сделками сверить комиссии, режим торгов, "
    "налоговые последствия и брокерский отчёт."
)

INPUT_FILES = (
    "kval_plan.json", "kval_progress.json", "kval_months.csv",
    "kval_quarters.csv", "kval_trades.csv", "instrument_scan.json",
)


class TurnoverPlanError(Exception):
    """Понятная ошибка для пользователя (без traceback)."""


# ─── Модель данных ───────────────────────────────────────────────────────────


@dataclass
class SelectedInstrument:
    ticker: str = ""
    name: str = ""
    resolved_class_code: str = ""
    bid_best: Decimal | None = None
    ask_best: Decimal | None = None
    spread_bps: Decimal | None = None
    estimated_roundtrip_cost_bps: Decimal | None = None
    estimated_monthly_cost_rub: Decimal | None = None
    lot: int = 1
    currency: str = ""
    trading_status: str = ""
    verdict: str = ""


@dataclass
class MonthPlan:
    month: str = ""
    status: str = ""
    planned_required_trade_count: int = 0
    current_trade_count: int = 0
    missing_trade_count: int = 0
    suggested_turnover: Decimal = Decimal("0")
    current_turnover: Decimal = Decimal("0")
    remaining_turnover: Decimal = Decimal("0")
    recommended_turnover_per_missing_trade: Decimal = Decimal("0")
    recommended_roundtrip_side_notional: Decimal = Decimal("0")


@dataclass
class QuarterPlan:
    quarter: str = ""
    planned_required_trade_count: int = 0
    current_trade_count: int = 0
    missing_trade_count: int = 0
    current_turnover: Decimal = Decimal("0")
    suggested_turnover: Decimal = Decimal("0")
    remaining_turnover: Decimal = Decimal("0")


@dataclass
class Recommendations:
    mode: str = "roundtrip"
    recommended_trade_turnover: Decimal = Decimal("0")
    recommended_roundtrip_side_notional: Decimal = Decimal("0")
    recommended_side_lots: int | None = None
    trade_plan_closed: bool = False
    # Явное разделение для автоматического исполнения (execution-plan)
    broker_trade_count_required: int = 0
    broker_trade_count_current: int = 0
    broker_trade_count_missing: int = 0
    roundtrip_cycle_count_required: int = 0
    side_notional: Decimal = Decimal("0")
    cycle_turnover: Decimal = Decimal("0")
    total_planned_turnover: Decimal = Decimal("0")
    expected_broker_trades_after_execution: int = 0
    expected_turnover_after_execution: Decimal = Decimal("0")
    note: str = ""
    disclaimer: str = DISCLAIMER


@dataclass
class ManualTurnoverPlan:
    as_of: date
    period_policy: str
    period_kind: str
    period_start: str
    period_end: str
    check_date: str
    target_monthly_turnover: Decimal
    commission_bps: Decimal
    mode: str
    selected_instrument: SelectedInstrument
    current_month_plan: MonthPlan
    current_quarter_plan: QuarterPlan | None
    recommendations: Recommendations
    months_csv: list[MonthPlan]
    warnings: list[str]
    source_files: dict[str, bool]
    generated_at: str
    disclaimer: str = DISCLAIMER_FULL


# ─── Утилиты ─────────────────────────────────────────────────────────────────


def _dec(v) -> Decimal | None:
    if v in (None, "", "None"):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _decz(v) -> Decimal:
    return _dec(v) or Decimal("0")


def _round(v: Decimal, places: str = "0.01") -> Decimal:
    return v.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _quarter_label(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


# ─── Выбор инструмента ───────────────────────────────────────────────────────


def select_instrument(
    scan: dict, requested: str | None
) -> tuple[dict, list[str]]:
    results = scan.get("results") or []
    warnings: list[str] = []

    if requested:
        req = requested.strip().upper()
        for r in results:
            if str(r.get("ticker", "")).upper() == req:
                return r, warnings
        raise TurnoverPlanError(
            f"Инструмент {requested} не найден в instrument_scan.json. "
            "Запустите instrument-scan с этим тикером."
        )

    good = [r for r in results if r.get("verdict") == "GOOD"
            and r.get("trading_status_ok") and r.get("data_ok")]
    if good:
        return good[0], warnings

    watch = [r for r in results if r.get("verdict") == "WATCH" and r.get("data_ok")]
    if watch:
        warnings.append(
            f"Нет инструмента с verdict=GOOD — выбран WATCH: {watch[0].get('ticker')}. "
            "Сверьте ликвидность/спред вручную."
        )
        return watch[0], warnings

    raise TurnoverPlanError(
        "Нет подходящего инструмента (GOOD/WATCH) в instrument_scan.json. "
        "Проверьте кандидатов и запустите instrument-scan во время торгов."
    )


def _to_selected(r: dict) -> SelectedInstrument:
    return SelectedInstrument(
        ticker=str(r.get("ticker", "")),
        name=str(r.get("name", "")),
        resolved_class_code=str(r.get("resolved_class_code") or r.get("class_code") or ""),
        bid_best=_dec(r.get("bid_best")),
        ask_best=_dec(r.get("ask_best")),
        spread_bps=_dec(r.get("spread_bps")),
        estimated_roundtrip_cost_bps=_dec(r.get("estimated_roundtrip_cost_bps")),
        estimated_monthly_cost_rub=_dec(r.get("estimated_monthly_cost_rub")),
        lot=int(r.get("lot") or 1),
        currency=str(r.get("currency", "")),
        trading_status=str(r.get("trading_status", "")),
        verdict=str(r.get("verdict", "")),
    )


# ─── Выбор месяца/квартала ───────────────────────────────────────────────────


def _pick_month(monthly: list[dict], as_of: date) -> dict | None:
    target = as_of.strftime("%Y-%m")
    by_month = {m.get("month"): m for m in monthly}
    cur = by_month.get(target)
    if cur and cur.get("status") == "future_required":
        return cur
    # иначе ближайший будущий месяц (не планируем задним числом)
    for m in monthly:
        if m.get("status") == "future_required":
            return m
    return cur  # всё закрыто — вернём текущий (или None)


def _pick_quarter(quarterly: list[dict], as_of: date) -> dict | None:
    target = _quarter_label(as_of)
    by_q = {q.get("quarter"): q for q in quarterly}
    cur = by_q.get(target)
    if cur and cur.get("status") == "future_required":
        return cur
    for q in quarterly:
        if q.get("status") == "future_required":
            return q
    return cur


# ─── Расчёт месяца ───────────────────────────────────────────────────────────


def _month_plan_from(entry: dict, mode: str, min_rub: Decimal,
                     max_rub: Decimal) -> MonthPlan:
    mp = MonthPlan(
        month=str(entry.get("month", "")),
        status=str(entry.get("status", "")),
        planned_required_trade_count=int(entry.get("planned_required_trade_count") or 0),
        current_trade_count=int(entry.get("current_trade_count") or 0),
        missing_trade_count=int(entry.get("missing_trade_count") or 0),
        suggested_turnover=_decz(entry.get("suggested_turnover")),
        current_turnover=_decz(entry.get("current_turnover")),
    )
    mp.remaining_turnover = max(Decimal("0"), mp.suggested_turnover - mp.current_turnover)

    missing = mp.missing_trade_count
    per_trade = mp.remaining_turnover / missing if missing > 0 else Decimal("0")

    # Корректный roundtrip-номинал на сторону: оборот делится на ВСЕ стороны
    # (cycles*2), а не per_trade/2 — иначе появляются лишние циклы.
    if mode == "roundtrip" and missing > 0:
        cycles = ceil(missing / 2)
        side = mp.remaining_turnover / (cycles * 2)
    else:
        side = Decimal("0")

    if min_rub > 0:
        per_trade = max(per_trade, min_rub)
        if side > 0:
            side = max(side, min_rub)
    if max_rub > 0:
        if per_trade > 0:
            per_trade = min(per_trade, max_rub)
        if side > 0:
            side = min(side, max_rub)

    mp.recommended_turnover_per_missing_trade = _round(per_trade)
    mp.recommended_roundtrip_side_notional = _round(side)
    return mp


def _quarter_plan_from(entry: dict | None) -> QuarterPlan | None:
    if not entry:
        return None
    qp = QuarterPlan(
        quarter=str(entry.get("quarter", "")),
        planned_required_trade_count=int(entry.get("required_min_trade_count") or 0),
        current_trade_count=int(entry.get("current_trade_count") or 0),
        missing_trade_count=int(entry.get("missing_trade_count") or 0),
        current_turnover=_decz(entry.get("current_turnover")),
        suggested_turnover=_decz(entry.get("suggested_turnover")),
    )
    qp.remaining_turnover = max(Decimal("0"), qp.suggested_turnover - qp.current_turnover)
    return qp


# ─── Сборка плана ────────────────────────────────────────────────────────────


def build(
    reports_dir: str | Path,
    as_of: date | None = None,
    instrument: str | None = None,
    mode: str = "roundtrip",
    commission_bps_cli: Decimal | None = None,
    min_trade_rub: Decimal = Decimal("0"),
    max_trade_rub: Decimal = Decimal("0"),
    round_lots: bool = True,
) -> ManualTurnoverPlan:
    out = Path(reports_dir)
    warnings: list[str] = []
    source_files = {name: (out / name).exists() for name in INPUT_FILES}

    plan_path = out / "kval_plan.json"
    scan_path = out / "instrument_scan.json"
    if not plan_path.exists():
        raise TurnoverPlanError(
            "Не найден kval_plan.json. Выполните по порядку: "
            "1) kval-status, 2) kval-plan, 3) instrument-scan."
        )
    if not scan_path.exists():
        raise TurnoverPlanError(
            "Не найден instrument_scan.json. Сначала выполните instrument-scan "
            "(а до него kval-status и kval-plan)."
        )

    plan = _load_json(plan_path)
    scan = _load_json(scan_path)
    as_of = as_of or date.today()

    # Инструмент
    sel_raw, sel_warns = select_instrument(scan, instrument)
    warnings.extend(sel_warns)
    selected = _to_selected(sel_raw)

    # Комиссия: instrument_scan.json → CLI → 0 + warning
    commission_bps = _dec(scan.get("commission_bps"))
    if commission_bps is None:
        commission_bps = commission_bps_cli
    if commission_bps is None:
        commission_bps = Decimal("0")
        warnings.append("commission_bps не задан — издержки учитывают только спред.")

    # Период / проверка
    period = plan.get("earliest_possible_period") or {}
    period_start = str(period.get("start") or "")
    period_end = str(period.get("end") or "")
    check_date = str(plan.get("earliest_possible_check_date") or "")

    monthly = plan.get("monthly_plan") or []
    quarterly = plan.get("quarterly_plan") or []

    month_entry = _pick_month(monthly, as_of)
    if not month_entry:
        raise TurnoverPlanError(
            "В kval_plan.json нет месяцев для планирования. Перезапустите kval-plan."
        )
    current_month = _month_plan_from(month_entry, mode, min_trade_rub, max_trade_rub)
    current_quarter = _quarter_plan_from(_pick_quarter(quarterly, as_of))

    # Целевой месячный оборот = suggested_turnover текущего месяца
    target_monthly_turnover = current_month.suggested_turnover

    # Рекомендации (по выбранному режиму)
    recs = Recommendations(mode=mode, disclaimer=DISCLAIMER)
    m = current_month
    recs.broker_trade_count_required = m.planned_required_trade_count
    recs.broker_trade_count_current = m.current_trade_count
    recs.broker_trade_count_missing = m.missing_trade_count

    if m.missing_trade_count <= 0:
        recs.trade_plan_closed = True
        recs.note = "Месячный trade-план закрыт: недостающих сделок нет."
        recs.total_planned_turnover = Decimal("0")
        recs.expected_broker_trades_after_execution = m.current_trade_count
    else:
        recs.recommended_trade_turnover = m.recommended_turnover_per_missing_trade
        if mode == "roundtrip":
            cycles = ceil(m.missing_trade_count / 2)
            recs.roundtrip_cycle_count_required = cycles
            recs.side_notional = _round(m.remaining_turnover / (cycles * 2))
            recs.cycle_turnover = _round(recs.side_notional * 2)
            recs.recommended_roundtrip_side_notional = recs.side_notional
            recs.note = ("roundtrip BUY+SELL: 1 цикл = 2 broker trades; "
                         "номинал на сторону = оборот / (циклы*2)")
            sides = cycles * 2
        else:
            recs.roundtrip_cycle_count_required = 0
            recs.side_notional = recs.recommended_trade_turnover
            recs.recommended_roundtrip_side_notional = Decimal("0")
            recs.note = "gross: каждая ручная сделка — отдельное turnover-событие"
            sides = m.missing_trade_count
        recs.total_planned_turnover = m.remaining_turnover
        recs.expected_broker_trades_after_execution = m.current_trade_count + sides

        if round_lots:
            price = selected.ask_best or selected.bid_best
            lot_value = (price * selected.lot) if price else None
            side = (recs.side_notional if mode == "roundtrip"
                    else recs.recommended_trade_turnover)
            if lot_value and lot_value > 0 and side > 0:
                recs.recommended_side_lots = max(
                    1, int((side / lot_value).to_integral_value(rounding=ROUND_HALF_UP)))

    recs.expected_turnover_after_execution = _round(
        m.current_turnover + recs.total_planned_turnover)

    months_csv = [
        _month_plan_from(m, mode, min_trade_rub, max_trade_rub) for m in monthly
    ]

    logger.info(
        f"Manual turnover plan: as_of={as_of}, инструмент={selected.ticker}, "
        f"режим={mode}, месяц={current_month.month}, "
        f"осталось оборота={current_month.remaining_turnover} ₽"
    )

    return ManualTurnoverPlan(
        as_of=as_of,
        period_policy=str(plan.get("period_policy", "")),
        period_kind=str(plan.get("period_kind", "")),
        period_start=period_start, period_end=period_end, check_date=check_date,
        target_monthly_turnover=target_monthly_turnover,
        commission_bps=commission_bps, mode=mode,
        selected_instrument=selected, current_month_plan=current_month,
        current_quarter_plan=current_quarter, recommendations=recs,
        months_csv=months_csv, warnings=warnings, source_files=source_files,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
