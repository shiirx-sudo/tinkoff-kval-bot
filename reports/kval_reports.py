"""
Сборка выходных отчётов этапа 1 из KvalProgress:
  kval_progress.json, kval_progress.csv, kval_accounts.csv,
  kval_trades.csv, kval_quarters.csv, broker_sync_status.csv
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from common.helpers import mask_identifier
from modules.kval_tracker import KvalProgress
from reports.output_contract import (
    report_metadata,
    write_report_csv,
    write_report_json,
    write_report_jsonl,
)

BROKER = "tinkoff_invest"


def _progress_row(p: KvalProgress) -> dict[str, Any]:
    from modules.period_calculator import PERIOD_POLICY, PERIOD_POLICY_NOTE
    return {
        "date": date.today().isoformat(),
        "period_policy": PERIOD_POLICY,
        "period_kind": "official_fact",
        "as_of": p.period.as_of_date.isoformat(),
        "current_quarter_included": False,
        "note": PERIOD_POLICY_NOTE,
        "period_start": p.period.start.isoformat(),
        "period_end": p.period.end.isoformat(),
        "quarters": ",".join(q.label for q in p.period.quarters),
        "current_quarter": p.period.current_quarter.label,
        "total_turnover": p.total_turnover,
        "target": p.target,
        "effective_target": p.effective_target,
        "progress_pct": p.progress_pct,
        "remaining_to_target": p.remaining_to_target,
        "remaining_to_effective": p.remaining_to_effective,
        "achieved": p.achieved,
        "achieved_bare": p.achieved_bare,
        "turnover_ok": p.turnover_ok,
        "months_ok": p.months_ok,
        "quarters_ok": p.quarters_ok,
        "qualification_ready": p.qualification_ready,
        "operation_count": p.total_operation_count,
        "trade_count": p.total_trade_count,
        "exact_trade_count": p.total_exact_trade_count,
        "approximate_trade_count": p.total_approximate_trade_count,
        "approximate_warning_count": p.approximate_warning_count,
        "generated_at_utc": report_metadata()["generated_at_utc"],
    }


def _accounts_rows(p: KvalProgress) -> list[dict[str, Any]]:
    today = date.today().isoformat()
    return [{
        "date": today,
        "broker": BROKER,
        "account_id_masked": mask_identifier(a.account_id),
        "account_name": a.account_name,
        "account_type": a.account_type,
        "total_turnover": a.total_turnover,
        "operation_count": a.operation_count,
        "trade_count": a.trade_count,
        "exact_trade_count": a.exact_trade_count,
        "approximate_trade_count": a.approximate_trade_count,
    } for a in p.accounts]


def _trades_rows(p: KvalProgress) -> list[dict[str, Any]]:
    today = date.today().isoformat()
    rows: list[dict[str, Any]] = []
    for t in p.all_trades:
        rows.append({
            "date": today,
            "broker": BROKER,
            "account_id_masked": mask_identifier(t.account_id),
            "operation_id": t.operation_id,
            "op_date": t.date,
            "instrument_uid": t.instrument_uid,
            "ticker": t.ticker,
            "instrument_name": t.instrument_name,
            "figi": t.figi,
            "instrument_type": t.instrument_type,
            "direction": t.direction,
            "price": t.price,
            "quantity": t.quantity,
            "turnover": t.turnover,
            "is_approximate": t.is_approximate,
            "raw_payment": t.raw_payment,
        })
    return rows


def _quarters_rows(p: KvalProgress) -> list[dict[str, Any]]:
    today = date.today().isoformat()
    rows: list[dict[str, Any]] = []
    for a in p.accounts:
        for label, qt in a.by_quarter.items():
            rows.append({
                "date": today,
                "account_id_masked": mask_identifier(a.account_id),
                "quarter": label,
                "turnover": qt.turnover,
                "operation_count": qt.operation_count,
                "trade_count": qt.trade_count,
            })
    return rows


def _months_rows(p: KvalProgress) -> list[dict[str, Any]]:
    today = date.today().isoformat()
    return [{
        "date": today,
        "month": m.label,
        "operation_count": m.operation_count,
        "trade_count": m.trade_count,
        "turnover": m.turnover,
        "status": "ok" if m.ok else "fail",
    } for m in p.months]


def _monthly_status(p: KvalProgress) -> list[dict[str, Any]]:
    return [{
        "month": m.label,
        "operation_count": m.operation_count,
        "trade_count": m.trade_count,
        "turnover": m.turnover,
        "ok": m.ok,
    } for m in p.months]


def _quarterly_status(p: KvalProgress) -> list[dict[str, Any]]:
    return [{
        "quarter": q.label,
        "operation_count": q.operation_count,
        "trade_count": q.trade_count,
        "turnover": q.turnover,
        "ok": q.ok,
    } for q in p.quarter_checks]


def broker_sync_status_rows(
    p: KvalProgress | None,
    *,
    connection_status: str,
    sync_status: str,
    error_message: str = "",
    accounts_count: int = 0,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    meta = report_metadata()
    return [{
        "date": date.today().isoformat(),
        "broker": BROKER,
        "enabled": enabled,
        "dry_run": True,
        "connection_status": connection_status,
        "accounts_count": len(p.accounts) if p else accounts_count,
        "operations_count": p.total_operation_count if p else 0,
        "trade_count": p.total_trade_count if p else 0,
        "sync_status": sync_status,
        "error_message": error_message,
        "synced_at_utc": meta["generated_at_utc"],
    }]


def write_all(p: KvalProgress, reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    """Пишет все отчёты этапа 1 в reports_dir. Возвращает {name: path}."""
    out = Path(reports_dir)
    written: dict[str, Path] = {}

    progress_row = _progress_row(p)
    json_payload = {
        **progress_row,
        "monthly_status": _monthly_status(p),
        "quarterly_status": _quarterly_status(p),
        **report_metadata(),
    }
    written["kval_progress.json"] = write_report_json(
        json_payload, out / "kval_progress.json",
    )
    written["kval_progress.csv"] = write_report_csv(
        [progress_row], "kval_progress", out / "kval_progress.csv"
    )
    written["kval_accounts.csv"] = write_report_csv(
        _accounts_rows(p), "kval_accounts", out / "kval_accounts.csv"
    )
    written["kval_trades.csv"] = write_report_csv(
        _trades_rows(p), "kval_trades", out / "kval_trades.csv"
    )
    written["kval_months.csv"] = write_report_csv(
        _months_rows(p), "kval_months", out / "kval_months.csv"
    )
    written["kval_quarters.csv"] = write_report_csv(
        _quarters_rows(p), "kval_quarters", out / "kval_quarters.csv"
    )
    written["broker_sync_status.csv"] = write_report_csv(
        broker_sync_status_rows(p, connection_status="connected", sync_status="ok"),
        "broker_sync_status", out / "broker_sync_status.csv",
    )
    written["kval_operations_raw.jsonl"] = write_report_jsonl(
        p.raw_operations, out / "kval_operations_raw.jsonl"
    )
    return written
