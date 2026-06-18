"""Отчёты income engine: income_summary.{json,csv,md} + календарь + Telegram-текст."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from common.helpers import utc_now
from modules.income_engine import IncomeItem, IncomeSummary, WatchlistItem

ITEM_COLUMNS = [
    "ticker", "class_code", "figi", "instrument_name", "source_type",
    "position_quantity", "position_value_rub", "expected_annual_yield_pct",
    "expected_annual_income_rub", "expected_monthly_income_rub", "gross_yield_pct",
    "net_yield_pct", "confidence", "income_data_source", "dividend_source",
    "coupon_source", "yield_source", "known_future_income_rub", "trailing_income_rub",
    "manual_income_rub", "next_payment_date", "fundamental_verdict",
    "risk_notes", "income_verdict", "notes",
    "policy_bucket", "policy_confidence", "policy_reasons",
    "base_annual_income_rub", "estimate_annual_income_rub",
    "excluded_annual_income_rub", "conservative_yield_pct",
]
CALENDAR_COLUMNS = ["month", "ticker", "source_type", "source",
                    "expected_payment_date", "gross_amount", "net_amount",
                    "confidence", "notes"]
WATCHLIST_COLUMNS = [
    "ticker", "class_code", "figi", "instrument_uid", "instrument_name",
    "instrument_type", "current_price", "price_source", "source_type",
    "expected_annual_yield_pct", "gross_yield_pct", "net_yield_pct", "confidence",
    "income_data_source", "dividend_source", "coupon_source", "yield_source",
    "trailing_12m_dividend", "known_future_dividend",
    "fundamental_verdict", "income_verdict", "risk_notes", "notes",
    "policy_bucket", "policy_confidence", "policy_reasons", "conservative_yield_pct",
]


def _s(v) -> str:
    return "" if v is None else str(v)


def _money(v) -> str:
    if v is None:
        return "n/a"
    return f"{Decimal(str(v)):,.0f} ₽".replace(",", " ")


def _item_row(it: IncomeItem) -> dict:
    d = asdict(it)
    d["risk_notes"] = " | ".join(it.risk_notes)
    d["policy_reasons"] = " | ".join(it.policy_reasons)
    return {k: _s(d.get(k)) for k in ITEM_COLUMNS}


def _summary_md(s: IncomeSummary) -> str:
    def pct(v):
        return "n/a" if v is None else f"{Decimal(str(v)):.2f}%"
    lines = [
        "# Income summary — READ ONLY", "",
        "Портфель:",
        f"- Общая стоимость: {_money(s.total_value_rub)}",
        f"- Денежный рынок: {_money(s.money_market_rub)}",
        f"- Акции: {_money(s.shares_rub)}",
        f"- Облигации: {_money(s.bonds_rub)}",
        f"- Свободный кэш: {_money(s.free_cash_rub)}", "",
        "Raw expected income (всё, что посчитали источники):",
        f"- Валовый годовой доход: {_money(s.gross_annual_rub)}",
        f"- Валовый месячный эквивалент: {_money(s.gross_monthly_rub)}",
        f"- Чистый годовой доход после налога: {_money(s.net_annual_rub)}",
        f"- Чистый месячный эквивалент: {_money(s.net_monthly_rub)}",
        f"- Доходность портфеля годовая (gross/net): "
        f"{pct(s.portfolio_gross_yield_pct)} / {pct(s.portfolio_net_yield_pct)}", "",
        "Консервативная оценка дохода:",
        f"- Base годовой доход (net): {_money(s.base_annual_net_rub)}",
        f"- Base месячный доход (net): {_money(s.base_monthly_net_rub)}",
        f"- Estimate годовой доход (net): {_money(s.estimate_annual_net_rub)}",
        f"- Estimate месячный доход (net): {_money(s.estimate_monthly_net_rub)}",
        f"- Excluded годовой доход (gross): {_money(s.excluded_annual_gross_rub)}",
        f"- Unknown instruments: {s.unknown_instruments}",
        f"- Консервативная доходность портфеля (gross/net): "
        f"{pct(s.conservative_gross_yield_pct)} / {pct(s.conservative_net_yield_pct)}", "",
        "До цели:",
        f"- Цель в месяц: {_money(s.target_monthly_rub)}",
        f"- Текущий прогноз в месяц (net): {_money(s.current_monthly_net_rub)}",
        f"- Gap по raw (net/мес): {_money(s.gap_raw_monthly_rub)}",
        f"- Gap по base (net/мес): {_money(s.gap_base_monthly_rub)}",
        f"- Оценка капитала до цели (по консервативной доходности): "
        f"{_money(s.required_capital_rub)}", "",
        "| Тикер | Source | Raw/год | Policy | Base/год | Estimate/год | "
        "Excluded/год | Reasons |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for it in s.items:
        lines.append(
            f"| {it.ticker or it.figi} | {it.income_data_source} | "
            f"{_money(it.expected_annual_income_rub)} | {it.policy_bucket} | "
            f"{_money(it.base_annual_income_rub)} | {_money(it.estimate_annual_income_rub)} | "
            f"{_money(it.excluded_annual_income_rub)} | {', '.join(it.policy_reasons) or '—'} |")
    lines += ["", "_Аналитика, не рекомендация. Исторические выплаты, manual override "
              "и trailing yield не гарантия будущих выплат. Заявки не отправляются._", ""]
    return "\n".join(lines)


def write_summary(s: IncomeSummary, reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = utc_now()

    payload = {k: (_s(v) if isinstance(v, Decimal) else v)
               for k, v in asdict(s).items() if k != "items"}
    payload["generated_at_utc"] = ts
    payload["items"] = [_item_row(it) for it in s.items]
    json_path = out / "income_summary.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = out / "income_summary.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ITEM_COLUMNS, delimiter=";")
        w.writeheader()
        for it in s.items:
            w.writerow(_item_row(it))

    md_path = out / "income_summary.md"
    md_path.write_text(_summary_md(s), encoding="utf-8")
    return {"income_summary.json": json_path, "income_summary.csv": csv_path,
            "income_summary.md": md_path}


def write_calendar(rows: list[dict], reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = utc_now()
    norm = [{k: _s(r.get(k)) for k in CALENDAR_COLUMNS} for r in rows]
    json_path = out / "income_calendar.json"
    json_path.write_text(json.dumps(
        {"generated_at_utc": ts, "calendar": norm}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    csv_path = out / "income_calendar.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CALENDAR_COLUMNS, delimiter=";")
        w.writeheader()
        for r in norm:
            w.writerow(r)
    return {"income_calendar.json": json_path, "income_calendar.csv": csv_path}


def _pct(v) -> str:
    return "n/a" if v is None else f"{Decimal(str(v)):.2f}%"


def _price(v) -> str:
    return "n/a" if v is None else f"{Decimal(str(v)):.2f}"


def _watchlist_row(it: WatchlistItem) -> dict:
    d = asdict(it)
    d["risk_notes"] = " | ".join(it.risk_notes)
    d["policy_reasons"] = " | ".join(it.policy_reasons)
    return {k: _s(d.get(k)) for k in WATCHLIST_COLUMNS}


def render_watchlist_line(it: WatchlistItem) -> str:
    """Строка CLI: тикер/цена/доходность/вердикт (read-only аналитика)."""
    div = ""
    if it.expected_annual_dividend_rub_per_share is not None:
        div = f"div={it.expected_annual_dividend_rub_per_share} ₽ "
    yld = it.gross_yield_pct if it.gross_yield_pct is not None else it.expected_annual_yield_pct
    yld_s = "n/a" if yld is None else f"{Decimal(str(yld)):.2f}%"
    cons = "" if it.conservative_yield_pct is None else f" cons={_pct(it.conservative_yield_pct)}"
    return (f"{it.ticker:8} {it.class_code or '—':6} price={_price(it.current_price)} "
            f"{div}yield={yld_s} net={_pct(it.net_yield_pct)} conf={it.confidence} "
            f"src={it.income_data_source} policy={it.policy_bucket}{cons} "
            f"fund={it.fundamental_verdict or 'quality_unknown'} -> {it.income_verdict}")


def _watchlist_md(items: list[WatchlistItem]) -> str:
    lines = [
        "# Income watchlist — READ ONLY", "",
        "| Тикер | Класс | Цена | Источник | Доходность | Net | Policy | "
        "Conservative | Confidence | Fund | Verdict | Риски |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for it in items:
        yld = it.gross_yield_pct if it.gross_yield_pct is not None else it.expected_annual_yield_pct
        lines.append(
            f"| {it.ticker} | {it.class_code or '—'} | {_price(it.current_price)} | "
            f"{it.income_data_source} | {_pct(yld)} | {_pct(it.net_yield_pct)} | "
            f"{it.policy_bucket} | {_pct(it.conservative_yield_pct)} | {it.confidence} | "
            f"{it.fundamental_verdict or 'quality_unknown'} | {it.income_verdict} | "
            f"{', '.join(it.risk_notes) or '—'} |")
    lines += ["", "_Аналитика, не рекомендация. Ручные оценки не гарантия выплат. "
              "Заявки не отправляются._", ""]
    return "\n".join(lines)


def write_watchlist(items: list[WatchlistItem],
                    reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = utc_now()

    rows = [_watchlist_row(it) for it in items]
    json_path = out / "income_watchlist.json"
    json_path.write_text(json.dumps(
        {"generated_at_utc": ts, "items": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    csv_path = out / "income_watchlist.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=WATCHLIST_COLUMNS, delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    md_path = out / "income_watchlist.md"
    md_path.write_text(_watchlist_md(items), encoding="utf-8")
    return {"income_watchlist.json": json_path, "income_watchlist.csv": csv_path,
            "income_watchlist.md": md_path}


def _telegram_item_line(it: IncomeItem) -> str:
    """Строка по инструменту: доход/доходность + источник и confidence."""
    src = it.income_data_source or "unknown"
    parts = [it.ticker or it.figi]
    if it.source_type == "dividend" and it.position_quantity:
        dps = (it.expected_annual_income_rub / it.position_quantity
               if it.position_quantity else None)
        if dps is not None:
            parts.append(f"{Decimal(str(dps)):.2f} ₽/акц")
    if it.gross_yield_pct is not None:
        parts.append(f"yield {Decimal(str(it.gross_yield_pct)):.1f}%")
    head = parts[0] + ": " + ", ".join(parts[1:]) if len(parts) > 1 else parts[0] + ":"
    return f"{head} source={src}, confidence={it.confidence}"


def build_summary_telegram(s: IncomeSummary, calendar: list[dict] | None = None) -> str:
    lines = [
        "💰 Income summary — READ ONLY", "",
        "Raw income:",
        f"Год: {_money(s.net_annual_rub)} net",
        f"Месяц: {_money(s.net_monthly_rub)} net",
        "",
        "Conservative income:",
        f"Base: {_money(s.base_monthly_net_rub)}/мес",
        f"Estimate: {_money(s.estimate_monthly_net_rub)}/мес",
        f"Excluded: {_money(s.excluded_monthly_net_rub)}/мес",
    ]
    if s.target_monthly_rub > 0:
        lines += [
            "", "Gap to target:",
            f"Target: {_money(s.target_monthly_rub)}/мес",
            f"Gap by raw: {_money(s.gap_raw_monthly_rub)}/мес",
            f"Gap by base: {_money(s.gap_base_monthly_rub)}/мес",
        ]
    earning = [it for it in s.items if it.expected_annual_income_rub > 0][:8]
    if earning:
        lines.append("")
        lines.append("Источники дохода:")
        for it in earning:
            lines.append(f"- {_telegram_item_line(it)}")
    near = [r for r in (calendar or []) if r.get("expected_payment_date") not in
            ("", "month_unknown")][:5]
    if near:
        lines.append("")
        lines.append("Календарь ближайших выплат:")
        for r in near:
            lines.append(f"- {r['ticker']}: {r['expected_payment_date']} / "
                         f"{_money(r['net_amount'])} / {r['confidence']}")
    lines += [
        "",
        "Исторические выплаты, manual override и trailing yield не гарантируют "
        "будущий доход.",
        "Аналитика, не рекомендация. Заявки не отправляются.",
    ]
    return "\n".join(lines)
