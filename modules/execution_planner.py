"""
Execution Planner — DRY-RUN модель автоматического набора оборота.

ВАЖНО: этот модуль НИЧЕГО не покупает и не продаёт. Он только строит план
будущих BUY/SELL действий и проверки рисков, чтобы перед возможным live-этапом
не ошибиться в числе сделок, roundtrip-циклов и номинале. Реальные заявки не
отправляются: здесь нет вызовов размещения/отмены заявок, нет изменения
портфеля, нет обязательного full-доступа. dry_run всегда включён.

Live-исполнение в этом этапе НЕ реализовано. Подключение реального адаптера —
отдельный будущий шаг, только после проверки dry-run и явного включения.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from math import ceil
from pathlib import Path

from loguru import logger

from modules.turnover_planner import (
    _dec,
    _load_json,
    _month_plan_from,
    _pick_month,
    _round,
    select_instrument,
)

DISCLAIMER = "Это dry-run. Реальные заявки не отправляются."


class ExecutionPlanError(Exception):
    """Понятная ошибка для пользователя (без traceback)."""


@dataclass
class PlannedAction:
    seq: int
    side: str               # BUY | SELL
    ticker: str
    class_code: str
    notional_rub: Decimal
    estimated_lots: int | None
    estimated_price: Decimal | None
    expected_turnover_contribution: Decimal
    dry_run: bool = True


@dataclass
class RiskCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Sizing:
    mode: str = "fixed"
    available_cash_rub: Decimal | None = None
    cash_reserve_rub: Decimal = Decimal("0")
    utilization_pct: Decimal = Decimal("0")
    usable_side_cap_rub: Decimal = Decimal("0")
    monthly_turnover_needed: Decimal = Decimal("0")
    actions_by_turnover: int = 0
    actions_by_rules: int = 0
    planned_actions: int = 0
    projected_total_trades: int = 0
    kval_min_total_trades: int = 41
    kval_target_total_trades: int = 48


def _compute_balance_sizing(
    monthly_needed: Decimal, mode: str, *,
    available_cash_rub: Decimal | None, reserve: Decimal, utilization: Decimal,
    max_side: Decimal, min_side_depth: Decimal | None, min_depth_multiplier: Decimal,
    min_monthly_actions: int, missing: int,
    kval_min_total_trades: int, kval_target_total_trades: int,
    max_monthly_actions: int = 0,
) -> tuple[int, int, Decimal, Sizing, list[RiskCheck]]:
    """Balance-adaptive расчёт. Возвращает (sides, cycles, side_notional, sizing, checks)."""
    sizing = Sizing(
        mode="balance", available_cash_rub=available_cash_rub, cash_reserve_rub=reserve,
        utilization_pct=utilization, monthly_turnover_needed=monthly_needed,
        kval_min_total_trades=kval_min_total_trades,
        kval_target_total_trades=kval_target_total_trades,
    )
    checks: list[RiskCheck] = []
    present = available_cash_rub is not None
    checks.append(RiskCheck("available_cash_present", present,
                            f"available_cash={available_cash_rub}"))

    def _fail(detail: str) -> tuple[int, int, Decimal, Sizing, list[RiskCheck]]:
        checks.extend([
            RiskCheck("side_notional_within_balance", False, detail),
            RiskCheck("reserve_preserved", False, detail),
            RiskCheck("min_monthly_actions_met", False, detail),
            RiskCheck("min_total_trades_met", False, detail),
        ])
        return 0, 0, Decimal("0"), sizing, checks

    if not present:
        return _fail("нет данных о свободном балансе")

    # Нечего исполнять — оборот уже набран
    if monthly_needed <= 0:
        checks.extend([
            RiskCheck("side_notional_within_balance", True, "оборот набран"),
            RiskCheck("reserve_preserved", True, "оборот набран"),
            RiskCheck("min_monthly_actions_met", True, "оборот набран"),
            RiskCheck("min_total_trades_met", True, "оборот набран"),
        ])
        return 0, 0, Decimal("0"), sizing, checks

    usable = max(Decimal("0"), available_cash_rub - reserve)
    cap_balance = usable * utilization
    caps = [cap_balance]
    if max_side and max_side > 0:
        caps.append(max_side)
    if min_side_depth is not None and min_depth_multiplier > 0:
        caps.append(min_side_depth / min_depth_multiplier)
    side_cap = min(caps)
    sizing.usable_side_cap_rub = _round(side_cap if side_cap > 0 else Decimal("0"))

    if side_cap <= 0:
        return _fail(f"side_cap<=0 (usable={usable}, reserve={reserve})")

    actions_by_turnover = ceil(monthly_needed / side_cap)
    actions_by_rules = max(min_monthly_actions, missing)
    planned = max(actions_by_turnover, actions_by_rules)
    if mode == "roundtrip" and planned % 2 == 1:
        planned += 1

    side_notional = _round(monthly_needed / planned)
    sides = planned
    cycles = planned // 2 if mode == "roundtrip" else 0
    projected_total = planned * 12

    sizing.actions_by_turnover = actions_by_turnover
    sizing.actions_by_rules = actions_by_rules
    sizing.planned_actions = planned
    sizing.projected_total_trades = projected_total

    within = side_notional <= usable
    reserve_ok = (available_cash_rub - side_notional) >= reserve
    checks.extend([
        RiskCheck("side_notional_within_balance", within,
                  f"side={side_notional}, usable={usable}"),
        RiskCheck("reserve_preserved", reserve_ok,
                  f"available-side={available_cash_rub - side_notional}, reserve={reserve}"),
        RiskCheck("min_monthly_actions_met", planned >= min_monthly_actions,
                  f"planned={planned}, min={min_monthly_actions}"),
        RiskCheck("min_total_trades_met", projected_total >= kval_min_total_trades,
                  f"projected={projected_total}, min={kval_min_total_trades}, "
                  f"target={kval_target_total_trades}"),
    ])
    if max_monthly_actions and max_monthly_actions > 0:
        checks.append(RiskCheck(
            "max_monthly_actions_ok", planned <= max_monthly_actions,
            f"planned_actions={planned}, max_monthly_actions={max_monthly_actions}"))
    return sides, cycles, side_notional, sizing, checks


@dataclass
class ExecutionPlan:
    as_of: date
    period: str
    ticker: str
    name: str
    class_code: str
    trading_status: str
    verdict: str
    mode: str
    commission_bps: Decimal
    broker_trade_count_required: int
    broker_trade_count_current: int
    broker_trade_count_missing: int
    roundtrip_cycle_count_required: int
    side_notional: Decimal
    cycle_turnover: Decimal
    total_turnover: Decimal
    expected_broker_trades_after_execution: int
    expected_turnover_after_execution: Decimal
    planned_actions: list[PlannedAction]
    risk_checks: list[RiskCheck]
    status: str             # OK | BLOCKED
    warnings: list[str]
    generated_at: str
    sizing: Sizing | None = None
    dry_run: bool = True
    disclaimer: str = DISCLAIMER


def build(
    reports_dir: str | Path,
    as_of: date | None = None,
    instrument: str | None = "LQDT",
    mode: str = "roundtrip",
    commission_bps: Decimal | None = None,
    max_side_notional_rub: Decimal = Decimal("0"),
    min_side_notional_rub: Decimal = Decimal("0"),
    spread_bps_limit: Decimal = Decimal("5"),
    dry_run: bool = True,
    size_mode: str = "fixed",
    available_cash_rub: Decimal | None = None,
    balance_utilization_pct: Decimal = Decimal("0.80"),
    min_cash_reserve_rub: Decimal = Decimal("5000"),
    min_monthly_actions: int = 4,
    min_depth_multiplier: Decimal = Decimal("1.2"),
    kval_min_total_trades: int = 41,
    kval_target_total_trades: int = 48,
    max_monthly_actions: int = 0,
) -> ExecutionPlan:
    out = Path(reports_dir)
    warnings: list[str] = []

    # Жёсткая гарантия: live-исполнение не реализовано — всегда dry-run.
    if not dry_run:
        warnings.append(
            "Live-исполнение не реализовано на этом этапе — план построен как "
            "dry-run, реальные заявки не отправляются."
        )
    dry_run = True

    plan_path = out / "kval_plan.json"
    scan_path = out / "instrument_scan.json"
    if not plan_path.exists():
        raise ExecutionPlanError(
            "Не найден kval_plan.json. Выполните по порядку: "
            "1) kval-status, 2) kval-plan, 3) instrument-scan."
        )
    if not scan_path.exists():
        raise ExecutionPlanError(
            "Не найден instrument_scan.json. Сначала выполните instrument-scan."
        )

    plan = _load_json(plan_path)
    scan = _load_json(scan_path)
    as_of = as_of or date.today()

    sel, sel_warns = select_instrument(scan, instrument)
    warnings.extend(sel_warns)

    ticker = str(sel.get("ticker", ""))
    name = str(sel.get("name", ""))
    class_code = str(sel.get("resolved_class_code") or sel.get("class_code") or "")
    verdict = str(sel.get("verdict", ""))
    trading_status = str(sel.get("trading_status", ""))
    trading_ok = bool(sel.get("trading_status_ok"))
    data_ok = bool(sel.get("data_ok"))
    spread_bps = _dec(sel.get("spread_bps"))
    min_side_depth = _dec(sel.get("min_side_top_depth_rub"))
    ask = _dec(sel.get("ask_best"))
    bid = _dec(sel.get("bid_best"))
    lot = int(sel.get("lot") or 1)

    # Комиссия: instrument_scan.json → CLI → 0 + warning
    commission = _dec(scan.get("commission_bps"))
    if commission is None:
        commission = commission_bps
    if commission is None:
        commission = Decimal("0")
        warnings.append("commission_bps не задан — издержки учитывают только спред.")

    month_entry = _pick_month(plan.get("monthly_plan") or [], as_of)
    if not month_entry:
        raise ExecutionPlanError(
            "В kval_plan.json нет месяца для планирования. Перезапустите kval-plan."
        )
    month = _month_plan_from(month_entry, mode, Decimal("0"), Decimal("0"))
    missing = month.missing_trade_count
    remaining = month.remaining_turnover

    sizing: Sizing | None = None
    balance_checks: list[RiskCheck] = []

    if size_mode == "balance":
        sides, cycles, side_notional, sizing, balance_checks = _compute_balance_sizing(
            remaining, mode,
            available_cash_rub=available_cash_rub, reserve=min_cash_reserve_rub,
            utilization=balance_utilization_pct, max_side=max_side_notional_rub,
            min_side_depth=min_side_depth, min_depth_multiplier=min_depth_multiplier,
            min_monthly_actions=min_monthly_actions, missing=missing,
            kval_min_total_trades=kval_min_total_trades,
            kval_target_total_trades=kval_target_total_trades,
            max_monthly_actions=max_monthly_actions,
        )
        cycle_turnover = _round(side_notional * 2) if mode == "roundtrip" else Decimal("0")
        total_turnover = _round(side_notional * sides) if sides > 0 else Decimal("0")
    else:
        # Циклы и номинал стороны (fixed: от недостающих broker trades)
        if mode == "roundtrip":
            cycles = ceil(missing / 2) if missing > 0 else 0
            sides = cycles * 2
            if missing > 0 and missing % 2 == 1:
                warnings.append(
                    f"Не хватает {missing} broker trades (нечётное): roundtrip даёт "
                    f"{sides} сделок — на 1 больше минимума."
                )
        else:  # gross
            cycles = 0
            sides = missing
        side_notional = _round(remaining / sides) if sides > 0 else Decimal("0")
        cycle_turnover = _round(side_notional * 2) if mode == "roundtrip" else Decimal("0")
        total_turnover = _round(side_notional * sides) if sides > 0 else Decimal("0")

    # Planned actions (BUY/SELL чередуются, чтобы не накапливать позицию)
    price_buy = ask or bid
    price_sell = bid or ask
    actions: list[PlannedAction] = []
    for i in range(sides):
        side = "BUY" if i % 2 == 0 else "SELL"
        price = price_buy if side == "BUY" else price_sell
        est_lots = None
        if price and price > 0 and lot > 0 and side_notional > 0:
            est_lots = max(1, int(
                (side_notional / (price * lot)).to_integral_value(ROUND_HALF_UP)))
        actions.append(PlannedAction(
            seq=i + 1, side=side, ticker=ticker, class_code=class_code,
            notional_rub=side_notional, estimated_lots=est_lots,
            estimated_price=price, expected_turnover_contribution=side_notional,
            dry_run=True,
        ))

    # Risk checks
    checks: list[RiskCheck] = [
        RiskCheck("instrument_good", verdict == "GOOD", f"verdict={verdict}"),
        RiskCheck("trading_status_normal", trading_ok,
                  f"trading_status={trading_status or 'UNKNOWN'}"),
        RiskCheck("market_data_present", data_ok, f"data_ok={data_ok}"),
    ]
    spread_ok = spread_bps is not None and spread_bps <= spread_bps_limit
    checks.append(RiskCheck(
        "spread_within_limit", spread_ok,
        f"spread_bps={spread_bps}, limit={spread_bps_limit}"))
    if side_notional <= 0:
        # сайзинг дал 0 действий (например, balance: usable<reserve) — глубина
        # стакана тут неприменима, не делаем её причиной BLOCKED.
        checks.append(RiskCheck(
            "depth_sufficient", True,
            "n/a: side_notional=0 (см. balance-проверки)"))
    else:
        depth_ok = min_side_depth is not None and min_side_depth >= side_notional
        checks.append(RiskCheck(
            "depth_sufficient", depth_ok,
            f"min_side_depth={min_side_depth}, side_notional={side_notional}"))

    if max_side_notional_rub and max_side_notional_rub > 0:
        checks.append(RiskCheck(
            "side_within_max", side_notional <= max_side_notional_rub,
            f"side_notional={side_notional}, max={max_side_notional_rub}"))
    else:
        warnings.append(
            "max-side-notional-rub=0 — лимит на размер одной стороны не задан.")

    if (min_side_notional_rub and min_side_notional_rub > 0
            and side_notional > 0 and side_notional < min_side_notional_rub):
        warnings.append(
            f"side_notional {side_notional} меньше min {min_side_notional_rub}.")

    suggested = month.suggested_turnover
    if suggested > 0 and total_turnover > suggested * Decimal("2"):
        checks.append(RiskCheck(
            "monthly_target_overshoot_ok", False,
            f"total={total_turnover} > 2x suggested={suggested}"))

    checks.extend(balance_checks)

    hard_fail = any(not c.ok for c in checks)
    if sides <= 0 and remaining <= 0:
        status = "OK"
        warnings.append("Недостающего оборота нет — исполнять нечего.")
    elif sides <= 0:
        status = "BLOCKED"
    else:
        status = "BLOCKED" if hard_fail else "OK"

    expected_after_trades = month.current_trade_count + sides
    expected_after_turnover = _round(month.current_turnover + total_turnover)

    logger.info(
        f"Execution plan (DRY-RUN): {ticker} {month.month} mode={mode} "
        f"missing={missing} cycles={cycles} side={side_notional} ₽ status={status}"
    )

    return ExecutionPlan(
        as_of=as_of, period=month.month, ticker=ticker, name=name,
        class_code=class_code, trading_status=trading_status, verdict=verdict,
        mode=mode, commission_bps=commission,
        broker_trade_count_required=month.planned_required_trade_count,
        broker_trade_count_current=month.current_trade_count,
        broker_trade_count_missing=missing,
        roundtrip_cycle_count_required=cycles,
        side_notional=side_notional, cycle_turnover=cycle_turnover,
        total_turnover=total_turnover,
        expected_broker_trades_after_execution=expected_after_trades,
        expected_turnover_after_execution=expected_after_turnover,
        planned_actions=actions, risk_checks=checks, status=status,
        warnings=warnings, generated_at=datetime.now(timezone.utc).isoformat(),
        sizing=sizing, dry_run=True,
    )
