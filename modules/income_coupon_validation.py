"""
income_coupon_validation — read-only диагностика купонных/облигационных кандидатов
из audit group C (coupon-validation) income universe.

Что делает:
- читает ТОЛЬКО локальные отчёты:
  data/reports/income_universe_builder_report.json (builder)
  data/reports/income_universe_disabled_audit.json (audit);
- выбирает кандидатов только из audit group C (coupon-validation);
- классифицирует купон: floating / fixed / unknown;
- определяет coupon_validation_status и income_readiness;
- блокирует наивную annualization для floating / неполных данных
  (annualization guard);
- в offline-режиме работает только по локальным отчётам, без сети;
- в API-режиме может дополнить данные read-only методами T-Invest
  (резолв инструмента, купонный календарь, НКД, последняя цена);
- пишет json + md в data/reports/.

Чего НЕ делает (жёсткий контракт):
- НЕ отправляет/не отменяет заявки, НЕ исполняет, НЕ торгует;
- НЕ использует full-access токен; только read-only методы;
- НЕ меняет income policy / target portfolio / income universe builder enable
  logic / resolver behavior;
- НЕ пишет в data/config/*.yaml;
- НЕ включает (auto-enable) ни одного disabled-кандидата;
- НЕ даёт инвестиционных рекомендаций — это аналитика, а не рекомендация.

auto_enable_allowed всегда false для всех кандидатов. recommendation_guard
всегда "candidate_for_analysis_only".
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from common.helpers import quotation_to_decimal

# роли (совпадают с income_universe_builder / income_universe_audit)
ROLE_DIVIDEND = "dividend_candidate"
ROLE_BOND = "bond_candidate"
ROLE_OFZ = "ofz_pk_candidate"
ROLE_MONEY_MARKET = "money_market"

DEFAULT_BUILDER_REPORT = "data/reports/income_universe_builder_report.json"
DEFAULT_AUDIT_REPORT = "data/reports/income_universe_disabled_audit.json"
DEFAULT_OUTPUT_JSON = "data/reports/income_coupon_validation.json"
DEFAULT_OUTPUT_MD = "data/reports/income_coupon_validation.md"

RECOMMENDATION_GUARD = "candidate_for_analysis_only"

RECOMMENDED_NEXT_PR = (
    "coupon-validation report only is complete; next implementation candidates: "
    "resolver/mapping PR for group D, manual-income policy PR for group A/B, "
    "and a separate floating-coupon / future policy review for instruments "
    "validated here. Do not auto-enable disabled candidates."
)

# coupon_validation_status
STATUS_SCHEDULE_AVAILABLE = "coupon_schedule_available"
STATUS_FLOATING = "floating_coupon_detected"
STATUS_FIXED = "fixed_coupon_detected"
STATUS_DATA_MISSING = "coupon_data_missing"
STATUS_UNRESOLVED = "unresolved_instrument"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_ERROR = "validation_error"

# income_readiness (ни одно значение НЕ означает auto-enable)
READY_NOT_READY = "not_ready"
READY_DATA_MISSING = "data_missing"
READY_NEEDS_FLOATING_POLICY = "needs_floating_coupon_policy"
READY_NEEDS_ANNUALIZATION_GUARD = "needs_annualization_guard"
READY_NEEDS_MANUAL_REVIEW = "needs_manual_review"
READY_FUTURE_POLICY_REVIEW = "candidate_for_future_policy_review"

# coupon_type
COUPON_FIXED = "fixed"
COUPON_FLOATING = "floating"
COUPON_UNKNOWN = "unknown"

# couponType enum (T-Invest REST) → наш coupon_type
FLOATING_COUPON_ENUMS = {
    "COUPON_TYPE_FLOATING",
    "COUPON_TYPE_OFZ_PK",
    "COUPON_TYPE_VARIABLE",
}
FIXED_COUPON_ENUMS = {
    "COUPON_TYPE_FIXED",
    "COUPON_TYPE_CONSTANT",
}


class CouponValidationError(Exception):
    """Понятная ошибка (например, нет builder/audit отчёта)."""


# ─── чтение локальных отчётов (read-only) ─────────────────────────────────────

def _load_json_report(path: str, *, kind: str, regen_hint: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise CouponValidationError(
            f"Не найден {kind}-отчёт: {p}. Сначала выполните:\n"
            f"  python main.py build-income-universe --force\n"
            f"  python main.py income-universe-audit\n"
            f"({regen_hint})"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise CouponValidationError(
            f"Не удалось прочитать {kind}-отчёт {p}: {exc}. "
            f"Перегенерируйте его ({regen_hint})."
        ) from exc
    if not isinstance(data, dict):
        raise CouponValidationError(
            f"{kind}-отчёт {p} имеет неожиданный формат (ожидался JSON-объект). "
            f"Перегенерируйте его ({regen_hint})."
        )
    return data


def load_builder_report(path: str | None = None) -> dict:
    return _load_json_report(
        path or DEFAULT_BUILDER_REPORT,
        kind="builder",
        regen_hint="python main.py build-income-universe --force",
    )


def load_audit_report(path: str | None = None) -> dict:
    return _load_json_report(
        path or DEFAULT_AUDIT_REPORT,
        kind="audit",
        regen_hint="python main.py income-universe-audit",
    )


def select_group_c(audit_report: dict) -> list[dict]:
    """Выбирает кандидатов ТОЛЬКО из audit group C (coupon-validation).

    Группы A/B/D/E игнорируются. Совместимо с разными именами поля группы.
    """
    candidates = audit_report.get("candidates")
    if not isinstance(candidates, list):
        return []
    out: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        group = str(c.get("audit_group") or "").strip().upper()
        name = str(c.get("audit_group_name") or "").strip().lower()
        if group == "C" or name == "coupon_validation":
            out.append(c)
    return out


def _builder_index(builder_report: dict) -> dict[tuple[str, str], dict]:
    """Индекс disabled-entries builder по (ticker, class_code) для обогащения."""
    index: dict[tuple[str, str], dict] = {}
    entries = builder_report.get("disabled_entries")
    if not isinstance(entries, list):
        entries = builder_report.get("entries") or []
    for e in entries:
        if not isinstance(e, dict):
            continue
        key = (str(e.get("ticker") or "").upper(),
               str(e.get("class_code") or "").upper())
        index.setdefault(key, e)
    return index


# ─── классификация купона (pure) ──────────────────────────────────────────────

def _looks_ofz_floater(ticker: str) -> bool:
    """OFZ-PK / SU29… secid (флоатеры)."""
    t = (ticker or "").strip().upper()
    return t.startswith("SU29") or (t.startswith("SU") and t[2:5].isdigit())


def classify_coupon_type(*, role: str, ticker: str, notes: str = "",
                         coupon_events: list[dict] | None = None,
                         instrument: dict | None = None) -> str:
    """Определяет coupon_type: floating / fixed / unknown.

    Приоритет: явные данные API (couponType / floatingCouponFlag) →
    роль/тикер-эвристики (offline). Никогда не угадывает доход.
    """
    # 1. По данным купонного календаря (если есть из API)
    enums = {str(ev.get("couponType") or "").strip().upper()
             for ev in (coupon_events or []) if isinstance(ev, dict)}
    enums.discard("")
    if enums & FLOATING_COUPON_ENUMS:
        return COUPON_FLOATING
    if enums and enums <= FIXED_COUPON_ENUMS:
        return COUPON_FIXED

    # 2. По флагу инструмента (если есть из API)
    if isinstance(instrument, dict):
        flt = instrument.get("floatingCouponFlag")
        if flt is True:
            return COUPON_FLOATING
        if flt is False and enums:
            return COUPON_FIXED

    # 3. Offline-эвристики по роли/тикеру/notes
    role = (role or "").strip()
    low_notes = (notes or "").lower()
    if role == ROLE_OFZ or _looks_ofz_floater(ticker) or "ofz-pk" in low_notes \
            or "floating" in low_notes or "плаваю" in low_notes:
        return COUPON_FLOATING
    if role == ROLE_BOND:
        return COUPON_FIXED
    return COUPON_UNKNOWN


def _is_unresolved(candidate: dict) -> bool:
    reason = str(candidate.get("excluded_reason") or "").strip().lower()
    notes = str(candidate.get("notes") or "").lower()
    class_code = str(candidate.get("class_code") or "").strip()
    return (reason == "unresolved"
            or "class_code unresolved" in notes
            or "short-name" in notes
            or not class_code)


# ─── annualization guard (pure) ───────────────────────────────────────────────

def annualization_guard(*, coupon_type: str,
                        next_coupon_value: Decimal | None,
                        nominal: Decimal | None,
                        price: Decimal | None,
                        coupon_freq_per_year: int | None,
                        schedule_available: bool) -> tuple[bool, str, Decimal | None]:
    """Решает, можно ли посчитать диагностический gross coupon yield.

    Возвращает (annualization_allowed, block_reason, estimated_gross_yield_pct).
    Любой пробел в данных или floating-купон блокируют annualization.
    Никогда не angualize-ит floating или неполные данные.
    """
    reasons: list[str] = []
    if coupon_type == COUPON_FLOATING:
        reasons.append("floating coupon: будущий купон неизвестен, annualize нельзя")
    if coupon_type == COUPON_UNKNOWN:
        reasons.append("coupon type unknown")
    if not schedule_available:
        reasons.append("нет купонного календаря")
    if coupon_freq_per_year is None or coupon_freq_per_year <= 0:
        reasons.append("частота купонов неизвестна")
    if next_coupon_value is None or next_coupon_value <= 0:
        reasons.append("нет ближайшего купона")
    if nominal is None or nominal <= 0:
        reasons.append("номинал неизвестен")
    if price is None or price <= 0:
        reasons.append("цена неизвестна")

    if reasons:
        return False, "; ".join(reasons), None

    # Все guard-условия пройдены — только диагностический gross yield.
    gross = (next_coupon_value * Decimal(coupon_freq_per_year)) / price * Decimal("100")
    return True, "", gross.quantize(Decimal("0.0001"))


# ─── API-обогащение (read-only, опционально) ──────────────────────────────────

def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _fmt_date(value) -> str | None:
    dt = _parse_iso(value)
    return dt.date().isoformat() if dt else None


def _enrich_from_api(candidate: dict, client) -> dict:
    """Read-only обогащение одного кандидата данными T-Invest. Без сети → {}.

    Использует только read-only методы фасада ReadOnlyClient. Любая ошибка
    деградирует в пустой результат (offline-like), без падения.
    """
    out: dict = {}
    ticker = str(candidate.get("ticker") or "").strip()
    class_code = str(candidate.get("class_code") or "").strip()
    if not (ticker and class_code):
        return out
    try:
        instrument = client.find_instrument(ticker, class_code)
    except Exception:  # noqa: BLE001 — обогащение опционально
        return out
    if not isinstance(instrument, dict):
        return out

    out["instrument"] = instrument
    out["figi"] = str(instrument.get("figi") or "")
    out["uid"] = str(instrument.get("uid") or instrument.get("instrumentUid") or "")
    out["isin"] = str(instrument.get("isin") or "")
    out["name"] = str(instrument.get("name") or "")
    nominal = instrument.get("nominal")
    if nominal:
        out["nominal"] = quotation_to_decimal(nominal)
    freq = instrument.get("couponQuantityPerYear")
    try:
        out["coupon_freq_per_year"] = int(freq) if freq else None
    except (TypeError, ValueError):
        out["coupon_freq_per_year"] = None

    instrument_id = out["figi"] or out["uid"]
    if not instrument_id:
        return out

    # купонный календарь (read-only)
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    to = (now + timedelta(days=400)).isoformat().replace("+00:00", "Z")
    try:
        events = client.get_bond_coupons(instrument_id, frm, to) or []
    except Exception:  # noqa: BLE001
        events = []
    out["coupon_events"] = [e for e in events if isinstance(e, dict)]

    # ближайший будущий купон
    future = []
    for ev in out["coupon_events"]:
        dt = _parse_iso(ev.get("couponDate"))
        if dt and dt >= now:
            future.append((dt, ev))
    future.sort(key=lambda x: x[0])
    if future:
        nxt = future[0][1]
        out["next_coupon_date"] = _fmt_date(nxt.get("couponDate"))
        out["next_coupon_value"] = quotation_to_decimal(nxt.get("payOneBond"))
    out["coupon_count"] = len(future)

    # последняя цена (read-only)
    try:
        last = client.get_last_price(instrument_id)
        if last and last.get("price"):
            out["price"] = quotation_to_decimal(last.get("price"))
    except Exception:  # noqa: BLE001
        pass
    return out


# ─── построение строки кандидата (pure + опц. API) ────────────────────────────

def build_candidate_row(candidate: dict, *, client=None,
                        builder_entry: dict | None = None) -> dict:
    """Строит одну строку coupon-validation для кандидата группы C.

    client=None → offline-режим (только по отчётам). client задан → read-only
    API-обогащение. auto_enable_allowed всегда False.
    """
    ticker = str(candidate.get("ticker") or "")
    class_code = str(candidate.get("class_code") or "")
    role = str(candidate.get("role") or "")
    policy_bucket = str(candidate.get("policy_bucket") or "")
    excluded_reason = str(candidate.get("excluded_reason") or "")
    notes = str(candidate.get("notes")
                or (builder_entry or {}).get("notes") or "")

    api = _enrich_from_api(candidate, client) if client is not None else {}

    figi = api.get("figi") or str(candidate.get("figi") or "") or None
    uid = api.get("uid") or str(candidate.get("uid") or "") or None
    isin = api.get("isin") or str(candidate.get("isin") or "") or None
    name = api.get("name") or str(candidate.get("name") or "") or None
    nominal = api.get("nominal")
    price = api.get("price")
    coupon_events = api.get("coupon_events") or []
    coupon_freq = api.get("coupon_freq_per_year")
    next_coupon_date = api.get("next_coupon_date")
    next_coupon_value = api.get("next_coupon_value")
    coupon_count = int(api.get("coupon_count") or 0)
    schedule_available = bool(coupon_events)

    unresolved = _is_unresolved(candidate)
    coupon_type = classify_coupon_type(
        role=role, ticker=ticker, notes=notes,
        coupon_events=coupon_events, instrument=api.get("instrument"),
    )

    allowed, block_reason, est_yield = annualization_guard(
        coupon_type=coupon_type,
        next_coupon_value=next_coupon_value,
        nominal=nominal,
        price=price,
        coupon_freq_per_year=coupon_freq,
        schedule_available=schedule_available,
    )

    status = _coupon_status(
        unresolved=unresolved, role=role, coupon_type=coupon_type,
        schedule_available=schedule_available,
    )
    readiness = _income_readiness(
        status=status, coupon_type=coupon_type, annualization_allowed=allowed,
    )

    return {
        "ticker": ticker,
        "class_code": class_code,
        "figi": figi,
        "uid": uid,
        "isin": isin,
        "name": name,
        "role": role,
        "policy_bucket": policy_bucket,
        "excluded_reason": excluded_reason,
        "notes": notes,
        "audit_group": "C",
        "coupon_validation_status": status,
        "income_readiness": readiness,
        "coupon_type": coupon_type,
        "coupon_count": coupon_count,
        "next_coupon_date": next_coupon_date,
        "next_coupon_value": next_coupon_value,
        "nominal": nominal,
        "price": price,
        "annualization_allowed": allowed,
        "annualization_block_reason": block_reason,
        "estimated_gross_yield_pct": est_yield,
        "auto_enable_allowed": False,
        "recommendation_guard": RECOMMENDATION_GUARD,
        "required_next_step": _required_next_step(status, coupon_type),
    }


def _coupon_status(*, unresolved: bool, role: str, coupon_type: str,
                   schedule_available: bool) -> str:
    if unresolved:
        return STATUS_UNRESOLVED
    # money_market / dividend, попавшие в группу C по notes, — не купонные
    if role in (ROLE_MONEY_MARKET, ROLE_DIVIDEND):
        return STATUS_INSUFFICIENT
    if schedule_available:
        if coupon_type == COUPON_FLOATING:
            return STATUS_FLOATING
        if coupon_type == COUPON_FIXED:
            return STATUS_FIXED
        return STATUS_SCHEDULE_AVAILABLE
    # нет календаря (offline или API без событий)
    if coupon_type == COUPON_FLOATING:
        return STATUS_FLOATING
    if coupon_type == COUPON_FIXED:
        return STATUS_DATA_MISSING
    return STATUS_INSUFFICIENT


def _income_readiness(*, status: str, coupon_type: str,
                      annualization_allowed: bool) -> str:
    if status == STATUS_UNRESOLVED:
        return READY_NEEDS_MANUAL_REVIEW
    if coupon_type == COUPON_FLOATING:
        return READY_NEEDS_FLOATING_POLICY
    if status in (STATUS_DATA_MISSING, STATUS_INSUFFICIENT):
        return READY_DATA_MISSING
    if status in (STATUS_FIXED, STATUS_SCHEDULE_AVAILABLE):
        if annualization_allowed:
            return READY_FUTURE_POLICY_REVIEW
        return READY_NEEDS_ANNUALIZATION_GUARD
    return READY_NOT_READY


def _required_next_step(status: str, coupon_type: str) -> str:
    if status == STATUS_UNRESOLVED:
        return ("Verified secid/ISIN/ticker/class_code mapping (resolver/mapping), "
                "затем повторить coupon validation. Auto-enable нельзя.")
    if coupon_type == COUPON_FLOATING:
        return ("Отдельная floating-coupon policy + annualization guard: не "
                "annualize-ить последний купон. Auto-enable нельзя.")
    if status == STATUS_DATA_MISSING:
        return ("Получить купонный календарь (read-only API) и проверить "
                "frequency/nominal/price. Auto-enable нельзя.")
    if status == STATUS_INSUFFICIENT:
        return ("Manual review: инструмент не похож на купонный или данных "
                "недостаточно. Auto-enable нельзя.")
    if status in (STATUS_FIXED, STATUS_SCHEDULE_AVAILABLE):
        return ("Отдельный future policy review (annualization + tax/liquidity). "
                "Auto-enable нельзя.")
    return "Auto-enable нельзя; требуется дополнительная валидация."


# ─── сборка отчёта ────────────────────────────────────────────────────────────

def build_report(group_c: list[dict], *, client=None,
                 builder_report: dict | None = None) -> dict:
    """Полный coupon-validation отчёт по кандидатам группы C."""
    index = _builder_index(builder_report or {})
    rows: list[dict] = []
    for cand in group_c:
        key = (str(cand.get("ticker") or "").upper(),
               str(cand.get("class_code") or "").upper())
        rows.append(build_candidate_row(
            cand, client=client, builder_entry=index.get(key)))

    by_status: dict[str, int] = {}
    by_readiness: dict[str, int] = {}
    for r in rows:
        by_status[r["coupon_validation_status"]] = \
            by_status.get(r["coupon_validation_status"], 0) + 1
        by_readiness[r["income_readiness"]] = \
            by_readiness.get(r["income_readiness"], 0) + 1

    floating = sum(1 for r in rows if r["coupon_type"] == COUPON_FLOATING)
    fixed = sum(1 for r in rows if r["coupon_type"] == COUPON_FIXED)
    missing = sum(1 for r in rows if r["coupon_validation_status"]
                  in (STATUS_DATA_MISSING, STATUS_INSUFFICIENT, STATUS_UNRESOLVED))

    summary = {
        "total_candidates": len(rows),
        "by_status": by_status,
        "by_readiness": by_readiness,
        "auto_enable_allowed_count": sum(1 for r in rows if r["auto_enable_allowed"]),
        "annualization_allowed_count": sum(1 for r in rows if r["annualization_allowed"]),
        "floating_coupon_count": floating,
        "fixed_coupon_count": fixed,
        "missing_data_count": missing,
        "recommended_next_pr": RECOMMENDED_NEXT_PR,
    }
    return {
        "kind": "income_coupon_validation",
        "read_only": True,
        "mode": "api" if client is not None else "offline",
        "summary": summary,
        "candidates": rows,
    }


# ─── markdown (pure) ──────────────────────────────────────────────────────────

def _md_cell(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _row_line(r: dict) -> str:
    return (
        f"- {_md_cell(r['ticker'])} ({_md_cell(r['class_code'])}, "
        f"role={_md_cell(r['role'])}, bucket={_md_cell(r['policy_bucket'])}) — "
        f"status={_md_cell(r['coupon_validation_status'])}, "
        f"readiness={_md_cell(r['income_readiness'])}, "
        f"coupon_type={_md_cell(r['coupon_type'])}, "
        f"annualization_allowed={_md_cell(r['annualization_allowed'])}; "
        f"{_md_cell(r['required_next_step'])} auto_enable_allowed=false"
    )


def render_md(report: dict) -> str:
    s = report["summary"]
    rows = report["candidates"]
    floating = [r for r in rows if r["coupon_type"] == COUPON_FLOATING]
    fixed = [r for r in rows if r["coupon_type"] == COUPON_FIXED]
    missing = [r for r in rows if r["coupon_validation_status"]
               in (STATUS_DATA_MISSING, STATUS_INSUFFICIENT, STATUS_UNRESOLVED)]
    future = [r for r in rows
              if r["income_readiness"] == READY_FUTURE_POLICY_REVIEW]

    lines = [
        "# Income coupon validation — read-only",
        "",
        "Аналитика, не рекомендация. Заявки не отправляются.",
        "Ни один инструмент не включается автоматически.",
        "auto_enable_allowed=false для всех кандидатов.",
        "",
        "## Summary",
        "",
        f"- total candidates: {s['total_candidates']}",
        f"- floating coupon: {s['floating_coupon_count']}",
        f"- fixed coupon: {s['fixed_coupon_count']}",
        f"- missing/insufficient data: {s['missing_data_count']}",
        f"- annualization allowed: {s['annualization_allowed_count']}",
        f"- auto_enable_allowed: {s['auto_enable_allowed_count']}",
        "",
        "by_status:",
    ]
    for k, v in sorted(s["by_status"].items()):
        lines.append(f"- {k}: {v}")
    lines += ["", "by_readiness:"]
    for k, v in sorted(s["by_readiness"].items()):
        lines.append(f"- {k}: {v}")
    lines += ["", f"Recommended next PR: {s['recommended_next_pr']}", ""]

    def _section(title: str, group: list[dict], empty: str) -> list[str]:
        out = [f"## {title}", ""]
        if not group:
            out += [empty, ""]
            return out
        out += [_row_line(r) for r in group]
        out.append("")
        return out

    lines += _section(
        "Floating coupon / OFZ-PK candidates", floating,
        "_(нет floating-кандидатов)_")
    lines += _section(
        "Fixed coupon candidates", fixed,
        "_(нет fixed-кандидатов с купонным календарём)_")
    lines += _section(
        "Missing data / unresolved", missing,
        "_(нет кандидатов с недостающими данными)_")
    lines += _section(
        "Next implementation candidates", future,
        "_(пока нет кандидатов, готовых к отдельному policy review)_")

    lines += [
        "## Safety contract",
        "",
        "- read-only T-Invest API, локальные отчёты, диагностика;",
        "- заявки не отправляются, исполнения нет, live нет, full-access токена нет;",
        "- floating / неполные данные не annualize-ятся (annualization guard);",
        "- auto_enable_allowed=false для всех кандидатов;",
        "- не меняет income policy, target portfolio, resolver, builder enable logic;",
        "- это аналитика, не инвестиционная рекомендация.",
        "",
        "_Generated by income-coupon-validation; read-only; не включает кандидатов._",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация / оркестрация ───────────────────────────────────────────────

def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Не сериализуется: {type(obj)}")


def run(*, builder_report_path: str | None = None,
        audit_report_path: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None,
        offline: bool = True,
        client=None) -> dict:
    """Читает builder+audit отчёты, строит coupon-validation, пишет json+md.

    offline=True или client=None → без сети. offline=False + client → read-only
    API-обогащение. Возвращает отчёт-словарь (+ пути в _output_json/_output_md).
    """
    builder_report = load_builder_report(builder_report_path)
    audit_report = load_audit_report(audit_report_path)
    group_c = select_group_c(audit_report)

    active_client = None if offline else client
    report = build_report(group_c, client=active_client, builder_report=builder_report)

    out_json = Path(output_json or DEFAULT_OUTPUT_JSON)
    out_md = Path(output_md or DEFAULT_OUTPUT_MD)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8")
    out_md.write_text(render_md(report), encoding="utf-8")
    report["_output_json"] = str(out_json)
    report["_output_md"] = str(out_md)
    return report
