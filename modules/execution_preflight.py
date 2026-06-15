"""
Execution Preflight — READ-ONLY проверка готовности dry-run плана.

Ничего не исполняет и не отправляет заявок. Перечитывает/пересобирает
execution-plan, проверяет лимиты и данные, и статически убеждается, что в
кодовой базе нет order-endpoints или live-адаптера. Итог: READY_DRY_RUN /
BLOCKED / STALE_REPORTS / MISSING_REPORTS.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from loguru import logger

from modules.execution_planner import ExecutionPlanError
from modules.execution_planner import build as build_execution_plan
from modules.turnover_planner import _dec, select_instrument

REQUIRED_REPORTS = ("kval_plan.json", "instrument_scan.json", "execution_plan.json")

# Каталоги продакшн-кода для статической проверки (без tests/).
_SCAN_DIRS = ("brokers", "modules", "api")
_SCAN_FILES = ("main.py",)

# Токены собраны из фрагментов, поэтому соответствующих литералов в этом файле
# нет — статическая проверка и тесты не ловят сам preflight как ложный плюс.
_ORDER_TOKENS = (
    "place" "_limit_" "order",
    "place" "_order",
    "submit" "_order",
    "cancel" "_order",
    "Orders" "Service",
)
_ADAPTER_TOKENS = (
    "order" "_client",
    "LIVE_" "EXECUTION_" "ENABLED",
    "live" "_order",
)


class PreflightError(Exception):
    """Понятная ошибка для пользователя (без traceback)."""


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str = ""
    blocking: bool = True


@dataclass
class PreflightResult:
    as_of: date
    status: str                       # READY_DRY_RUN|BLOCKED|STALE_REPORTS|MISSING_REPORTS
    instrument: str
    class_code: str
    trading_status: str
    verdict: str
    period: str
    broker_trade_count_missing: int
    roundtrip_cycle_count_required: int
    side_notional: Decimal
    planned_actions_count: int
    checks: list[PreflightCheck]
    warnings: list[str]
    errors: list[str]
    source_reports: dict[str, bool]
    generated_at: str


def _scan_codebase() -> tuple[bool, bool, list[str]]:
    """Статически ищет order-endpoints / live-адаптер в продакшн-коде."""
    found_order: list[str] = []
    found_adapter: list[str] = []
    paths: list[Path] = []
    for d in _SCAN_DIRS:
        p = Path(d)
        if p.exists():
            paths.extend(p.rglob("*.py"))
    for f in _SCAN_FILES:
        if Path(f).exists():
            paths.append(Path(f))

    for path in paths:
        # сам чекер не сканируем (он только перечисляет токены для поиска)
        if path.name == "execution_preflight.py":
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        for tok in _ORDER_TOKENS:
            if tok in src:
                found_order.append(f"{path}:{tok}")
        for tok in _ADAPTER_TOKENS:
            if tok in src:
                found_adapter.append(f"{path}:{tok}")

    return (not found_order, not found_adapter, found_order + found_adapter)


def _is_stale(reports_dir: Path, saved: dict, plan) -> tuple[bool, str]:
    """План на диске устарел относительно входных отчётов?"""
    ep = reports_dir / "execution_plan.json"
    try:
        ep_m = os.path.getmtime(ep)
        newest_input = max(
            os.path.getmtime(reports_dir / "kval_plan.json"),
            os.path.getmtime(reports_dir / "instrument_scan.json"),
        )
        if ep_m < newest_input:
            return True, "execution_plan.json старше входных отчётов"
    except OSError:
        pass

    try:
        if Decimal(str(saved.get("side_notional"))) != plan.side_notional:
            return True, "side_notional на диске не совпадает с пересчётом"
        if int(saved.get("broker_trade_count_missing") or -1) != plan.broker_trade_count_missing:
            return True, "broker_trade_count_missing не совпадает"
        if len(saved.get("planned_actions") or []) != len(plan.planned_actions):
            return True, "число planned_actions не совпадает"
    except Exception:  # noqa: BLE001
        return True, "не удалось сверить сохранённый execution_plan.json"
    return False, ""


def run(
    reports_dir: str | Path,
    as_of: date | None = None,
    instrument: str = "LQDT",
    mode: str = "roundtrip",
    commission_bps: Decimal | None = None,
    max_side_notional_rub: Decimal = Decimal("0"),
    spread_bps_limit: Decimal = Decimal("5"),
    min_depth_multiplier: Decimal = Decimal("1.2"),
) -> PreflightResult:
    out = Path(reports_dir)
    as_of = as_of or date.today()
    warnings: list[str] = []
    errors: list[str] = []
    source = {name: (out / name).exists() for name in REQUIRED_REPORTS}

    def _empty(status: str) -> PreflightResult:
        return PreflightResult(
            as_of=as_of, status=status, instrument=instrument, class_code="",
            trading_status="", verdict="", period="",
            broker_trade_count_missing=0, roundtrip_cycle_count_required=0,
            side_notional=Decimal("0"), planned_actions_count=0,
            checks=[], warnings=warnings, errors=errors,
            source_reports=source, generated_at=datetime.now(timezone.utc).isoformat(),
        )

    missing = [n for n, ok in source.items() if not ok]
    if missing:
        errors.append(
            "Отсутствуют отчёты: " + ", ".join(missing) + ". Выполните по порядку: "
            "1) kval-status, 2) kval-plan, 3) instrument-scan, 4) execution-plan."
        )
        return _empty("MISSING_REPORTS")

    # Пересборка плана (read-only) + сырой инструмент для проверки глубины
    try:
        plan = build_execution_plan(
            reports_dir=out, as_of=as_of, instrument=instrument, mode=mode,
            commission_bps=commission_bps,
            max_side_notional_rub=max_side_notional_rub,
            spread_bps_limit=spread_bps_limit,
        )
        scan = json.loads((out / "instrument_scan.json").read_text(encoding="utf-8"))
        sel, _ = select_instrument(scan, instrument)
        saved = json.loads((out / "execution_plan.json").read_text(encoding="utf-8"))
    except ExecutionPlanError as exc:
        errors.append(str(exc))
        return _empty("BLOCKED")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Не удалось пересобрать план: {exc}")
        return _empty("BLOCKED")

    spread_bps = _dec(sel.get("spread_bps"))
    min_depth = _dec(sel.get("min_side_top_depth_rub"))
    side = plan.side_notional

    checks: list[PreflightCheck] = []

    def add(name, ok, detail="", blocking=True):
        checks.append(PreflightCheck(name=name, ok=bool(ok), detail=detail,
                                     blocking=blocking))

    add("instrument_selected", plan.ticker.upper() == instrument.upper(),
        f"ticker={plan.ticker}")
    add("class_code_spbru", plan.class_code == "SPBRU",
        f"class_code={plan.class_code}", blocking=False)
    if plan.class_code != "SPBRU":
        warnings.append(f"class_code={plan.class_code} (ожидался SPBRU)")
    add("verdict_good", plan.verdict == "GOOD", f"verdict={plan.verdict}")
    add("trading_status_normal",
        plan.trading_status == "SECURITY_TRADING_STATUS_NORMAL_TRADING",
        f"trading_status={plan.trading_status or 'UNKNOWN'}")

    spread_ok = spread_bps is not None and spread_bps <= spread_bps_limit
    add("spread_within_limit", spread_ok,
        f"spread_bps={spread_bps}, limit={spread_bps_limit}")

    need_depth = side * min_depth_multiplier
    depth_ok = min_depth is not None and side > 0 and min_depth >= need_depth
    add("depth_sufficient", depth_ok,
        f"min_side_depth={min_depth} >= side*{min_depth_multiplier}={need_depth}")

    if max_side_notional_rub and max_side_notional_rub > 0:
        add("side_within_max", side <= max_side_notional_rub,
            f"side_notional={side}, max={max_side_notional_rub}")
    else:
        warnings.append("max-side-notional-rub=0 — лимит стороны не задан.")
        add("side_within_max", False, "max-side-notional-rub не задан")

    add("planned_actions_nonempty", len(plan.planned_actions) > 0,
        f"count={len(plan.planned_actions)}")
    add("actions_are_dry_run", all(a.dry_run for a in plan.planned_actions),
        "все planned_actions dry_run=true")

    order_clean, adapter_clean, found = _scan_codebase()
    add("no_order_endpoints", order_clean,
        "order-endpoints не найдены" if order_clean else f"найдено: {found}")
    add("no_live_adapter", adapter_clean,
        "live-адаптер не найден" if adapter_clean else f"найдено: {found}")

    # Несовпадение с сохранённым планом → STALE
    stale, stale_reason = _is_stale(out, saved, plan)
    if stale:
        warnings.append(
            f"execution_plan.json устарел ({stale_reason}); перезапустите execution-plan.")

    hard_fail = any(c.blocking and not c.ok for c in checks)
    if hard_fail:
        status = "BLOCKED"
        for c in checks:
            if c.blocking and not c.ok:
                errors.append(f"{c.name}: {c.detail}")
    elif stale:
        status = "STALE_REPORTS"
    else:
        status = "READY_DRY_RUN"

    logger.info(
        f"Execution preflight (READ-ONLY): {plan.ticker} {plan.period} "
        f"side={side} ₽ status={status}"
    )

    return PreflightResult(
        as_of=as_of, status=status, instrument=plan.ticker,
        class_code=plan.class_code, trading_status=plan.trading_status,
        verdict=plan.verdict, period=plan.period,
        broker_trade_count_missing=plan.broker_trade_count_missing,
        roundtrip_cycle_count_required=plan.roundtrip_cycle_count_required,
        side_notional=side, planned_actions_count=len(plan.planned_actions),
        checks=checks, warnings=warnings, errors=errors, source_reports=source,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
