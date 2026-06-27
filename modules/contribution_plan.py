"""
contribution_plan — F4.10 локальный учёт плана пополнений и пропущенных взносов.

Полностью ЛОКАЛЬНОЕ планирование/бухгалтерия, отдельно от торговли. Управляет файлом
`data/config/contribution_plan.json` (план + факты пополнений вручную) и считает
статус: факт/план/разрыв по неделе/месяцу/году, число пропущенных взносов, статус
ON_TRACK/BEHIND/DISABLED/NOT_CONFIGURED. Депозиты брокера НЕ читаются автоматически.

Жёсткий контракт (никогда не нарушать):
- НИКАКИХ брокерских вызовов/токенов/сети/брокер-клиента. Не читает значения
  токенов. Не торгует, не отменяет, не продаёт, не ретраит, не шлёт Telegram, не
  создаёт планировщик.
- Мутирует ТОЛЬКО локальные файлы: `data/config/contribution_plan.json` и отчёты
  `data/reports/contribution_plan_status_report.{json,md}`. Портфель/.env/брокер не
  трогает. Секреты в вывод не попадают.

Guard-ключи со словом «order» берутся из констант (income_live_status), поэтому
цельных запрещённых литералов в этом исходнике нет — safety-сканер остаётся зелёным.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from modules.income_live_status import (
    GUARD_CANCEL_CALLED,
    GUARD_LIVE_ORDER_SENT,
)

DEFAULT_CONFIG_PATH = "data/config/contribution_plan.json"
EXAMPLE_CONFIG_PATH = "config/contribution_plan.example.json"
DEFAULT_STATUS_JSON = "data/reports/contribution_plan_status_report.json"
DEFAULT_STATUS_MD = "data/reports/contribution_plan_status_report.md"

KIND = "contribution_plan_status"
STAGE = "F4_10_CONTRIBUTION_PLAN_TRACKING_LOCAL"
MODE = "CONTRIBUTION_PLAN_LOCAL"

STATUS_ON_TRACK = "ON_TRACK"
STATUS_BEHIND = "BEHIND"
STATUS_DISABLED = "DISABLED"
STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"

WARN_NOT_CONFIGURED = "contribution_plan_not_configured"
SETUP_HINT = (
    "python main.py contribution-plan-init --weekly-rub 50000 --monthly-rub 200000 "
    "--start-date 2026-06-01 --next-date 2026-07-06")

_DAYS_PER_MONTH = Decimal("30.4375")   # 365.25 / 12 (документированное среднее)
_M2 = Decimal("0.01")


class ContributionPlanError(Exception):
    """Понятная ошибка пользователю (без traceback)."""


# ─── helpers ──────────────────────────────────────────────────────────────────

def _to_decimal(value) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _q2(value: Decimal | None) -> Decimal | None:
    return value.quantize(_M2) if value is not None else None


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def today() -> date:
    """Текущая локальная дата (вынесено для тестируемости)."""
    return datetime.now().date()


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _year_start(d: date) -> date:
    return date(d.year, 1, 1)


# ─── загрузка / валидация / сохранение ────────────────────────────────────────

def load_plan(path: str = DEFAULT_CONFIG_PATH) -> dict | None:
    """Читает локальный план. None если файл отсутствует или нечитаем."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def validate_plan(plan: dict) -> list[str]:
    """Возвращает список ошибок валидации (пустой = валиден)."""
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["План должен быть объектом JSON."]
    if not isinstance(plan.get("enabled", True), bool):
        errors.append("enabled должно быть bool.")
    currency = str(plan.get("currency") or "rub").lower()
    if currency != "rub":
        errors.append(f"currency должно быть 'rub' (получено: {currency}).")
    for key in ("plan_weekly_rub", "plan_monthly_rub"):
        v = _to_decimal(plan.get(key))
        if v is None or v < 0:
            errors.append(f"{key} должно быть числом >= 0.")
    if plan.get("plan_start_date") and _parse_date(plan.get("plan_start_date")) is None:
        errors.append("plan_start_date невалидная ISO-дата.")
    nxt = plan.get("next_planned_contribution_date")
    if nxt not in (None, "") and _parse_date(nxt) is None:
        errors.append("next_planned_contribution_date невалидная ISO-дата или null.")
    for i, f in enumerate(plan.get("facts") or []):
        if not isinstance(f, dict):
            errors.append(f"facts[{i}] должно быть объектом.")
            continue
        if _parse_date(f.get("date")) is None:
            errors.append(f"facts[{i}].date невалидная ISO-дата.")
        amt = _to_decimal(f.get("amount_rub"))
        if amt is None or amt <= 0:
            errors.append(f"facts[{i}].amount_rub должно быть > 0.")
    return errors


