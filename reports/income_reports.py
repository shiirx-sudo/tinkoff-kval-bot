"""Отчёты income engine: income_summary.{json,csv,md} + календарь + Telegram-текст."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from common.helpers import utc_now
from modules.income_engine import IncomeItem, IncomeSummary

ITEM_COLUMNS = [
    "ticker", "class_code", "figi", "instrument_name", "source_type",
    "position_quantity", "position_value_rub", "expected_annual_yield_pct",
    "expected_annual_income_rub", "expected_monthly_income_rub", "gross_yield_pct",
    "net_yield_pct", "confidence", "next_payment_date", "fundamental_verdict",
    "risk_notes", "income_verdict",
]
CALENDAR_COLUMNS = ["month", "ticker", "source_type", "expected_payment_date",
                    "gross_amount", "net_amount", "confidence"]


def _s(v) -> str:
    return "" if v is None else str(v)


def _money(v) -> str:
    if v is None:
        return "n/a"
    return f"{Decimal(str(v)):,.0f} ₽".replace(",", " ")


def _item_row(it: IncomeItem) -> dict:
    d = asdict(it)
    d["risk_notes"] = " | ".join(it.risk_notes)
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
        "Ожидаемый доход:",
        f"- Валовый годовой доход: {_money(s.gross_annual_rub)}",
        f"- Валовый месячный эквивалент: {_money(s.gross_monthly_rub)}",
        f"- Чистый годовой доход после налога: {_money(s.net_annual_rub)}",
        f"- Чистый месячный эквивалент: {_money(s.net_monthly_rub)}",
        f"- Доходность портфеля годовая (gross/net): "
        f"{pct(s.portfolio_gross_yield_pct)} / {pct(s.portfolio_net_yield_pct)}", "",
        "До цели:",
        f"- Цель в месяц: {_money(s.target_monthly_rub)}",
        f"- Текущий прогноз в месяц (net): {_money(s.current_monthly_net_rub)}",
        f"- Не хватает в месяц: {_money(s.gap_monthly_rub)}",
        f"- Оценка капитала, нужного до цели: {_money(s.required_capital_rub)}", "",
        "| Тикер | Тип | Доход/год | Confidence | Verdict | Риски |",
        "|---|---|---|---|---|---|",
    ]
    for it in s.items:
        lines.append(f"| {it.ticker or it.figi} | {it.source_type} | "
                     f"{_money(it.expected_annual_income_rub)} | {it.confidence} | "
                     f"{it.income_verdict} | {', '.join(it.risk_notes) or '—'} |")
    lines += ["", "_Аналитика, не рекомендация. Ручные оценки не гарантия выплат. "
              "Заявки не отправляются._", ""]
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


def build_summary_telegram(s: IncomeSummary, calendar: list[dict] | None = None) -> str:
    lines = [
        "💰 Income summary — READ ONLY", "",
        "Ожидаемый доход:",
        f"Год: {_money(s.gross_annual_rub)} gross / {_money(s.net_annual_rub)} net",
        f"Месяц: {_money(s.gross_monthly_rub)} gross / {_money(s.net_monthly_rub)} net",
    ]
    if s.target_monthly_rub > 0:
        lines += [
            "", "До цели:",
            f"Цель: {_money(s.target_monthly_rub)}/мес",
            f"Сейчас: {_money(s.current_monthly_net_rub)}/мес",
            f"Gap: {_money(s.gap_monthly_rub)}/мес",
        ]
    near = [r for r in (calendar or []) if r.get("expected_payment_date") not in
            ("", "month_unknown")][:5]
    if near:
        lines.append("")
        lines.append("Календарь ближайших выплат:")
        for r in near:
            lines.append(f"- {r['ticker']}: {r['expected_payment_date']} / "
                         f"{_money(r['net_amount'])} / {r['confidence']}")
    lines += ["", "Статус: аналитика, не рекомендация. Заявки не отправляются."]
    return "\n".join(lines)
