"""
Точка входа: расчёт прогресса к статусу квалифицированного инвестора
по обороту в Т-Инвестициях.

Только чтение. Торговые операции не выполняются (LIVE_ENABLED=false).

Примеры
-------
    python main.py
    python main.py --as-of 2026-03-31
    python main.py --json out/report.json --csv out/trades.csv
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from loguru import logger

from reports import console_report, csv_report, json_report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="T-Invest Qualification Tracker (read-only)",
    )
    parser.add_argument(
        "--as-of",
        type=lambda s: date.fromisoformat(s),
        default=None,
        metavar="YYYY-MM-DD",
        help="Дата расчёта (по умолчанию — сегодня).",
    )
    parser.add_argument(
        "--json",
        default=None,
        metavar="PATH",
        help="Сохранить JSON-отчёт по указанному пути.",
    )
    parser.add_argument(
        "--csv",
        default=None,
        metavar="PATH",
        help="Сохранить CSV со сделками по указанному пути.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Подробное логирование (DEBUG).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if args.verbose else "INFO",
        format="<dim>{time:HH:mm:ss}</dim> | <level>{level:<7}</level> | {message}",
    )

    # Импортируем здесь, чтобы ошибка конфигурации (.env) выводилась дружелюбно
    try:
        from modules.kval_tracker import KvalTracker
    except EnvironmentError as exc:
        logger.error(str(exc))
        return 2

    try:
        progress = KvalTracker().analyze(args.as_of)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка при расчёте: {exc}")
        return 1

    console_report.render(progress)

    if args.json:
        path = json_report.render(progress, args.json)
        logger.info(f"JSON-отчёт сохранён: {path}")
    if args.csv:
        path = csv_report.render(progress, args.csv)
        logger.info(f"CSV-отчёт сохранён: {path}")

    # Код возврата 0 если цель (с буфером) достигнута, иначе 0 всё равно —
    # это информационный инструмент, не gate. Меняйте при необходимости.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
