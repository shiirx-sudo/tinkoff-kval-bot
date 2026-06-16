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


def cmd_turnover_plan(args: argparse.Namespace) -> int:
    from decimal import Decimal
    from modules.turnover_planner import TurnoverPlanError, build
    from reports import console_turnover, turnover_plan_reports

    commission = (Decimal(str(args.commission_bps))
                  if args.commission_bps is not None else None)
    try:
        plan = build(
            reports_dir=args.reports_dir, as_of=args.as_of,
            instrument=args.instrument, mode=args.mode,
            commission_bps_cli=commission,
            min_trade_rub=Decimal(str(args.min_trade_rub)),
            max_trade_rub=Decimal(str(args.max_trade_rub)),
            round_lots=args.round_lots,
        )
    except TurnoverPlanError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка построения плана: {exc}")
        return 1

    console_turnover.render(plan)
    written = turnover_plan_reports.write_all(plan, args.reports_dir)
    for name, path in written.items():
        logger.info(f"Отчёт: {path}")
    return 0


_TARIFF_BPS = {"investor": "30", "trader": "5", "premium": "4"}


def _resolve_execution_sizing(args) -> dict:
    """Собирает balance-параметры из env/CLI и (read-only) свободный баланс."""
    import os
    from decimal import Decimal

    size_mode = getattr(args, "size_mode", None) or os.getenv("EXECUTION_SIZE_MODE", "fixed")
    size_mode = size_mode.strip().lower()

    kwargs = {
        "size_mode": size_mode,
        "balance_utilization_pct": Decimal(os.getenv("EXECUTION_BALANCE_UTILIZATION_PCT", "0.80") or "0.80"),
        "min_cash_reserve_rub": Decimal(os.getenv("EXECUTION_MIN_CASH_RESERVE_RUB", "5000") or "5000"),
        "min_monthly_actions": int(os.getenv("EXECUTION_MIN_MONTHLY_ACTIONS", "4") or "4"),
        "kval_min_total_trades": int(os.getenv("KVAL_MIN_TOTAL_TRADES", "41") or "41"),
        "kval_target_total_trades": int(os.getenv("KVAL_TARGET_TOTAL_TRADES", "48") or "48"),
    }

    # available_cash: явный CLI-override → read-only чтение со счёта
    cash = getattr(args, "available_cash_rub", None)
    if cash is not None:
        kwargs["available_cash_rub"] = Decimal(str(cash))
    elif size_mode == "balance":
        try:
            from api.client import ReadOnlyClient
            from modules.balance import available_cash_rub
            kwargs["available_cash_rub"] = available_cash_rub(
                ReadOnlyClient(), getattr(args, "account_id", None))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Не удалось прочитать баланс (read-only): {exc}")
            kwargs["available_cash_rub"] = None
    return kwargs


def _resolve_commission(args):
    import os
    from decimal import Decimal
    if args.commission_bps is not None:
        return Decimal(str(args.commission_bps))
    tariff = os.getenv("TINVEST_TARIFF", "").strip().lower()
    if tariff in _TARIFF_BPS:
        return Decimal(_TARIFF_BPS[tariff])
    return None


def cmd_execution_plan(args: argparse.Namespace) -> int:
    from decimal import Decimal
    from modules.execution_planner import ExecutionPlanError, build
    from reports import console_execution, execution_plan_reports

    commission = _resolve_commission(args)
    sizing = _resolve_execution_sizing(args)
    try:
        plan = build(
            reports_dir=args.reports_dir, as_of=args.as_of,
            instrument=args.instrument, mode=args.mode,
            commission_bps=commission,
            max_side_notional_rub=Decimal(str(args.max_side_notional_rub)),
            min_side_notional_rub=Decimal(str(args.min_side_notional_rub)),
            spread_bps_limit=Decimal(str(args.spread_bps_limit)),
            min_depth_multiplier=Decimal(str(args.min_depth_multiplier)),
            dry_run=args.dry_run, **sizing,
        )
    except ExecutionPlanError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка построения execution-plan: {exc}")
        return 1

    console_execution.render(plan)
    written = execution_plan_reports.write_all(plan, args.reports_dir)
    for name, path in written.items():
        logger.info(f"Отчёт: {path}")
    return 0


