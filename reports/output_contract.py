"""
Контракт выходных отчётов. Идея перенесена из MOEX Advisor
(reports/output_contract.py), но на stdlib (csv/json) — без pandas.

Гарантии:
  - стабильный порядок колонок per-отчёт;
  - схемафульный пустой отчёт (заголовок есть даже при нуле строк);
  - валидация обязательных колонок;
  - метаданные отчёта (версия, контракт, время генерации).
"""
from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from common.helpers import utc_now

REPORT_VERSION = "0.1.0"
OUTPUT_CONTRACT_VERSION = "2026-06.kval.v1"

# Стабильный порядок колонок по каждому отчёту этапа 1.
REPORT_COLUMN_ORDER: dict[str, list[str]] = {
    "kval_progress": [
        "date", "period_start", "period_end", "quarters", "current_quarter",
        "total_turnover", "target", "effective_target", "progress_pct",
        "remaining_to_target", "remaining_to_effective",
        "achieved", "achieved_bare",
        "turnover_ok", "months_ok", "quarters_ok", "qualification_ready",
        "operation_count", "trade_count", "exact_trade_count",
        "approximate_trade_count", "approximate_warning_count",
        "generated_at_utc",
    ],
    "kval_months": [
        "date", "month", "operation_count", "trade_count", "turnover", "status",
    ],
    "kval_accounts": [
        "date", "broker", "account_id_masked", "account_name", "account_type",
        "total_turnover", "operation_count", "trade_count",
        "exact_trade_count", "approximate_trade_count",
    ],
    "kval_trades": [
        "date", "broker", "account_id_masked", "operation_id", "op_date",
        "instrument_uid", "ticker", "instrument_name", "figi", "instrument_type",
        "direction", "price", "quantity", "turnover", "is_approximate",
        "raw_payment",
    ],
    "kval_quarters": [
        "date", "account_id_masked", "quarter", "turnover",
        "operation_count", "trade_count",
    ],
    "broker_sync_status": [
        "date", "broker", "enabled", "dry_run", "connection_status",
        "accounts_count", "operations_count", "trade_count", "sync_status",
        "error_message", "synced_at_utc",
    ],
    "kval_candidate_windows": [
        "check_date", "period_start", "period_end", "included_quarters",
        "total_turnover", "remaining_turnover_to_target",
        "months_ok", "quarters_ok", "turnover_ok", "qualification_ready",
        "impossible_due_to_past_gaps",
    ],
    "kval_plan_months": [
        "month", "status", "current_trade_count", "required_min_trade_count",
        "missing_trade_count", "current_turnover", "suggested_turnover",
    ],
    "kval_plan_quarters": [
        "quarter", "current_trade_count", "required_min_trade_count",
        "missing_trade_count", "current_turnover", "suggested_turnover", "status",
    ],
}

REQUIRED_COLUMNS = {name: set(cols) for name, cols in REPORT_COLUMN_ORDER.items()}


def validate_output_contract(rows: list[dict[str, Any]], report_name: str) -> None:
    """Бросает ValueError, если в строках нет обязательных колонок."""
    required = REQUIRED_COLUMNS.get(report_name, set())
    if not required or not rows:
        return
    missing = sorted(required.difference(rows[0].keys()))
    if missing:
        raise ValueError(
            f"{report_name}: нарушение контракта, нет колонок {missing} "
            f"(report_version={REPORT_VERSION})."
        )


def _cell(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool):
        return int(value)
    return value


def write_report_csv(rows: list[dict[str, Any]], report_name: str, path: str | Path) -> Path:
    """Пишет CSV со стабильным порядком колонок (заголовок всегда есть)."""
    columns = REPORT_COLUMN_ORDER.get(report_name) or (list(rows[0].keys()) if rows else [])
    validate_output_contract(rows, report_name)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _cell(row.get(c, "")) for c in columns})
    return path


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Не сериализуется: {type(obj)}")


def write_report_json(payload: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return path


def write_report_jsonl(records: list[dict[str, Any]], path: str | Path) -> Path:
    """Пишет JSONL: по одному JSON-объекту на строку (для raw-диагностики)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=_json_default) + "\n")
    return path


def report_metadata() -> dict[str, str]:
    return {
        "report_version": REPORT_VERSION,
        "output_contract_version": OUTPUT_CONTRACT_VERSION,
        "generated_at_utc": utc_now(),
    }
