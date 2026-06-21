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
        "max_monthly_actions": int(os.getenv("EXECUTION_MAX_MONTHLY_ACTIONS", "0") or "0"),
        "kval_min_total_trades": int(os.getenv("KVAL_MIN_TOTAL_TRADES", "41") or "41"),
        "kval_target_total_trades": int(os.getenv("KVAL_TARGET_TOTAL_TRADES", "48") or "48"),
    }

    # CLI override практического лимита действий
    mma = getattr(args, "max_monthly_actions", None)
    if mma is not None:
        kwargs["max_monthly_actions"] = int(mma)

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


def cmd_strategy_scan(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from api.client import ReadOnlyClient
    from modules.strategy_signals import load_signal_config, scan
    from notifications import signals as sg
    from notifications import telegram as tg
    from reports import strategy_signals_reports

    opts = load_signal_config()
    if args.watchlist:
        opts["watchlist"] = [t.strip().upper() for t in args.watchlist.split(",") if t.strip()]
    if args.min_score is not None:
        opts["config"].min_score = int(args.min_score)
    if args.timeframe:
        opts["timeframe"] = args.timeframe
    if args.max_signals is not None:
        opts["max_per_run"] = int(args.max_signals)
    if getattr(args, "fundamental_filter", False):
        opts["fundamental_filter_enabled"] = True
    if getattr(args, "fundamental_filter_path", None):
        opts["fundamental_filter_path"] = args.fundamental_filter_path
    if getattr(args, "require_fundamental_pass", False):
        opts["require_fundamental_pass"] = True

    try:
        signals = scan(ReadOnlyClient(), opts, as_of=args.as_of,
                       account_id=getattr(args, "account_id", None))
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка скана сигналов (read-only): {exc}")
        return 1

    now = datetime.now(timezone.utc)
    state = sg.load_signal_state(opts["state_path"])

    # Telegram вызывается ТОЛЬКО при --notify; иначе просто пишем отчёты.
    if args.notify and opts["notify_telegram"]:
        tg_cfg = tg.load_config()
        for s in signals:
            if s.action not in ("BUY", "SELL"):
                continue
            send, _ = sg.should_notify(s, state, now, opts["dedup_hours"],
                                       opts["notify_on_hold"])
            if not send:
                continue
            text = sg.build_signal_message(s, strategy=args.strategy)
            result = tg.send_telegram_message(
                tg_cfg.bot_token, tg_cfg.chat_id, text,
                enabled=tg_cfg.enabled, dry_run=False)
            if result.get("sent"):
                s.notified = True
                sg.update_state(state, s, now)
        sg.save_signal_state(opts["state_path"], state)
    written = strategy_signals_reports.write_all(signals, args.strategy, "data/reports")
    for name, path in written.items():
        logger.info(f"Отчёт: {path}")

    actionable = [s for s in signals if s.action in ("BUY", "SELL")]
    avoid = [s for s in signals if s.action == "AVOID"]
    print(f"Стратегия {args.strategy}: {len(signals)} инструментов, "
          f"{len(actionable)} BUY/SELL, {len(avoid)} AVOID, отправлено: "
          f"{sum(1 for s in signals if s.notified)}.")
    for s in signals:
        if s.action == "AVOID" and not getattr(args, "include_avoid", False):
            # AVOID всегда в отчётах; в консоли по умолчанию свернём в одну строку
            pass
        extra = f" score={s.score}" if s.action in ("BUY", "HOLD") else ""
        tag = ""
        if s.action == "SELL":
            tag = " [held]"
        elif s.action == "AVOID":
            tag = " [raw=SELL, not_held]" if not s.held_unknown else " [raw=SELL, held_unknown]"
        print(f"  {s.action:5} {s.ticker}{extra}{tag}"
              + (f" — {', '.join(s.blocked_reasons)}" if s.action == "SKIP" else ""))
    print("Режим: SIGNAL_ONLY / READ_ONLY. Заявки не отправляются.")
    return 0


def cmd_strategy_status(args: argparse.Namespace) -> int:
    from modules.strategy_signals import load_signal_config
    from notifications import signals as sg
    opts = load_signal_config()
    print(sg.signals_status_text(opts["config"], opts["enabled"], opts["watchlist"]))
    print(sg.signals_last_text("data/reports"))
    return 0


def cmd_income_summary(args: argparse.Namespace) -> int:
    from decimal import Decimal

    from api.client import ReadOnlyClient
    from modules.fundamental_filter import load_fundamental_filter
    from modules.income_engine import load_income_config, load_income_env, summarize_account
    from modules.income_engine import build_calendar
    from reports import income_reports

    config = load_income_config(getattr(args, "config_path", None))
    env = load_income_env(config)
    if args.target_monthly_rub is not None:
        env.target_monthly_rub = Decimal(str(args.target_monthly_rub))
    try:
        summary = summarize_account(ReadOnlyClient(), args.account_id, config, env,
                                    load_fundamental_filter())
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-summary (read-only): {exc}")
        return 1

    written = income_reports.write_summary(summary, "data/reports")
    print(income_reports._summary_md(summary))
    for name, path in written.items():
        logger.info(f"Отчёт: {path}")

    if getattr(args, "notify", False):
        from notifications import telegram as tg
        cal = build_calendar(summary.items, env.horizon_months, env.tax_rate_pct)
        text = income_reports.build_summary_telegram(summary, cal)
        cfg = tg.load_config()
        tg.send_telegram_message(cfg.bot_token, cfg.chat_id, text,
                                 enabled=cfg.enabled, dry_run=False)
    return 0


def cmd_income_calendar(args: argparse.Namespace) -> int:
    from api.client import ReadOnlyClient
    from modules.fundamental_filter import load_fundamental_filter
    from modules.income_engine import (
        build_calendar,
        load_income_config,
        load_income_env,
        summarize_account,
    )
    from reports import income_reports

    config = load_income_config(getattr(args, "config_path", None))
    env = load_income_env(config)
    months = args.months or env.horizon_months
    try:
        summary = summarize_account(ReadOnlyClient(), args.account_id, config, env,
                                    load_fundamental_filter())
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-calendar (read-only): {exc}")
        return 1
    rows = build_calendar(summary.items, months, env.tax_rate_pct)
    income_reports.write_calendar(rows, "data/reports")
    print(f"Income calendar — READ ONLY ({len(rows)} строк, горизонт {months} мес.):")
    for r in rows[:40]:
        print(f"  {r['month']:>10} {r['ticker']:8} {r['source_type']:12} "
              f"{r['expected_payment_date']:>12} net={r['net_amount']} ({r['confidence']})")
    print("Статус: аналитика, не рекомендация. Заявки не отправляются.")
    return 0


def cmd_income_watchlist(args: argparse.Namespace) -> int:
    import os

    from api.client import ReadOnlyClient
    from modules.fundamental_filter import load_fundamental_filter
    from modules.income_engine import (
        DEFAULT_CLASS_CODE_PRIORITY,
        IncomeEnv,
        build_watchlist,
        load_income_config,
        load_income_env,
    )
    from reports import income_reports

    from modules.income_universe import resolve_watchlist

    config = load_income_config(getattr(args, "config_path", None))
    env: IncomeEnv = load_income_env(config)
    fdata = load_fundamental_filter()
    try:
        raw_items, _umeta = resolve_watchlist(
            args.watchlist, getattr(args, "universe_profile", None),
            getattr(args, "universe_path", None))
    except ValueError as exc:
        logger.error(str(exc))
        return 1
    priority = [c.strip().upper() for c in
                os.getenv("SIGNALS_CLASS_CODE_PRIORITY",
                          ",".join(DEFAULT_CLASS_CODE_PRIORITY)).split(",") if c.strip()]

    try:
        items = build_watchlist(ReadOnlyClient(), raw_items, config, env, fdata, priority)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-watchlist (read-only): {exc}")
        return 1

    written = income_reports.write_watchlist(items, "data/reports")
    print("Income watchlist — READ ONLY")
    print("")
    for it in items:
        print(income_reports.render_watchlist_line(it))
    for _name, path in written.items():
        logger.info(f"Отчёт: {path}")
    print("Статус: аналитика, не рекомендация. Заявки не отправляются.")
    return 0


def cmd_income_source_audit(args: argparse.Namespace) -> int:
    import os

    from api.client import ReadOnlyClient
    from modules.income_audit import build_audit
    from modules.income_engine import (
        DEFAULT_CLASS_CODE_PRIORITY,
        load_income_config,
        load_income_env,
    )
    from reports import income_audit_reports as rep

    config = load_income_config(getattr(args, "config_path", None))
    env = load_income_env(config)
    if args.lookback_months is not None:
        env.dividend_lookback_months = args.lookback_months
    if args.trailing_months is not None:
        env.dividend_trailing_months = args.trailing_months
    if args.mm_trailing_days is not None:
        env.mm_trailing_days = args.mm_trailing_days

    raw_items = [t.strip() for t in (args.watchlist or "").split(",") if t.strip()]
    if not raw_items and args.account_id is None:
        logger.error("Укажите --watchlist и/или --account-id для аудита.")
        return 1
    priority = [c.strip().upper() for c in
                os.getenv("SIGNALS_CLASS_CODE_PRIORITY",
                          ",".join(DEFAULT_CLASS_CODE_PRIORITY)).split(",") if c.strip()]

    try:
        items = build_audit(ReadOnlyClient(), raw_items=raw_items,
                            account_id=args.account_id, config=config, env=env,
                            priority=priority)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-source-audit (read-only): {exc}")
        return 1

    written = rep.write_audit(items, "data/reports")
    fmt = getattr(args, "format", "md")
    if fmt == "json":
        print((written["income_source_audit.json"]).read_text(encoding="utf-8"))
    elif fmt == "csv":
        print((written["income_source_audit.csv"]).read_text(encoding="utf-8-sig"))
    else:
        print(rep.render_audit_console(items))
    for _name, path in written.items():
        logger.info(f"Отчёт: {path}")
    return 0


def cmd_target_portfolio(args: argparse.Namespace) -> int:
    import os
    from decimal import Decimal

    from api.client import ReadOnlyClient
    from modules.fundamental_filter import load_fundamental_filter
    from modules.income_engine import (
        DEFAULT_CLASS_CODE_PRIORITY,
        load_income_config,
        load_income_env,
    )
    from modules.income_policy import load_policy_env
    from modules.target_portfolio import build_target_portfolio, load_target_env
    from reports import target_portfolio_reports as rep

    config = load_income_config(getattr(args, "config_path", None))
    income_env = load_income_env(config)
    if args.target_monthly_rub is not None:
        income_env.target_monthly_rub = Decimal(str(args.target_monthly_rub))
    policy_env = load_policy_env()
    target_env = load_target_env(income_env)

    # CLI-оверрайды (read-only расчёт)
    def _set(attr, val, cast=Decimal):
        if val is not None:
            setattr(target_env, attr, cast(str(val)) if cast is Decimal else cast(val))
    _set("max_position_pct", args.max_position_pct)
    _set("max_issuer_pct", args.max_issuer_pct)
    _set("cash_reserve_rub", args.cash_reserve_rub)
    _set("new_capital_rub", args.new_capital_rub)
    _set("monthly_contribution_rub", args.monthly_contribution_rub)
    if args.min_policy_bucket is not None:
        target_env.min_policy_bucket = args.min_policy_bucket
    if args.include_estimated:
        target_env.include_estimated = True
    if args.no_include_variable:
        target_env.include_variable = False
    if args.months is not None:
        target_env.months = args.months

    from modules.income_universe import resolve_watchlist
    try:
        raw_items, umeta = resolve_watchlist(
            args.watchlist, getattr(args, "universe_profile", None),
            getattr(args, "universe_path", None))
    except ValueError as exc:
        logger.error(str(exc))
        return 1
    if not raw_items:
        logger.error("Укажите --watchlist или --universe-profile для target-portfolio.")
        return 1
    priority = [c.strip().upper() for c in
                os.getenv("SIGNALS_CLASS_CODE_PRIORITY",
                          ",".join(DEFAULT_CLASS_CODE_PRIORITY)).split(",") if c.strip()]

    try:
        tp = build_target_portfolio(
            ReadOnlyClient(), raw_watchlist=raw_items, account_id=args.account_id,
            config=config, income_env=income_env, target_env=target_env,
            fundamental_data=load_fundamental_filter(), policy_env=policy_env,
            priority=priority)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка target-portfolio (read-only): {exc}")
        return 1

    tp.universe_profile = umeta["universe_profile"]
    tp.universe_path = umeta["universe_path"]
    tp.universe_watchlist_count = umeta["universe_watchlist_count"]

    written = rep.write_target_portfolio(tp, "data/reports")
    print(rep.render_console(tp))
    for _name, path in written.items():
        logger.info(f"Отчёт: {path}")

    if getattr(args, "notify", False):
        from notifications import telegram as tg
        cfg = tg.load_config()
        tg.send_telegram_message(cfg.bot_token, cfg.chat_id, rep.build_telegram(tp),
                                 enabled=cfg.enabled, dry_run=False)
    return 0


def cmd_income_universe_audit(args: argparse.Namespace) -> int:
    from modules import income_universe_audit as audit

    try:
        result = audit.run_audit(
            builder_report_path=args.builder_report,
            output_json=args.output_json,
            output_md=args.output_md,
        )
    except audit.AuditError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-universe-audit (read-only): {exc}")
        return 1

    s = result["summary"]
    gc = s["group_counts"]
    print("Income universe disabled audit — READ ONLY")
    print("Аналитика, не рекомендация. Заявки не отправляются.")
    print(f"  total disabled: {s['total_disabled']}")
    print(f"  A manual-audit={gc['A']} B policy-review={gc['B']} "
          f"C coupon-validation={gc['C']} D resolver-mapping={gc['D']} "
          f"E keep-disabled={gc['E']}")
    print(f"  auto_enable_allowed: {s['auto_enable_allowed_count']} "
          f"(ни один кандидат не включается автоматически)")
    print(f"  requires code PR: {s['requires_code_pr_count']} | "
          f"requires local rules: {s['requires_local_rules_count']}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return 0


def cmd_income_coupon_validation(args: argparse.Namespace) -> int:
    from modules import income_coupon_validation as cv

    client = None
    offline = bool(getattr(args, "offline", False))
    if not offline:
        # read-only API-обогащение опционально: без токена/ошибки → offline-like
        try:
            from api.client import ReadOnlyClient
            client = ReadOnlyClient()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"API недоступен ({exc}); coupon-validation работает offline "
                f"только по локальным отчётам.")
            client = None
            offline = True

    try:
        result = cv.run(
            builder_report_path=args.builder_report,
            audit_report_path=args.audit_report,
            output_json=args.output_json,
            output_md=args.output_md,
            offline=offline,
            client=client,
        )
    except cv.CouponValidationError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-coupon-validation (read-only): {exc}")
        return 1

    s = result["summary"]
    print("Income coupon validation — READ ONLY")
    print("Аналитика, не рекомендация. Заявки не отправляются.")
    print(f"  total candidates: {s['total_candidates']}")
    print(f"  floating coupon: {s['floating_coupon_count']} | "
          f"fixed coupon: {s['fixed_coupon_count']} | "
          f"missing data: {s['missing_data_count']}")
    print(f"  by_status: {s['by_status']}")
    print(f"  by_readiness: {s['by_readiness']}")
    print(f"  annualization_allowed: {s['annualization_allowed_count']}")
    print(f"  auto_enable_allowed: {s['auto_enable_allowed_count']} "
          f"(ни один кандидат не включается автоматически)")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return 0


def cmd_income_floating_coupon_policy(args: argparse.Namespace) -> int:
    from modules import floating_coupon_policy as fcp

    try:
        result = fcp.run(
            input_json=args.input_json,
            output_json=args.output_json,
            output_md=args.output_md,
        )
    except fcp.FloatingCouponPolicyError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-floating-coupon-policy (read-only): {exc}")
        return 1

    s = result["summary"]
    print("Income floating coupon policy — READ ONLY")
    print("Аналитика, не рекомендация. Заявки не отправляются.")
    print(f"  total candidates: {s['total_candidates']}")
    print(f"  floating coupon candidates: {s['floating_coupon_candidates']}")
    print(f"  annualization_allowed: {s['annualization_allowed_count']} | "
          f"forecast_allowed: {s['forecast_allowed_count']} | "
          f"auto_enable_allowed: {s['auto_enable_allowed_count']}")
    print(f"  by_policy_status: {s['by_policy_status']}")
    print(f"  by_readiness: {s['by_readiness']}")
    print("  forecast_method=not_supported_yet "
          "(ни один кандидат не включается и не прогнозируется автоматически)")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return 0


def cmd_build_income_universe(args: argparse.Namespace) -> int:
    import json
    import shutil
    from datetime import datetime, timezone
    from pathlib import Path

    from api.client import ReadOnlyClient
    from modules import income_universe_builder as builder
    from modules.fundamental_filter import load_fundamental_filter
    from modules.income_engine import load_income_config, load_income_env
    from modules.income_policy import load_policy_env

    rules = builder.load_rules(getattr(args, "rules_path", None))
    if not rules:
        logger.error("Не найдены rules income_universe. Укажите --rules-path или "
                     "создайте config/income_universe_rules.example.yaml.")
        return 1
    config = load_income_config(getattr(args, "config_path", None))
    income_env = load_income_env(config)
    policy_env = load_policy_env()
    fdata = load_fundamental_filter()
    mode = args.enable_mode
    now = datetime.now(timezone.utc)
    rules_path = str(rules.get("_source_path", "") or "rules")

    try:
        result = builder.build_universe(
            rules=rules, mode=mode, max_bonds=args.max_bonds,
            include_disabled=args.include_disabled, output=args.output,
            dry_run=args.dry_run, profile_set=args.profile_set, client=ReadOnlyClient(),
            config=config, income_env=income_env, fundamental_data=fdata,
            policy_env=policy_env, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка build-income-universe (read-only): {exc}")
        return 1

    rep = result.report
    print("Income universe builder — READ ONLY")
    print(f"  mode={rep['mode']} dry_run={rep['dry_run']} output={args.output}")
    print(f"  rules={rules_path}")
    print(f"  instruments scanned: {rep['instruments_scanned']}")
    for name in rep["generated_profiles"]:
        print(f"  {name}: {rep['included_by_profile'][name]} "
              f"(enabled {rep['enabled_by_profile'][name]})")
    print(f"  unresolved: {rep['unresolved']}")
    print(f"  policy-excluded: {rep['policy_excluded_count']} | "
          f"unknown-income: {rep['unknown_income_count']}")
    print(f"  disabled by reason: {rep['disabled_by_reason']}")
    for w in rep.get("warnings") or []:
        logger.warning(w)

    if args.dry_run:
        print("DRY-RUN: ничего не записано. Заявки не отправляются.")
        return 0

    out = Path(args.output)
    if out.exists():
        if args.backup:
            bak = out.with_name(out.name + f".bak.{now.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(out, bak)
            logger.info(f"Backup: {bak}")
        elif not args.force:
            logger.error(f"{out} уже существует. Используйте --backup или --force.")
            return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        builder.render_universe_yaml(result, mode=mode, rules_path=rules_path, now=now),
        encoding="utf-8")
    logger.info(f"Generated universe: {out}")

    rep_dir = Path("data/reports")
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / "income_universe_builder_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (rep_dir / "income_universe_builder_report.md").write_text(
        builder.render_report_md(rep), encoding="utf-8")
    print("Заявки не отправляются. Это аналитика, не рекомендация.")
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
    p_exec.add_argument("--max-monthly-actions", type=int, default=None,
                        help="Практический лимит действий в месяц (0=выкл; иначе из env)")
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
    p_pre.add_argument("--max-monthly-actions", type=int, default=None,
                       help="Практический лимит действий в месяц (0=выкл; иначе из env)")

    p_pis = sub.add_parser(
        "passive-income-summary",
        help="READ-ONLY аналитика портфеля (деньги/фонды/облигации/акции)")
    p_pis.add_argument("--account-id", default=None,
                       help="Счёт (read-only; иначе первый брокерский)")

    p_ss = sub.add_parser(
        "strategy-scan",
        help="READ-ONLY скан сигналов BUY/SELL/HOLD/SKIP (заявки НЕ отправляются)")
    p_ss.add_argument("--strategy", default="trend_signal_v1")
    p_ss.add_argument("--watchlist", default=None, help="Тикеры через запятую")
    p_ss.add_argument("--min-score", type=int, default=None)
    p_ss.add_argument("--notify", action="store_true",
                      help="Отправлять BUY/SELL в Telegram (иначе только отчёты)")
    p_ss.add_argument("--as-of", type=lambda s: date.fromisoformat(s), default=None,
                      metavar="YYYY-MM-DD")
    p_ss.add_argument("--timeframe", default=None, choices=("day", "hour", "week"))
    p_ss.add_argument("--max-signals", type=int, default=None)
    p_ss.add_argument("--account-id", default=None,
                      help="Счёт для чтения позиций (read-only; иначе первый брокерский)")
    p_ss.add_argument("--include-avoid", action="store_true",
                      help="Подробнее показывать AVOID в консоли (в отчётах всегда есть)")
    p_ss.add_argument("--fundamental-filter", action="store_true",
                      help="Включить read-only фундаментальный фильтр качества")
    p_ss.add_argument("--fundamental-filter-path", default=None,
                      help="Путь к YAML-базе оценок (иначе из env/по умолчанию)")
    p_ss.add_argument("--require-fundamental-pass", action="store_true",
                      help="Понижать BUY до HOLD, если качество ниже quality_pass")

    sub.add_parser("strategy-status",
                   help="Статус стратегии сигналов + последние сигналы (read-only)")

    p_is = sub.add_parser("income-summary",
                          help="READ-ONLY доходная аналитика портфеля (дивиденды/купоны/MM)")
    p_is.add_argument("--account-id", default=None)
    p_is.add_argument("--target-monthly-rub", type=float, default=None)
    p_is.add_argument("--config-path", default=None)
    p_is.add_argument("--notify", action="store_true",
                      help="Отправить summary в Telegram (иначе только отчёты)")

    p_ic = sub.add_parser("income-calendar",
                          help="READ-ONLY календарь ожидаемых выплат")
    p_ic.add_argument("--account-id", default=None)
    p_ic.add_argument("--months", type=int, default=None)
    p_ic.add_argument("--config-path", default=None)

    p_iw = sub.add_parser("income-watchlist",
                          help="READ-ONLY доходный обзор watchlist (аналитика, не рекомендация)")
    p_iw.add_argument("--watchlist", default="")
    p_iw.add_argument("--universe-profile", default=None,
                      help="Профиль вселенной из income_universe.yaml (если --watchlist пуст)")
    p_iw.add_argument("--universe-path", default=None,
                      help="Путь к income_universe.yaml (иначе data/config → example)")
    p_iw.add_argument("--account-id", default=None)
    p_iw.add_argument("--config-path", default=None)

    p_isa = sub.add_parser(
        "income-source-audit",
        help="READ-ONLY аудит сырых API-событий, легших в расчёт доходности")
    p_isa.add_argument("--watchlist", default="", help="Тикеры через запятую")
    p_isa.add_argument("--account-id", default=None,
                       help="Аудит позиций портфеля (read-only; можно вместе с watchlist)")
    p_isa.add_argument("--lookback-months", type=int, default=None)
    p_isa.add_argument("--trailing-months", type=int, default=None)
    p_isa.add_argument("--mm-trailing-days", type=int, default=None)
    p_isa.add_argument("--format", default="md", choices=("md", "json", "csv"))
    p_isa.add_argument("--config-path", default=None)

    p_tp = sub.add_parser(
        "target-portfolio",
        help="READ-ONLY план целевого доходного портфеля и докупки (заявок нет)")
    p_tp.add_argument("--watchlist", default="", help="Вселенная инструментов через запятую")
    p_tp.add_argument("--universe-profile", default=None,
                      help="Профиль вселенной из income_universe.yaml (если --watchlist пуст)")
    p_tp.add_argument("--universe-path", default=None,
                      help="Путь к income_universe.yaml (иначе data/config → example)")
    p_tp.add_argument("--account-id", default=None,
                      help="Текущий портфель (read-only; иначе только universe)")
    p_tp.add_argument("--target-monthly-rub", type=float, default=None)
    p_tp.add_argument("--max-position-pct", type=float, default=None)
    p_tp.add_argument("--max-issuer-pct", type=float, default=None)
    p_tp.add_argument("--min-policy-bucket", default=None,
                      choices=("income_reliable", "income_variable"))
    p_tp.add_argument("--include-estimated", action="store_true",
                      help="Включить income_estimated отдельным слоем")
    p_tp.add_argument("--no-include-variable", action="store_true",
                      help="Исключить income_variable из base target")
    p_tp.add_argument("--cash-reserve-rub", type=float, default=None)
    p_tp.add_argument("--new-capital-rub", type=float, default=None)
    p_tp.add_argument("--monthly-contribution-rub", type=float, default=None)
    p_tp.add_argument("--months", type=int, default=None)
    p_tp.add_argument("--config-path", default=None)
    p_tp.add_argument("--notify", action="store_true",
                      help="Отправить краткий отчёт в Telegram (иначе только отчёты)")

    p_iua = sub.add_parser(
        "income-universe-audit",
        help="READ-ONLY диагностика disabled-кандидатов income universe (A/B/C/D/E)")
    p_iua.add_argument(
        "--builder-report",
        default="data/reports/income_universe_builder_report.json",
        help="Путь к income_universe_builder_report.json (только чтение)")
    p_iua.add_argument(
        "--output-json", dest="output_json",
        default="data/reports/income_universe_disabled_audit.json",
        help="Путь для JSON-отчёта аудита")
    p_iua.add_argument(
        "--output-md", dest="output_md",
        default="data/reports/income_universe_disabled_audit.md",
        help="Путь для Markdown-отчёта аудита")

    p_icv = sub.add_parser(
        "income-coupon-validation",
        help="READ-ONLY coupon-validation диагностика disabled-кандидатов группы C")
    p_icv.add_argument(
        "--builder-report",
        default="data/reports/income_universe_builder_report.json",
        help="Путь к income_universe_builder_report.json (только чтение)")
    p_icv.add_argument(
        "--audit-report", dest="audit_report",
        default="data/reports/income_universe_disabled_audit.json",
        help="Путь к income_universe_disabled_audit.json (только чтение)")
    p_icv.add_argument(
        "--output-json", dest="output_json",
        default="data/reports/income_coupon_validation.json",
        help="Путь для JSON-отчёта coupon-validation")
    p_icv.add_argument(
        "--output-md", dest="output_md",
        default="data/reports/income_coupon_validation.md",
        help="Путь для Markdown-отчёта coupon-validation")
    p_icv.add_argument(
        "--offline", action="store_true",
        help="Работать только по локальным отчётам, без read-only API")

    p_fcp = sub.add_parser(
        "income-floating-coupon-policy",
        help="READ-ONLY floating-coupon policy диагностика ОФЗ-ПК кандидатов "
             "из coupon-validation")
    p_fcp.add_argument(
        "--input-json", dest="input_json",
        default="data/reports/income_coupon_validation.json",
        help="Путь к income_coupon_validation.json (только чтение)")
    p_fcp.add_argument(
        "--output-json", dest="output_json",
        default="data/reports/income_floating_coupon_policy.json",
        help="Путь для JSON-отчёта floating-coupon policy")
    p_fcp.add_argument(
        "--output-md", dest="output_md",
        default="data/reports/income_floating_coupon_policy.md",
        help="Путь для Markdown-отчёта floating-coupon policy")

    p_biu = sub.add_parser(
        "build-income-universe",
        help="READ-ONLY генератор income universe из rules + T-Invest данных")
    p_biu.add_argument("--output", default="data/config/income_universe.generated.yaml",
                       help="Путь для сгенерированного YAML")
    p_biu.add_argument("--rules-path", default=None,
                       help="Локальные правила (иначе data/config → example)")
    p_biu.add_argument("--enable-mode", default="disabled",
                       choices=("disabled", "policy", "conservative"))
    p_biu.add_argument("--backup", action="store_true",
                       help="Сделать backup существующего output перед записью")
    p_biu.add_argument("--force", action="store_true",
                       help="Перезаписать существующий output без backup")
    p_biu.add_argument("--dry-run", action="store_true",
                       help="Ничего не записывать, только summary")
    p_biu.add_argument("--include-disabled", action=argparse.BooleanOptionalAction,
                       default=True, help="Писать disabled-кандидатов (очередь на аудит)")
    p_biu.add_argument("--max-bonds", type=int, default=100)
    p_biu.add_argument("--profile-set", default="income",
                       help="Набор профилей (пока реализован только 'income'; иначе warning)")
    p_biu.add_argument("--config-path", default=None,
                       help="Путь к income_engine config (для income policy)")

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
    "strategy-scan": cmd_strategy_scan,
    "strategy-status": cmd_strategy_status,
    "income-summary": cmd_income_summary,
    "income-calendar": cmd_income_calendar,
    "income-watchlist": cmd_income_watchlist,
    "income-source-audit": cmd_income_source_audit,
    "target-portfolio": cmd_target_portfolio,
    "income-universe-audit": cmd_income_universe_audit,
    "income-coupon-validation": cmd_income_coupon_validation,
    "income-floating-coupon-policy": cmd_income_floating_coupon_policy,
    "build-income-universe": cmd_build_income_universe,
    "telegram-test": cmd_telegram_test,
    "telegram-summary": cmd_telegram_summary,
    "telegram-notify": cmd_telegram_notify,
}


def main(argv: list[str] | None = None) -> int:
    # На Windows-консоли (cp1251) символы вроде '₽' рушат вывод — печатаем в UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # не TextIOWrapper / уже сконфигурирован
            pass
    args = _parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return _HANDLERS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
