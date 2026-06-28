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


def cmd_income_resolver_mapping_diagnostics(args: argparse.Namespace) -> int:
    from modules import resolver_mapping_diagnostics as rmd

    client = None
    offline = bool(getattr(args, "offline", False))
    if not offline:
        # read-only API-обогащение опционально: без токена/ошибки → offline-like
        try:
            from api.client import ReadOnlyClient
            client = ReadOnlyClient()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"API недоступен ({exc}); resolver-mapping-diagnostics работает "
                f"offline только по локальному audit-отчёту.")
            client = None
            offline = True

    try:
        result = rmd.run(
            input_json=args.input_json,
            output_json=args.output_json,
            output_md=args.output_md,
            offline=offline,
            client=client,
        )
    except rmd.ResolverMappingError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(
            f"Ошибка income-resolver-mapping-diagnostics (read-only): {exc}")
        return 1

    s = result["summary"]
    print("Income resolver/mapping diagnostics — READ ONLY (group D)")
    print("Аналитика, не рекомендация. Заявки не отправляются.")
    print(f"  mode: {result['mode']}")
    print(f"  total_candidates: {s['total_candidates']}")
    print(f"  unresolved: {s['unresolved_count']} | "
          f"candidate_matches: {s['candidate_matches_found_count']} | "
          f"ambiguous: {s['ambiguous_matches_count']} | "
          f"no_matches: {s['no_matches_count']}")
    print(f"  by_mapping_status: {s['by_mapping_status']}")
    print(f"  auto_mapping_allowed: {s['auto_mapping_allowed_count']} | "
          f"auto_enable_allowed: {s['auto_enable_allowed_count']} "
          f"(ничего не маппится и не включается автоматически)")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return 0


def cmd_income_owner_decision_report(args: argparse.Namespace) -> int:
    from modules import income_owner_decision_report as odr

    try:
        result = odr.run(
            universe_report=args.universe_report,
            audit_json=args.audit_json,
            coupon_json=args.coupon_json,
            floating_policy_json=args.floating_policy_json,
            resolver_json=args.resolver_json,
            target_json=args.target_json,
            output_json=args.output_json,
            output_md=args.output_md,
            max_candidates=args.max_candidates,
            min_score=args.min_score,
            offline=bool(getattr(args, "offline", False)),
        )
    except odr.OwnerDecisionError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-owner-decision-report (read-only): {exc}")
        return 1

    s = result["summary"]
    print("Owner income decision report — READ ONLY (F1)")
    print("Owner-only decision support. Заявки не отправляются.")
    print("order_send_allowed=false | auto_execution_allowed=false | "
          "execution_requires_manual_confirmation=true")
    if result.get("missing_inputs"):
        print(f"  missing_inputs: {result['missing_inputs']} "
              "(деградация безопасна; см. smoke chain в md)")
    print(f"  total_candidates: {s['total_candidates']}")
    print(f"  BUY_CANDIDATE: {s['buy_candidate_count']} | WAIT: {s['wait_count']} | "
          f"NEEDS_POLICY: {s['needs_policy_count']} | "
          f"NEEDS_MAPPING: {s['needs_mapping_count']} | "
          f"NEEDS_DATA: {s['needs_data_count']} | BLOCKED: {s['blocked_count']}")
    print(f"  by_proposed_action: {s['by_proposed_action']}")
    print("  Следующий этап перед сделкой: order preview / no-send (F2); "
          "ручное подтверждение обязательно.")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return 0


def cmd_income_order_preview(args: argparse.Namespace) -> int:
    from modules import income_order_preview as iop

    price_mode = iop.PRICE_MODE_OFFLINE if getattr(args, "offline", False) \
        else getattr(args, "price_mode", iop.PRICE_MODE_AUTO)

    client = None
    if price_mode != iop.PRICE_MODE_OFFLINE:
        # read-only API опционально: без токена/ошибки → offline-like NEEDS_PRICE
        try:
            from api.client import ReadOnlyClient
            client = ReadOnlyClient()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"API недоступен ({exc}); income-order-preview работает offline "
                f"только по F1 decision report (NEEDS_PRICE при отсутствии цены).")
            client = None
            if price_mode == iop.PRICE_MODE_AUTO:
                price_mode = iop.PRICE_MODE_OFFLINE

    # безопасная fee model: только из настроек (read-only), если задана в .env
    commission_bps = None
    try:
        from config.settings import settings
        commission_bps = settings.commission_bps
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"settings.commission_bps недоступен ({exc}); комиссия UNAVAILABLE.")

    try:
        result = iop.run(
            decision_json=args.decision_json,
            output_json=args.output_json,
            output_md=args.output_md,
            candidate_action=args.candidate_action,
            tickers=getattr(args, "ticker", None),
            max_candidates=args.max_candidates,
            max_order_rub=args.max_order_rub,
            min_lots=args.min_lots,
            max_lots=args.max_lots,
            price_mode=price_mode,
            commission_bps=commission_bps,
            client=client,
        )
    except iop.OrderPreviewError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-order-preview (read-only): {exc}")
        return 1

    s = result["summary"]
    print("Income order preview — READ ONLY (F2 order preview / no-send)")
    print(f"Заявки не отправляются. {iop.ORDERS_SERVICE_LABEL} не используется. "
          "full-access token не используется.")
    print("order_send_allowed=false | auto_execution_allowed=false | "
          "manual confirmation required")
    print(f"  mode: {result['mode']}")
    print(f"  total_decision_candidates: {s['total_decision_candidates']} | "
          f"selected: {s['selected_candidates']}")
    print(f"  PREVIEW_READY: {s['preview_ready_count']} | "
          f"NEEDS_PRICE: {s['needs_price_count']} | "
          f"BLOCKED: {s['blocked_count']}")
    print(f"  order_send_allowed_count: {s['order_send_allowed_count']} | "
          f"auto_execution_allowed_count: {s['auto_execution_allowed_count']} | "
          f"orders_service_used: {s['orders_service_used']}")
    print(f"  Следующий этап перед сделкой: {iop.NEXT_STAGE}; "
          "ручное подтверждение обязательно.")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return 0 if s["selected_candidates"] > 0 else 1


