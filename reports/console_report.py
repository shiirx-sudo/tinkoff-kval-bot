"""
Консольный отчёт о прогрессе к статусу квалифицированного инвестора.
Использует rich для форматированного вывода.
"""
from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from common.helpers import mask_identifier
from modules.kval_tracker import KvalProgress

_console = Console()


def _fmt_money(value: Decimal) -> str:
    """Форматирует сумму с разделителями разрядов."""
    return f"{value:,.2f} ₽".replace(",", " ")


def _bar(ratio: float, width: int = 30) -> str:
    """Текстовый прогресс-бар."""
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def render(progress: KvalProgress) -> None:
    """Печатает полный отчёт в консоль."""
    p = progress

    # ─── Заголовок ──────────────────────────────────────────────────────────
    status = "✅ ДОСТИГНУТО" if p.achieved else "🟡 В ПРОЦЕССЕ"
    if not p.achieved and p.achieved_bare:
        status = "🟢 ЦЕЛЬ ВЗЯТА (без буфера)"

    quarters = ", ".join(q.label for q in p.period.quarters)
    header = (
        f"[bold]Период:[/bold] {p.period.start} … {p.period.end}  "
        f"([dim]{quarters}[/dim])\n"
        f"[bold]Статус:[/bold] {status}"
    )
    _console.print(Panel(header, title="T-Invest Qualification Tracker"))

    # ─── Общий прогресс ─────────────────────────────────────────────────────
    ratio = float(p.progress_ratio)
    _console.print(
        f"\n[bold]Оборот:[/bold] {_fmt_money(p.total_turnover)} / "
        f"{_fmt_money(p.target)}  "
        f"[bold]{p.progress_pct}%[/bold]"
    )
    _console.print(f"  {_bar(ratio)}  ")
    _console.print(
        f"  До цели: {_fmt_money(p.remaining_to_target)}  |  "
        f"До цели+буфер: {_fmt_money(p.remaining_to_effective)}"
    )

    # ─── Разбивка по счетам ─────────────────────────────────────────────────
    acc_table = Table(title="\nПо счетам", show_lines=False)
    acc_table.add_column("Счёт", style="cyan")
    acc_table.add_column("ID", style="dim")
    acc_table.add_column("Оборот", justify="right")
    acc_table.add_column("Операций", justify="right")
    acc_table.add_column("Сделок точн.", justify="right")
    acc_table.add_column("Сделок прибл.", justify="right", style="yellow")

    for a in p.accounts:
        acc_table.add_row(
            a.account_name,
            mask_identifier(a.account_id),
            _fmt_money(a.total_turnover),
            str(a.operation_count),
            str(a.exact_trade_count),
            str(a.approximate_trade_count) if a.approximate_trade_count else "—",
        )
    _console.print(acc_table)

    # ─── Разбивка по кварталам ──────────────────────────────────────────────
    q_table = Table(title="\nПо кварталам", show_lines=False)
    q_table.add_column("Квартал", style="cyan")
    q_table.add_column("Оборот", justify="right")
    q_table.add_column("Операций", justify="right")
    q_table.add_column("Сделок", justify="right")
    q_table.add_column("Статус", justify="center")
    for q in p.quarter_checks:
        status_cell = "[green]OK[/green]" if q.ok else "[red]FAIL[/red]"
        q_table.add_row(
            q.label,
            _fmt_money(q.turnover),
            str(q.operation_count),
            str(q.trade_count),
            status_cell,
        )
    _console.print(q_table)

    # ─── Разбивка по месяцам ────────────────────────────────────────────────
    m_table = Table(title="\nПо месяцам", show_lines=False)
    m_table.add_column("Месяц", style="cyan")
    m_table.add_column("Операций", justify="right")
    m_table.add_column("Сделок", justify="right")
    m_table.add_column("Оборот", justify="right")
    m_table.add_column("Статус", justify="center")
    for m in p.months:
        status_cell = "[green]OK[/green]" if m.ok else "[red]FAIL[/red]"
        m_table.add_row(
            m.label,
            str(m.operation_count),
            str(m.trade_count),
            _fmt_money(m.turnover),
            status_cell,
        )
    _console.print(m_table)

    # ─── Готовность к квал-статусу ──────────────────────────────────────────
    def _flag(ok: bool) -> str:
        return "[green]OK[/green]" if ok else "[red]FAIL[/red]"

    verdict = "[bold green]READY[/bold green]" if p.qualification_ready \
        else "[bold red]NOT READY[/bold red]"
    gate = (
        f"[bold]Оборот:[/bold]   {_flag(p.turnover_ok)}\n"
        f"[bold]Месяцы:[/bold]   {_flag(p.months_ok)}\n"
        f"[bold]Кварталы:[/bold] {_flag(p.quarters_ok)}\n"
        f"[bold]Итог:[/bold]     {verdict}"
    )
    _console.print(Panel(gate, title="Готовность к квал-статусу"))

    # ─── Предупреждения ─────────────────────────────────────────────────────
    if p.has_approximate:
        _console.print(
            f"\n[yellow]⚠ Внимание:[/yellow] "
            f"{len(p.approximate_warnings)} операций посчитаны приближённо "
            f"(по payment, без trades). Сверьте с брокерским отчётом."
        )
