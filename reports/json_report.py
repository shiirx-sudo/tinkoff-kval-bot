"""
JSON-отчёт о прогрессе. Сериализует KvalProgress в читаемый JSON.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from modules.kval_tracker import KvalProgress


def _default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Не сериализуется: {type(obj)}")


def to_dict(progress: KvalProgress) -> dict:
    """Преобразует прогресс в обычный dict (готовый к json.dumps)."""
    p = progress
    return {
        "generated_at": p.generated_at,
        "period": {
            "start": p.period.start.isoformat(),
            "end": p.period.end.isoformat(),
            "as_of": p.period.as_of_date.isoformat(),
            "quarters": [q.label for q in p.period.quarters],
            "current_quarter": p.period.current_quarter.label,
        },
        "target": p.target,
        "effective_target": p.effective_target,
        "total_turnover": p.total_turnover,
        "progress_pct": p.progress_pct,
        "remaining_to_target": p.remaining_to_target,
        "remaining_to_effective": p.remaining_to_effective,
        "achieved": p.achieved,
        "achieved_bare": p.achieved_bare,
        "has_approximate": p.has_approximate,
        "approximate_warnings": p.approximate_warnings,
        "accounts": [
            {
                "account_id": a.account_id,
                "account_name": a.account_name,
                "total_turnover": a.total_turnover,
                "operation_count": a.operation_count,
                "trade_count": a.trade_count,
                "approximate_count": a.approximate_count,
                "by_quarter": {
                    label: {
                        "turnover": qt.turnover,
                        "operation_count": qt.operation_count,
                        "trade_count": qt.trade_count,
                    }
                    for label, qt in a.by_quarter.items()
                },
            }
            for a in p.accounts
        ],
    }


def render(progress: KvalProgress, path: str | Path = "kval_report.json") -> Path:
    """Сохраняет JSON-отчёт в файл и возвращает путь."""
    path = Path(path)
    data = to_dict(progress)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=_default),
        encoding="utf-8",
    )
    return path
