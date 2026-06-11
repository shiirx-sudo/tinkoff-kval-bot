"""Консольный вывод планировщика выхода на квал-статус (read-only)."""
from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from modules.kval_planner import KvalPlan

_console = Console()


def _money(v: Decimal) -> str:
    return f"{v:,.0f} ₽".replace(",", " ")


def _flag(ok: bool) -> str:
    return "[green]OK[/green]" if ok else "[red]FAIL[/red]"


def _status_cell(status: str) -> str:
    return {
        "done_ok": "[green]done_ok[/green]",
        "done_fail": "[red]done_fail[/red]",
        "future_required": "[yellow]future_required[/yellow]",
    }.get(status, status)


def render(plan: KvalPlan) -> None:
    p = plan

    # ─── Ближайшая возможная готовность ─────────────────────────────────────
    if p.earliest is not None:
        head = (
            f"[bold]Дата проверки:[/bold] {p.earliest.check_date}\n"
            f"[bold]Период:[/bold] {p.earliest.period_start} … {p.earliest.period_end}\n"
            f"[bold]Цель:[/bold] {_money(p.goal)} "
            f"([dim]{p.target_mode}[/dim])\n"
            f"[bold]Причина:[/bold] {p.earliest_reason}"
        )
    else:
        head = f"[bold red]Достижимого окна нет.[/bold red]\n{p.earliest_reason}"
    _console.print(Panel(head, title="Ближайшая возможная готовность"))

    # ─── Кандидатные окна ───────────────────────────────────────────────────
    w_table = Table(title="\nКандидатные окна", show_lines=False)
    w_table.add_column("Проверка", style="cyan")
    w_table.add_column("Период")
    w_table.add_column("Оборот", justify="right")
    w_table.add_column("Мес.", justify="center")
    w_table.add_column("Кварт.", justify="center")
    w_table.add_column("Оборот?", justify="center")
    w_table.add_column("Итог", justify="center")
    for w in p.windows:
        if w.impossible_due_to_past_gaps:
            result = "[red]НЕВОЗМОЖНО[/red]"
        elif w.qualification_ready:
            result = "[bold green]READY[/bold green]"
        else:
            result = "[yellow]NOT READY[/yellow]"
        w_table.add_row(
            str(w.check_date),
            f"{w.period_start} … {w.period_end}",
            _money(w.total_turnover),
            _flag(w.months_ok), _flag(w.quarters_ok), _flag(w.turnover_ok),
            result,
        )
    _console.print(w_table)

    # ─── План по месяцам ────────────────────────────────────────────────────
    if p.monthly_plan:
        m_table = Table(title="\nПлан по месяцам (до ближайшего окна)", show_lines=False)
        m_table.add_column("Месяц", style="cyan")
        m_table.add_column("Статус")
        m_table.add_column("Сделок", justify="right")
        m_table.add_column("Не хватает", justify="right")
        m_table.add_column("Оборот", justify="right")
        m_table.add_column("Рекоменд. оборот", justify="right")
        for m in p.monthly_plan:
            m_table.add_row(
                m.month, _status_cell(m.status),
                str(m.current_trade_count), str(m.missing_trade_count),
                _money(m.current_turnover),
                _money(m.suggested_turnover) if m.suggested_turnover else "—",
            )
        _console.print(m_table)

    # ─── План по кварталам ──────────────────────────────────────────────────
    if p.quarterly_plan:
        q_table = Table(title="\nПлан по кварталам (до ближайшего окна)", show_lines=False)
        q_table.add_column("Квартал", style="cyan")
        q_table.add_column("Сделок", justify="right")
        q_table.add_column("Не хватает", justify="right")
        q_table.add_column("Оборот", justify="right")
        q_table.add_column("Статус")
        for q in p.quarterly_plan:
            q_table.add_row(
                q.quarter, str(q.current_trade_count), str(q.missing_trade_count),
                _money(q.current_turnover), _status_cell(q.status),
            )
        _console.print(q_table)

    _console.print(f"\n[dim]⚠ {p.disclaimer}[/dim]")
