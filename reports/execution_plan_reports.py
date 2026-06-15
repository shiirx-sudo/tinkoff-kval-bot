"""Отчёты Execution Plan (DRY-RUN): execution_plan.json + .csv."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from modules.execution_planner import ExecutionPlan, PlannedAction
from reports.output_contract import (
    report_metadata,
    write_report_csv,
    write_report_json,
)


def _action_row(a: PlannedAction) -> dict[str, Any]:
    return {
        "seq": a.seq,
        "side": a.side,
        "ticker": a.ticker,
        "class_code": a.class_code,
        "notional_rub": a.notional_rub,
        "estimated_lots": a.estimated_lots,
        "estimated_price": a.estimated_price,
        "expected_turnover_contribution": a.expected_turnover_contribution,
        "dry_run": a.dry_run,
    }


def write_all(p: ExecutionPlan, reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    out = Path(reports_dir)
    written: dict[str, Path] = {}

    rows = [_action_row(a) for a in p.planned_actions]

    payload = {
        "as_of": p.as_of.isoformat(),
        "period": p.period,
        "instrument": {
            "ticker": p.ticker, "name": p.name, "class_code": p.class_code,
            "trading_status": p.trading_status, "verdict": p.verdict,
        },
        "mode": p.mode,
        "commission_bps": p.commission_bps,
        "broker_trade_count_required": p.broker_trade_count_required,
        "broker_trade_count_current": p.broker_trade_count_current,
        "broker_trade_count_missing": p.broker_trade_count_missing,
        "roundtrip_cycle_count_required": p.roundtrip_cycle_count_required,
        "side_notional": p.side_notional,
        "cycle_turnover": p.cycle_turnover,
        "total_turnover": p.total_turnover,
        "expected_broker_trades_after_execution": p.expected_broker_trades_after_execution,
        "expected_turnover_after_execution": p.expected_turnover_after_execution,
        "planned_actions": [asdict(a) for a in p.planned_actions],
        "risk_checks": [asdict(c) for c in p.risk_checks],
        "status": p.status,
        "warnings": p.warnings,
        "dry_run": p.dry_run,
        "disclaimer": p.disclaimer,
        **report_metadata(),
    }

    written["execution_plan.json"] = write_report_json(
        payload, out / "execution_plan.json")
    written["execution_plan.csv"] = write_report_csv(
        rows, "execution_plan", out / "execution_plan.csv")
    return written
