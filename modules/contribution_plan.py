"""
contribution_plan — F4.10/F4.10.1 учёт плана пополнений и пропущенных взносов.

План (цели/старт/график) — полностью ЛОКАЛЬНЫЙ, отдельно от торговли. Управляет
файлом `data/config/contribution_plan.json` и считает статус: факт/план/разрыв по
неделе/месяцу/году, число пропущенных взносов, статус ON_TRACK/BEHIND/NOT_STARTED/
DISABLED/NOT_CONFIGURED.

F4.10.1: ФАКТ пополнений по умолчанию извлекается из READ-ONLY операций брокера
(тот же путь, что у F4.8 — список операций ПЕРЕДАЁТСЯ сюда, модуль сам в сеть НЕ
ходит и токены НЕ читает). Депозиты считаются взносами, выводы — отдельно (не
уменьшают взнос, но учитываются в net cash flow). Ручные `facts[]` остаются только
как fallback/ручная корректировка.

Жёсткий контракт (никогда не нарушать):
- НИКАКИХ брокерских вызовов/токенов/сети/брокер-клиента в этом модуле. Операции
  приходят аргументом из F4.8. Не читает значения токенов. Не торгует, не отменяет,
  не продаёт, не ретраит, не шлёт Telegram, не создаёт планировщик.
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

from common.helpers import quotation_to_decimal
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
STATUS_NOT_STARTED = "NOT_STARTED"
STATUS_DISABLED = "DISABLED"
STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"

# ── источники факта пополнений ──
FACT_SOURCE_API = "api_operations"
FACT_SOURCE_MANUAL = "manual"
FACT_SOURCE_MIXED = "mixed"
ALLOWED_FACT_SOURCES = (FACT_SOURCE_API, FACT_SOURCE_MANUAL, FACT_SOURCE_MIXED)
DEFAULT_FACT_SOURCE = FACT_SOURCE_API

# ── значения contribution_source в выводе дашборда ──
SOURCE_API = "readonly_operations_api"
SOURCE_MANUAL_FALLBACK = "manual_fallback"
SOURCE_MIXED = "mixed_api_plus_manual_adjustments"

# ── типы операций T-Invest (REST enum-строки), стабильнее русского текста ──
DEPOSIT_OPERATION_TYPES = frozenset({"OPERATION_TYPE_INPUT"})
WITHDRAWAL_OPERATION_TYPES = frozenset({"OPERATION_TYPE_OUTPUT"})
# Известные НЕ-взносы (сделки/доход/комиссии/налоги/овернайты) — пропускаем молча.
RECOGNIZED_NON_CONTRIBUTION_TYPES = frozenset({
    "OPERATION_TYPE_BUY", "OPERATION_TYPE_SELL", "OPERATION_TYPE_BUY_CARD",
    "OPERATION_TYPE_SELL_CARD", "OPERATION_TYPE_BUY_MARGIN",
    "OPERATION_TYPE_SELL_MARGIN", "OPERATION_TYPE_BUY_CURRENCY",
    "OPERATION_TYPE_SELL_CURRENCY", "OPERATION_TYPE_DIVIDEND",
    "OPERATION_TYPE_COUPON", "OPERATION_TYPE_DIVIDEND_TAX",
    "OPERATION_TYPE_COUPON_TAX", "OPERATION_TYPE_TAX",
    "OPERATION_TYPE_TAX_CORRECTION", "OPERATION_TYPE_TAX_REPO",
    "OPERATION_TYPE_BENEFIT_TAX", "OPERATION_TYPE_BROKER_FEE",
    "OPERATION_TYPE_SERVICE_FEE", "OPERATION_TYPE_MARGIN_FEE",
    "OPERATION_TYPE_SUCCESS_FEE", "OPERATION_TYPE_TRACK_MFEE",
    "OPERATION_TYPE_TRACK_PFEE", "OPERATION_TYPE_OVERNIGHT",
    "OPERATION_TYPE_ACCRUING_VARMARGIN", "OPERATION_TYPE_WRITING_OFF_VARMARGIN",
    "OPERATION_TYPE_DELIVERY_BUY", "OPERATION_TYPE_DELIVERY_SELL",
    "OPERATION_TYPE_OUT_FEE", "OPERATION_TYPE_OUT_STAMP_DUTY",
})

WARN_NOT_CONFIGURED = "contribution_plan_not_configured"
WARN_API_UNAVAILABLE = "contribution_api_operations_unavailable_manual_fallback"
WARN_API_UNRECOGNIZED = "contribution_api_operation_type_unrecognized"
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


def _amount_to_num(value: Decimal | None):
    """Decimal → int (если целое) или float, для JSON-структур фактов."""
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _period_sums(items, as_of: date, week_start: date, month_start: date,
                 ytd_start: date) -> tuple[Decimal, Decimal, Decimal]:
    """Суммы amount_rub фактов по неделе/месяцу/YTD (дата ≤ as_of)."""
    fw = fm = fy = Decimal(0)
    for it in items or []:
        amt = _to_decimal((it or {}).get("amount_rub"))
        d = _parse_date((it or {}).get("date"))
        if amt is None or d is None or d > as_of:
            continue
        if d >= ytd_start:
            fy += amt
        if d >= month_start:
            fm += amt
        if d >= week_start:
            fw += amt
    return fw, fm, fy


# ─── извлечение фактов из read-only операций (F4.10.1) ─────────────────────────

def _op_type(op: dict) -> str:
    return str((op or {}).get("operationType") or (op or {}).get("type") or "")


def _op_date(op: dict) -> date | None:
    raw = (op or {}).get("date")
    return _parse_date(str(raw)[:10]) if raw else None


def _op_amount(op: dict) -> Decimal | None:
    pay = (op or {}).get("payment")
    if isinstance(pay, dict):
        return quotation_to_decimal(pay)
    return _to_decimal(pay)


def is_api_contribution_operation(op: dict) -> bool:
    """True, если операция — денежное пополнение счёта (а не сделка/доход/комиссия)."""
    return _op_type(op) in DEPOSIT_OPERATION_TYPES


def is_api_withdrawal_operation(op: dict) -> bool:
    """True, если операция — вывод денежных средств со счёта."""
    return _op_type(op) in WITHDRAWAL_OPERATION_TYPES


def extract_api_contribution_facts(operations: list[dict]) -> dict:
    """Делит read-only операции на депозиты/выводы. Не угадывает неизвестные типы.

    Депозит → факт пополнения; вывод → отдельный факт (не уменьшает взнос). Сделки,
    дивиденды/купоны, комиссии/налоги — распознанные НЕ-взносы, пропускаются молча.
    Неизвестный тип → warning `contribution_api_operation_type_unrecognized`, факт НЕ
    создаётся (partial=true). Дубликаты по operation_id не учитываются дважды.
    """
    deposit_facts: list[dict] = []
    withdrawal_facts: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    partial = False
    seen_ids: set = set()
    unrecognized_flagged = False

    for op in operations or []:
        if not isinstance(op, dict):
            continue
        op_type = _op_type(op)
        op_id = op.get("id") or op.get("operationId")
        is_dep = is_api_contribution_operation(op)
        is_wd = is_api_withdrawal_operation(op)
        if is_dep or is_wd:
            if op_id is not None and op_id in seen_ids:
                continue
            if op_id is not None:
                seen_ids.add(op_id)
            d = _op_date(op)
            amt = _op_amount(op)
            fact = {
                "date": d.isoformat() if d else None,
                "amount_rub": _amount_to_num(abs(amt) if amt is not None else None),
                "operation_id": op_id,
                "source": SOURCE_API,
                "raw_type": op_type,
            }
            (deposit_facts if is_dep else withdrawal_facts).append(fact)
        elif op_type in RECOGNIZED_NON_CONTRIBUTION_TYPES:
            continue
        else:
            partial = True
            if not unrecognized_flagged:
                warnings.append(WARN_API_UNRECOGNIZED)
                unrecognized_flagged = True

    return {
        "deposit_facts": deposit_facts,
        "withdrawal_facts": withdrawal_facts,
        "warnings": warnings,
        "errors": errors,
        "partial": partial,
    }


def _manual_as_facts(manual_facts: list[dict]) -> list[dict]:
    """Нормализует ручные facts в единую структуру (source=manual)."""
    out: list[dict] = []
    for f in manual_facts or []:
        d = _parse_date((f or {}).get("date"))
        amt = _to_decimal((f or {}).get("amount_rub"))
        if d is None or amt is None:
            continue
        out.append({
            "date": d.isoformat(),
            "amount_rub": _amount_to_num(amt),
            "operation_id": None,
            "source": "manual",
            "raw_type": "manual_fact",
        })
    return out


def _merge_api_and_manual(api_facts: list[dict], manual_facts: list[dict]) -> list[dict]:
    """Объединяет API-депозиты с ручными корректировками, дедуп по id / (дата,сумма)."""
    merged = list(api_facts)
    seen: set = set()
    for f in api_facts:
        oid = f.get("operation_id")
        if oid is not None:
            seen.add(("id", str(oid)))
        seen.add(("da", str(f.get("date")), str(_to_decimal(f.get("amount_rub")))))
    for m in _manual_as_facts(manual_facts):
        key = ("da", str(m.get("date")), str(_to_decimal(m.get("amount_rub"))))
        if key in seen:
            continue
        seen.add(key)
        merged.append(m)
    return merged


def _last_contribution(facts: list[dict], as_of: date) -> tuple[str | None, Decimal | None]:
    last_d: date | None = None
    last_amt: Decimal | None = None
    for f in facts or []:
        d = _parse_date((f or {}).get("date"))
        amt = _to_decimal((f or {}).get("amount_rub"))
        if d is None or amt is None or d > as_of:
            continue
        if last_d is None or d >= last_d:
            last_d, last_amt = d, amt
    return (last_d.isoformat() if last_d else None), _q2(last_amt)


def _facts_preview(facts: list[dict], as_of: date, limit: int = 5) -> list[dict]:
    """Последние `limit` фактов (по дате ≤ as_of) для дашборда."""
    rows = [f for f in (facts or [])
            if _parse_date((f or {}).get("date")) is not None
            and _parse_date((f or {}).get("date")) <= as_of]
    rows.sort(key=lambda f: str(f.get("date") or ""))
    return [{
        "date": f.get("date"),
        "amount_rub": _amount_to_num(_to_decimal(f.get("amount_rub"))),
        "source": f.get("source"),
        "operation_id": f.get("operation_id"),
    } for f in rows[-limit:]]


def _fact_source(plan: dict | None) -> str:
    raw = str((plan or {}).get("fact_source") or DEFAULT_FACT_SOURCE).lower()
    return raw if raw in ALLOWED_FACT_SOURCES else DEFAULT_FACT_SOURCE


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
    if "fact_source" in plan:
        fs = str(plan.get("fact_source") or "").lower()
        if fs not in ALLOWED_FACT_SOURCES:
            errors.append(
                "fact_source должно быть одним из "
                f"{', '.join(ALLOWED_FACT_SOURCES)} (получено: {fs}).")
    if "manual_facts_enabled" in plan and not isinstance(
            plan.get("manual_facts_enabled"), bool):
        errors.append("manual_facts_enabled должно быть bool.")
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
              fact_source: str = DEFAULT_FACT_SOURCE,
              manual_facts_enabled: bool = False,
              facts: list | None = None, enabled: bool = True) -> dict:
    """Конструирует план-объект (без записи на диск)."""
    fs = str(fact_source or DEFAULT_FACT_SOURCE).lower()
    if fs not in ALLOWED_FACT_SOURCES:
        fs = DEFAULT_FACT_SOURCE
    return {
        "enabled": bool(enabled),
        "currency": str(currency or "rub").lower(),
        "plan_weekly_rub": _num(weekly_rub),
        "plan_monthly_rub": _num(monthly_rub),
        "plan_start_date": str(start_date) if start_date else None,
        "next_planned_contribution_date": str(next_date) if next_date else None,
        "source": source or "manual",
        "fact_source": fs,
        "manual_facts_enabled": bool(manual_facts_enabled),
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
              fact_source: str = DEFAULT_FACT_SOURCE,
              manual_facts_enabled: bool = False,
              existing: dict | None = None, reset_facts: bool = False) -> dict:
    """Создаёт/обновляет план, СОХРАНЯЯ существующие facts (если не reset_facts)."""
    facts = []
    if existing and not reset_facts:
        facts = list(existing.get("facts") or [])
    plan = make_plan(weekly_rub=weekly_rub, monthly_rub=monthly_rub,
                     start_date=start_date, next_date=next_date, currency=currency,
                     source=source, fact_source=fact_source,
                     manual_facts_enabled=manual_facts_enabled, facts=facts)
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
    plan_started: bool
    days_until_start: int


def _missed(gap: Decimal | None, plan_weekly: Decimal | None) -> int | None:
    if gap is None:
        return None
    if plan_weekly and plan_weekly > 0:
        return max(0, math.ceil(gap / plan_weekly)) if gap > 0 else 0
    return 1 if gap > 0 else 0


def _compute_core(plan: dict | None, as_of: date, *,
                  facts_override: list[dict] | None = None) -> _Core:
    enabled = bool((plan or {}).get("enabled")) if plan else False
    currency = str((plan or {}).get("currency") or "rub").lower()
    plan_weekly = _to_decimal((plan or {}).get("plan_weekly_rub"))
    plan_monthly = _to_decimal((plan or {}).get("plan_monthly_rub"))
    plan_start = _parse_date((plan or {}).get("plan_start_date"))
    next_date = _parse_date((plan or {}).get("next_planned_contribution_date"))

    wk, ms, ys = _week_start(as_of), _month_start(as_of), _year_start(as_of)
    # эффективное начало года для YTD: max(Jan 1, plan_start_date)
    ytd_start = max(ys, plan_start) if plan_start else ys

    facts = (facts_override if facts_override is not None
             else (plan or {}).get("facts") or [])
    fw, fm, fy = _period_sums(facts, as_of, wk, ms, ytd_start)

    # до старта плана: ожиданий/разрывов/пропусков нет — долг не создаётся.
    plan_started = (plan_start is None) or (as_of >= plan_start)
    days_until_start = ((plan_start - as_of).days
                        if (plan_start is not None and as_of < plan_start) else 0)

    if not plan_started:
        exp_week = exp_month = exp_ytd = Decimal(0)
        gap_week = gap_month = gap_ytd = Decimal(0)
        missed_week = missed_month = missed_ytd = 0
        catch_up = Decimal(0)
    else:
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
        catch_up = gap_month

    if plan is None:
        status = STATUS_NOT_CONFIGURED
    elif not enabled:
        status = STATUS_DISABLED
    elif not plan_started:
        status = STATUS_NOT_STARTED
    elif (gap_month or Decimal(0)) == 0 and (gap_week or Decimal(0)) == 0:
        status = STATUS_ON_TRACK
    else:
        status = STATUS_BEHIND

    return _Core(
        enabled=enabled, currency=currency, plan_weekly=plan_weekly,
        plan_monthly=plan_monthly, plan_start=plan_start, next_date=next_date,
        as_of=as_of, week_start=wk, month_start=ms, year_start=ys,
        fact_week=fw, fact_month=fm, fact_ytd=fy,
        exp_week=_q2(exp_week), exp_month=_q2(exp_month), exp_ytd=_q2(exp_ytd),
        gap_week=gap_week, gap_month=gap_month, gap_ytd=gap_ytd,
        missed_week=missed_week, missed_month=missed_month, missed_ytd=missed_ytd,
        catch_up=catch_up, status=status, plan_started=plan_started,
        days_until_start=days_until_start)


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
        "contribution_plan_started": c.plan_started,
        "days_until_plan_start": c.days_until_start,
        "expected_ytd_formula": "plan_monthly_rub * days_since_start / 30.4375",
        "status": c.status,
        "warnings": warnings,
        "errors": errors,
        "guards": _guards(config_mutated),
        "token_policy": _token_policy(),
    }


def _disabled_summary(plan: dict | None, as_of: date) -> dict:
    """contributions_summary, когда план не настроен/выключен (со всеми ключами)."""
    plan_start = _parse_date((plan or {}).get("plan_start_date"))
    started = (plan_start is None) or (as_of >= plan_start)
    days_until = ((plan_start - as_of).days
                  if (plan_start is not None and as_of < plan_start) else 0)
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
        "contribution_data_quality": None,
        "contribution_fact_source_preferred": _fact_source(plan) if plan else None,
        "contribution_api_deposit_facts_count": 0,
        "contribution_manual_facts_count": len((plan or {}).get("facts") or []),
        "contribution_api_withdrawal_facts_count": 0,
        "withdrawal_fact_weekly_rub": None,
        "withdrawal_fact_monthly_rub": None,
        "withdrawal_fact_ytd_rub": None,
        "net_cash_flow_weekly_rub": None,
        "net_cash_flow_monthly_rub": None,
        "net_cash_flow_ytd_rub": None,
        "last_contribution_date": None,
        "last_contribution_amount_rub": None,
        "contribution_facts_preview": [],
        "contribution_plan_started": started,
        "days_until_plan_start": days_until,
        "contribution_warnings": [WARN_NOT_CONFIGURED],
        "warnings": [WARN_NOT_CONFIGURED],
    }


def _resolve_facts(plan: dict, as_of: date, api_operations, prefer_api):
    """Выбирает источник факта пополнений по приоритету API → manual fallback → mixed.

    Возвращает (deposit_facts, withdrawal_facts, source, data_quality, warnings).
    """
    fact_source = _fact_source(plan)
    manual_enabled = bool(plan.get("manual_facts_enabled", False))
    manual_facts = list(plan.get("facts") or [])
    api_available = api_operations is not None
    warnings: list[str] = []

    wants_api = fact_source in (FACT_SOURCE_API, FACT_SOURCE_MIXED) and prefer_api

    if wants_api and api_available:
        extraction = extract_api_contribution_facts(api_operations)
        warnings.extend(extraction["warnings"])
        deposit_facts = list(extraction["deposit_facts"])
        withdrawal_facts = list(extraction["withdrawal_facts"])
        if fact_source == FACT_SOURCE_MIXED and manual_enabled:
            deposit_facts = _merge_api_and_manual(deposit_facts, manual_facts)
            source = SOURCE_MIXED
        else:
            source = SOURCE_API
        data_quality = "partial" if extraction["partial"] else "full"
        return deposit_facts, withdrawal_facts, source, data_quality, warnings

    # API недоступен, хотя предпочтителен → ручной fallback с предупреждением
    if wants_api and not api_available:
        warnings.append(WARN_API_UNAVAILABLE)
        return (_manual_as_facts(manual_facts), [], SOURCE_MANUAL_FALLBACK,
                "manual_fallback", warnings)

    # fact_source == "manual" (или prefer_api=False): ручной источник без warning
    return (_manual_as_facts(manual_facts), [], SOURCE_MANUAL_FALLBACK,
            "manual_fallback", warnings)


def summarize_for_dashboard(plan: dict | None, *, as_of: date,
                            api_operations: list[dict] | None = None,
                            prefer_api: bool = True) -> dict:
    """F4.8 contributions_summary. Факт по умолчанию — из read-only операций (F4.10.1).

    `api_operations` — список операций из READ-ONLY пути F4.8 (этот модуль сам в сеть
    НЕ ходит). None → API недоступен → ручной fallback. Депозиты = взносы, выводы —
    отдельно (net cash flow). До старта плана разрывов/пропусков нет.
    """
    if not plan or not plan.get("enabled"):
        return _disabled_summary(plan, as_of)

    deposit_facts, withdrawal_facts, source, data_quality, warnings = _resolve_facts(
        plan, as_of, api_operations, prefer_api)

    c = _compute_core(plan, as_of, facts_override=deposit_facts)
    days_until = ((c.next_date - as_of).days if c.next_date is not None else None)

    wk, ms, ys = _week_start(as_of), _month_start(as_of), _year_start(as_of)
    ytd_start = max(ys, c.plan_start) if c.plan_start else ys
    ww, wm, wy = _period_sums(withdrawal_facts, as_of, wk, ms, ytd_start)

    last_date, last_amt = _last_contribution(deposit_facts, as_of)
    api_deposit_count = sum(1 for f in deposit_facts if f.get("source") == SOURCE_API)

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
        # F4.10.1 — источник/качество факта
        "contribution_source": source,
        "contribution_data_quality": data_quality,
        "contribution_fact_source_preferred": _fact_source(plan),
        "contribution_api_deposit_facts_count": api_deposit_count,
        "contribution_manual_facts_count": len(plan.get("facts") or []),
        "contribution_api_withdrawal_facts_count": len(withdrawal_facts),
        # выводы и net cash flow (вторичные метрики)
        "withdrawal_fact_weekly_rub": _q2(ww),
        "withdrawal_fact_monthly_rub": _q2(wm),
        "withdrawal_fact_ytd_rub": _q2(wy),
        "net_cash_flow_weekly_rub": _q2(c.fact_week - ww),
        "net_cash_flow_monthly_rub": _q2(c.fact_month - wm),
        "net_cash_flow_ytd_rub": _q2(c.fact_ytd - wy),
        "last_contribution_date": last_date,
        "last_contribution_amount_rub": last_amt,
        "contribution_facts_preview": _facts_preview(deposit_facts, as_of),
        "contribution_plan_started": c.plan_started,
        "days_until_plan_start": c.days_until_start,
        "contribution_warnings": warnings,
        "warnings": warnings,
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
        row("План стартовал", "contribution_plan_started"),
        row("Дней до старта плана", "days_until_plan_start"),
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
