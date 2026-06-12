"""Отчёты Instrument Scanner: instrument_scan.json + instrument_scan.csv."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from modules.instrument_scanner import ScanReport, ScanResult
from reports.output_contract import (
    report_metadata,
    write_report_csv,
    write_report_json,
)


def _result_row(r: ScanResult) -> dict[str, Any]:
    return {
        "ticker": r.ticker,
        "name": r.name,
        "figi": r.figi,
        "instrument_uid": r.instrument_uid,
        "class_code": r.class_code,
        "requested_class_code": r.requested_class_code,
        "resolved_class_code": r.resolved_class_code,
        "resolution_method": r.resolution_method,
        "lot": r.lot,
        "currency": r.currency,
        "trading_status": r.trading_status,
        "bid_best": r.bid_best,
        "ask_best": r.ask_best,
        "mid_price": r.mid_price,
        "last_price": r.last_price,
        "spread_abs": r.spread_abs,
        "spread_bps": r.spread_bps,
        "bid_top_depth_rub": r.bid_top_depth_rub,
        "ask_top_depth_rub": r.ask_top_depth_rub,
        "min_side_top_depth_rub": r.min_side_top_depth_rub,
        "estimated_roundtrip_cost_bps": r.estimated_roundtrip_cost_bps,
        "estimated_monthly_cost_rub": r.estimated_monthly_cost_rub,
        "score": r.score,
        "verdict": r.verdict,
        "suitable_for_turnover": r.suitable_for_turnover,
        "resolution_warning": r.resolution_warning,
        "trading_status_warning": r.trading_status_warning,
        "warnings": "|".join(r.warnings),
    }


def write_all(report: ScanReport, reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    out = Path(reports_dir)
    written: dict[str, Path] = {}

    rows = [_result_row(r) for r in report.results]

    payload = {
        "as_of": report.as_of.isoformat(),
        "commission_bps": report.commission_bps,
        "target_monthly_turnover": report.target_monthly_turnover,
        "filters": {
            "max_spread_bps": report.filters.max_spread_bps,
            "min_top_depth_rub": report.filters.min_top_depth_rub,
            "depth": report.filters.depth,
        },
        "candidates": [asdict(c) for c in report.candidates],
        "results": [asdict(r) for r in report.results],
        "warnings": report.warnings,
        "disclaimer": report.disclaimer,
        **report_metadata(),
    }

    written["instrument_scan.json"] = write_report_json(
        payload, out / "instrument_scan.json")
    written["instrument_scan.csv"] = write_report_csv(
        rows, "instrument_scan", out / "instrument_scan.csv")
    return written
