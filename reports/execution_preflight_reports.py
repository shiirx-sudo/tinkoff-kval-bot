"""Отчёты Execution Preflight: execution_preflight.json + .csv + .md."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from modules.execution_preflight import PreflightResult
from reports.output_contract import (
    report_metadata,
    write_report_csv,
    write_report_json,
)


def _check_row(c) -> dict[str, Any]:
    return {"check": c.name, "ok": c.ok, "blocking": c.blocking, "detail": c.detail}


def _render_md(p: PreflightResult) -> str:
    lines = [
        "# Execution Preflight — READ ONLY",
        "",
        f"- Дата: {p.as_of.isoformat()}",
        f"- Период: {p.period}",
        f"- Инструмент: {p.instrument} / {p.class_code}",
        f"- Статус инструмента: {p.trading_status}",
        f"- Вердикт: {p.verdict}",
        f"- Broker trades missing: {p.broker_trade_count_missing}",
        f"- Roundtrip cycles: {p.roundtrip_cycle_count_required}",
        f"- Planned actions: {p.planned_actions_count}",
        f"- Side notional: {p.side_notional}",
        "",
        f"## Итог: {p.status}",
        "",
        "## Checks",
    ]
    for c in p.checks:
        mark = "✓" if c.ok else "✗"
        lines.append(f"- {mark} {c.name} — {c.detail}")
    if p.warnings:
        lines += ["", "## Warnings"] + [f"- {w}" for w in p.warnings]
    if p.errors:
        lines += ["", "## Errors"] + [f"- {e}" for e in p.errors]
    lines += ["", "_Это read-only preflight. Реальные заявки не отправляются._", ""]
    return "\n".join(lines)


def write_all(p: PreflightResult, reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    rows = [_check_row(c) for c in p.checks]

    payload = {
        "as_of": p.as_of.isoformat(),
        "status": p.status,
        "instrument": {
            "ticker": p.instrument, "class_code": p.class_code,
            "trading_status": p.trading_status, "verdict": p.verdict,
        },
        "period": p.period,
        "broker_trade_count_missing": p.broker_trade_count_missing,
        "roundtrip_cycle_count_required": p.roundtrip_cycle_count_required,
        "side_notional": p.side_notional,
        "planned_actions_count": p.planned_actions_count,
        "checks": [asdict(c) for c in p.checks],
        "warnings": p.warnings,
        "errors": p.errors,
        "source_reports": p.source_reports,
        **report_metadata(),
    }

    written["execution_preflight.json"] = write_report_json(
        payload, out / "execution_preflight.json")
    written["execution_preflight.csv"] = write_report_csv(
        rows, "execution_preflight", out / "execution_preflight.csv")
    md_path = out / "execution_preflight.md"
    md_path.write_text(_render_md(p), encoding="utf-8")
    written["execution_preflight.md"] = md_path
    return written
