"""
Отчёты income-source-audit: income_source_audit.{json,csv,md}.

Показывают сводку источника дохода и СЫРЫЕ события API (дивиденды/купоны/свечи)
с бакетом классификации. Read-only аналитика, не рекомендация; заявок нет.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from common.helpers import utc_now
from modules.income_audit import AuditItem

DISCLAIMER_LINES = [
    "Исторические выплаты и trailing yield не гарантируют будущий доход.",
    "Это аналитика, не рекомендация. Заявки не отправляются.",
]

# Плоская событийная схема CSV (одна строка на событие/базис/сводку)
CSV_COLUMNS = [
    "ticker", "class_code", "figi", "instrument_uid", "origin", "source_type",
    "income_data_source", "confidence", "manual_override_active",
    "kind", "source_bucket", "date",
    "dividend_net", "dividend_gross", "yield_value",
    "record_date", "last_buy_date", "declared_date", "created_at",
    "pay_one_bond", "coupon_number", "coupon_period", "coupon_type",
    "start_date", "start_close", "end_date", "end_close",
    "span_days", "growth_pct", "annualized_yield_pct",
]


def _s(v) -> str:
    return "" if v is None else str(v)


def _dec_to_str(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Не сериализуется: {type(obj)}")


# ─── JSON ─────────────────────────────────────────────────────────────────────

def _item_payload(it: AuditItem) -> dict:
    d = asdict(it)
    # candle_basis уже dict (asdict рекурсивно), events — списки dict
    return d


def write_audit(items: list[AuditItem],
                reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = utc_now()

    json_path = out / "income_source_audit.json"
    payload = {
        "generated_at_utc": ts,
        "disclaimer": DISCLAIMER_LINES,
        "items": [_item_payload(it) for it in items],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_dec_to_str),
        encoding="utf-8")

    csv_path = out / "income_source_audit.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, delimiter=";",
                           extrasaction="ignore")
        w.writeheader()
        for row in _csv_rows(items):
            w.writerow(row)

    md_path = out / "income_source_audit.md"
    md_path.write_text(_audit_md(items), encoding="utf-8")
    return {"income_source_audit.json": json_path,
            "income_source_audit.csv": csv_path,
            "income_source_audit.md": md_path}


# ─── CSV (плоские строки событий) ─────────────────────────────────────────────

def _base_row(it: AuditItem) -> dict:
    return {
        "ticker": it.ticker, "class_code": it.class_code, "figi": it.figi,
        "instrument_uid": it.instrument_uid, "origin": it.origin,
        "source_type": it.source_type, "income_data_source": it.income_data_source,
        "confidence": it.confidence,
        "manual_override_active": int(it.manual_override_active),
    }


def _csv_rows(items: list[AuditItem]) -> list[dict]:
    rows: list[dict] = []
    for it in items:
        base = _base_row(it)
        # сводная строка (чтобы инструменты без событий тоже были видны)
        rows.append({**base, "kind": "summary",
                     "source_bucket": "", "date": it.next_dividend_date
                     or it.next_coupon_date or ""})
        for ev in it.dividend_events:
            rows.append({**base, "kind": "dividend_event",
                         "source_bucket": ev.source_bucket,
                         "date": ev.payment_date,
                         "dividend_net": _s(ev.dividend_net),
                         "dividend_gross": _s(ev.dividend_gross),
                         "yield_value": _s(ev.yield_value),
                         "record_date": ev.record_date,
                         "last_buy_date": ev.last_buy_date,
                         "declared_date": ev.declared_date,
                         "created_at": ev.created_at})
        for ev in it.coupon_events:
            rows.append({**base, "kind": "coupon_event",
                         "source_bucket": ev.source_bucket,
                         "date": ev.coupon_date,
                         "pay_one_bond": _s(ev.pay_one_bond),
                         "coupon_number": ev.coupon_number,
                         "coupon_period": ev.coupon_period,
                         "coupon_type": ev.coupon_type})
        cb = it.candle_basis
        if cb is not None:
            rows.append({**base, "kind": "candle_basis", "source_bucket": "",
                         "date": cb.end_date,
                         "start_date": cb.start_date, "start_close": _s(cb.start_close),
                         "end_date": cb.end_date, "end_close": _s(cb.end_close),
                         "span_days": _s(cb.span_days),
                         "growth_pct": _s(cb.growth_pct),
                         "annualized_yield_pct": _s(cb.annualized_yield_pct)})
    return rows


# ─── Markdown ─────────────────────────────────────────────────────────────────

def _pct(v) -> str:
    return "n/a" if v is None else f"{Decimal(str(v)):.2f}%"


def _num(v) -> str:
    return "n/a" if v is None else f"{Decimal(str(v))}"


def _audit_md(items: list[AuditItem]) -> str:
    lines = ["# Income source audit — READ ONLY", ""]
    for it in items:
        lines.append(f"## {it.ticker} ({it.source_type}) — {it.origin}")
        lines += [
            f"- class_code: {it.class_code or '—'} | figi: {it.figi or '—'} | "
            f"uid: {it.instrument_uid or '—'}",
            f"- name: {it.instrument_name or '—'}",
            f"- current_price: {_num(it.current_price)} ({it.price_source})",
            f"- income_data_source: **{it.income_data_source}** | "
            f"confidence: {it.confidence}"
            + (" | manual_override" if it.manual_override_active else ""),
        ]
        if it.source_type == "dividend":
            lines += [
                f"- known_future_div/share: {_num(it.known_future_dividends_rub_per_share)} | "
                f"trailing_12m/share: {_num(it.trailing_12m_dividends_rub_per_share)}",
                f"- last_dividend_date: {it.last_dividend_date or '—'} | "
                f"next_dividend_date: {it.next_dividend_date or '—'}",
            ]
            lines += _dividend_table(it.dividend_events)
        elif it.source_type == "coupon":
            lines += [
                f"- next_coupon_date: {it.next_coupon_date or '—'} | "
                f"coupon_amount: {_num(it.coupon_amount_rub)} | "
                f"freq/year: {_num(it.coupon_frequency_per_year)}",
                f"- horizon_income: {_num(it.known_coupon_income_horizon_rub)} | "
                f"annualized_income: {_num(it.known_coupon_income_annualized_rub)}",
                f"- accrued_interest: {_num(it.accrued_interest)} | "
                f"maturity_date: {it.maturity_date or '—'}",
            ]
            lines += _coupon_table(it.coupon_events)
        elif it.source_type == "money_market":
            lines += [
                f"- yield_source: {it.yield_source} | "
                f"expected_annual_yield: {_pct(it.expected_annual_yield_pct)}",
            ]
            lines += _candle_table(it.candle_basis)
        if it.risk_notes:
            lines.append(f"- risk_notes: {', '.join(it.risk_notes)}")
        lines.append("")
    lines.append("---")
    lines += ["", *DISCLAIMER_LINES, ""]
    return "\n".join(lines)


def _dividend_table(events) -> list[str]:
    if not events:
        return ["", "_Нет дивидендных событий в окне lookback._", ""]
    out = ["", "| payment_date | record_date | last_buy | div_net | yield | bucket |",
           "|---|---|---|---|---|---|"]
    for e in events:
        out.append(f"| {e.payment_date or '—'} | {e.record_date or '—'} | "
                   f"{e.last_buy_date or '—'} | {_num(e.dividend_net)} | "
                   f"{_num(e.yield_value)} | {e.source_bucket} |")
    out.append("")
    return out


def _coupon_table(events) -> list[str]:
    if not events:
        return ["", "_Нет купонных событий в окне._", ""]
    out = ["", "| coupon_date | pay_one_bond | number | period | type | bucket |",
           "|---|---|---|---|---|---|"]
    for e in events:
        out.append(f"| {e.coupon_date or '—'} | {_num(e.pay_one_bond)} | "
                   f"{e.coupon_number or '—'} | {e.coupon_period or '—'} | "
                   f"{e.coupon_type or '—'} | {e.source_bucket} |")
    out.append("")
    return out


def _candle_table(cb) -> list[str]:
    if cb is None:
        return ["", "_Нет данных свечей для trailing-доходности._", ""]
    return [
        "", "| start_date | start_close | end_date | end_close | span_days | "
        "growth_pct | annualized_yield_pct |",
        "|---|---|---|---|---|---|---|",
        f"| {cb.start_date or '—'} | {_num(cb.start_close)} | {cb.end_date or '—'} | "
        f"{_num(cb.end_close)} | {_num(cb.span_days)} | {_pct(cb.growth_pct)} | "
        f"{_pct(cb.annualized_yield_pct)} |", "",
    ]


def render_audit_console(items: list[AuditItem]) -> str:
    """Короткий CLI-вывод (read-only аналитика)."""
    lines = ["Income source audit — READ ONLY", ""]
    for it in items:
        head = (f"{it.ticker:8} {it.source_type:12} src={it.income_data_source} "
                f"conf={it.confidence}")
        if it.source_type == "dividend":
            head += (f" future/share={_num(it.known_future_dividends_rub_per_share)} "
                     f"trailing/share={_num(it.trailing_12m_dividends_rub_per_share)} "
                     f"events={len(it.dividend_events)}")
        elif it.source_type == "coupon":
            head += (f" next={it.next_coupon_date or '—'} "
                     f"events={len(it.coupon_events)}")
        elif it.source_type == "money_market":
            head += f" yield={_pct(it.expected_annual_yield_pct)}"
        if it.manual_override_active:
            head += " [manual_override]"
        lines.append(head)
        # trailing-базис свечей показываем отдельно, чтобы не путать с manual yield
        if it.source_type == "money_market" and it.candle_basis is not None:
            cb = it.candle_basis
            risk = (" " + ", ".join(it.risk_notes)) if it.risk_notes else ""
            lines.append(f"         trailing_30d={_pct(cb.annualized_yield_pct)}{risk}")
        elif it.risk_notes:
            lines.append(f"         risk: {', '.join(it.risk_notes)}")
    lines += ["", *DISCLAIMER_LINES]
    return "\n".join(lines)