def cmd_income_sandbox_execute_preview(args: argparse.Namespace) -> int:
    from modules import income_sandbox_execution as ise

    send_sandbox = bool(getattr(args, "send_sandbox", False))
    price_mode = getattr(args, "price_mode", ise.PRICE_MODE_AUTO)

    # read-only API нужен только для свежей preflight-цены (send или readonly-api)
    client = None
    if price_mode != ise.PRICE_MODE_OFFLINE and (
            send_sandbox or price_mode == ise.PRICE_MODE_READONLY_API):
        try:
            from api.client import ReadOnlyClient
            client = ReadOnlyClient()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"API недоступен ({exc}); preflight-цена берётся из F2 preview.")
            client = None

    try:
        result = ise.run(
            ticker=args.ticker,
            preview_json=args.preview_json,
            output_json=args.output_json,
            output_md=args.output_md,
            max_order_rub=args.max_order_rub,
            max_price_deviation_bps=args.max_price_deviation_bps,
            dry_run=getattr(args, "dry_run", True),
            send_sandbox=send_sandbox,
            confirm=getattr(args, "confirm", None),
            price_mode=price_mode,
            client_order_id_prefix=args.client_order_id_prefix,
            sandbox_account_id=getattr(args, "sandbox_account_id", None),
            sandbox_transport=getattr(args, "sandbox_transport",
                                      ise.TRANSPORT_UNCONFIGURED),
            instrument_id_source=getattr(args, "instrument_id_source",
                                         ise.INSTRUMENT_ID_SOURCE_AUTO),
            client=client,
        )
    except ise.SandboxExecutionError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-sandbox-execute-preview: {exc}")
        return 1

    g = result["guards"]
    tr = result.get("sandbox_transport") or {}
    print("Income sandbox execute preview — F3 (sandbox manual-confirmed execution)")
    print("LIVE-заявки запрещены. Sandbox only. full-access live токен не используется.")
    print(f"  mode: {result['mode']} | ticker: {result['ticker']}")
    print(f"  sandbox_transport: {tr.get('selected_transport')} "
          f"(configured: {tr.get('configured')})")
    print(f"  required_confirmation_phrase: {result['required_confirmation_phrase']}")
    print(f"  confirmation_matched: {result['confirmation_matched']}")
    print(f"  sandbox_order_sent: {g['sandbox_order_sent']} | dry_run: {g['dry_run']}")
    print(f"  Следующий этап: {ise.NEXT_STAGE}")
    for e in result.get("errors", []):
        print(f"  ! {e}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return int(result.get("_exit_code", 0))


def cmd_income_sandbox_account(args: argparse.Namespace) -> int:
    from modules import income_sandbox_account as isa

    try:
        result = isa.run(
            action=getattr(args, "action", isa.ACTION_STATUS),
            sandbox_transport=getattr(args, "sandbox_transport",
                                      isa.TRANSPORT_VERIFIED_REST),
            sandbox_account_id=getattr(args, "sandbox_account_id", None),
            pay_in_rub=getattr(args, "pay_in_rub", None),
            confirm=getattr(args, "confirm", None),
            dry_run=getattr(args, "dry_run", True),
            output_json=args.output_json,
            output_md=args.output_md,
        )
    except isa.SandboxAccountError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-sandbox-account: {exc}")
        return 1

    g = result["guards"]
    tr = result.get("sandbox_transport") or {}
    print("Income sandbox account — F3.2 (sandbox account bootstrap)")
    print("Только sandbox. Заявки не отправляются. full-access live токен не используется.")
    print(f"  action: {result['action']} | mode: {result['mode']}")
    print(f"  sandbox_transport: {tr.get('selected_transport')} "
          f"(configured: {tr.get('configured')})")
    if result.get("required_confirmation_phrase"):
        print(f"  required_confirmation_phrase: {result['required_confirmation_phrase']}")
        print(f"  confirmation_matched: {result['confirmation_matched']}")
    print(f"  sandbox_accounts: {len(result.get('sandbox_accounts') or [])}")
    print(f"  selected_sandbox_account_id: {result.get('selected_sandbox_account_id')}")
    print(f"  sandbox_account_opened: {result['sandbox_account_opened']} | "
          f"sandbox_payin_done: {result['sandbox_payin_done']}")
    print(f"  sandbox_token_used: {g['sandbox_token_used']} | "
          f"token_printed: {g['token_printed']}")
    print(f"  Следующий этап: {isa.NEXT_STAGE}")
    for e in result.get("errors", []):
        print(f"  ! {e}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return int(result.get("_exit_code", 0))


def cmd_income_live_readiness(args: argparse.Namespace) -> int:
    from modules import income_live_readiness as ilr

    try:
        result = ilr.run(
            ticker=getattr(args, "ticker", ilr.DEFAULT_TICKER),
            lots=getattr(args, "lots", ilr.DEFAULT_LOTS),
            max_order_rub=getattr(args, "max_order_rub", ilr.DEFAULT_MAX_ORDER_RUB),
            sandbox_report=args.sandbox_report,
            output_json=args.output_json,
            output_md=args.output_md,
        )
    except ilr.LiveReadinessError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-live-readiness: {exc}")
        return 1

    lp = result["live_plan"]
    tp = result["token_policy"]
    print("Income live readiness — F4.0 (pre-live readiness; НЕ live-исполнение)")
    print("LIVE-заявки не отправляются. Sandbox-заявки не отправляются. "
          "Execution-токен не используется.")
    print(f"  stage: {result['stage']} | mode: {result['mode']} | "
          f"ticker: {result['ticker']}")
    print(f"  sandbox_gate_passed: {result['sandbox_gate_passed']}")
    print(f"  sandbox_order_id: {result['sandbox_order_id']} | "
          f"sandbox_execution_report_status: {result['sandbox_execution_report_status']}")
    print(f"  ready_for_f4_live_manual_order: {result['ready_for_f4_live_manual_order']}")
    print(f"  live_plan: {lp['ticker']} {lp['side']} {lp['order_type']} "
          f"{lp['lots']} лот(а), cap {lp['max_order_rub']} ₽, "
          f"instrument_id_source={lp['instrument_id_source']}")
    print(f"  required_future_confirmation_phrase: "
          f"{result['required_future_confirmation_phrase']}")
    print(f"  {tp['live_trading_token_env']} present: {tp['live_trading_token_present']} "
          f"(только наличие; значение не печатается)")
    for r in result.get("blocking_reasons", []):
        print(f"  ! blocked: {r}")
    for w in result.get("warnings", []):
        print(f"  - {w}")
    print(f"  Следующий этап: {ilr.NEXT_STAGE}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return int(result.get("_exit_code", 0))


def cmd_income_live_execute(args: argparse.Namespace) -> int:
    from modules import income_live_execution as ile

    send_live = bool(getattr(args, "send_live", False))

    # Read-only клиент ТОЛЬКО для tradability preflight (справочные данные инструмента
    # + режим торгов). Не для исполнения; live-токен не используется. Если read-only
    # API недоступен — работаем без проверки (tradability_checked=false).
    client = None
    if not getattr(args, "no_tradability_check", False):
        try:
            from api.client import ReadOnlyClient
            client = ReadOnlyClient()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"Read-only API недоступен ({exc}); tradability preflight "
                "пропущен (live-отправка будет заблокирована до проверки).")
            client = None

    try:
        result = ile.run(
            ticker=getattr(args, "ticker", ile.DEFAULT_TICKER),
            live_account_id=getattr(args, "live_account_id", None),
            max_order_rub=getattr(args, "max_order_rub", ile.DEFAULT_MAX_ORDER_RUB),
            lots=getattr(args, "lots", ile.DEFAULT_LOTS),
            instrument_id_source=getattr(args, "instrument_id_source", "auto"),
            send_live=send_live,
            confirm=getattr(args, "confirm", None),
            dry_run=getattr(args, "dry_run", True),
            readiness_report=args.readiness_report,
            preview_report=args.preview_report,
            output_json=args.output_json,
            output_md=args.output_md,
            client=client,
        )
    except ile.LiveExecutionError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-live-execute: {exc}")
        return 1

    lp = result["live_plan"]
    tp = result["token_policy"]
    g = result["guards"]
    print("Income live execute — F4.1 (tiny live manual-confirmed order)")
    print("⚠️ РЕАЛЬНЫЕ ДЕНЬГИ. Только T/BUY/LIMIT/1 лот/cap 300 RUB. MARKET запрещён. "
          "Аналитический токен для исполнения не используется.")
    print(f"  stage: {result['stage']} | mode: {result['mode']} | "
          f"ticker: {result['ticker']}")
    print(f"  readiness_gate_passed: {result['readiness_gate_passed']} | "
          f"preview_gate_passed: {result['preview_gate_passed']}")
    print(f"  live_tradability_checked: {result['live_tradability_checked']} | "
          f"live_tradability_passed: {result['live_tradability_passed']} | "
          f"trading_status: {result['live_trading_status']}")
    print(f"  live_postorder_blocked_before_call: "
          f"{result['live_postorder_blocked_before_call']}")
    print(f"  live_plan: {lp['ticker']} {lp['side']} {lp['order_type']} "
          f"{lp['lots']} лот(а), cap {lp['max_order_rub']} RUB, "
          f"instrument_id_source={lp['instrument_id_source']}")
    print(f"  required_confirmation_phrase: {result['required_confirmation_phrase']}")
    print(f"  confirmation_matched: {result['confirmation_matched']}")
    print(f"  {tp['live_trading_token_env']} present: "
          f"{tp['live_trading_token_present']} (только наличие; значение не печатается)")
    print(f"  order sent: {g[ile.GUARD_KEY_LIVE_ORDER_SENT]} | "
          f"sandbox_order_sent: {g['sandbox_order_sent']} | "
          f"market_order_used: {g['market_order_used']}")
    for r in result.get("blocking_reasons", []):
        print(f"  ! blocked: {r}")
    for w in result.get("warnings", []):
        print(f"  - {w}")
    print(f"  Следующий этап: {ile.NEXT_STAGE}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return int(result.get("_exit_code", 0))


def cmd_income_live_status(args: argparse.Namespace) -> int:
    from modules import income_live_status as ils

    try:
        result = ils.run(
            order_id=getattr(args, "order_id", None),
            live_account_id=getattr(args, "live_account_id", None),
            watch=bool(getattr(args, "watch", False)),
            interval_sec=getattr(args, "interval_sec", ils.DEFAULT_INTERVAL_SEC),
            timeout_sec=getattr(args, "timeout_sec", ils.DEFAULT_TIMEOUT_SEC),
            output_json=args.output_json,
            output_md=args.output_md,
        )
    except ils.LiveOrderStatusError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-live-order-status: {exc}")
        return 1

    g = result["guards"]
    print("Income live order status — F4.2 (READ ONLY мониторинг статуса заявки)")
    print("Только чтение GetOrderState. Никаких PostOrder/отмены/продаж/ретраев/MARKET.")
    print(f"  stage: {result['stage']} | mode: {result['mode']}")
    print(f"  order_id: {result['order_id']} | account: "
          f"{result['live_account_id_masked']}")
    print(f"  execution_report_status: {result['execution_report_status']}")
    print(f"  lots_requested: {result['lots_requested']} | "
          f"lots_executed: {result['lots_executed']}")
    print(f"  is_terminal: {result['is_terminal']} | is_filled: {result['is_filled']} "
          f"| is_partially_filled: {result['is_partially_filled']} | "
          f"is_rejected: {result['is_rejected']} | is_cancelled: {result['is_cancelled']}")
    print(f"  checks_count: {result['checks_count']} | "
          f"watch_timed_out: {result['watch_timed_out']}")
    print(f"  post_order_called: {g['post_order_called']} | "
          f"cancel called: {g[ils.GUARD_CANCEL_CALLED]} | "
          f"sell_order_sent: {g['sell_order_sent']} | "
          f"market_order_used: {g['market_order_used']}")
    for e in result.get("errors", []):
        print(f"  ! {e}")
    for w in result.get("warnings", []):
        print(f"  - {w}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return int(result.get("_exit_code", 0))


def cmd_income_live_position(args: argparse.Namespace) -> int:
    from modules import income_live_position as ilp

    # Read-only клиент ТОЛЬКО для аналитики/портфеля (TINKOFF_TOKEN). Live/sandbox
    # токен НЕ используется. Если read-only API недоступен (нет TINKOFF_TOKEN) —
    # падаем чисто, без сетевых вызовов.
    client = None
    client_error = None
    try:
        from api.client import ReadOnlyClient
        client = ReadOnlyClient()
    except Exception as exc:  # noqa: BLE001
        client_error = str(exc)
        client = None

    try:
        result = ilp.run(
            ticker=getattr(args, "ticker", "T"),
            order_id=getattr(args, "order_id", None),
            live_account_id=getattr(args, "live_account_id", None),
            f41_report=args.f41_report,
            f42_report=args.f42_report,
            output_json=args.output_json,
            output_md=args.output_md,
            client=client,
            client_error=client_error,
        )
    except ilp.LivePositionError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-live-position-report: {exc}")
        return 1

    g = result["guards"]
    print("Income live position report — F4.3 (READ ONLY сверка позиции)")
    print("Только read-only portfolio/positions. Никаких PostOrder/отмены/продаж/"
          "ретраев/MARKET. Live/sandbox токен не используется.")
    print(f"  stage: {result['stage']} | mode: {result['mode']} | "
          f"ticker: {result['ticker']}")
    print(f"  order_id: {result['order_id']} | account: "
          f"{result['live_account_id_masked']}")
    print(f"  order_status: {result['order_status']} | "
          f"lots_executed: {result['lots_executed']}")
    print(f"  position_found: {result['position_found']} | "
          f"position_quantity_lots: {result['position_quantity_lots']} | "
          f"position_quantity_units: {result['position_quantity_units']}")
    print(f"  **reconciliation_passed: {result['reconciliation_passed']}**")
    print(f"  base_monthly_living_basket_rub: "
          f"{result['base_monthly_living_basket_rub']} | "
          f"income_target_coverage_pct: {result['income_target_coverage_pct']}")
    print(f"  live_token_used: {g['live_token_used']} | "
          f"sandbox_token_used: {g['sandbox_token_used']} | "
          f"post_order_called: {g['post_order_called']} | "
          f"cancel called: {g[ilp.GUARD_CANCEL_CALLED]}")
    for r in result.get("reconciliation_warnings", []):
        print(f"  · recon: {r}")
    for e in result.get("errors", []):
        print(f"  ! {e}")
    for w in result.get("warnings", []):
        print(f"  - {w}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return int(result.get("_exit_code", 0))


def cmd_income_live_fill_attribution(args: argparse.Namespace) -> int:
    from modules import income_live_fill_attribution as ilfa

    # Read-only клиент ТОЛЬКО для аналитики/операций/портфеля (TINKOFF_TOKEN).
    # Live/sandbox токен НЕ используется. Нет TINKOFF_TOKEN — падаем чисто, без сети.
    client = None
    client_error = None
    try:
        from api.client import ReadOnlyClient
        client = ReadOnlyClient()
    except Exception as exc:  # noqa: BLE001
        client_error = str(exc)
        client = None

    try:
        result = ilfa.run(
            ticker=getattr(args, "ticker", "T"),
            order_id=getattr(args, "order_id", None),
            live_account_id=getattr(args, "live_account_id", None),
            f41_report=args.f41_report,
            f42_report=args.f42_report,
            f43_report=args.f43_report,
            output_json=args.output_json,
            output_md=args.output_md,
            client=client,
            client_error=client_error,
        )
    except ilfa.FillAttributionError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-live-fill-attribution: {exc}")
        return 1

    g = result["guards"]
    print("Income live fill attribution — F4.4 (READ ONLY атрибуция сделки)")
    print("Только read-only operations/portfolio. Никаких PostOrder/отмены/продаж/"
          "ретраев/MARKET. Live/sandbox токен не используется.")
    print(f"  stage: {result['stage']} | mode: {result['mode']} | "
          f"ticker: {result['ticker']}")
    print(f"  order_id: {result['order_id']} | account: "
          f"{result['live_account_id_masked']}")
    print(f"  fill_attribution_confidence: {result['fill_attribution_confidence']} "
          f"(method: {result['attribution_method']})")
    print(f"  fill: units={result['fill_quantity_units']} price={result['fill_price']} "
          f"commission={result['fill_commission']} source={result['fill_source']}")
    print(f"  current TOTAL: units={result['current_total_position_units']} "
          f"avg={result['current_average_position_price']} "
          f"total_unrealized_pnl={result['current_total_unrealized_pnl']} "
          f"(src: {result['current_total_position_source']})")
    print(f"  new-fill: value={result['estimated_new_fill_current_value']} "
          f"pnl={result['estimated_new_fill_unrealized_pnl']} "
          f"weight%={result['estimated_new_fill_weight_in_position_pct']}")
    print(f"  prev (estimated): units={result['estimated_previous_position_units']} "
          f"avg={result['estimated_previous_average_price']}")
    print(f"  live_token_used: {g['live_token_used']} | "
          f"sandbox_token_used: {g['sandbox_token_used']} | "
          f"post_order_called: {g['post_order_called']} | "
          f"cancel called: {g[ilfa.GUARD_CANCEL_CALLED]}")
    for e in result.get("errors", []):
        print(f"  ! {e}")
    for w in result.get("warnings", []):
        print(f"  - {w}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return int(result.get("_exit_code", 0))


def cmd_income_live_fill_economics(args: argparse.Namespace) -> int:
    from modules import income_live_fill_economics as ilfe

    # Read-only клиент ТОЛЬКО для опционального refresh цены (TINKOFF_TOKEN), и то
    # лишь если current_price отсутствует в отчётах. Live/sandbox токен НЕ
    # используется. Нет TINKOFF_TOKEN — не блокирует, если отчётов достаточно.
    client = None
    client_error = None
    try:
        from api.client import ReadOnlyClient
        client = ReadOnlyClient()
    except Exception as exc:  # noqa: BLE001
        client_error = str(exc)
        client = None

    try:
        result = ilfe.run(
            ticker=getattr(args, "ticker", "T"),
            order_id=getattr(args, "order_id", None),
            live_account_id=getattr(args, "live_account_id", None),
            f41_report=args.f41_report,
            f42_report=args.f42_report,
            f43_report=args.f43_report,
            f44_report=args.f44_report,
            output_json=args.output_json,
            output_md=args.output_md,
            client=client,
            client_error=client_error,
        )
    except ilfe.FillEconomicsError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-live-fill-economics: {exc}")
        return 1

    g = result["guards"]
    print("Income live fill economics — F4.5 (READ ONLY экономика сделки)")
    print("Только read-only отчёты/цена. Никаких PostOrder/отмены/продаж/ретраев/"
          "MARKET. Live/sandbox токен не используется.")
    print(f"  stage: {result['stage']} | mode: {result['mode']} | "
          f"ticker: {result['ticker']}")
    print(f"  order_id: {result['order_id']} | account: "
          f"{result['live_account_id_masked']}")
    print(f"  fill: units={result['fill_quantity_units']} "
          f"price={result['fill_price']} gross={result['fill_gross_amount']} "
          f"cash_outflow={result['fill_cash_outflow']}")
    print(f"  new-fill gross PnL: {result['new_fill_gross_unrealized_pnl']} "
          f"| net PnL (after commission): "
          f"{result['new_fill_net_unrealized_pnl_after_commission']}")
    print(f"  commission_drag_rub: {result['commission_drag_rub']} | "
          f"break_even: {result['break_even_price_after_commission']} | "
          f"distance_to_break_even_rub: {result['distance_to_break_even_rub']}")
    print(f"  TOTAL position PnL (отдельно): "
          f"{result['current_total_unrealized_pnl']} "
          f"(kept_separate={result['total_position_pnl_kept_separate']})")
    print(f"  live_token_used: {g['live_token_used']} | "
          f"sandbox_token_used: {g['sandbox_token_used']} | "
          f"post_order_called: {g['post_order_called']} | "
          f"cancel called: {g[ilfe.GUARD_CANCEL_CALLED]}")
    for e in result.get("errors", []):
        print(f"  ! {e}")
    for w in result.get("warnings", []):
        print(f"  - {w}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return int(result.get("_exit_code", 0))


def cmd_income_live_income_validation(args: argparse.Namespace) -> int:
    from modules import income_live_income_validation as iliv

    # Read-only клиент ТОЛЬКО для read-only валидации доходных данных (TINKOFF_TOKEN).
    # Live/sandbox токен НЕ используется. Нет TINKOFF_TOKEN — не блокирует, если
    # локальных отчётов достаточно (income-поля = null с объяснением).
    client = None
    client_error = None
    try:
        from api.client import ReadOnlyClient
        client = ReadOnlyClient()
    except Exception as exc:  # noqa: BLE001
        client_error = str(exc)
        client = None

    try:
        result = iliv.run(
            ticker=getattr(args, "ticker", "T"),
            order_id=getattr(args, "order_id", None),
            live_account_id=getattr(args, "live_account_id", None),
            f41_report=args.f41_report,
            f42_report=args.f42_report,
            f43_report=args.f43_report,
            f44_report=args.f44_report,
            f45_report=args.f45_report,
            output_json=args.output_json,
            output_md=args.output_md,
            client=client,
            client_error=client_error,
        )
    except iliv.IncomeValidationError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка income-live-income-validation: {exc}")
        return 1

    g = result["guards"]
    print("Income live income validation — F4.6 (READ ONLY валидация доходных данных)")
    print("Только read-only отчёты/доходные данные. Никаких PostOrder/отмены/продаж/"
          "ретраев/MARKET. Live/sandbox токен не используется.")
    print(f"  stage: {result['stage']} | mode: {result['mode']} | "
          f"ticker: {result['ticker']}")
    print(f"  order_id: {result['order_id']} | account: "
          f"{result['live_account_id_masked']}")
    print(f"  instrument: figi={result['figi']} uid={result['instrument_uid']} "
          f"class={result['class_code']}")
    print(f"  income_data_checked: {result['income_data_checked']} | "
          f"reliable: {result['reliable_income_data_found']} | "
          f"confidence: {result['income_data_confidence']} | "
          f"source: {result['income_data_source']}")
    print(f"  sources_checked: {result['income_data_sources_checked']}")
    print(f"  expected per-unit: {result['expected_dividend_per_unit_rub']} | "
          f"new-fill yearly: {result['expected_income_rub_yearly_new_fill']} | "
          f"total yearly: {result['expected_income_rub_yearly_total_position']}")
    print(f"  next event: {result['next_known_income_event_date']} "
          f"({result['next_known_income_event_type']}) "
          f"amount/unit={result['next_known_income_event_amount_per_unit']}")
    print(f"  income_validation_passed: {result['income_validation_passed']} | "
          f"blocking: {result['income_validation_blocking_reasons']}")
    print(f"  live_token_used: {g['live_token_used']} | "
          f"sandbox_token_used: {g['sandbox_token_used']} | "
          f"post_order_called: {g['post_order_called']} | "
          f"cancel called: {g[iliv.GUARD_CANCEL_CALLED]}")
    for e in result.get("errors", []):
        print(f"  ! {e}")
    for w in result.get("warnings", []):
        print(f"  - {w}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return int(result.get("_exit_code", 0))


def cmd_dashboard(args: argparse.Namespace) -> int:
    # F4.7 локальный READ-ONLY дашборд. НЕ инициализирует брокер-клиент, НЕ читает
    # токены, НЕ ходит в сеть. Только читает локальные data/reports/*.json.
    from modules import read_only_dashboard as dash

    host = getattr(args, "host", dash.DEFAULT_HOST)
    port = int(getattr(args, "port", dash.DEFAULT_PORT))
    reports_dir = getattr(args, "reports_dir", dash.DEFAULT_REPORTS_DIR)

    print("F4.7 read-only dashboard — локальный просмотрщик отчётов (READ ONLY)")
    print("Только data/reports/*.json. Не торгует, без токенов/брокера/сети, "
          "без POST/действий.")
    if host not in dash._LOCAL_HOSTS:
        print(f"  ⚠️ ВНИМАНИЕ: host={host} (не localhost) — дашборд может быть "
              "доступен другим в сети. Рекомендуется 127.0.0.1.")

    try:
        httpd = dash.serve(host=host, port=port, reports_dir=reports_dir)
    except OSError as exc:
        logger.error(f"Не удалось привязать {host}:{port}: {exc}")
        return 1

    url = f"http://{host}:{port}"
    print(f"  Открой в браузере: {url}")
    print("  Остановить: Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Остановлено (Ctrl+C).")
    finally:
        httpd.server_close()
    return 0


def cmd_portfolio_dashboard(args: argparse.Namespace) -> int:
    # F4.9 локальный READ-ONLY портфельный кокпит. НЕ инициализирует брокер-клиент,
    # НЕ читает токены, НЕ ходит в сеть. Рендерит только data/reports/
    # portfolio_dashboard_data.json (отчёт F4.8).
    from pathlib import Path

    from modules import portfolio_dashboard as pdash

    host = getattr(args, "host", pdash.DEFAULT_HOST)
    port = int(getattr(args, "port", pdash.DEFAULT_PORT))
    report_path = getattr(args, "report_path", pdash.DEFAULT_REPORT_PATH)

    print("F4.9 portfolio cockpit — локальный read-only дашборд (РЕНДЕР F4.8)")
    print("Только data/reports/portfolio_dashboard_data.json. Не торгует, без "
          "токенов/брокера/сети, без POST/действий.")
    if host not in pdash._LOCAL_HOSTS:
        print(f"  ⚠️ ВНИМАНИЕ: host={host} (не localhost) — дашборд может быть "
              "доступен другим в сети. Рекомендуется 127.0.0.1.")
    if not Path(report_path).exists():
        print(f"  ⚠️ Отчёт F4.8 не найден ({report_path}). Сначала запустите: "
              "python main.py portfolio-dashboard-data --live-account-id <ACCOUNT_ID>")

    try:
        httpd = pdash.serve(host=host, port=port, report_path=report_path)
    except OSError as exc:
        logger.error(f"Не удалось привязать {host}:{port}: {exc}")
        return 1

    print(f"  Открой в браузере: http://{host}:{port}")
    print("  Остановить: Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Остановлено (Ctrl+C).")
    finally:
        httpd.server_close()
    return 0


def cmd_portfolio_dashboard_data(args: argparse.Namespace) -> int:
    from modules import portfolio_dashboard_data as pdd

    # Read-only клиент ТОЛЬКО для аналитики (portfolio/operations/market/dividends)
    # через TINKOFF_TOKEN. Live/sandbox токен НЕ используется. Нет токена — не
    # блокируем: partial-режим из локальных отчётов.
    client = None
    try:
        from api.client import ReadOnlyClient
        client = ReadOnlyClient()
    except Exception:  # noqa: BLE001 — без токена работаем partial
        client = None

    try:
        result = pdd.run(
            live_account_id=getattr(args, "live_account_id", None),
            reports_dir=getattr(args, "reports_dir", "data/reports"),
            contribution_plan_path=getattr(args, "contribution_plan", None),
            output_json=args.output_json,
            output_md=args.output_md,
            client=client,
        )
    except pdd.PortfolioDashboardError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Ошибка portfolio-dashboard-data: {exc}")
        return 1

    g = result["guards"]
    kpi = result["dashboard_kpi"]
    print("Portfolio dashboard data — F4.8 (READ ONLY модель данных)")
    print("Только read-only отчёты/портфель/операции. Не торгует, без записи/мутаций. "
          "Live/sandbox токен не используется.")
    print(f"  stage: {result['stage']} | mode: {result['mode']} | account: "
          f"{result['live_account_id_masked']}")
    print(f"  data_freshness: {result['data_freshness'].get('overall')} | "
          f"sources: {result['data_sources_used']}")
    print(f"  portfolio_value: {kpi['portfolio_value_rub']} | cash: {kpi['cash_rub']} "
          f"| positions: {result['portfolio_summary']['positions_count']}")
    print(f"  passive_income/mo: {kpi['passive_income_monthly_rub']} | coverage%: "
          f"{kpi['passive_income_coverage_pct']} | gap/mo: {kpi['income_gap_rub_monthly']}")
    print(f"  turnover_ytd: {kpi['turnover_ytd_rub']} / {kpi['turnover_annual_target_rub']} "
          f"(progress% {kpi['turnover_ytd_progress_pct']})")
    print(f"  safety_status: {kpi['safety_status']} | live_token_used: "
          f"{g['live_token_used']} | sandbox_token_used: {g['sandbox_token_used']} | "
          f"cancel called: {g[pdd.GUARD_CANCEL_CALLED]}")
    for w in result.get("warnings", []):
        print(f"  - {w}")
    logger.info(f"Отчёт: {result['_output_json']}")
    logger.info(f"Отчёт: {result['_output_md']}")
    return int(result.get("_exit_code", 0))


# ─── F4.10 contribution plan (локальный учёт, не торговля) ─────────────────────

def _cp_as_of(args):
    from datetime import date

    from modules import contribution_plan as cp
    raw = getattr(args, "as_of", None)
    if raw:
        d = date.fromisoformat(str(raw)) if _cp_valid_date(raw) else None
        if d is None:
            raise cp.ContributionPlanError(f"Невалидная --as-of дата: {raw}")
        return d
    return cp.today()


def _cp_valid_date(value) -> bool:
    from datetime import date
    try:
        date.fromisoformat(str(value))
        return True
    except (ValueError, TypeError):
        return False


def _cp_print_status(cp, status: dict) -> None:
    print(f"  status: {status['status']} | as_of: {status['as_of_date']} | "
          f"включён: {status['contributions_tracking_enabled']}")
    print(f"  план: неделя={status['plan_weekly_rub']} месяц={status['plan_monthly_rub']}")
    print(f"  факт: неделя={status['contribution_fact_weekly_rub']} "
          f"месяц={status['contribution_fact_monthly_rub']} "
          f"ytd={status['contribution_fact_ytd_rub']}")
    print(f"  разрыв: неделя={status['contribution_gap_weekly_rub']} "
          f"месяц={status['contribution_gap_monthly_rub']} "
          f"ytd={status['contribution_gap_ytd_rub']}")
    print(f"  пропущено: неделя={status['missed_contributions_count_week']} "
          f"месяц={status['missed_contributions_count_month']} "
          f"ytd={status['missed_contributions_count_ytd']}")
    print(f"  след. взнос: {status['next_planned_contribution_date']} "
          f"(через {status['days_until_next_planned_contribution']} дн.) | "
          f"довнести: {status['contribution_required_to_catch_up_rub']}")
    g = status["guards"]
    print(f"  guards: broker_api_called={g['broker_api_called']} "
          f"config_mutated={g['config_mutated']} "
          f"telegram_sent={g['telegram_sent']} token_printed={g['token_printed']}")
    for w in status.get("warnings", []):
        print(f"  - {w}")
    for e in status.get("errors", []):
        print(f"  ! {e}")


def cmd_contribution_plan_init(args: argparse.Namespace) -> int:
    from modules import contribution_plan as cp
    path = getattr(args, "config_path", cp.DEFAULT_CONFIG_PATH)
    try:
        existing = cp.load_plan(path)
        plan = cp.init_plan(
            weekly_rub=args.weekly_rub, monthly_rub=args.monthly_rub,
            start_date=args.start_date, next_date=args.next_date,
            currency=getattr(args, "currency", "rub"),
            source=getattr(args, "source", "manual"),
            fact_source=getattr(args, "fact_source", cp.DEFAULT_FACT_SOURCE),
            manual_facts_enabled=getattr(args, "manual_facts_enabled", False),
            existing=existing, reset_facts=getattr(args, "reset_facts", False))
        out_path = cp.save_plan(plan, path)
    except cp.ContributionPlanError as exc:
        logger.error(str(exc))
        return 1
    kept = len(plan.get("facts") or [])
    print("Contribution plan init — F4.10 (ЛОКАЛЬНО, не торговля)")
    print("Только data/config/contribution_plan.json. Без брокера/токенов/сети.")
    print(f"  записан: {out_path}")
    print(f"  план: неделя={plan['plan_weekly_rub']} месяц={plan['plan_monthly_rub']} "
          f"старт={plan['plan_start_date']} след={plan['next_planned_contribution_date']}")
    print(f"  fact_source={plan['fact_source']} "
          f"manual_facts_enabled={plan['manual_facts_enabled']}")
    print("  Факт пополнений по умолчанию берётся из read-only операций брокера "
          "(F4.8 dashboard). Ручные facts — только fallback/корректировки.")
    print(f"  фактов сохранено: {kept}"
          + (" (reset)" if getattr(args, 'reset_facts', False) else ""))
    return 0


def cmd_contribution_plan_add(args: argparse.Namespace) -> int:
    from modules import contribution_plan as cp
    path = getattr(args, "config_path", cp.DEFAULT_CONFIG_PATH)
    plan = cp.load_plan(path)
    if plan is None:
        logger.error("План не найден. Сначала: " + cp.SETUP_HINT)
        return 1
    try:
        plan, added = cp.add_fact(
            plan, date_str=args.date, amount_rub=args.amount_rub,
            note=getattr(args, "note", None),
            allow_duplicate=getattr(args, "allow_duplicate", False))
    except cp.ContributionPlanError as exc:
        logger.error(str(exc))
        return 1
    if not added:
        print(f"Дубликат {args.date} / {args.amount_rub} ₽ не добавлен "
              "(используйте --allow-duplicate).")
        return 0
    out_path = cp.save_plan(plan, path)
    try:
        as_of = _cp_as_of(args)
    except cp.ContributionPlanError as exc:
        logger.error(str(exc))
        return 1
    status = cp.compute_status(plan, as_of=as_of)
    print("Contribution plan add — F4.10 (ЛОКАЛЬНО, не торговля)")
    print(f"  добавлен факт: {args.date} / {args.amount_rub} ₽ → {out_path}")
    print(f"  фактов всего: {len(plan.get('facts') or [])}")
    print("  ВНИМАНИЕ: ручные facts — это fallback/корректировки. По умолчанию "
          "факт пополнений для дашборда F4.8 берётся из read-only операций брокера "
          "(fact_source=api_operations).")
    _cp_print_status(cp, status)
    return 0


def cmd_contribution_plan_status(args: argparse.Namespace) -> int:
    from modules import contribution_plan as cp
    path = getattr(args, "config_path", cp.DEFAULT_CONFIG_PATH)
    plan = cp.load_plan(path)
    try:
        as_of = _cp_as_of(args)
    except cp.ContributionPlanError as exc:
        logger.error(str(exc))
        return 1
    status = cp.compute_status(plan, as_of=as_of)
    result = cp.write_status_report(
        status,
        json_path=getattr(args, "output_json", cp.DEFAULT_STATUS_JSON),
        md_path=getattr(args, "output_md", cp.DEFAULT_STATUS_MD))
    print("Contribution plan status — F4.10 (ЛОКАЛЬНО, не торговля)")
    print("Только локальные config/reports. Без брокера/токенов/сети/торговли.")
    print("  ПРИМЕЧАНИЕ: read-only операции брокера в этой CLI-команде НЕ читаются — "
          "факт здесь только локальный/ручной. Авторитетный факт пополнений считает "
          "дашборд F4.8 (portfolio-dashboard-data) из read-only операций API.")
    _cp_print_status(cp, status)
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

    p_rmd = sub.add_parser(
        "income-resolver-mapping-diagnostics",
        help="READ-ONLY resolver/mapping диагностика неразрешённых income "
             "кандидатов (audit group D); не маппит и не включает автоматически")
    p_rmd.add_argument(
        "--input-json", dest="input_json",
        default="data/reports/income_universe_disabled_audit.json",
        help="Путь к income_universe_disabled_audit.json (только чтение)")
    p_rmd.add_argument(
        "--output-json", dest="output_json",
        default="data/reports/income_resolver_mapping_diagnostics.json",
        help="Путь для JSON-отчёта resolver/mapping диагностики")
    p_rmd.add_argument(
        "--output-md", dest="output_md",
        default="data/reports/income_resolver_mapping_diagnostics.md",
        help="Путь для Markdown-отчёта resolver/mapping диагностики")
    p_rmd.add_argument(
        "--offline", action="store_true",
        help="Работать только по audit-отчёту, без read-only API enrichment")

    p_odr = sub.add_parser(
        "income-owner-decision-report",
        help="READ-ONLY owner-only decision report (F1): объединяет income "
             "universe, audit, coupon, floating policy, resolver и target context; "
             "заявки не отправляются")
    p_odr.add_argument(
        "--universe-report", dest="universe_report",
        default="data/reports/income_universe_builder_report.json",
        help="Путь к income_universe_builder_report.json (только чтение)")
    p_odr.add_argument(
        "--audit-json", dest="audit_json",
        default="data/reports/income_universe_disabled_audit.json",
        help="Путь к income_universe_disabled_audit.json (только чтение)")
    p_odr.add_argument(
        "--coupon-json", dest="coupon_json",
        default="data/reports/income_coupon_validation.json",
        help="Путь к income_coupon_validation.json (только чтение)")
    p_odr.add_argument(
        "--floating-policy-json", dest="floating_policy_json",
        default="data/reports/income_floating_coupon_policy.json",
        help="Путь к income_floating_coupon_policy.json (только чтение)")
    p_odr.add_argument(
        "--resolver-json", dest="resolver_json",
        default="data/reports/income_resolver_mapping_diagnostics.json",
        help="Путь к income_resolver_mapping_diagnostics.json (только чтение)")
    p_odr.add_argument(
        "--target-json", dest="target_json",
        default="data/reports/target_portfolio.json",
        help="Путь к target_portfolio.json (опционально, только чтение)")
    p_odr.add_argument(
        "--output-json", dest="output_json",
        default="data/reports/income_owner_decision_report.json",
        help="Путь для JSON-отчёта owner decision report")
    p_odr.add_argument(
        "--output-md", dest="output_md",
        default="data/reports/income_owner_decision_report.md",
        help="Путь для Markdown-отчёта owner decision report")
    p_odr.add_argument(
        "--max-candidates", dest="max_candidates", type=int, default=30,
        help="Максимум кандидатов в отчёте (по умолчанию 30)")
    p_odr.add_argument(
        "--min-score", dest="min_score", type=int, default=None,
        help="Опциональный фильтр: исключить кандидатов со score < min-score")
    p_odr.add_argument(
        "--offline", action="store_true",
        help="Только локальные отчёты (по умолчанию и так без сети)")

    p_iop = sub.add_parser(
        "income-order-preview",
        help="READ-ONLY F2 order preview / no-send для BUY_CANDIDATE из owner "
             "decision report; заявки не отправляются, orders-service не вызывается")
    p_iop.add_argument(
        "--decision-json", dest="decision_json",
        default="data/reports/income_owner_decision_report.json",
        help="Путь к income_owner_decision_report.json (только чтение)")
    p_iop.add_argument(
        "--output-json", dest="output_json",
        default="data/reports/income_order_preview.json",
        help="Путь для JSON-отчёта order preview")
    p_iop.add_argument(
        "--output-md", dest="output_md",
        default="data/reports/income_order_preview.md",
        help="Путь для Markdown-отчёта order preview")
    p_iop.add_argument(
        "--candidate-action", dest="candidate_action", default="BUY_CANDIDATE",
        help="proposed_action из F1 для preview (по умолчанию BUY_CANDIDATE)")
    p_iop.add_argument(
        "--ticker", dest="ticker", action="append", default=None,
        help="Опциональный фильтр по тикеру (повторяемый: --ticker T --ticker VTBR)")
    p_iop.add_argument(
        "--max-candidates", dest="max_candidates", type=int, default=5,
        help="Максимум кандидатов в preview (по умолчанию 5)")
    p_iop.add_argument(
        "--max-order-rub", dest="max_order_rub", type=int, default=1000,
        help="Preview cap размера превью в рублях (НЕ лимит реальной заявки)")
    p_iop.add_argument(
        "--min-lots", dest="min_lots", type=int, default=1,
        help="Минимальное число лотов в preview (по умолчанию 1)")
    p_iop.add_argument(
        "--max-lots", dest="max_lots", type=int, default=None,
        help="Опциональный максимум лотов в preview")
    p_iop.add_argument(
        "--price-mode", dest="price_mode",
        choices=("auto", "offline", "readonly-api"), default="auto",
        help="Источник цены: auto (read-only API, fallback offline), offline, "
             "readonly-api")
    p_iop.add_argument(
        "--offline", action="store_true",
        help="Ярлык для --price-mode offline (только локальный decision report)")

    p_ise = sub.add_parser(
        "income-sandbox-execute-preview",
        help="F3 sandbox manual-confirmed execution для одного PREVIEW_READY "
             "кандидата из F2; dry-run по умолчанию, live-заявки запрещены")
    p_ise.add_argument(
        "--ticker", dest="ticker", required=True,
        help="Ровно один тикер из F2 preview (например T или VTBR)")
    p_ise.add_argument(
        "--preview-json", dest="preview_json",
        default="data/reports/income_order_preview.json",
        help="Путь к F2 income_order_preview.json (только чтение)")
    p_ise.add_argument(
        "--output-json", dest="output_json",
        default="data/reports/income_sandbox_execution_report.json",
        help="Путь для JSON-отчёта F3")
    p_ise.add_argument(
        "--output-md", dest="output_md",
        default="data/reports/income_sandbox_execution_report.md",
        help="Путь для Markdown-отчёта F3")
    p_ise.add_argument(
        "--sandbox-account-id", dest="sandbox_account_id", default=None,
        help="Sandbox account id (обязателен для --send-sandbox; не для dry-run)")
    p_ise.add_argument(
        "--sandbox-transport", dest="sandbox_transport",
        choices=("unconfigured", "verified-rest", "verified-sdk"),
        default="unconfigured",
        help="Sandbox-транспорт: unconfigured (по умолчанию, отправка заблокирована), "
             "verified-rest (проверенный sandbox REST), verified-sdk (SDK, если "
             "установлен)")
    p_ise.add_argument(
        "--instrument-id-source", dest="instrument_id_source",
        choices=("auto", "uid", "figi"), default="auto",
        help="Источник instrumentId для wire payload: auto (uid-first, figi-fallback), "
             "uid (только uid), figi (только figi)")
    p_ise.add_argument(
        "--max-order-rub", dest="max_order_rub", type=int, default=1000,
        help="Жёсткий cap размера заявки в рублях (по умолчанию 1000)")
    p_ise.add_argument(
        "--max-price-deviation-bps", dest="max_price_deviation_bps", type=int,
        default=100,
        help="Максимальное отклонение свежей цены от preview в bps (по умолчанию 100)")
    p_ise.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="Dry-run (по умолчанию). Реальная отправка только при --send-sandbox")
    p_ise.add_argument(
        "--send-sandbox", dest="send_sandbox", action="store_true", default=False,
        help="Явный флаг: попытаться отправить ОДНУ sandbox-заявку (только при "
             "точном --confirm). Без него заявка не отправляется")
    p_ise.add_argument(
        "--confirm", dest="confirm", default=None,
        help="Точная фраза подтверждения, обязательна для --send-sandbox")
    p_ise.add_argument(
        "--price-mode", dest="price_mode",
        choices=("auto", "offline", "readonly-api"), default="auto",
        help="Источник свежей цены для preflight: auto, offline, readonly-api")
    p_ise.add_argument(
        "--client-order-id-prefix", dest="client_order_id_prefix",
        default="sandbox-f3",
        help="Префикс client order id (по умолчанию sandbox-f3)")

    p_isa = sub.add_parser(
        "income-sandbox-account",
        help="F3.2 sandbox account bootstrap: status/list/open/pay-in. Только "
             "sandbox, заявки не отправляются, live-токен не используется")
    p_isa.add_argument(
        "--action", dest="action",
        choices=("status", "list", "open", "pay-in"), default="status",
        help="status (инспекция, API не вызывается), list (read-only список счетов), "
             "open (создать sandbox-счёт), pay-in (пополнить sandbox-счёт)")
    p_isa.add_argument(
        "--sandbox-transport", dest="sandbox_transport",
        choices=("verified-rest", "unconfigured"), default="verified-rest",
        help="Sandbox-транспорт: verified-rest (по умолчанию, проверенный sandbox "
             "REST), unconfigured (реальные операции заблокированы)")
    p_isa.add_argument(
        "--sandbox-account-id", dest="sandbox_account_id", default=None,
        help="Sandbox account id (обязателен для pay-in)")
    p_isa.add_argument(
        "--pay-in-rub", dest="pay_in_rub", type=int, default=None,
        help="Сумма пополнения sandbox-счёта в рублях (обязательна для pay-in)")
    p_isa.add_argument(
        "--confirm", dest="confirm", default=None,
        help='Точная фраза подтверждения для open/pay-in (например '
             '"CONFIRM SANDBOX ACCOUNT OPEN")')
    p_isa.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="Dry-run (по умолчанию). open/pay-in мутируют sandbox только при "
             "точной фразе --confirm")
    p_isa.add_argument(
        "--output-json", dest="output_json",
        default="data/reports/income_sandbox_account_report.json",
        help="Путь для JSON-отчёта F3.2")
    p_isa.add_argument(
        "--output-md", dest="output_md",
        default="data/reports/income_sandbox_account_report.md",
        help="Путь для Markdown-отчёта F3.2")

    p_ilr = sub.add_parser(
        "income-live-readiness",
        help="F4.0 pre-live readiness: проверяет F3 sandbox FILL-gate и готовит tiny "
             "live plan. НЕ live-исполнение, заявки не отправляются")
    p_ilr.add_argument(
        "--ticker", dest="ticker", default="T",
        help="Тикер tiny live plan (по умолчанию T)")
    p_ilr.add_argument(
        "--lots", dest="lots", type=int, default=1,
        help="Число лотов tiny live plan (по умолчанию 1)")
    p_ilr.add_argument(
        "--max-order-rub", dest="max_order_rub", type=int, default=300,
        help="Жёсткий cap размера будущей live-заявки в рублях (по умолчанию 300)")
    p_ilr.add_argument(
        "--sandbox-report", dest="sandbox_report",
        default="data/reports/income_sandbox_execution_report.json",
        help="Путь к F3 income_sandbox_execution_report.json (только чтение)")
    p_ilr.add_argument(
        "--output-json", dest="output_json",
        default="data/reports/income_live_readiness_report.json",
        help="Путь для JSON-отчёта F4.0")
    p_ilr.add_argument(
        "--output-md", dest="output_md",
        default="data/reports/income_live_readiness_report.md",
        help="Путь для Markdown-отчёта F4.0")

    p_ile = sub.add_parser(
        "income-live-execute",
        help="F4.1 tiny LIVE manual-confirmed order (РЕАЛЬНЫЕ деньги): одна BUY "
             "LIMIT заявка T/1 лот/cap 300 RUB после всех gate'ов и точной фразы. "
             "dry-run по умолчанию")
    p_ile.add_argument(
        "--ticker", dest="ticker", default="T",
        help="Тикер (по умолчанию T)")
    p_ile.add_argument(
        "--live-account-id", dest="live_account_id", default=None,
        help="Live account id (обязателен для --send-live; не для dry-run)")
    p_ile.add_argument(
        "--max-order-rub", dest="max_order_rub", type=int, default=300,
        help="Жёсткий cap размера live-заявки в рублях (по умолчанию 300)")
    p_ile.add_argument(
        "--lots", dest="lots", type=int, default=1,
        help="Число лотов (по умолчанию 1)")
    p_ile.add_argument(
        "--instrument-id-source", dest="instrument_id_source",
        choices=("auto", "uid", "figi"), default="auto",
        help="Источник instrumentId для wire payload: auto (uid-first, figi-fallback), "
             "uid, figi")
    p_ile.add_argument(
        "--send-live", dest="send_live", action="store_true", default=False,
        help="Явный флаг: попытаться отправить ОДНУ live-заявку (только при точном "
             "--confirm, live account id и TINKOFF_LIVE_TRADING_TOKEN). Без него "
             "заявка не отправляется")
    p_ile.add_argument(
        "--confirm", dest="confirm", default=None,
        help='Точная фраза подтверждения, обязательна для --send-live '
             '("CONFIRM LIVE BUY T 1 LOT MAX 300 RUB")')
    p_ile.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="Dry-run (по умолчанию). Реальная отправка только при --send-live")
    p_ile.add_argument(
        "--no-tradability-check", dest="no_tradability_check", action="store_true",
        default=False,
        help="Не выполнять read-only tradability preflight (offline). При --send-live "
             "это оставит инструмент непроверенным и заблокирует отправку")
    p_ile.add_argument(
        "--readiness-report", dest="readiness_report",
        default="data/reports/income_live_readiness_report.json",
        help="Путь к F4.0 income_live_readiness_report.json (только чтение)")
    p_ile.add_argument(
        "--preview-report", dest="preview_report",
        default="data/reports/income_order_preview.json",
        help="Путь к F2 income_order_preview.json (только чтение)")
    p_ile.add_argument(
        "--output-json", dest="output_json",
        default="data/reports/income_live_execution_report.json",
        help="Путь для JSON-отчёта F4.1")
    p_ile.add_argument(
        "--output-md", dest="output_md",
        default="data/reports/income_live_execution_report.md",
        help="Путь для Markdown-отчёта F4.1")

    from modules import income_live_status as _ils
    p_ilos = sub.add_parser(
        "income-live-order-status",
        help="F4.2 READ-ONLY мониторинг статуса УЖЕ созданной live-заявки "
             "(GetOrderState). Не создаёт/не отменяет/не повторяет/не продаёт заявки")
    p_ilos.add_argument(
        "--order-id", dest="order_id", required=True,
        help="ID live-заявки для чтения статуса (read-only)")
    p_ilos.add_argument(
        "--live-account-id", dest="live_account_id", required=True,
        help="Live account id заявки")
    p_ilos.add_argument(
        "--watch", dest="watch", action="store_true", default=False,
        help="Периодически читать статус (read-only) до терминального или timeout")
    p_ilos.add_argument(
        "--interval-sec", dest="interval_sec", type=int, default=10,
        help="Интервал опроса в секундах (по умолчанию 10, только для --watch)")
    p_ilos.add_argument(
        "--timeout-sec", dest="timeout_sec", type=int, default=300,
        help="Максимум опроса в секундах (по умолчанию 300, только для --watch)")
    p_ilos.add_argument(
        "--output-json", dest="output_json", default=_ils.DEFAULT_OUTPUT_JSON,
        help="Путь для JSON-отчёта F4.2")
    p_ilos.add_argument(
        "--output-md", dest="output_md", default=_ils.DEFAULT_OUTPUT_MD,
        help="Путь для Markdown-отчёта F4.2")

    from modules import income_live_execution as _ile_defaults
    from modules import income_live_position as _ilp
    p_ilp = sub.add_parser(
        "income-live-position-report",
        help="F4.3 READ-ONLY сверка реальной позиции с завершённой F4.1/F4.2 "
             "заявкой. Только read-only portfolio; ничего не исполняет")
    p_ilp.add_argument(
        "--ticker", dest="ticker", default="T", help="Тикер (по умолчанию T)")
    p_ilp.add_argument(
        "--order-id", dest="order_id", required=True,
        help="ID завершённой live-заявки (read-only сверка)")
    p_ilp.add_argument(
        "--live-account-id", dest="live_account_id", required=True,
        help="Live account id заявки")
    p_ilp.add_argument(
        "--f41-report", dest="f41_report", default=_ile_defaults.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.1 income_live_execution_report.json (только чтение)")
    p_ilp.add_argument(
        "--f42-report", dest="f42_report", default=_ils.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.2 order status report (только чтение)")
    p_ilp.add_argument(
        "--output-json", dest="output_json", default=_ilp.DEFAULT_OUTPUT_JSON,
        help="Путь для JSON-отчёта F4.3")
    p_ilp.add_argument(
        "--output-md", dest="output_md", default=_ilp.DEFAULT_OUTPUT_MD,
        help="Путь для Markdown-отчёта F4.3")

    from modules import income_live_fill_attribution as _ilfa
    p_ilfa = sub.add_parser(
        "income-live-fill-attribution",
        help="F4.4 READ-ONLY атрибуция новой сделки к завершённой заявке "
             "(отделяет новый лот от прежней позиции). Только read-only")
    p_ilfa.add_argument(
        "--ticker", dest="ticker", default="T", help="Тикер (по умолчанию T)")
    p_ilfa.add_argument(
        "--order-id", dest="order_id", required=True,
        help="ID завершённой live-заявки (read-only атрибуция)")
    p_ilfa.add_argument(
        "--live-account-id", dest="live_account_id", required=True,
        help="Live account id заявки")
    p_ilfa.add_argument(
        "--f41-report", dest="f41_report", default=_ile_defaults.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.1 income_live_execution_report.json (только чтение)")
    p_ilfa.add_argument(
        "--f42-report", dest="f42_report", default=_ils.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.2 order status report (только чтение)")
    p_ilfa.add_argument(
        "--f43-report", dest="f43_report", default=_ilp.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.3 position report (только чтение)")
    p_ilfa.add_argument(
        "--output-json", dest="output_json", default=_ilfa.DEFAULT_OUTPUT_JSON,
        help="Путь для JSON-отчёта F4.4")
    p_ilfa.add_argument(
        "--output-md", dest="output_md", default=_ilfa.DEFAULT_OUTPUT_MD,
        help="Путь для Markdown-отчёта F4.4")

    from modules import income_live_fill_economics as _ilfe
    p_ilfe = sub.add_parser(
        "income-live-fill-economics",
        help="F4.5 READ-ONLY экономика новой сделки поверх F4.4 (gross vs net "
             "PnL, комиссионный drag, безубыток). Только read-only; ничего не исполняет")
    p_ilfe.add_argument(
        "--ticker", dest="ticker", default="T", help="Тикер (по умолчанию T)")
    p_ilfe.add_argument(
        "--order-id", dest="order_id", required=True,
        help="ID завершённой live-заявки (read-only экономика)")
    p_ilfe.add_argument(
        "--live-account-id", dest="live_account_id", required=True,
        help="Live account id заявки")
    p_ilfe.add_argument(
        "--f41-report", dest="f41_report", default=_ile_defaults.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.1 income_live_execution_report.json (только чтение)")
    p_ilfe.add_argument(
        "--f42-report", dest="f42_report", default=_ils.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.2 order status report (только чтение)")
    p_ilfe.add_argument(
        "--f43-report", dest="f43_report", default=_ilp.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.3 position report (только чтение)")
    p_ilfe.add_argument(
        "--f44-report", dest="f44_report", default=_ilfa.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.4 fill attribution report (основной источник, только чтение)")
    p_ilfe.add_argument(
        "--output-json", dest="output_json", default=_ilfe.DEFAULT_OUTPUT_JSON,
        help="Путь для JSON-отчёта F4.5")
    p_ilfe.add_argument(
        "--output-md", dest="output_md", default=_ilfe.DEFAULT_OUTPUT_MD,
        help="Путь для Markdown-отчёта F4.5")

    from modules import income_live_income_validation as _iliv
    p_iliv = sub.add_parser(
        "income-live-income-validation",
        help="F4.6 READ-ONLY валидация доходных данных (есть ли надёжные "
             "дивиденды/доход для инструмента). Только read-only; ничего не исполняет")
    p_iliv.add_argument(
        "--ticker", dest="ticker", default="T", help="Тикер (по умолчанию T)")
    p_iliv.add_argument(
        "--order-id", dest="order_id", required=True,
        help="ID завершённой live-заявки (контекст позиции, read-only)")
    p_iliv.add_argument(
        "--live-account-id", dest="live_account_id", required=True,
        help="Live account id заявки")
    p_iliv.add_argument(
        "--f41-report", dest="f41_report", default=_ile_defaults.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.1 income_live_execution_report.json (только чтение)")
    p_iliv.add_argument(
        "--f42-report", dest="f42_report", default=_ils.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.2 order status report (только чтение)")
    p_iliv.add_argument(
        "--f43-report", dest="f43_report", default=_ilp.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.3 position report (только чтение)")
    p_iliv.add_argument(
        "--f44-report", dest="f44_report", default=_ilfa.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.4 fill attribution report (только чтение)")
    p_iliv.add_argument(
        "--f45-report", dest="f45_report", default=_ilfe.DEFAULT_OUTPUT_JSON,
        help="Путь к F4.5 fill economics report (только чтение)")
    p_iliv.add_argument(
        "--output-json", dest="output_json", default=_iliv.DEFAULT_OUTPUT_JSON,
        help="Путь для JSON-отчёта F4.6")
    p_iliv.add_argument(
        "--output-md", dest="output_md", default=_iliv.DEFAULT_OUTPUT_MD,
        help="Путь для Markdown-отчёта F4.6")

    from modules import read_only_dashboard as _dash
    p_dash = sub.add_parser(
        "dashboard",
        help="F4.7 локальный READ-ONLY веб-дашборд: визуализирует data/reports/"
             "*.json. Не торгует, без токенов/брокера/сети, без POST/действий")
    p_dash.add_argument(
        "--host", dest="host", default=_dash.DEFAULT_HOST,
        help="Адрес привязки (по умолчанию 127.0.0.1; другой host = предупреждение)")
    p_dash.add_argument(
        "--port", dest="port", type=int, default=_dash.DEFAULT_PORT,
        help="Порт (по умолчанию 8765)")
    p_dash.add_argument(
        "--reports-dir", dest="reports_dir", default=_dash.DEFAULT_REPORTS_DIR,
        help="Каталог с локальными отчётами (только чтение)")

    from modules import portfolio_dashboard_data as _pdd
    p_pdd = sub.add_parser(
        "portfolio-dashboard-data",
        help="F4.8 READ-ONLY модель данных портфельного дашборда (агрегирует "
             "F4.1–F4.6 + опц. read-only портфель/операции). Не торгует")
    p_pdd.add_argument(
        "--live-account-id", dest="live_account_id", required=True,
        help="Live account id (read-only; маскируется в отчёте)")
    p_pdd.add_argument(
        "--reports-dir", dest="reports_dir", default="data/reports",
        help="Каталог с локальными отчётами F4.1–F4.6 (только чтение)")
    p_pdd.add_argument(
        "--contribution-plan", dest="contribution_plan",
        default=_pdd.DEFAULT_CONTRIBUTION_PLAN,
        help="Путь к contribution_plan.json (локальный; иначе tracking disabled)")
    p_pdd.add_argument(
        "--output-json", dest="output_json", default=_pdd.DEFAULT_OUTPUT_JSON,
        help="Путь для JSON-отчёта F4.8")
    p_pdd.add_argument(
        "--output-md", dest="output_md", default=_pdd.DEFAULT_OUTPUT_MD,
        help="Путь для Markdown-отчёта F4.8")

    from modules import portfolio_dashboard as _pdash
    p_pdash = sub.add_parser(
        "portfolio-dashboard",
        help="F4.9 локальный READ-ONLY портфельный кокпит: рендерит отчёт F4.8 "
             "portfolio_dashboard_data.json. Не торгует, без токенов/брокера/сети")
    p_pdash.add_argument(
        "--host", dest="host", default=_pdash.DEFAULT_HOST,
        help="Адрес привязки (по умолчанию 127.0.0.1; другой host = предупреждение)")
    p_pdash.add_argument(
        "--port", dest="port", type=int, default=_pdash.DEFAULT_PORT,
        help="Порт (по умолчанию 8766)")
    p_pdash.add_argument(
        "--report-path", dest="report_path", default=_pdash.DEFAULT_REPORT_PATH,
        help="Путь к отчёту F4.8 (только чтение)")

    from modules import contribution_plan as _cp
    p_cpi = sub.add_parser(
        "contribution-plan-init",
        help="F4.10 ЛОКАЛЬНО: создать/обновить план пополнений "
             "data/config/contribution_plan.json (не торговля, без брокера)")
    p_cpi.add_argument("--weekly-rub", dest="weekly_rub", required=True,
                       help="Плановое пополнение в неделю, ₽")
    p_cpi.add_argument("--monthly-rub", dest="monthly_rub", required=True,
                       help="Плановое пополнение в месяц, ₽")
    p_cpi.add_argument("--start-date", dest="start_date", required=True,
                       help="Дата старта плана YYYY-MM-DD")
    p_cpi.add_argument("--next-date", dest="next_date", default=None,
                       help="Дата следующего планового взноса YYYY-MM-DD (или пусто)")
    p_cpi.add_argument("--currency", dest="currency", default="rub")
    p_cpi.add_argument("--source", dest="source", default="manual")
    p_cpi.add_argument(
        "--fact-source", dest="fact_source", default=_cp.DEFAULT_FACT_SOURCE,
        choices=_cp.ALLOWED_FACT_SOURCES,
        help="Источник факта пополнений: api_operations (по умолчанию, read-only "
             "операции F4.8) | manual | mixed")
    p_cpi.add_argument(
        "--manual-facts-enabled", dest="manual_facts_enabled", action="store_true",
        help="Разрешить ручные facts как корректировки (по умолчанию выкл)")
    p_cpi.add_argument("--reset-facts", dest="reset_facts", action="store_true",
                       help="Очистить существующие facts (по умолчанию сохраняются)")
    p_cpi.add_argument("--force", dest="force", action="store_true",
                       help="Явно разрешить перезапись (facts сохраняются)")
    p_cpi.add_argument("--config-path", dest="config_path",
                       default=_cp.DEFAULT_CONFIG_PATH)

    p_cpa = sub.add_parser(
        "contribution-plan-add",
        help="F4.10 ЛОКАЛЬНО: добавить факт пополнения в план (не торговля)")
    p_cpa.add_argument("--date", dest="date", required=True,
                       help="Дата факта YYYY-MM-DD")
    p_cpa.add_argument("--amount-rub", dest="amount_rub", required=True,
                       help="Сумма пополнения, ₽ (> 0)")
    p_cpa.add_argument("--note", dest="note", default=None)
    p_cpa.add_argument("--allow-duplicate", dest="allow_duplicate",
                       action="store_true", help="Разрешить дубликат date+amount")
    p_cpa.add_argument("--as-of", dest="as_of", default=None,
                       help="Дата расчёта статуса YYYY-MM-DD (по умолчанию сегодня)")
    p_cpa.add_argument("--config-path", dest="config_path",
                       default=_cp.DEFAULT_CONFIG_PATH)

    for _name in ("contribution-plan-status", "contribution-plan-report"):
        p_cps = sub.add_parser(
            _name,
            help="F4.10 ЛОКАЛЬНО: статус плана пополнений + отчёт (не торговля)")
        p_cps.add_argument("--as-of", dest="as_of", default=None,
                           help="Дата расчёта YYYY-MM-DD (по умолчанию сегодня)")
        p_cps.add_argument("--config-path", dest="config_path",
                           default=_cp.DEFAULT_CONFIG_PATH)
        p_cps.add_argument("--output-json", dest="output_json",
                           default=_cp.DEFAULT_STATUS_JSON)
        p_cps.add_argument("--output-md", dest="output_md",
                           default=_cp.DEFAULT_STATUS_MD)

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
    "income-resolver-mapping-diagnostics": cmd_income_resolver_mapping_diagnostics,
    "income-owner-decision-report": cmd_income_owner_decision_report,
    "income-order-preview": cmd_income_order_preview,
    "income-sandbox-execute-preview": cmd_income_sandbox_execute_preview,
    "income-sandbox-account": cmd_income_sandbox_account,
    "income-live-readiness": cmd_income_live_readiness,
    "income-live-execute": cmd_income_live_execute,
    "income-live-order-status": cmd_income_live_status,
    "income-live-position-report": cmd_income_live_position,
    "income-live-fill-attribution": cmd_income_live_fill_attribution,
    "income-live-fill-economics": cmd_income_live_fill_economics,
    "income-live-income-validation": cmd_income_live_income_validation,
    "dashboard": cmd_dashboard,
    "portfolio-dashboard-data": cmd_portfolio_dashboard_data,
    "portfolio-dashboard": cmd_portfolio_dashboard,
    "contribution-plan-init": cmd_contribution_plan_init,
    "contribution-plan-add": cmd_contribution_plan_add,
    "contribution-plan-status": cmd_contribution_plan_status,
    "contribution-plan-report": cmd_contribution_plan_status,
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