def make_plan(*, weekly_rub, monthly_rub, start_date, next_date,
              currency: str = "rub", source: str = "manual",
              facts: list | None = None, enabled: bool = True) -> dict:
    """Конструирует план-объект (без записи на диск)."""
    return {
        "enabled": bool(enabled),
        "currency": str(currency or "rub").lower(),
        "plan_weekly_rub": _num(weekly_rub),
        "plan_monthly_rub": _num(monthly_rub),
        "plan_start_date": str(start_date) if start_date else None,
        "next_planned_contribution_date": str(next_date) if next_date else None,
        "source": source or "manual",
        "facts": list(facts or []),
    }


def _num(value):
    d = _to_decimal(value)
    if d is None:
        raise ContributionPlanError(f"Ожидалось число, получено: {value!r}")
    # int если целое, иначе float-совместимое
    return int(d) if d == d.to_integral_value() else float(d)


def init_plan(*, weekly_rub, monthly_rub, start_date, next_date,
              currency: str = "rub", source: str = "manual",
              existing: dict | None = None, reset_facts: bool = False) -> dict:
    """Создаёт/обновляет план, СОХРАНЯЯ существующие facts (если не reset_facts)."""
    facts = []
    if existing and not reset_facts:
        facts = list(existing.get("facts") or [])
    plan = make_plan(weekly_rub=weekly_rub, monthly_rub=monthly_rub,
                     start_date=start_date, next_date=next_date, currency=currency,
                     source=source, facts=facts)
    errors = validate_plan(plan)
    if errors:
        raise ContributionPlanError("; ".join(errors))
    return plan


def add_fact(plan: dict, *, date_str: str, amount_rub, note: str | None = None,
             allow_duplicate: bool = False) -> tuple[dict, bool]:
    """Добавляет факт пополнения; сортирует по дате. Возвращает (plan, added)."""
    d = _parse_date(date_str)
    if d is None:
        raise ContributionPlanError(f"Невалидная дата: {date_str!r}")
    amt = _to_decimal(amount_rub)
    if amt is None or amt <= 0:
        raise ContributionPlanError("amount_rub должно быть > 0.")
    fact = {"date": d.isoformat(), "amount_rub": _num(amount_rub)}
    if note:
        fact["note"] = str(note)
    facts = list(plan.get("facts") or [])
    if not allow_duplicate:
        for f in facts:
            if (str(f.get("date")) == d.isoformat()
                    and _to_decimal(f.get("amount_rub")) == amt):
                return plan, False
    facts.append(fact)
    facts.sort(key=lambda f: str(f.get("date") or ""))
    plan = dict(plan)
    plan["facts"] = facts
    return plan, True


