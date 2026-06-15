"""Консольный вывод Execution Preflight (READ-ONLY)."""
from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.panel import Panel

from modules.execution_preflight import PreflightResult

_console = Console()

_RISK_CHECK_ORDER = (
    "spread_within_limit", "depth_sufficient", "side_within_max",
    "actions_are_dry_run", "no_order_endpoints", "no_live_adapter",
)

_STATUS_COLOR = {
    "READY_DRY_RUN": "green", "BLOCKED": "red",
    "STALE_REPORTS": "yellow", "MISSING_REPORTS": "red",
}


def _money(v) -> str:
    if v is None:
        return "—"
    return f"{Decimal(v):,.0f} ₽".replace(",", " ")


def render(p: PreflightResult) -> None:
    if p.status == "MISSING_REPORTS":
        _console.print(Panel(
            "[red]Не хватает отчётов для preflight.[/red]",
            title="Execution Preflight — READ ONLY"))
        for e in p.errors:
            _console.print(f"[red]{e}[/red]")
        return

    head = (
        f"[bold]Дата:[/bold] {p.as_of}\n"
        f"[bold]Период:[/bold] {p.period}\n"
        f"[bold]Инструмент:[/bold] {p.instrument} / {p.class_code or '—'}\n"
        f"[bold]Статус инструмента:[/bold] "
        f"{p.trading_status.replace('SECURITY_TRADING_STATUS_', '') or '—'}\n"
        f"[bold]Вердикт:[/bold] {p.verdict or '—'}"
    )
    _console.print(Panel(head, title="Execution Preflight — READ ONLY"))

    _console.print(
        f"\n[bold]План:[/bold]\n"
        f"Broker trades missing: {p.broker_trade_count_missing}\n"
        f"Roundtrip cycles: {p.roundtrip_cycle_count_required}\n"
        f"Planned actions: {p.planned_actions_count}\n"
        f"Side notional: {_money(p.side_notional)}"
    )

    by_name = {c.name: c for c in p.checks}
    _console.print("\n[bold]Risk checks:[/bold]")
    for name in _RISK_CHECK_ORDER:
        c = by_name.get(name)
        if not c:
            continue
        mark = "[green]✓[/green]" if c.ok else "[red]✗[/red]"
        _console.print(f"  {mark} {name}")

    color = _STATUS_COLOR.get(p.status, "white")
    _console.print(f"\n[bold]Итог:[/bold] [{color}]{p.status}[/{color}]")
    if p.status == "BLOCKED" and p.errors:
        _console.print(f"[red]Причина: {p.errors[0]}[/red]")
    if p.status == "STALE_REPORTS":
        _console.print(
            "[yellow]Отчёты устарели — перезапустите execution-plan.[/yellow]")

    if p.warnings:
        for w in p.warnings:
            _console.print(f"[yellow]• {w}[/yellow]")

    _console.print(
        "\n[dim]Это read-only preflight. Реальные заявки не отправляются.[/dim]")
