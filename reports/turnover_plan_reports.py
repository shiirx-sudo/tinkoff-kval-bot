"""Отчёты Manual Turnover Plan: manual_turnover_plan.json + .csv."""
from __future__ import annotations

from dataclasses import asdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from modules.turnover_planner import ManualTurnoverPlan, MonthPlan
from reports.output_contract import (
    report_metadata,
    write_report_csv,
    write_report_json,
)


def _est_cost(suggested: Decimal, roundtrip_bps: Decimal | None) -> Decimal:
    if not roundtrip_bps:
        return Decimal("0")
    return (suggested * roundtrip_bps / 10000).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def _month_row(m: MonthPlan, p: ManualTurnoverPlan) -> dict[str, Any]:
    warning = ("месячный trade-план закрыт" if m.missing_trade_count <= 0 else "")
    return {
        "month": m.month,
        "status": m.status,
        "planned_required_trade_count": m.planned_required_trade_count,
        "current_trade_count": m.current_trade_count,
        "missing_trade_count": m.missing_trade_count,
        "suggested_turnover": m.suggested_turnover,
        "current_turnover": m.current_turnover,
        "remaining_turnover": m.remaining_turnover,
        "recommended_trade_turnover": m.recommended_turnover_per_missing_trade,
        "recommended_roundtrip_side_notional": m.recommended_roundtrip_side_notional,
        "selected_ticker": p.selected_instrument.ticker,
        "selected_instrument_name": p.selected_instrument.name,
        "estimated_roundtrip_cost_bps": p.selected_instrument.estimated_roundtrip_cost_bps,
        "estimated_cost_rub": _est_cost(
            m.suggested_turnover, p.selected_instrument.estimated_roundtrip_cost_bps),
        "warning": warning,
    }


def write_all(p: ManualTurnoverPlan, reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    out = Path(reports_dir)
    written: dict[str, Path] = {}

    rows = [_month_row(m, p) for m in p.months_csv]

    payload = {
        "as_of": p.as_of.isoformat(),
        "period_policy": p.period_policy,
        "period_kind": p.period_kind,
        "check_date": p.check_date,
        "period": {"start": p.period_start, "end": p.period_end},
        "target_monthly_turnover": p.target_monthly_turnover,
        "commission_bps": p.commission_bps,
        "mode": p.mode,
        "selected_instrument": asdict(p.selected_instrument),
        "current_month_plan": asdict(p.current_month_plan),
        "current_quarter_plan": asdict(p.current_quarter_plan)
        if p.current_quarter_plan else None,
        "recommendations": asdict(p.recommendations),
        "warnings": p.warnings,
        "source_files": p.source_files,
        "disclaimer": p.disclaimer,
        **report_metadata(),
    }

    written["manual_turnover_plan.json"] = write_report_json(
        payload, out / "manual_turnover_plan.json")
    written["manual_turnover_plan.csv"] = write_report_csv(
        rows, "manual_turnover_plan", out / "manual_turnover_plan.csv")
    return written