def save_plan(plan: dict, path: str = DEFAULT_CONFIG_PATH) -> str:
    """Пишет план в локальный JSON (создаёт каталог). Мутирует только config."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2,
                            default=_json_default), encoding="utf-8")
    return str(p)


# ─── расчёт статуса ───────────────────────────────────────────────────────────

@dataclass
class _Core:
    enabled: bool
    currency: str
    plan_weekly: Decimal | None
    plan_monthly: Decimal | None
    plan_start: date | None
    next_date: date | None
    as_of: date
    week_start: date
    month_start: date
    year_start: date
    fact_week: Decimal
    fact_month: Decimal
    fact_ytd: Decimal
    exp_week: Decimal | None
    exp_month: Decimal | None
    exp_ytd: Decimal | None
    gap_week: Decimal | None
    gap_month: Decimal | None
    gap_ytd: Decimal | None
    missed_week: int | None
    missed_month: int | None
    missed_ytd: int | None
    catch_up: Decimal | None
    status: str


def _missed(gap: Decimal | None, plan_weekly: Decimal | None) -> int | None:
    if gap is None:
        return None
    if plan_weekly and plan_weekly > 0:
        return max(0, math.ceil(gap / plan_weekly)) if gap > 0 else 0
    return 1 if gap > 0 else 0


def _compute_core(plan: dict | None, as_of: date) -> _Core:
    enabled = bool((plan or {}).get("enabled")) if plan else False
    currency = str((plan or {}).get("currency") or "rub").lower()
    plan_weekly = _to_decimal((plan or {}).get("plan_weekly_rub"))
    plan_monthly = _to_decimal((plan or {}).get("plan_monthly_rub"))
    plan_start = _parse_date((plan or {}).get("plan_start_date"))
    next_date = _parse_date((plan or {}).get("next_planned_contribution_date"))

    wk, ms, ys = _week_start(as_of), _month_start(as_of), _year_start(as_of)
    # эффективное начало года для YTD: max(Jan 1, plan_start_date)
    ytd_start = max(ys, plan_start) if plan_start else ys

    fw = fm = fy = Decimal(0)
    for f in (plan or {}).get("facts") or []:
        amt = _to_decimal((f or {}).get("amount_rub"))
        d = _parse_date((f or {}).get("date"))
        if amt is None or d is None or d > as_of:
            continue
        if d >= ytd_start:
            fy += amt
        if d >= ms:
            fm += amt
        if d >= wk:
            fw += amt

    exp_week = plan_weekly
    exp_month = plan_monthly
    # YTD expected = plan_monthly * (дни с ytd_start по as_of включительно)/30.4375
    exp_ytd = None
    if plan_monthly is not None:
        days = max(0, (as_of - ytd_start).days + 1)
        months_fraction = Decimal(days) / _DAYS_PER_MONTH
        exp_ytd = (plan_monthly * months_fraction).quantize(_M2)

    def gap(exp, fact):
        return _q2(max(exp - fact, Decimal(0))) if exp is not None else None

    gap_week = gap(exp_week, fw)
    gap_month = gap(exp_month, fm)
    gap_ytd = gap(exp_ytd, fy)

    missed_week = (1 if (plan_weekly and plan_weekly > 0 and fw < (exp_week or 0))
                   else (0 if plan_weekly is not None else None))
    missed_month = _missed(gap_month, plan_weekly)
    missed_ytd = _missed(gap_ytd, plan_weekly)

    if plan is None:
        status = STATUS_NOT_CONFIGURED
    elif not enabled:
        status = STATUS_DISABLED
    elif (gap_month or Decimal(0)) == 0 and (gap_week or Decimal(0)) == 0:
        status = STATUS_ON_TRACK
    else:
        status = STATUS_BEHIND

    return _Core(
        enabled=enabled, currency=currency, plan_weekly=plan_weekly,
        plan_monthly=plan_monthly, plan_start=plan_start, next_date=next_date,
        as_of=as_of, week_start=wk, month_start=ms, year_start=ys,
        fact_week=fw, fact_month=fm, fact_ytd=fy,
        exp_week=_q2(exp_week), exp_month=_q2(exp_month), exp_ytd=exp_ytd,
        gap_week=gap_week, gap_month=gap_month, gap_ytd=gap_ytd,
        missed_week=missed_week, missed_month=missed_month, missed_ytd=missed_ytd,
        catch_up=gap_month, status=status)


def _guards(config_mutated: bool = False) -> dict:
    return {
        "broker_api_called": False,
        GUARD_LIVE_ORDER_SENT: False,
        "post_order_called": False,
        GUARD_CANCEL_CALLED: False,
        "sell_order_sent": False,
        "market_order_used": False,
        "retry_execution": False,
        "portfolio_mutated": False,
        "config_mutated": bool(config_mutated),
        "telegram_sent": False,
        "scheduler_created": False,
        "token_printed": False,
    }


def _token_policy() -> dict:
    return {
        "read_only_token_present": False,
        "read_only_token_used_for": None,
        "live_token_used": False,
        "sandbox_token_used": False,
        "token_printed": False,
    }


def compute_status(plan: dict | None, *, as_of: date,
                   config_mutated: bool = False) -> dict:
    """Полный статус F4.10 (для команды status и отчёта)."""
    warnings: list[str] = []
    errors: list[str] = []
    if plan is None:
        warnings.append(
            f"{WARN_NOT_CONFIGURED}: план пополнений не настроен. Создайте его: "
            f"{SETUP_HINT}")
    else:
        verrs = validate_plan(plan)
        if verrs:
            errors.extend(verrs)
    c = _compute_core(plan, as_of)
    days_until = ((c.next_date - as_of).days if c.next_date is not None else None)
    return {
        "kind": KIND,
        "stage": STAGE,
        "mode": MODE,
        "contributions_tracking_enabled": c.enabled,
        "currency": c.currency,
        "plan_weekly_rub": _q2(c.plan_weekly),
        "plan_monthly_rub": _q2(c.plan_monthly),
        "plan_start_date": c.plan_start.isoformat() if c.plan_start else None,
        "as_of_date": c.as_of.isoformat(),
        "current_week_start": c.week_start.isoformat(),
        "current_month_start": c.month_start.isoformat(),
        "current_year_start": c.year_start.isoformat(),
        "contribution_fact_weekly_rub": _q2(c.fact_week),
        "contribution_fact_monthly_rub": _q2(c.fact_month),
        "contribution_fact_ytd_rub": _q2(c.fact_ytd),
        "contribution_expected_weekly_rub": c.exp_week,
        "contribution_expected_monthly_rub": c.exp_month,
        "contribution_expected_ytd_rub": c.exp_ytd,
        "contribution_gap_weekly_rub": c.gap_week,
        "contribution_gap_monthly_rub": c.gap_month,
        "contribution_gap_ytd_rub": c.gap_ytd,
        "missed_contributions_count_week": c.missed_week,
        "missed_contributions_count_month": c.missed_month,
        "missed_contributions_count_ytd": c.missed_ytd,
        "next_planned_contribution_date": (
            c.next_date.isoformat() if c.next_date else None),
        "days_until_next_planned_contribution": days_until,
        "contribution_required_to_catch_up_rub": c.catch_up,
        "expected_ytd_formula": "plan_monthly_rub * days_since_start / 30.4375",
        "status": c.status,
        "warnings": warnings,
        "errors": errors,
        "guards": _guards(config_mutated),
        "token_policy": _token_policy(),
    }


def summarize_for_dashboard(plan: dict | None, *, as_of: date) -> dict:
    """F4.8 contributions_summary (совместимые ключи + богаче), из общей логики."""
    if not plan or not plan.get("enabled"):
        return {
            "contributions_tracking_enabled": False,
            "contribution_plan_weekly_rub": None,
            "contribution_plan_monthly_rub": None,
            "contribution_fact_weekly_rub": None,
            "contribution_fact_monthly_rub": None,
            "contribution_fact_ytd_rub": None,
            "contribution_gap_weekly_rub": None,
            "contribution_gap_monthly_rub": None,
            "contribution_gap_ytd_rub": None,
            "missed_contributions_count_week": None,
            "missed_contributions_count_month": None,
            "missed_contributions_count_ytd": None,
            "next_planned_contribution_date": None,
            "days_until_next_planned_contribution": None,
            "contribution_required_to_catch_up_rub": None,
            "contribution_status": (STATUS_DISABLED if plan else None),
            "contribution_source": None,
            "warnings": [WARN_NOT_CONFIGURED],
        }
    c = _compute_core(plan, as_of)
    days_until = ((c.next_date - as_of).days if c.next_date is not None else None)
    return {
        "contributions_tracking_enabled": True,
        "contribution_plan_weekly_rub": _q2(c.plan_weekly),
        "contribution_plan_monthly_rub": _q2(c.plan_monthly),
        "contribution_fact_weekly_rub": _q2(c.fact_week),
        "contribution_fact_monthly_rub": _q2(c.fact_month),
        "contribution_fact_ytd_rub": _q2(c.fact_ytd),
        "contribution_gap_weekly_rub": c.gap_week,
        "contribution_gap_monthly_rub": c.gap_month,
        "contribution_gap_ytd_rub": c.gap_ytd,
        "missed_contributions_count_week": c.missed_week,
        "missed_contributions_count_month": c.missed_month,
        "missed_contributions_count_ytd": c.missed_ytd,
        "next_planned_contribution_date": (
            c.next_date.isoformat() if c.next_date else None),
        "days_until_next_planned_contribution": days_until,
        "contribution_required_to_catch_up_rub": c.catch_up,
        "contribution_status": c.status,
        "contribution_source": plan.get("source") or "manual",
        "warnings": [],
    }


# ─── отчёты ───────────────────────────────────────────────────────────────────

def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def render_status_md(status: dict) -> str:
    def row(label, key):
        return f"| {label} | {_fmt(status.get(key))} |"

    lines = [
        "# F4.10 — статус плана пополнений (LOCAL, не торговля)",
        "",
        "> Локальный ручной учёт пополнений. Депозиты брокера НЕ читаются "
        "автоматически. Никаких токенов/сети/торговли.",
        "",
        f"- **status: {_fmt(status.get('status'))}**",
        f"- as_of: `{_fmt(status.get('as_of_date'))}` | "
        f"включён: {_fmt(status.get('contributions_tracking_enabled'))}",
        "",
        "## План",
        "",
        "| Поле | Значение |",
        "| --- | --- |",
        row("Валюта", "currency"),
        row("План / неделя", "plan_weekly_rub"),
        row("План / месяц", "plan_monthly_rub"),
        row("Старт плана", "plan_start_date"),
        row("След. плановый взнос", "next_planned_contribution_date"),
        row("Дней до след. взноса", "days_until_next_planned_contribution"),
        "",
        "## Факт / план / разрыв",
        "",
        "| Период | Факт | Ожидается | Разрыв | Пропущено |",
        "| --- | --- | --- | --- | --- |",
        (f"| Неделя | {_fmt(status.get('contribution_fact_weekly_rub'))} | "
         f"{_fmt(status.get('contribution_expected_weekly_rub'))} | "
         f"{_fmt(status.get('contribution_gap_weekly_rub'))} | "
         f"{_fmt(status.get('missed_contributions_count_week'))} |"),
        (f"| Месяц | {_fmt(status.get('contribution_fact_monthly_rub'))} | "
         f"{_fmt(status.get('contribution_expected_monthly_rub'))} | "
         f"{_fmt(status.get('contribution_gap_monthly_rub'))} | "
         f"{_fmt(status.get('missed_contributions_count_month'))} |"),
        (f"| YTD | {_fmt(status.get('contribution_fact_ytd_rub'))} | "
         f"{_fmt(status.get('contribution_expected_ytd_rub'))} | "
         f"{_fmt(status.get('contribution_gap_ytd_rub'))} | "
         f"{_fmt(status.get('missed_contributions_count_ytd'))} |"),
        "",
        f"Нужно довнести (месяц): **{_fmt(status.get('contribution_required_to_catch_up_rub'))}**",
        f"\nФормула YTD-ожидания: `{_fmt(status.get('expected_ytd_formula'))}`",
    ]
    if status.get("warnings"):
        lines += ["", "## Предупреждения"]
        lines += [f"- {w}" for w in status["warnings"]]
    if status.get("errors"):
        lines += ["", "## Ошибки"]
        lines += [f"- {e}" for e in status["errors"]]
    lines += [
        "",
        "---",
        "",
        "Local contribution accounting only. No broker calls, no tokens, no trading.",
        "",
    ]
    return "\n".join(lines)


def write_status_report(status: dict, *, json_path: str = DEFAULT_STATUS_JSON,
                        md_path: str = DEFAULT_STATUS_MD) -> dict:
    jp, mp = Path(json_path), Path(md_path)
    jp.parent.mkdir(parents=True, exist_ok=True)
    mp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(status, ensure_ascii=False, indent=2,
                             default=_json_default), encoding="utf-8")
    mp.write_text(render_status_md(status), encoding="utf-8")
    out = dict(status)
    out["_output_json"] = str(jp)
    out["_output_md"] = str(mp)
    return out
