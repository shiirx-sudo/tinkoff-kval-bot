"""Консольный вывод Execution Plan (DRY-RUN). Никаких заявок."""
from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.panel import Panel

from modules.execution_planner import ExecutionPlan

_console = Console()


def _money(v) -> str:
    if v is None:
        return "—"
    return f"{Decimal(v):,.0f} ₽".replace(",", " ")


def render(p: ExecutionPlan) -> None:
    status_color = "green" if p.status == "OK" else "red"
    head = (
        f"[bold]Период:[/bold] {p.period}\n"
        f"[bold]Инструмент:[/bold] {p.ticker} / {p.class_code or '—'}\n"
        f"[bold]Статус:[/bold] "
        f"{p.trading_status.replace('SECURITY_TRADING_STATUS_', '') or '—'}\n"
        f"[bold]Вердикт:[/bold] {p.verdict or '—'}\n"
        f"[bold]План:[/bold] [{status_color}]{p.status}[/{status_color}]"
    )
    _console.print(Panel(head, title="Execution Plan — DRY RUN"))

    _console.print(
        f"\nНужно broker trades: {p.broker_trade_count_missing}\n"
        f"Нужно roundtrip cycles: {p.roundtrip_cycle_count_required}\n"
        f"Оборот всего: {_money(p.total_turnover)}\n"
        f"Оборот на цикл: {_money(p.cycle_turnover)}\n"
        f"BUY side notional: {_money(p.side_notional)}\n"
        f"SELL side notional: {_money(p.side_notional)}"
    )

    _console.print("\n[bold]Planned actions:[/bold]")
    if not p.planned_actions:
        _console.print("[dim](нет действий — исполнять нечего)[/dim]")
    for a in p.planned_actions:
        lots = f" (~{a.estimated_lots} лот.)" if a.estimated_lots else ""
        _console.print(
            f"  {a.seq}. {a.side} {a.ticker} на ~{_money(a.notional_rub)}{lots}")

    # Risk checks
    _console.print("\n[bold]Risk checks:[/bold]")
    for c in p.risk_checks:
        mark = "[green]✓[/green]" if c.ok else "[red]✗[/red]"
        _console.print(f"  {mark} {c.name} [dim]({c.detail})[/dim]")

    if p.status == "BLOCKED":
        _console.print(
            "\n[red]План ЗАБЛОКИРОВАН проверками рисков — это всё равно только "
            "dry-run, но условия для исполнения не выполнены.[/red]")

    if p.warnings:
        _console.print("\n[yellow]Предупреждения:[/yellow]")
        for w in p.warnings:
            _console.print(f"[yellow]• {w}[/yellow]")

    _console.print(f"\n[bold]{p.disclaimer}[/bold]")
    _console.print(
        "[dim]Live-исполнение не реализовано: реальный адаптер заявок — отдельный "
        "будущий шаг только после проверки dry-run и явного включения.[/dim]")
