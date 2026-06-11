"""
CSV-отчёт: построчная выгрузка всех сделок (trades) для сверки.
"""
from __future__ import annotations

import csv
from pathlib import Path

from modules.kval_tracker import KvalProgress

_FIELDNAMES = [
    "operation_id",
    "account_id",
    "date",
    "ticker",
    "figi",
    "direction",
    "price",
    "quantity",
    "turnover",
    "is_approximate",
    "raw_payment",
]


def render(progress: KvalProgress, path: str | Path = "kval_trades.csv") -> Path:
    """Сохраняет CSV со всеми сделками и возвращает путь."""
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES, delimiter=";")
        writer.writeheader()
        for t in progress.all_trades:
            writer.writerow({
                "operation_id": t.operation_id,
                "account_id": t.account_id,
                "date": t.date,
                "ticker": t.ticker,
                "figi": t.figi,
                "direction": t.direction,
                "price": str(t.price),
                "quantity": t.quantity,
                "turnover": str(t.turnover),
                "is_approximate": int(t.is_approximate),
                "raw_payment": str(t.raw_payment),
            })
    return path