def cmd_execution_preflight(args: argparse.Namespace) -> int:
    from decimal import Decimal
    from modules.execution_preflight import PreflightError, run
    from reports import console_preflight, execution_preflight_reports

    commission = _resolve_commission(args)
    sizing = _resolve_execution_sizing(args)
    try:
        result = run(
            reports_dir=args.reports_dir, as_of=args.as_of,
            instrument=args.instrument, mode=args.mode, commission_bps=commission,
            max_side_notional_rub=Decimal(str(args.max_side_notional_rub)),
            spread_bps_limit=Decimal(str(args.spread_bps_limit)),
            min_depth_multiplier=Decimal(str(args.min_depth_multiplier)),
            **sizing,
        )
    except PreflightError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка preflight: {exc}")
        return 1

    console_preflight.render(result)
    written = execution_preflight_reports.write_all(result, args.reports_dir)
    for name, path in written.items():
        logger.info(f"Отчёт: {path}")
    # READY_DRY_RUN / STALE_REPORTS → 0; BLOCKED / MISSING_REPORTS → 2
    return 0 if result.status in ("READY_DRY_RUN", "STALE_REPORTS") else 2


def cmd_passive_income_summary(args: argparse.Namespace) -> int:
    from api.client import ReadOnlyClient
    from modules.balance import portfolio_breakdown

    def _m(v):
        from decimal import Decimal as _D
        return f"{_D(v):,.0f} ₽".replace(",", " ")

    try:
        b = portfolio_breakdown(ReadOnlyClient(), args.account_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка чтения портфеля (read-only): {exc}")
        return 1

    print("💰 Passive Income Summary — READ ONLY\n")
    print(f"Счёт: {b.account_id_masked or '—'}")
    print(f"Свободные рубли: {_m(b.free_rub)}")
    print(f"Фонды денежного рынка: {_m(b.money_market_funds_rub)}")
    print(f"Облигации: {_m(b.bonds_rub)}")
    print(f"Акции (потенц. дивиденды): {_m(b.dividend_shares_rub)}")
    print(f"Прочее: {_m(b.other_rub)}")
    print(f"Ожидаемая доходность портфеля: {_m(b.expected_yield_rub)}")
    print(f"Итого: {_m(b.total_rub)}")
    not_turnover = b.total_rub - b.free_rub
    if b.total_rub > 0:
        share = (not_turnover / b.total_rub * 100)
        print(f"Доля капитала вне kval-turnover: {share:.0f}%")
    for w in b.warnings:
        logger.warning(w)
    print("\nЭто аналитика, не рекомендация. Покупок/продаж не выполняется.")
    return 0


def _telegram_write_reports(reports_dir: str, text: str, result: dict) -> None:
    import json as _json
    from pathlib import Path as _Path
    out = _Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "telegram_last_message.md").write_text(text, encoding="utf-8")
    (out / "telegram_notify_result.json").write_text(
        _json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_telegram_test(args: argparse.Namespace) -> int:
    from notifications.telegram import load_config, send_telegram_message
    cfg = load_config()
    text = "✅ telegram-test: T-Invest Kval Monitor на связи. Реальных заявок нет."
    result = send_telegram_message(
        cfg.bot_token, cfg.chat_id, text,
        enabled=cfg.enabled, dry_run=args.dry_run, force=args.force)
    if result.get("sent"):
        logger.info("Telegram: тестовое сообщение отправлено.")
    elif result.get("dry_run"):
        logger.info("Telegram: dry-run — тестовое сообщение не отправлено.")
    else:
        logger.warning(f"Telegram: не отправлено ({result.get('reason') or result.get('error')}).")
    return 0


def cmd_telegram_summary(args: argparse.Namespace) -> int:
    from notifications.telegram import (
        build_summary_message,
        load_config,
        read_reports,
        send_telegram_message,
    )
    data = read_reports(args.reports_dir)
    text = build_summary_message(data)
    print(text)
    result = {"built": True, "sent": False, "reason": "summary_no_send"}
    if args.send:
        cfg = load_config()
        result = send_telegram_message(
            cfg.bot_token, cfg.chat_id, text,
            enabled=cfg.enabled, dry_run=args.dry_run, force=args.force)
    _telegram_write_reports(args.reports_dir, text, {"status": data.get("status"), **result})
    return 0


def cmd_telegram_notify(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from notifications.telegram import (
        decide_notification,
        load_alert_state,
        load_config,
        read_reports,
        save_alert_state,
        send_telegram_message,
    )
    cfg = load_config()
    data = read_reports(args.reports_dir)
    state_path = f"{args.alerts_dir.rstrip('/')}/telegram_alert_state.json"
    state = load_alert_state(state_path)
    now = datetime.now(timezone.utc)

    decision = decide_notification(data, state, cfg, now)

    if decision.should_send:
        if args.dry_run:
            send_result = {"sent": False, "dry_run": True, "reason": "dry_run", "error": None}
        else:
            send_result = send_telegram_message(
                cfg.bot_token, cfg.chat_id, decision.text,
                enabled=cfg.enabled, dry_run=False, force=args.force)
        if send_result.get("sent") or args.dry_run:
            state["last_sent_at_utc"] = now.isoformat()
        if "daily_summary" in decision.reasons:
            state["last_daily_summary_date"] = now.date().isoformat()
        if decision.deadline_keys.get("month"):
            state["last_month_deadline_alert"] = decision.deadline_keys["month"]
        if decision.deadline_keys.get("quarter"):
            state["last_quarter_deadline_alert"] = decision.deadline_keys["quarter"]
    else:
        send_result = {"sent": False, "reason": "suppressed", "error": None}

    state["last_status"] = decision.status
    state["last_hash"] = decision.text_hash
    save_alert_state(state_path, state)

    result = {
        "status": decision.status,
        "should_send": decision.should_send,
        "reasons": decision.reasons,
        **send_result,
    }
    _telegram_write_reports(args.reports_dir, decision.text, result)

    logger.info(
        f"telegram-notify: статус={decision.status}, "
        f"отправка={'да' if decision.should_send else 'нет'} "
        f"({', '.join(decision.reasons) or 'нет причин'}), "
        f"sent={send_result.get('sent')}"
    )
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

    def _boolish(s: str) -> bool:
        return str(s).strip().lower() in ("1", "true", "yes", "y", "да")

    p_turn = sub.add_parser(
        "turnover-plan",
        help="Read-only расчётный план ручного набора оборота (без сделок)")
    p_turn.add_argument("--as-of", type=lambda s: date.fromisoformat(s), default=None,
                        metavar="YYYY-MM-DD", help="Дата расчёта (по умолчанию сегодня)")
    p_turn.add_argument("--reports-dir", default="data/reports", metavar="DIR",
                        help="Каталог отчётов (по умолчанию data/reports/)")
    p_turn.add_argument("--instrument", default=None,
                        help="Тикер из instrument_scan.json (иначе лучший GOOD)")
    p_turn.add_argument("--mode", choices=("gross", "roundtrip"), default="roundtrip",
                        help="gross: каждая сделка — отдельный оборот; roundtrip: buy+sell")
    p_turn.add_argument("--commission-bps", type=float, default=None,
                        help="Комиссия в б.п. (иначе из instrument_scan.json, иначе 0+warning)")
    p_turn.add_argument("--min-trade-rub", type=float, default=0,
                        help="Минимальный ориентировочный номинал сделки (0 = без ограничения)")
    p_turn.add_argument("--max-trade-rub", type=float, default=0,
                        help="Максимальный ориентировочный номинал сделки (0 = без ограничения)")
    p_turn.add_argument("--round-lots", type=_boolish, default=True,
                        metavar="true|false", help="Округлять до целых лотов (по умолчанию true)")

    p_exec = sub.add_parser(
        "execution-plan",
        help="DRY-RUN план будущих BUY/SELL действий (реальные заявки НЕ отправляются)")
    p_exec.add_argument("--as-of", type=lambda s: date.fromisoformat(s), default=None,
                        metavar="YYYY-MM-DD", help="Дата расчёта (по умолчанию сегодня)")
    p_exec.add_argument("--reports-dir", default="data/reports", metavar="DIR",
                        help="Каталог отчётов (по умолчанию data/reports/)")
    p_exec.add_argument("--instrument", default="LQDT",
                        help="Тикер из instrument_scan.json (по умолчанию LQDT)")
    p_exec.add_argument("--mode", choices=("gross", "roundtrip"), default="roundtrip",
                        help="roundtrip: пары BUY+SELL; gross: отдельные сделки")
    p_exec.add_argument("--commission-bps", type=float, default=None,
                        help="Комиссия в б.п. (иначе из instrument_scan.json, иначе 0+warning)")
    p_exec.add_argument("--max-side-notional-rub", type=float, default=0,
                        help="Лимит номинала одной стороны (0 = без лимита, но warning)")
    p_exec.add_argument("--min-side-notional-rub", type=float, default=0,
                        help="Минимальный номинал одной стороны (0 = без ограничения)")
    p_exec.add_argument("--spread-bps-limit", type=float, default=5,
                        help="Максимальный spread_bps для исполнения (по умолчанию 5)")
    p_exec.add_argument("--dry-run", type=_boolish, default=True, metavar="true|false",
                        help="Только dry-run (по умолчанию true; live не реализован)")
    p_exec.add_argument("--size-mode", choices=("fixed", "balance"), default=None,
                        help="fixed или balance (иначе из EXECUTION_SIZE_MODE)")
    p_exec.add_argument("--account-id", default=None,
                        help="Счёт для чтения баланса (read-only; иначе первый)")
    p_exec.add_argument("--available-cash-rub", type=float, default=None,
                        help="Override свободного баланса (иначе читается со счёта)")
    p_exec.add_argument("--min-depth-multiplier", type=float, default=1.2,
                        help="Запас глубины к side_notional (по умолчанию 1.2)")

    p_pre = sub.add_parser(
        "execution-preflight",
        help="READ-ONLY проверка готовности dry-run плана (заявки НЕ отправляются)")
    p_pre.add_argument("--as-of", type=lambda s: date.fromisoformat(s), default=None,
                       metavar="YYYY-MM-DD", help="Дата расчёта (по умолчанию сегодня)")
    p_pre.add_argument("--reports-dir", default="data/reports", metavar="DIR",
                       help="Каталог отчётов (по умолчанию data/reports/)")
    p_pre.add_argument("--instrument", default="LQDT",
                       help="Тикер из instrument_scan.json (по умолчанию LQDT)")
    p_pre.add_argument("--mode", choices=("gross", "roundtrip"), default="roundtrip",
                       help="roundtrip: пары BUY+SELL; gross: отдельные сделки")
    p_pre.add_argument("--commission-bps", type=float, default=None,
                       help="Комиссия в б.п. (иначе из instrument_scan.json, иначе 0+warning)")
    p_pre.add_argument("--max-side-notional-rub", type=float, default=130000,
                       help="Лимит номинала одной стороны (по умолчанию 130000)")
    p_pre.add_argument("--spread-bps-limit", type=float, default=5,
                       help="Максимальный spread_bps (по умолчанию 5)")
    p_pre.add_argument("--min-depth-multiplier", type=float, default=1.2,
                       help="Требуемый запас глубины к side_notional (по умолчанию 1.2)")
    p_pre.add_argument("--size-mode", choices=("fixed", "balance"), default=None,
                       help="fixed или balance (иначе из EXECUTION_SIZE_MODE)")
    p_pre.add_argument("--account-id", default=None,
                       help="Счёт для чтения баланса (read-only; иначе первый)")
    p_pre.add_argument("--available-cash-rub", type=float, default=None,
                       help="Override свободного баланса (иначе читается со счёта)")

    p_pis = sub.add_parser(
        "passive-income-summary",
        help="READ-ONLY аналитика портфеля (деньги/фонды/облигации/акции)")
    p_pis.add_argument("--account-id", default=None,
                       help="Счёт (read-only; иначе первый брокерский)")

    p_tgt = sub.add_parser(
        "telegram-test", help="Проверка Telegram bot_token/chat_id (read-only)")
    p_tgt.add_argument("--dry-run", type=_boolish, default=True, metavar="true|false",
                       help="Не отправлять реально (по умолчанию true)")
    p_tgt.add_argument("--force", type=_boolish, default=False, metavar="true|false",
                       help="Отправить даже при TELEGRAM_ALERTS_ENABLED=false")

    p_tgs = sub.add_parser(
        "telegram-summary", help="Короткий текст-сводка по отчётам (по умолчанию без отправки)")
    p_tgs.add_argument("--reports-dir", default="data/reports", metavar="DIR")
    p_tgs.add_argument("--send", type=_boolish, default=False, metavar="true|false",
                       help="Отправить сводку (по умолчанию false)")
    p_tgs.add_argument("--dry-run", type=_boolish, default=True, metavar="true|false")
    p_tgs.add_argument("--force", type=_boolish, default=False, metavar="true|false")

    p_tgn = sub.add_parser(
        "telegram-notify", help="Авто-решение об уведомлении (для runner-скрипта)")
    p_tgn.add_argument("--reports-dir", default="data/reports", metavar="DIR")
    p_tgn.add_argument("--alerts-dir", default="data/alerts", metavar="DIR")
    p_tgn.add_argument("--dry-run", type=_boolish, default=True, metavar="true|false",
                       help="Не отправлять реально (по умолчанию true)")
    p_tgn.add_argument("--force", type=_boolish, default=False, metavar="true|false")
    return parser.parse_args(argv)


_HANDLERS = {
    "doctor": cmd_doctor,
    "accounts": cmd_accounts,
    "kval-status": cmd_kval_status,
    "kval-plan": cmd_kval_plan,
    "instrument-scan": cmd_instrument_scan,
    "turnover-plan": cmd_turnover_plan,
    "execution-plan": cmd_execution_plan,
    "execution-preflight": cmd_execution_preflight,
    "passive-income-summary": cmd_passive_income_summary,
    "telegram-test": cmd_telegram_test,
    "telegram-summary": cmd_telegram_summary,
    "telegram-notify": cmd_telegram_notify,
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return _HANDLERS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
