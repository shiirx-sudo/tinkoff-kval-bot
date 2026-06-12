"""
T-Invest Kval Bot — точка входа (read-only).

Команды:
    python main.py accounts        Список брокерских счетов (масками).
    python main.py kval-status     Прогресс к квал-статусу по обороту + отчёты.
    python main.py doctor          Проверка окружения/конфигурации.

Только чтение. Торговые операции не выполняются (LIVE_ENABLED=false).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from loguru import logger


def _setup_logging(verbose: bool) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<dim>{time:HH:mm:ss}</dim> | <level>{level:<7}</level> | {message}",
    )


def cmd_doctor(_args: argparse.Namespace) -> int:
    from reports.runtime_doctor import run_doctor
    rep = run_doctor()
    print("Runtime doctor:")
    for name, status, detail in rep.checks:
        mark = "✅" if status == "ok" else "❌"
        print(f"  {mark} {name}: {detail}" if detail else f"  {mark} {name}")
    return 0 if rep.ok else 1


def cmd_accounts(_args: argparse.Namespace) -> int:
    from common.helpers import mask_identifier
    from api.client import ReadOnlyClient
    from brokers.tinkoff.rest_client import account_type_label, is_turnover_account
    try:
        accounts = ReadOnlyClient().get_all_accounts()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Не удалось получить счета: {exc}")
        return 1
    if not accounts:
        print("Счетов по токену не найдено.")
        return 0

    included = 0
    print(f"Доступные счета ({len(accounts)}):")
    for acc in accounts:
        in_turnover = is_turnover_account(acc)
        included += int(in_turnover)
        mark = "✅ учитывается" if in_turnover else "➖ не учитывается"
        print(f"  {mask_identifier(acc.get('id'))}  "
              f"{acc.get('name', '') or '—':<24}  "
              f"{account_type_label(acc.get('type', '')):<11}  "
              f"[{acc.get('status', '')}]  {mark}")
    print(f"\nВ обороте учитывается счетов: {included} из {len(accounts)} "
          f"(брокерский + ИИС).")
    return 0


def cmd_kval_status(args: argparse.Namespace) -> int:
    from modules.kval_tracker import KvalTracker
    from reports import console_report, kval_reports
    try:
        progress = KvalTracker().analyze(args.as_of)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка при расчёте: {exc}")
        return 1

    console_report.render(progress)
    written = kval_reports.write_all(progress, args.reports_dir)
    for name, path in written.items():
        logger.info(f"Отчёт: {path}")
    return 0


def cmd_kval_plan(args: argparse.Namespace) -> int:
    from modules.kval_planner import KvalPlanner
    from reports import console_plan, kval_plan_reports
    try:
        plan = KvalPlanner().plan(
            as_of=args.as_of,
            horizon_quarters=args.horizon_quarters,
            target_mode=args.target_mode,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка при планировании: {exc}")
        return 1

    console_plan.render(plan)
    written = kval_plan_reports.write_all(plan, args.reports_dir)
    for name, path in written.items():
        logger.info(f"Отчёт: {path}")
    return 0


def cmd_instrument_scan(args: argparse.Namespace) -> int:
    from decimal import Decimal
    from config.settings import settings
    from modules.instrument_scanner import (
        InstrumentScanner, ScanFilters, load_candidates, target_from_kval_plan,
    )
    from reports import console_scan, instrument_scan_reports

    candidates = load_candidates(args.symbols, args.class_code)
    if not candidates:
        logger.error(
            "Нет кандидатов: создайте config/instrument_candidates.yaml "
            "или передайте --symbols TMON,LQDT"
        )
        return 1

    # Комиссия: CLI → env/настройки → 0 + warning
    if args.commission_bps is not None:
        commission_bps = Decimal(str(args.commission_bps))
    elif settings.commission_bps is not None:
        commission_bps = settings.commission_bps
    else:
        commission_bps = Decimal("0")
        logger.warning(
            "commission_bps не задан (нет --commission-bps и TINKOFF_COMMISSION_BPS) "
            "— издержки учитывают только спред."
        )

    # Целевой месячный оборот: CLI → kval_plan.json → 0
    if args.target_monthly_turnover is not None:
        target = Decimal(str(args.target_monthly_turnover))
    else:
        target = target_from_kval_plan(args.reports_dir) or Decimal("0")

    filters = ScanFilters(
        max_spread_bps=Decimal(str(args.max_spread_bps)),
        min_top_depth_rub=Decimal(str(args.min_top_depth_rub)),
        depth=args.depth,
    )

    try:
        report = InstrumentScanner().scan(
            candidates, as_of=args.as_of, commission_bps=commission_bps,
            target_monthly_turnover=target, filters=filters,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка сканирования: {exc}")
        return 1

    console_scan.render(report)
    written = instrument_scan_reports.write_all(report, args.reports_dir)
    for name, path in written.items():
        logger.info(f"Отчёт: {path}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="main.py", description="T-Invest Kval Bot (read-only)")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG-логирование")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Проверка окружения/конфигурации")
    sub.add_parser("accounts", help="Список брокерских счетов")

    p_kval = sub.add_parser("kval-status", help="Официальный факт по 4 завершённым календарным кварталам")
    p_kval.add_argument("--as-of", type=lambda s: date.fromisoformat(s), default=None,
                        metavar="YYYY-MM-DD", help="Дата расчёта (по умолчанию сегодня)")
    p_kval.add_argument("--reports-dir", default="data/reports", metavar="DIR",
                        help="Каталог для выходных отчётов (по умолчанию data/reports/)")

    p_plan = sub.add_parser("kval-plan", help="Прогноз будущих окон и календарь выполнения условий")
    p_plan.add_argument("--as-of", type=lambda s: date.fromisoformat(s), default=None,
                        metavar="YYYY-MM-DD", help="Дата расчёта (по умолчанию сегодня)")
    p_plan.add_argument("--horizon-quarters", type=int, default=8, metavar="N",
                        help="Сколько будущих квартальных окон анализировать (по умолчанию 8)")
    p_plan.add_argument("--reports-dir", default="data/reports", metavar="DIR",
                        help="Каталог для выходных отчётов (по умолчанию data/reports/)")
    p_plan.add_argument("--target-mode", choices=("effective", "bare"), default="effective",
                        help="Считать до цели с буфером (effective) или без (bare)")

    p_scan = sub.add_parser(
        "instrument-scan",
        help="Read-only оценка ликвидности/издержек инструментов под набор оборота")
    p_scan.add_argument("--as-of", type=lambda s: date.fromisoformat(s), default=None,
                        metavar="YYYY-MM-DD", help="Дата расчёта (по умолчанию сегодня)")
    p_scan.add_argument("--symbols", default=None, metavar="T1,T2",
                        help="Список тикеров через запятую (иначе config/instrument_candidates.yaml)")
    p_scan.add_argument("--class-code", default="TQBR",
                        help="Режим/класс торгов (по умолчанию TQBR)")
    p_scan.add_argument("--reports-dir", default="data/reports", metavar="DIR",
                        help="Каталог отчётов (по умолчанию data/reports/)")
    p_scan.add_argument("--depth", type=int, default=20, help="Глубина стакана (по умолчанию 20)")
    p_scan.add_argument("--commission-bps", type=float, default=None,
                        help="Комиссия в б.п. (иначе из окружения, иначе 0 + warning)")
    p_scan.add_argument("--target-monthly-turnover", type=float, default=None,
                        metavar="RUB",
                        help="Целевой месячный оборот (иначе из kval_plan.json, иначе 0)")
    p_scan.add_argument("--max-spread-bps", type=float, default=20,
                        help="Порог спреда для spread_ok (по умолчанию 20)")
    p_scan.add_argument("--min-top-depth-rub", type=float, default=100000,
                        help="Порог глубины топ-уровня для depth_ok (по умолчанию 100000)")
    return parser.parse_args(argv)


_HANDLERS = {
    "doctor": cmd_doctor,
    "accounts": cmd_accounts,
    "kval-status": cmd_kval_status,
    "kval-plan": cmd_kval_plan,
    "instrument-scan": cmd_instrument_scan,
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return _HANDLERS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
