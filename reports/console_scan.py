"""Консольный вывод Instrument Scanner (read-only)."""
from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.table import Table

from modules.instrument_scanner import ScanReport

_console = Console()

_VERDICT = {
    "GOOD": "[green]GOOD[/green]",
    "WATCH": "[yellow]WATCH[/yellow]",
    "BAD": "[red]BAD[/red]",
    "NO_DATA": "[dim]NO_DATA[/dim]",
    "NO_ORDERBOOK": "[dim]NO_ORDERBOOK[/dim]",
    "NOT_FOUND": "[red]NOT_FOUND[/red]",
    "RESOLVED_NOT_TRADING": "[yellow]RESOLVED_NOT_TRADING[/yellow]",
}


def _num(v, places: str = "0.00") -> str:
    if v is None:
        return "—"
    if isinstance(v, Decimal):
        return f"{v:.2f}"
    return str(v)


def _money(v) -> str:
    if v is None:
        return "—"
    return f"{Decimal(v):,.0f}".replace(",", " ")


def render(report: ScanReport) -> None:
    r = report
    table = Table(title="\nInstrument Scanner", show_lines=False)
    for col, just in (
        ("Тикер", "left"), ("Название", "left"), ("Status", "left"),
        ("Bid", "right"), ("Ask", "right"), ("Spread bps", "right"),
        ("Depth min ₽", "right"), ("Roundtrip bps", "right"),
        ("Est.month ₽", "right"), ("Score", "right"), ("Verdict", "center"),
    ):
        table.add_column(col, justify=just)

    for x in r.results:
        status = x.trading_status.replace("SECURITY_TRADING_STATUS_", "") or "—"
        table.add_row(
            x.ticker,
            (x.name[:22] if x.name else "—"),
            status,
            _num(x.bid_best), _num(x.ask_best), _num(x.spread_bps),
            _money(x.min_side_top_depth_rub),
            _num(x.estimated_roundtrip_cost_bps),
            _money(x.estimated_monthly_cost_rub),
            str(x.score),
            _VERDICT.get(x.verdict, x.verdict),
        )
    _console.print(table)

    # Заметки по инструментам: причины not_found / закрытых торгов / резолва
    notes: list[str] = []
    for x in r.results:
        if x.verdict == "NOT_FOUND":
            notes.append(
                f"[red]{x.ticker}[/red]: не найден по ticker/class_code; "
                "попробуйте другой class_code или проверьте доступность у брокера."
            )
        elif x.verdict == "RESOLVED_NOT_TRADING":
            notes.append(
                f"[yellow]{x.ticker}[/yellow] ({x.name or '—'}): найден, но "
                f"trading_status={x.trading_status or 'UNKNOWN'} — сейчас не торгуется."
            )
        if x.resolution_warning:
            notes.append(f"[dim]{x.ticker}: {x.resolution_warning}[/dim]")
    for n in notes:
        _console.print(n)

    if r.session_hint:
        _console.print(f"\n[yellow]⏰ {r.session_hint}[/yellow]")

    if r.commission_bps == 0:
        _console.print(
            "[yellow]⚠ commission_bps = 0[/yellow] (не задано в CLI/окружении) — "
            "оценка издержек учитывает только спред."
        )

    _console.print(
        "\n[dim]Это оценка ликвидности/спреда: по метрикам инструмент подходит "
        "лучше или хуже для дальнейшего анализа — это не рекомендация.[/dim]"
    )
    _console.print(f"[dim]⚠ {r.disclaimer}[/dim]")
