"""
Отчёты target_portfolio_v1: target_portfolio.{json,csv,md} + target_portfolio_plan.csv.

Read-only аналитика и план докупки под целевой доход. НИКАКОГО order-wording:
вместо «купить/продать» — planned_add_rub / underweight_by_rub / action_hint.
Это аналитика, не рекомендация; заявки не отправляются.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from common.helpers import utc_now
from modules.target_portfolio import TargetPortfolio

DISCLAIMER_LINES = [
    "Это аналитика, не рекомендация. Заявки не отправляются.",
    "Исторические выплаты, manual override и trailing yield не гарантируют будущий доход.",
]

ALLOCATION_COLUMNS = [
    "ticker", "target_layer", "target_weight_pct", "target_capital_rub",
    "expected_base_income_month_rub", "net_yield_pct",
    "capital_share_pct", "income_share_pct", "income_efficiency_ratio",
    "yield_vs_blended_ratio", "low_yield_slot", "reason",
]
PLAN_COLUMNS = [
    "kind", "ticker", "month", "planned_add_rub",
    "expected_extra_base_income_month_rub", "reason",
]


def _s(v) -> str:
    return "" if v is None else str(v)


def _money(v) -> str:
    if v is None:
        return "n/a"
    return f"{Decimal(str(v)):,.0f} ₽".replace(",", " ")


def _pct(v) -> str:
    return "n/a" if v is None else f"{Decimal(str(v)):.2f}%"


def _ratio(v) -> str:
    return "n/a" if v is None else f"{Decimal(str(v)):.2f}"


def _yesno(v) -> str:
    return "да" if v else "нет"


def _dec_to_str(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Не сериализуется: {type(obj)}")


# ─── JSON / CSV ───────────────────────────────────────────────────────────────

def _payload(tp: TargetPortfolio) -> dict:
    return {
        "generated_at_utc": utc_now(),
        "disclaimer": DISCLAIMER_LINES,
        "target": {
            "monthly_net_rub": tp.target_monthly_net_rub,
            "annual_net_rub": tp.target_annual_net_rub,
            "status": tp.target_status,
            "required_capital_rub": tp.required_capital_rub,
        },
        "universe": {
            "universe_profile": tp.universe_profile,
            "universe_path": tp.universe_path,
            "universe_watchlist_count": tp.universe_watchlist_count,
        },
        "current_summary": {
            "total_value_rub": tp.current_total_value_rub,
            "base_month_net_rub": tp.current_base_month_net_rub,
            "estimate_month_net_rub": tp.current_estimate_month_net_rub,
            "gap_base_month_rub": tp.gap_base_month_rub,
        },
        "eligible_universe": [asdict(c) for c in tp.eligible_universe],
        "excluded_universe": [asdict(c) for c in tp.excluded_universe],
        "target_allocation": [asdict(a) for a in tp.target_allocation],
        "current_vs_target": [asdict(r) for r in tp.current_vs_target],
        "new_capital_plan": [asdict(r) for r in tp.new_capital_plan],
        "monthly_plan": [asdict(r) for r in tp.monthly_plan],
        "warnings": tp.warnings,
    }


def write_target_portfolio(tp: TargetPortfolio,
                           reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)

    json_path = out / "target_portfolio.json"
    json_path.write_text(
        json.dumps(_payload(tp), ensure_ascii=False, indent=2, default=_dec_to_str),
        encoding="utf-8")

    csv_path = out / "target_portfolio.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALLOCATION_COLUMNS, delimiter=";",
                           extrasaction="ignore")
        w.writeheader()
        for a in tp.target_allocation:
            row = {k: _s(getattr(a, k)) for k in ALLOCATION_COLUMNS}
            w.writerow(row)

    plan_path = out / "target_portfolio_plan.csv"
    with plan_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_COLUMNS, delimiter=";",
                           extrasaction="ignore")
        w.writeheader()
        for r in tp.new_capital_plan:
            w.writerow({"kind": "new_capital", "ticker": r.ticker, "month": "",
                        "planned_add_rub": _s(r.planned_add_rub),
                        "expected_extra_base_income_month_rub":
                            _s(r.expected_extra_base_income_month_rub),
                        "reason": r.reason})
        for r in tp.monthly_plan:
            w.writerow({"kind": "monthly", "ticker": ",".join(r.target_tickers),
                        "month": r.month, "planned_add_rub": _s(r.contribution_rub),
                        "expected_extra_base_income_month_rub":
                            _s(r.expected_base_income_after_rub),
                        "reason": "dca"})

    md_path = out / "target_portfolio.md"
    md_path.write_text(render_md(tp), encoding="utf-8")
    return {"target_portfolio.json": json_path, "target_portfolio.csv": csv_path,
            "target_portfolio_plan.csv": plan_path, "target_portfolio.md": md_path}


# ─── Markdown ─────────────────────────────────────────────────────────────────

def _universe_line(tp: TargetPortfolio) -> str:
    if tp.universe_profile:
        return (f"Universe: {tp.universe_profile} "
                f"({tp.universe_watchlist_count} instruments)"
                + (f" [{tp.universe_path}]" if tp.universe_path else ""))
    return f"Universe: watchlist ({tp.universe_watchlist_count} instruments)"


def render_md(tp: TargetPortfolio) -> str:
    lines = [
        "# Target portfolio — READ ONLY", "",
        _universe_line(tp), "",
        "Цель:",
        f"- {_money(tp.target_monthly_net_rub)}/мес net",
        f"- {_money(tp.target_annual_net_rub)}/год net",
        f"- target_status: {tp.target_status}",
        f"- Оценка требуемого капитала (по консервативной доходности): "
        f"{_money(tp.required_capital_rub)}", "",
        "Текущий портфель:",
        f"- стоимость: {_money(tp.current_total_value_rub)}",
        f"- base income/мес (net): {_money(tp.current_base_month_net_rub)}",
        f"- estimate income/мес (net): {_money(tp.current_estimate_month_net_rub)}",
        f"- gap by base: {_money(tp.gap_base_month_rub)}", "",
        "Eligible universe:",
        "| ticker | policy | conservative net yield | source | verdict |",
        "|---|---|---|---|---|",
    ]
    for c in tp.eligible_universe:
        lines.append(f"| {c.ticker} | {c.policy_bucket} | "
                     f"{_pct(c.conservative_net_yield_pct)} | {c.income_data_source} | "
                     f"{c.income_verdict} |")
    lines += ["", "Excluded universe:", "| ticker | policy | excluded_reason |",
              "|---|---|---|"]
    for c in tp.excluded_universe:
        lines.append(f"| {c.ticker} | {c.policy_bucket} | {c.excluded_reason} |")
    lines += ["", "Target allocation:",
              "| ticker | weight_pct | conservative_net_yield_pct | "
              "expected_monthly_income | capital_share_pct | income_share_pct | "
              "income_efficiency_ratio | yield_vs_blended_ratio | low_yield_slot | reason |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for a in tp.target_allocation:
        lines.append(f"| {a.ticker} | {_pct(a.target_weight_pct)} | "
                     f"{_pct(a.net_yield_pct)} | "
                     f"{_money(a.expected_base_income_month_rub)} | "
                     f"{_pct(a.capital_share_pct)} | {_pct(a.income_share_pct)} | "
                     f"{_ratio(a.income_efficiency_ratio)} | "
                     f"{_ratio(a.yield_vs_blended_ratio)} | "
                     f"{_yesno(a.low_yield_slot)} | {a.reason} |")
    lines += ["", "Current vs target:",
              "| ticker | current_value | target_value | diff_value | action_hint |",
              "|---|---|---|---|---|"]
    for r in tp.current_vs_target:
        lines.append(f"| {r.ticker} | {_money(r.current_value_rub)} | "
                     f"{_money(r.target_value_rub)} | {_money(r.diff_value_rub)} | "
                     f"{r.action_hint} |")
    if tp.new_capital_plan:
        lines += ["", "New capital plan:",
                  "| ticker | planned_add_rub | expected_extra_base_income/мес | reason |",
                  "|---|---|---|---|"]
        for r in tp.new_capital_plan:
            lines.append(f"| {r.ticker} | {_money(r.planned_add_rub)} | "
                         f"{_money(r.expected_extra_base_income_month_rub)} | {r.reason} |")
    if tp.monthly_plan:
        lines += ["", "Monthly contribution plan:",
                  "| month | contribution | target tickers | base income after/мес |",
                  "|---|---|---|---|"]
        for r in tp.monthly_plan:
            lines.append(f"| {r.month} | {_money(r.contribution_rub)} | "
                         f"{', '.join(r.target_tickers) or '—'} | "
                         f"{_money(r.expected_base_income_after_rub)} |")
    if tp.warnings:
        lines += ["", "Warnings:"]
        lines += [f"- {w}" for w in tp.warnings]
    lines += ["", "---", "", *DISCLAIMER_LINES, ""]
    return "\n".join(lines)


# ─── console ──────────────────────────────────────────────────────────────────

def render_console(tp: TargetPortfolio) -> str:
    lines = ["Target portfolio — READ ONLY", ""]
    lines.append(_universe_line(tp))
    lines.append(f"Цель: {_money(tp.target_monthly_net_rub)}/мес net "
                 f"({_money(tp.target_annual_net_rub)}/год) | status={tp.target_status}")
    lines.append(f"Требуемый капитал (консервативно): {_money(tp.required_capital_rub)}")
    lines.append(f"Текущий портфель: {_money(tp.current_total_value_rub)} | "
                 f"base {_money(tp.current_base_month_net_rub)}/мес | "
                 f"gap_base {_money(tp.gap_base_month_rub)}/мес")
    lines.append("")
    lines.append("Target allocation:")
    for a in tp.target_allocation:
        low = " ⚠ low_yield_slot" if a.low_yield_slot else ""
        lines.append(f"  {a.ticker:8} weight={_pct(a.target_weight_pct)} "
                     f"cap={_money(a.target_capital_rub)} "
                     f"base_income={_money(a.expected_base_income_month_rub)}/мес "
                     f"eff={_ratio(a.income_efficiency_ratio)} "
                     f"y/blended={_ratio(a.yield_vs_blended_ratio)} [{a.reason}]{low}")
    if not tp.target_allocation:
        lines.append("  — (нет eligible инструментов)")
    lines.append("")
    lines.append("Current vs target:")
    for r in tp.current_vs_target:
        lines.append(f"  {r.ticker:8} current={_money(r.current_value_rub)} "
                     f"target={_money(r.target_value_rub)} diff={_money(r.diff_value_rub)} "
                     f"-> {r.action_hint}")
    if tp.new_capital_plan:
        lines.append("")
        lines.append("New capital plan:")
        for r in tp.new_capital_plan:
            lines.append(f"  {r.ticker:8} planned_add_rub={_money(r.planned_add_rub)} "
                         f"+income={_money(r.expected_extra_base_income_month_rub)}/мес")
    if tp.monthly_plan:
        lines.append("")
        lines.append("Monthly contribution plan:")
        for r in tp.monthly_plan:
            lines.append(f"  M{r.month:<2} {_money(r.contribution_rub)} -> "
                         f"{', '.join(r.target_tickers) or '—'} | "
                         f"base after={_money(r.expected_base_income_after_rub)}/мес")
    if tp.excluded_universe:
        lines.append("")
        lines.append("Excluded:")
        for c in tp.excluded_universe:
            lines.append(f"  {c.ticker:8} {c.policy_bucket} -> {c.excluded_reason}")
    if tp.warnings:
        lines.append("")
        for w in tp.warnings:
            lines.append(f"warning: {w}")
    lines += ["", *DISCLAIMER_LINES]
    return "\n".join(lines)


# ─── Telegram ─────────────────────────────────────────────────────────────────

def build_telegram(tp: TargetPortfolio) -> str:
    lines = [
        "🎯 Target portfolio — READ ONLY", "",
        f"Цель: {_money(tp.target_monthly_net_rub)}/мес net",
        f"Текущий base: {_money(tp.current_base_month_net_rub)}/мес",
        f"Gap: {_money(tp.gap_base_month_rub)}/мес",
        "",
        f"Target status: {tp.target_status}",
        f"Required capital estimate: {_money(tp.required_capital_rub)}",
    ]
    under = sorted([r for r in tp.current_vs_target if r.action_hint == "underweight"],
                   key=lambda r: -(r.diff_value_rub or Decimal("0")))[:5]
    if under:
        lines.append("")
        lines.append("Top underweight:")
        for r in under:
            lines.append(f"- {r.ticker}: underweight_by_rub {_money(r.diff_value_rub)}")
    lines += [
        "",
        "Заявки не отправляются.",
        "Это аналитика, не рекомендация.",
    ]
    return "\n".join(lines)
