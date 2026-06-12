"""Отчёты планировщика: kval_plan.json + три CSV (read-only аналитика)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from modules.kval_planner import (
    CandidateWindow,
    KvalPlan,
    MonthPlan,
    QuarterPlan,
)
from reports.output_contract import (
    report_metadata,
    write_report_csv,
    write_report_json,
)


def _window_row(w: CandidateWindow) -> dict[str, Any]:
    return {
        "check_date": w.check_date.isoformat(),
        "period_start": w.period_start.isoformat(),
        "period_end": w.period_end.isoformat(),
        "included_quarters": ",".join(w.included_quarters),
        "total_turnover": w.total_turnover,
        "remaining_turnover_to_target": w.remaining_turnover_to_target,
        "months_ok": w.months_ok,
        "quarters_ok": w.quarters_ok,
        "turnover_ok": w.turnover_ok,
        "qualification_ready": w.qualification_ready,
        "impossible_due_to_past_gaps": w.impossible_due_to_past_gaps,
        "window_kind": w.window_kind,
    }


def _month_row(m: MonthPlan) -> dict[str, Any]:
    return {
        "month": m.month,
        "status": m.status,
        "current_trade_count": m.current_trade_count,
        "planned_required_trade_count": m.planned_required_trade_count,
        "missing_trade_count": m.missing_trade_count,
        "current_turnover": m.current_turnover,
        "suggested_turnover": m.suggested_turnover,
    }


def _quarter_row(q: QuarterPlan) -> dict[str, Any]:
    return {
        "quarter": q.quarter,
        "current_trade_count": q.current_trade_count,
        "required_min_trade_count": q.required_min_trade_count,
        "missing_trade_count": q.missing_trade_count,
        "current_turnover": q.current_turnover,
        "suggested_turnover": q.suggested_turnover,
        "status": q.status,
    }


def write_all(p: KvalPlan, reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    """Пишет kval_plan.json и три CSV. Возвращает {name: path}."""
    out = Path(reports_dir)
    written: dict[str, Path] = {}

    window_rows = [_window_row(w) for w in p.windows]
    month_rows = [_month_row(m) for m in p.monthly_plan]
    quarter_rows = [_quarter_row(q) for q in p.quarterly_plan]

    earliest_period = None
    earliest_check = None
    if p.earliest is not None:
        earliest_check = p.earliest.check_date.isoformat()
        earliest_period = {
            "start": p.earliest.period_start.isoformat(),
            "end": p.earliest.period_end.isoformat(),
        }

    payload = {
        "as_of": p.as_of.isoformat(),
        "period_policy": p.period_policy,
        "period_kind": p.period_kind,
        "official_status_command_hint":
            "python main.py kval-status --as-of YYYY-MM-DD",
        "target": p.target,
        "effective_target": p.effective_target,
        "goal": p.goal,
        "target_mode": p.target_mode,
        "earliest_possible_check_date": earliest_check,
        "earliest_possible_period": earliest_period,
        "earliest_reason": p.earliest_reason,
        "candidate_windows": window_rows,
        "monthly_plan": month_rows,
        "quarterly_plan": quarter_rows,
        "disclaimer": p.disclaimer,
        **report_metadata(),
    }

    written["kval_plan.json"] = write_report_json(payload, out / "kval_plan.json")
    written["kval_candidate_windows.csv"] = write_report_csv(
        window_rows, "kval_candidate_windows", out / "kval_candidate_windows.csv"
    )
    written["kval_plan_months.csv"] = write_report_csv(
        month_rows, "kval_plan_months", out / "kval_plan_months.csv"
    )
    written["kval_plan_quarters.csv"] = write_report_csv(
        quarter_rows, "kval_plan_quarters", out / "kval_plan_quarters.csv"
    )
    return written
