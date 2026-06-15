"""Консольный вывод Manual Turnover Plan (read-only)."""
from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.panel import Panel

from modules.turnover_planner import ManualTurnoverPlan

_console = Console()


def _money(v) -> str:
    if v is None:
        return "—"
    return f"{Decimal(v):,.0f} ₽".replace(",", " ")


def _num(v, places: str = "0.00") -> str:
    if v is None:
        return "—"
    return f"{Decimal(v):.2f}"


def render(p: ManualTurnoverPlan) -> None:
    si = p.selected_instrument
    m = p.current_month_plan
    r = p.recommendations

    head = (
        f"[bold]Дата расчёта:[/bold] {p.as_of}\n"
        f"[bold]Период квалификации:[/bold] {p.period_start} … {p.period_end}\n"
        f"[bold]Ближайшая возможная проверка:[/bold] {p.check_date}\n"
        f"[bold]Инструмент:[/bold] {si.ticker} / {si.name or '—'} / "
        f"{si.resolved_class_code or '—'}\n"
        f"[bold]Статус инструмента:[/bold] "
        f"{si.trading_status.replace('SECURITY_TRADING_STATUS_', '') or '—'}\n"
        f"[bold]Вердикт сканера:[/bold] {si.verdict or '—'}"
    )
    _console.print(Panel(head, title="Manual Turnover Plan"))

    _console.print(
        f"\n[bold]Текущий месяц:[/bold] {m.month}\n"
        f"План сделок: {m.planned_required_trade_count}\n"
        f"Есть сделок: {m.current_trade_count}\n"
        f"Не хватает сделок: {m.missing_trade_count}\n"
        f"План оборота: {_money(m.suggested_turnover)}\n"
        f"Текущий оборот: {_money(m.current_turnover)}\n"
        f"Осталось оборота: {_money(m.remaining_turnover)}"
    )

    _console.print("\n[bold]Рекомендация для ручного плана:[/bold]")
    if r.trade_plan_closed:
        _console.print(f"[green]{r.note}[/green]")
    else:
        _console.print(
            f"Оборот ориентировочно на 1 недостающую broker-сделку: "
            f"{_money(r.recommended_trade_turnover)}")
        if p.mode == "roundtrip":
            lots = (f" (≈ {r.recommended_side_lots} лот.)"
                    if r.recommended_side_lots else "")
            _console.print(
                "[bold]Для автоматического исполнения (execution-plan):[/bold]")
            _console.print(
                f"  broker trades: {r.broker_trade_count_required} "
                f"(есть {r.broker_trade_count_current}, "
                f"не хватает {r.broker_trade_count_missing})")
            _console.print(
                f"  roundtrip циклов (BUY+SELL): {r.roundtrip_cycle_count_required}")
            _console.print(
                f"  номинал на сторону BUY/SELL: {_money(r.side_notional)}{lots}")
            _console.print(f"  оборот на цикл: {_money(r.cycle_turnover)}")
            _console.print(
                f"  ожидаемый оборот после исполнения: "
                f"{_money(r.expected_turnover_after_execution)}")
            _console.print(
                "[dim]Подробный план BUY/SELL: python main.py execution-plan[/dim]")

    _console.print("\n[bold]Оценочные издержки:[/bold]")
    _console.print(f"Roundtrip bps: {_num(si.estimated_roundtrip_cost_bps)}")
    _console.print(
        f"Ориентировочная оценка издержек на месяц: "
        f"{_money(si.estimated_monthly_cost_rub)}")

    if p.current_quarter_plan:
        q = p.current_quarter_plan
        _console.print(
            f"\n[bold]Текущий квартал:[/bold] {q.quarter} — "
            f"сделок {q.current_trade_count}/{q.planned_required_trade_count} "
            f"(не хватает {q.missing_trade_count}), "
            f"осталось оборота {_money(q.remaining_turnover)}")

    if p.warnings:
        _console.print("\n[yellow]Предупреждения:[/yellow]")
        for w in p.warnings:
            _console.print(f"[yellow]• {w}[/yellow]")

    _console.print(
        f"\n[bold]Важно:[/bold]\n[dim]{r.disclaimer}\n{p.disclaimer}[/dim]")
