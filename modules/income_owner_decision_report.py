"""
income_owner_decision_report — read-only owner-only decision support (ROADMAP F1).

Что делает:
- читает ТОЛЬКО локальные read-only отчёты (без сети, без API, без config):
  - income_universe_builder_report.json (enabled/disabled кандидаты, роль, bucket);
  - income_universe_disabled_audit.json (группы A/B/C/D/E и причины disable);
  - income_coupon_validation.json (fixed/floating/unknown купон);
  - income_floating_coupon_policy.json (блокировки для ОФЗ-ПК);
  - income_resolver_mapping_diagnostics.json (mapping-статус group D);
  - target_portfolio.json (опционально: аллокация/недовес/доходность);
- объединяет их в единый owner-only decision report;
- для каждого кандидата считает прозрачный deterministic score (без ML) и
  proposed_action ∈ {BUY_CANDIDATE, WAIT, BLOCKED, NEEDS_MAPPING, NEEDS_POLICY,
  NEEDS_DATA};
- пишет json + md в data/reports/.

Чего НЕ делает (жёсткий контракт F1):
- НЕ отправляет/не отменяет/не превью-сит заявки; нет orders-service вызовов,
  нет order-send адаптера; нет full-access токена; нет live-исполнения;
- НЕ мутирует портфель и НЕ мутирует config; не пишет в data/config;
- НЕ скрейпит; не даёт публичных инвестиционных рекомендаций.

Для КАЖДОГО кандидата жёстко: execution_requires_manual_confirmation=true,
order_preview_required=true, order_send_allowed=false, auto_execution_allowed=false.
proposed_action — это owner-only proposed action (candidate for owner review),
а не приказ на покупку/продажу и не order. BUY_CANDIDATE используется как enum из
ROADMAP F1, не как инвестиционная рекомендация. Перед любым будущим исполнением
обязателен этап F2 (order preview / no-send) и ручное подтверждение.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# ─── входные отчёты ───────────────────────────────────────────────────────────

DEFAULT_UNIVERSE_REPORT = "data/reports/income_universe_builder_report.json"
DEFAULT_AUDIT_JSON = "data/reports/income_universe_disabled_audit.json"
DEFAULT_COUPON_JSON = "data/reports/income_coupon_validation.json"
DEFAULT_FLOATING_JSON = "data/reports/income_floating_coupon_policy.json"
DEFAULT_RESOLVER_JSON = "data/reports/income_resolver_mapping_diagnostics.json"
DEFAULT_TARGET_JSON = "data/reports/target_portfolio.json"

DEFAULT_OUTPUT_JSON = "data/reports/income_owner_decision_report.json"
DEFAULT_OUTPUT_MD = "data/reports/income_owner_decision_report.md"

DEFAULT_MAX_CANDIDATES = 30

# ─── enum proposed_action (owner-only, не приказ на сделку) ────────────────────

ACTION_BUY_CANDIDATE = "BUY_CANDIDATE"
ACTION_WAIT = "WAIT"
ACTION_BLOCKED = "BLOCKED"
ACTION_NEEDS_MAPPING = "NEEDS_MAPPING"
ACTION_NEEDS_POLICY = "NEEDS_POLICY"
ACTION_NEEDS_DATA = "NEEDS_DATA"

ALL_ACTIONS = (
    ACTION_BUY_CANDIDATE, ACTION_WAIT, ACTION_BLOCKED,
    ACTION_NEEDS_MAPPING, ACTION_NEEDS_POLICY, ACTION_NEEDS_DATA,
)

RECOMMENDATION_GUARD = "owner_decision_support_only"
NEXT_STAGE = "F2 order preview / no-send"

# score: порог BUY_CANDIDATE (иначе resolved & income-ready кандидат → WAIT)
BUY_SCORE_THRESHOLD = 50

# excluded_reason / policy bucket, считающиеся hard blocker (BLOCKED)
HARD_BLOCK_REASONS = {
    "override_disable",
    "trailing_yield_above_cap",
    "policy_excluded",
    "not_allowed_by_policy",
    "state_control_risk",
}
EXCLUDED_BUCKETS = {"income_excluded"}
UNKNOWN_BUCKETS = {"income_unknown"}
# bucket, требующий отдельного policy review (NEEDS_POLICY)
POLICY_REVIEW_BUCKETS = {"income_estimated", "income_manual"}
CONSERVATIVE_BUCKETS = {"income_reliable", "income_variable"}

# роли (совпадают с income_universe_builder / audit)
ROLE_DIVIDEND = "dividend_candidate"
ROLE_BOND = "bond_candidate"
ROLE_OFZ = "ofz_pk_candidate"
ROLE_MONEY_MARKET = "money_market"

# resolver mapping_status, считающиеся «не разрешено» (NEEDS_MAPPING)
RESOLVER_UNRESOLVED = {"unresolved", "no_matches", "ambiguous_matches"}

# floating coupon policy_status, считающийся «нужна policy»
FLOATING_POLICY_REQUIRED = "needs_floating_coupon_policy"

# coupon_validation_status «нет данных»
COUPON_DATA_MISSING = {"coupon_data_missing", "insufficient_data"}
COUPON_UNRESOLVED = "unresolved_instrument"
COUPON_FLOATING = "floating_coupon_detected"
COUPON_FIXED = "fixed_coupon_detected"
COUPON_SCHEDULE = "coupon_schedule_available"

HARD_RISK_FLAGS = {"state_control_risk"}


class OwnerDecisionError(Exception):
    """Понятная ошибка (например, нет ни одного входного отчёта)."""


# ─── чтение локальных отчётов (read-only, graceful) ───────────────────────────

def _load_optional_json(path: str | None) -> tuple[dict | None, str | None]:
    """Грузит локальный JSON-отчёт. Возвращает (data | None, error | None).

    Отсутствующий/битый файл НЕ роняет команду: возвращается (None, reason),
    а вызывающий код добавляет путь в missing_inputs и деградирует безопасно.
    """
    if not path:
        return None, "path_not_set"
    p = Path(path)
    if not p.exists():
        return None, "missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None, "unreadable"
    if not isinstance(data, dict):
        return None, "unexpected_format"
    return data, None


def _candidates(report: dict | None, key: str = "candidates") -> list[dict]:
    if not isinstance(report, dict):
        return []
    rows = report.get(key)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _key(ticker: str) -> str:
    return str(ticker or "").strip().upper()


# ─── индексы по входным отчётам (pure) ────────────────────────────────────────

def _builder_entries(builder: dict | None) -> list[dict]:
    """Все entries builder (enabled + disabled) как единая вселенная кандидатов."""
    if not isinstance(builder, dict):
        return []
    entries = builder.get("entries")
    if isinstance(entries, list) and entries:
        return [e for e in entries if isinstance(e, dict)]
    out: list[dict] = []
    for k in ("enabled_entries", "disabled_entries"):
        rows = builder.get(k)
        if isinstance(rows, list):
            for e in rows:
                if isinstance(e, dict):
                    e = dict(e)
                    e.setdefault("enabled", k == "enabled_entries")
                    out.append(e)
    return out


def _index_by_ticker(rows: list[dict], ticker_field: str = "ticker") -> dict[str, dict]:
    index: dict[str, dict] = {}
    for r in rows:
        index.setdefault(_key(r.get(ticker_field)), r)
    return index


def _target_indexes(target: dict | None) -> dict[str, dict]:
    """Парсит target_portfolio.json (опционально). Неизвестная структура → {}.

    Возвращает индекс по тикеру с полями: conservative_yield, target_weight_pct,
    target_capital_rub, net_yield_pct, underweight (bool), diff_value_rub,
    action_hint, excluded_reason, eligible.
    """
    out: dict[str, dict] = {}
    if not isinstance(target, dict):
        return out

    def _slot(t: str) -> dict:
        return out.setdefault(_key(t), {})

    for c in _candidates(target, "eligible_universe"):
        slot = _slot(c.get("ticker"))
        slot["eligible"] = True
        slot["policy_bucket"] = c.get("policy_bucket")
        slot["conservative_yield"] = (
            c.get("conservative_net_yield_pct")
            or c.get("conservative_yield_pct"))
    for c in _candidates(target, "excluded_universe"):
        slot = _slot(c.get("ticker"))
        slot.setdefault("eligible", False)
        slot["excluded_reason"] = c.get("excluded_reason")
    for a in _candidates(target, "target_allocation"):
        slot = _slot(a.get("ticker"))
        slot["target_weight_pct"] = a.get("target_weight_pct")
        slot["target_capital_rub"] = a.get("target_capital_rub")
        slot["net_yield_pct"] = a.get("net_yield_pct")
    for r in _candidates(target, "current_vs_target"):
        slot = _slot(r.get("ticker"))
        diff = r.get("diff_value_rub")
        slot["diff_value_rub"] = diff
        slot["action_hint"] = r.get("action_hint")
        try:
            slot["underweight"] = float(diff) > 0 if diff is not None else False
        except (TypeError, ValueError):
            slot["underweight"] = False
    return out


# ─── скоринг (прозрачный, deterministic, без ML) ──────────────────────────────

def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_score(state: dict) -> tuple[int, dict]:
    """Прозрачный deterministic score 0..100 + score_components.

    Каждая компонента — целочисленный вклад; сумма клампится в [0, 100].
    Никакого ML; только наблюдаемые поля входных отчётов.
    """
    comp: dict[str, int] = {}
    if state.get("income_data_present"):
        comp["income_data_present"] = 20
    if state.get("policy_bucket") == "income_reliable":
        comp["conservative_income_bucket"] = 15
    elif state.get("policy_bucket") == "income_variable":
        comp["conservative_income_bucket"] = 8
    if state.get("resolved"):
        comp["resolved_identity"] = 15
    if state.get("known_future_income"):
        comp["fixed_or_known_income"] = 10
    if state.get("target_underweight"):
        comp["target_underweight_context"] = 10
    if state.get("missing_income_data"):
        comp["missing_data_penalty"] = -15
    if state.get("floating_policy_required"):
        comp["floating_policy_penalty"] = -20
    if state.get("unresolved_mapping"):
        comp["unresolved_mapping_penalty"] = -25
    if state.get("excluded_or_unknown_policy"):
        comp["excluded_unknown_policy_penalty"] = -20
    if state.get("risk_flag_count"):
        comp["risk_penalty"] = -10 * int(state["risk_flag_count"])

    score = max(0, min(100, sum(comp.values())))
    return score, comp


# ─── proposed_action (deterministic) ──────────────────────────────────────────

def decide_action(state: dict, score: int) -> tuple[str, str]:
    """Возвращает (proposed_action, proposed_action_reason).

    Приоритет (от наиболее блокирующего): NEEDS_MAPPING → BLOCKED →
    NEEDS_POLICY → NEEDS_DATA → BUY_CANDIDATE/WAIT (по score). Это owner-only
    proposed action для ручного review, не приказ на сделку.
    """
    # NEEDS_MAPPING: не разрешён инструмент (group D / resolver / нет class_code)
    if state.get("unresolved_mapping"):
        return (ACTION_NEEDS_MAPPING,
                "Инструмент не разрешён (resolver/mapping): нет проверенного "
                "secid/ISIN/ticker/class_code. Нужен ручной mapping review.")

    # BLOCKED: явный hard blocker (policy/cap/override/excluded/hard risk)
    if state.get("hard_blocked"):
        return (ACTION_BLOCKED,
                f"Hard blocker: {state.get('block_reason') or 'excluded/keep_disabled'}. "
                "Оставить disabled; менять только отдельным review.")

    # NEEDS_POLICY: floating coupon / unknown coupon / manual/estimated policy
    if state.get("floating_policy_required"):
        return (ACTION_NEEDS_POLICY,
                "Floating coupon (ОФЗ-ПК): нет утверждённой floating-coupon policy; "
                "доход нельзя annualize-ить как факт.")
    if state.get("needs_policy"):
        return (ACTION_NEEDS_POLICY,
                f"Требуется policy review: {state.get('policy_reason') or 'income policy'}.")

    # NEEDS_DATA: не хватает данных для решения
    if state.get("needs_data"):
        return (ACTION_NEEDS_DATA,
                f"Не хватает данных для решения: {state.get('data_reason') or 'missing inputs'}.")

    # resolved & income-ready → BUY_CANDIDATE (по score) или WAIT
    if state.get("hard_risk"):
        return (ACTION_WAIT,
                "Есть risk flag: оставить на ручной review владельца, не BUY_CANDIDATE.")
    if score >= BUY_SCORE_THRESHOLD:
        return (ACTION_BUY_CANDIDATE,
                f"Resolved income-ready кандидат, score={score} ≥ {BUY_SCORE_THRESHOLD}. "
                "Owner-only candidate for review; требуется F2 order preview и ручное "
                "подтверждение перед любым исполнением.")
    return (ACTION_WAIT,
            f"Потенциально интересен, но score={score} < {BUY_SCORE_THRESHOLD}: "
            "не хватает уверенности/доходности/данных. Нужен дополнительный review.")


def _next_step(action: str) -> str:
    return {
        ACTION_BUY_CANDIDATE: (
            "F2 order preview (no-send) для owner review; ручное подтверждение "
            "обязательно перед любым исполнением."),
        ACTION_WAIT: (
            "Собрать больше данных/доходности, поднять score; повторить decision report."),
        ACTION_NEEDS_MAPPING: (
            "Ручной resolver/mapping review (income-resolver-mapping-diagnostics)."),
        ACTION_NEEDS_POLICY: (
            "Policy review (floating-coupon / income policy) отдельным шагом."),
        ACTION_NEEDS_DATA: (
            "Запустить недостающие read-only отчёты / получить income-данные."),
        ACTION_BLOCKED: (
            "Оставить disabled; cap/override/policy менять только отдельным review."),
    }.get(action, "Manual review владельца.")


# ─── сборка merged-состояния и строки кандидата ───────────────────────────────

def _merge_state(ticker: str, *, builder: dict, audit: dict, coupon: dict,
                 floating: dict, resolver: dict, target: dict,
                 missing_inputs: list[str]) -> dict:
    """Собирает наблюдаемое состояние кандидата по всем источникам (pure)."""
    be = builder.get(_key(ticker)) or {}
    au = audit.get(_key(ticker)) or {}
    cv = coupon.get(_key(ticker)) or {}
    fl = floating.get(_key(ticker)) or {}
    rs = resolver.get(_key(ticker)) or {}
    tg = target.get(_key(ticker)) or {}

    class_code = (be.get("class_code") or au.get("class_code")
                  or cv.get("class_code") or "")
    role = (be.get("role") or au.get("role") or cv.get("role")
            or fl.get("role") or rs.get("role") or "")
    policy_bucket = (be.get("policy_bucket") or au.get("policy_bucket")
                     or cv.get("policy_bucket") or tg.get("policy_bucket") or "")
    excluded_reason = (be.get("excluded_reason") or au.get("excluded_reason")
                       or tg.get("excluded_reason") or "")
    enabled = bool(be.get("enabled")) if "enabled" in be else None

    audit_group = str(au.get("audit_group") or "").strip().upper() or None
    coupon_status = str(cv.get("coupon_validation_status") or "").strip()
    coupon_type = str(cv.get("coupon_type") or "").strip()
    resolver_status = str(rs.get("mapping_status") or "").strip()
    floating_policy_status = str(fl.get("policy_status") or "").strip()

    conservative_yield = (tg.get("conservative_yield")
                          if tg.get("conservative_yield") is not None else None)
    net_yield = tg.get("net_yield_pct")
    estimated_yield = cv.get("estimated_gross_yield_pct")

    # ── derived booleans ──
    unresolved = (
        not class_code
        or excluded_reason == "unresolved"
        or audit_group == "D"
        or resolver_status in RESOLVER_UNRESOLVED
        or coupon_status == COUPON_UNRESOLVED
    )
    resolved = not unresolved

    is_coupon_role = role in (ROLE_BOND, ROLE_OFZ) or "bond" in role.lower()
    floating_required = (
        floating_policy_status == FLOATING_POLICY_REQUIRED
        or coupon_type == "floating"
        or coupon_status == COUPON_FLOATING
    )

    # hard blocker: явный keep_disabled / excluded policy / hard risk reason
    block_reason = ""
    hard_blocked = False
    if excluded_reason in HARD_BLOCK_REASONS:
        hard_blocked, block_reason = True, excluded_reason
    elif policy_bucket in EXCLUDED_BUCKETS:
        hard_blocked, block_reason = True, "policy_excluded_bucket"
    elif policy_bucket in UNKNOWN_BUCKETS:
        hard_blocked, block_reason = True, "income_unknown_bucket"
    elif audit_group == "E":
        hard_blocked, block_reason = True, "keep_disabled"

    # needs policy: estimated/manual bucket, group A/B, или валидируемый купон
    needs_policy = False
    policy_reason = ""
    if not hard_blocked and resolved:
        if policy_bucket in POLICY_REVIEW_BUCKETS or audit_group in ("A", "B"):
            needs_policy, policy_reason = True, "manual/estimated income policy"
        elif audit_group == "C" and coupon_status in (COUPON_FIXED, COUPON_SCHEDULE):
            needs_policy, policy_reason = True, "coupon future-policy review"
        elif coupon_type == "unknown" and is_coupon_role:
            needs_policy, policy_reason = True, "coupon type unknown"

    # needs data: coupon-капабельный, но нет купонных данных
    needs_data = False
    data_reason = ""
    missing_income_data = False
    if not hard_blocked and resolved and not needs_policy and not floating_required:
        if audit_group == "C" and coupon_status in COUPON_DATA_MISSING:
            needs_data, data_reason = True, "нет купонного календаря/частоты/цены"
        # enabled, но вообще нет income-метрик
    income_data_present = bool(
        conservative_yield is not None or net_yield is not None
        or estimated_yield is not None
        or (be.get("source") and str(be.get("source")).lower() not in ("", "unknown"))
        or policy_bucket in CONSERVATIVE_BUCKETS
        or policy_bucket in POLICY_REVIEW_BUCKETS
    )
    if not income_data_present:
        missing_income_data = True
        if not (needs_data or needs_policy or floating_required):
            needs_data = needs_data or resolved
            data_reason = data_reason or "нет income/yield данных"

    # risk flags
    risk_flags: list[str] = []
    if excluded_reason == "state_control_risk":
        risk_flags.append("state_control_risk")
    hard_risk = any(f in HARD_RISK_FLAGS for f in risk_flags)

    # missing_data (per-candidate список недостающих полей)
    missing_data: list[str] = []
    if not class_code:
        missing_data.append("class_code")
    if conservative_yield is None and net_yield is None and estimated_yield is None:
        missing_data.append("yield")
    if DEFAULT_TARGET_JSON in missing_inputs or not target:
        missing_data.append("target_context")

    known_future_income = (
        coupon_type == "fixed" or coupon_status in (COUPON_FIXED, COUPON_SCHEDULE)
        or role in (ROLE_DIVIDEND, ROLE_MONEY_MARKET)
    )

    excluded_or_unknown = (
        policy_bucket in EXCLUDED_BUCKETS or policy_bucket in UNKNOWN_BUCKETS
        or excluded_reason in HARD_BLOCK_REASONS or audit_group == "E"
    )

    return {
        "ticker": str(ticker),
        "class_code": class_code,
        "figi": cv.get("figi") or be.get("figi") or "",
        "uid": cv.get("uid") or "",
        "isin": cv.get("isin") or "",
        "name": cv.get("name") or fl.get("name") or be.get("name") or "",
        "role": role,
        "source_role": role,
        "asset_type": role,
        "instrument_type": cv.get("coupon_type") and "bond" or (be.get("instrument_type") or ""),
        "current_enabled": enabled,
        "policy_bucket": policy_bucket,
        "audit_group": audit_group,
        "audit_reason": au.get("why_disabled") or excluded_reason or "",
        "excluded_reason": excluded_reason,
        "coupon_status": coupon_status or None,
        "coupon_type": coupon_type or None,
        "floating_policy_status": floating_policy_status or None,
        "resolver_mapping_status": resolver_status or None,
        "conservative_yield": conservative_yield,
        "net_yield_pct": net_yield,
        "estimated_yield": estimated_yield,
        "target_weight_pct": tg.get("target_weight_pct"),
        "target_underweight": bool(tg.get("underweight")),
        "risk_flags": risk_flags,
        "missing_data": missing_data,
        # derived booleans для score/decision
        "resolved": resolved,
        "unresolved_mapping": unresolved,
        "hard_blocked": hard_blocked,
        "block_reason": block_reason,
        "floating_policy_required": floating_required,
        "needs_policy": needs_policy,
        "policy_reason": policy_reason,
        "needs_data": needs_data,
        "data_reason": data_reason,
        "income_data_present": income_data_present,
        "missing_income_data": missing_income_data,
        "known_future_income": known_future_income,
        "excluded_or_unknown_policy": excluded_or_unknown,
        "risk_flag_count": len(risk_flags),
        "hard_risk": hard_risk,
    }


def build_candidate_row(state: dict) -> dict:
    """Строит одну owner-only decision-строку. Guard-флаги жёстко зафиксированы."""
    score, components = compute_score(state)
    action, reason = decide_action(state, score)
    return {
        "ticker": state["ticker"],
        "figi": state["figi"] or None,
        "uid": state["uid"] or None,
        "isin": state["isin"] or None,
        "class_code": state["class_code"] or None,
        "name": state["name"] or None,
        "asset_type": state["asset_type"] or None,
        "instrument_type": state["instrument_type"] or None,
        "source_role": state["source_role"] or None,
        "current_enabled": state["current_enabled"],
        "policy_bucket": state["policy_bucket"] or None,
        "audit_group": state["audit_group"],
        "audit_reason": state["audit_reason"] or None,
        "coupon_status": state["coupon_status"],
        "floating_policy_status": state["floating_policy_status"],
        "resolver_mapping_status": state["resolver_mapping_status"],
        "income_readiness": _income_readiness(state),
        "estimated_yield": state["estimated_yield"],
        "conservative_yield": state["conservative_yield"],
        "net_yield_pct": state["net_yield_pct"],
        "target_weight_pct": state["target_weight_pct"],
        "risk_flags": list(state["risk_flags"]),
        "missing_data": list(state["missing_data"]),
        "score": score,
        "score_components": components,
        "proposed_action": action,
        "proposed_action_reason": reason,
        "next_required_step": _next_step(action),
        # ── жёсткие guard-флаги F1 (одинаковы для каждой строки) ──
        "execution_requires_manual_confirmation": True,
        "order_preview_required": True,
        "order_send_allowed": False,
        "auto_execution_allowed": False,
    }


def _income_readiness(state: dict) -> str:
    if state["unresolved_mapping"]:
        return "not_resolved"
    if state["hard_blocked"]:
        return "blocked"
    if state["floating_policy_required"] or state["needs_policy"]:
        return "needs_policy"
    if state["needs_data"]:
        return "needs_data"
    if state["income_data_present"]:
        return "income_ready"
    return "incomplete"


# ─── сборка отчёта ────────────────────────────────────────────────────────────

def _collect_universe(builder_entries: list[dict], audit: dict, coupon: dict,
                      floating: dict, resolver: dict, target: dict) -> list[str]:
    """Собирает упорядоченную (стабильную) вселенную тикеров из всех источников."""
    order: list[str] = []
    seen: set[str] = set()

    def _add(t):
        k = _key(t)
        if k and k not in seen:
            seen.add(k)
            order.append(str(t).strip())

    for e in builder_entries:
        _add(e.get("ticker"))
    for src in (audit, coupon, floating, resolver, target):
        for k in src:
            if k not in seen:
                seen.add(k)
                order.append(k)
    return order


def build_report(*, builder: dict | None, audit: dict | None, coupon: dict | None,
                 floating: dict | None, resolver: dict | None,
                 target: dict | None, inputs: dict, missing_inputs: list[str],
                 mode: str, max_candidates: int = DEFAULT_MAX_CANDIDATES,
                 min_score: int | None = None) -> dict:
    """Строит полный owner-only decision report (pure)."""
    builder_entries = _builder_entries(builder)
    builder_idx = _index_by_ticker(builder_entries)
    audit_idx = _index_by_ticker(_candidates(audit))
    coupon_idx = _index_by_ticker(_candidates(coupon))
    floating_idx = _index_by_ticker(_candidates(floating))
    resolver_idx = _index_by_ticker(_candidates(resolver), ticker_field="original_ticker")
    target_idx = _target_indexes(target)

    universe = _collect_universe(
        builder_entries, audit_idx, coupon_idx, floating_idx, resolver_idx, target_idx)

    rows: list[dict] = []
    for ticker in universe:
        state = _merge_state(
            ticker, builder=builder_idx, audit=audit_idx, coupon=coupon_idx,
            floating=floating_idx, resolver=resolver_idx, target=target_idx,
            missing_inputs=missing_inputs)
        rows.append(build_candidate_row(state))

    # сортировка: BUY_CANDIDATE/WAIT по score вверх, затем стабильно по тикеру
    action_rank = {a: i for i, a in enumerate(ALL_ACTIONS)}
    rows.sort(key=lambda r: (action_rank.get(r["proposed_action"], 99),
                             -int(r["score"]), r["ticker"]))

    if min_score is not None:
        rows = [r for r in rows if int(r["score"]) >= int(min_score)]
    if max_candidates and max_candidates > 0:
        rows = rows[:max_candidates]

    summary = _build_summary(rows)
    return {
        "kind": "income_owner_decision_report",
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "recommendation_guard": RECOMMENDATION_GUARD,
        "inputs": inputs,
        "missing_inputs": missing_inputs,
        "summary": summary,
        "candidates": rows,
        "guards": {
            "owner_only": True,
            "order_send_allowed": False,
            "auto_execution_allowed": False,
            "execution_requires_manual_confirmation": True,
            "order_preview_required": True,
            "full_access_token_used": False,
            "portfolio_mutated": False,
            "config_mutated": False,
            "next_stage": NEXT_STAGE,
            "recommendation_guard": RECOMMENDATION_GUARD,
        },
    }


def _build_summary(rows: list[dict]) -> dict:
    total = len(rows)

    def _count(action: str) -> int:
        return sum(1 for r in rows if r["proposed_action"] == action)

    by_action: dict[str, int] = {}
    by_asset_type: dict[str, int] = {}
    by_policy_bucket: dict[str, int] = {}
    by_block_reason: dict[str, int] = {}
    for r in rows:
        by_action[r["proposed_action"]] = by_action.get(r["proposed_action"], 0) + 1
        at = r.get("asset_type") or "unknown"
        by_asset_type[at] = by_asset_type.get(at, 0) + 1
        pb = r.get("policy_bucket") or "unknown"
        by_policy_bucket[pb] = by_policy_bucket.get(pb, 0) + 1
        if r["proposed_action"] == ACTION_BLOCKED:
            br = r.get("audit_reason") or "blocked"
            by_block_reason[br] = by_block_reason.get(br, 0) + 1

    return {
        "total_candidates": total,
        "buy_candidate_count": _count(ACTION_BUY_CANDIDATE),
        "wait_count": _count(ACTION_WAIT),
        "blocked_count": _count(ACTION_BLOCKED),
        "needs_mapping_count": _count(ACTION_NEEDS_MAPPING),
        "needs_policy_count": _count(ACTION_NEEDS_POLICY),
        "needs_data_count": _count(ACTION_NEEDS_DATA),
        "order_send_allowed_count": 0,
        "auto_execution_allowed_count": 0,
        "execution_requires_manual_confirmation_count": total,
        "by_proposed_action": by_action,
        "by_asset_type": by_asset_type,
        "by_policy_bucket": by_policy_bucket,
        "by_block_reason": by_block_reason,
    }


# ─── markdown (pure) ──────────────────────────────────────────────────────────

def _md_cell(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _table(rows: list[dict]) -> list[str]:
    lines = [
        "| proposed_action | score | ticker | name | asset_type | "
        "policy/audit | reason | next_required_step |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        policy_audit = "/".join(
            x for x in (_md_cell(r.get("policy_bucket")),
                        ("group " + r["audit_group"]) if r.get("audit_group") else "")
            if x and x != "—")
        lines.append(
            f"| {_md_cell(r['proposed_action'])} | {_md_cell(r['score'])} | "
            f"{_md_cell(r['ticker'])} | {_md_cell(r.get('name'))} | "
            f"{_md_cell(r.get('asset_type'))} | {_md_cell(policy_audit)} | "
            f"{_md_cell(r['proposed_action_reason'])} | "
            f"{_md_cell(r['next_required_step'])} |")
    return lines


def render_md(report: dict) -> str:
    s = report["summary"]
    rows = report["candidates"]

    lines = [
        "# Owner income decision report — READ ONLY (F1)",
        "",
        "Owner-only decision support.",
        "Заявки не отправляются.",
        "order_send_allowed=false",
        "auto_execution_allowed=false",
        "execution_requires_manual_confirmation=true",
        "order_preview_required=true",
        "Следующий этап перед сделкой: order preview / no-send (F2).",
        "",
        "Это owner-only proposed action (candidate for owner review), не публичная "
        "инвестиционная рекомендация и не приказ на сделку. Перед любым исполнением "
        "обязательны F2 order preview и ручное подтверждение (requires manual "
        "confirmation).",
        "",
        f"Режим: {_md_cell(report.get('mode'))}. Сгенерировано: "
        f"{_md_cell(report.get('generated_at'))}.",
        "",
    ]

    if report.get("missing_inputs"):
        lines += [
            "## Missing inputs",
            "",
            "Не все входные отчёты найдены — отчёт деградирует безопасно "
            "(NEEDS_DATA там, где данных не хватает). Сначала выполните smoke chain:",
            "",
        ]
        lines += [f"- отсутствует: `{m}`" for m in report["missing_inputs"]]
        lines += [
            "",
            "```",
            "python main.py build-income-universe --force",
            "python main.py income-universe-audit",
            "python main.py income-coupon-validation",
            "python main.py income-floating-coupon-policy",
            "python main.py income-resolver-mapping-diagnostics",
            "python main.py income-owner-decision-report",
            "```",
            "",
        ]

    lines += [
        "## Summary",
        "",
        f"- total_candidates: {s['total_candidates']}",
        f"- BUY_CANDIDATE: {s['buy_candidate_count']}",
        f"- WAIT: {s['wait_count']}",
        f"- NEEDS_POLICY: {s['needs_policy_count']}",
        f"- NEEDS_MAPPING: {s['needs_mapping_count']}",
        f"- NEEDS_DATA: {s['needs_data_count']}",
        f"- BLOCKED: {s['blocked_count']}",
        f"- order_send_allowed_count: {s['order_send_allowed_count']}",
        f"- auto_execution_allowed_count: {s['auto_execution_allowed_count']}",
        f"- execution_requires_manual_confirmation_count: "
        f"{s['execution_requires_manual_confirmation_count']}",
        "",
        "by_proposed_action:",
    ]
    if s["by_proposed_action"]:
        for k in ALL_ACTIONS:
            if k in s["by_proposed_action"]:
                lines.append(f"- {k}: {s['by_proposed_action'][k]}")
    else:
        lines.append("- —")

    lines += ["", "## Все кандидаты (owner review)", ""]
    lines += _table(rows) if rows else ["_(нет кандидатов)_"]

    def _section(title: str, action: str, empty: str) -> list[str]:
        group = [r for r in rows if r["proposed_action"] == action]
        out = ["", f"## {title}", ""]
        out += _table(group) if group else [empty]
        return out

    lines += _section("BUY_CANDIDATE", ACTION_BUY_CANDIDATE,
                      "_(нет BUY_CANDIDATE)_")
    lines += _section("WAIT", ACTION_WAIT, "_(нет WAIT)_")
    lines += _section("NEEDS_POLICY", ACTION_NEEDS_POLICY, "_(нет NEEDS_POLICY)_")
    lines += _section("NEEDS_MAPPING", ACTION_NEEDS_MAPPING, "_(нет NEEDS_MAPPING)_")

    blocked_data = [r for r in rows
                    if r["proposed_action"] in (ACTION_BLOCKED, ACTION_NEEDS_DATA)]
    lines += ["", "## BLOCKED / NEEDS_DATA", ""]
    lines += _table(blocked_data) if blocked_data else ["_(нет BLOCKED/NEEDS_DATA)_"]

    lines += [
        "",
        "## Safety contract (F1)",
        "",
        "- read-only: только локальные отчёты; нет сети/API/order/execution/live;",
        "- No orders were sent.",
        "- No full-access token was used.",
        "- Manual confirmation is required before any future execution.",
        "- order_send_allowed=false, auto_execution_allowed=false для каждого кандидата;",
        "- proposed_action — owner-only proposed action (candidate for owner review), "
        "requires order preview, requires manual confirmation; not public advice;",
        "- не мутирует портфель и config; не пишет в data/config.",
        "",
        "_Generated by income-owner-decision-report; read-only; F1; перед сделкой — "
        "F2 order preview / no-send и ручное подтверждение._",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация / оркестрация ───────────────────────────────────────────────

def _json_default(obj):
    # Decimal'ы в input-отчётах уже сериализованы как числа; на всякий случай:
    try:
        return float(obj)
    except (TypeError, ValueError):
        return str(obj)


def run(*, universe_report: str | None = None, audit_json: str | None = None,
        coupon_json: str | None = None, floating_policy_json: str | None = None,
        resolver_json: str | None = None, target_json: str | None = None,
        output_json: str | None = None, output_md: str | None = None,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        min_score: int | None = None, offline: bool = False) -> dict:
    """Читает входные отчёты, строит owner-only decision report, пишет json+md.

    Отсутствующие входные отчёты не роняют команду: они попадают в missing_inputs
    и отчёт деградирует безопасно. Если НЕ найден ни один источник кандидатов —
    бросаем OwnerDecisionError с понятной подсказкой про smoke chain.
    """
    paths = {
        "universe_report": universe_report or DEFAULT_UNIVERSE_REPORT,
        "audit_json": audit_json or DEFAULT_AUDIT_JSON,
        "coupon_json": coupon_json or DEFAULT_COUPON_JSON,
        "floating_policy_json": floating_policy_json or DEFAULT_FLOATING_JSON,
        "resolver_json": resolver_json or DEFAULT_RESOLVER_JSON,
        "target_json": target_json or DEFAULT_TARGET_JSON,
    }
    loaded: dict[str, dict | None] = {}
    inputs: dict[str, dict] = {}
    missing_inputs: list[str] = []
    for name, path in paths.items():
        data, err = _load_optional_json(path)
        loaded[name] = data
        inputs[name] = {"path": path, "loaded": data is not None,
                        "status": "ok" if data is not None else (err or "missing")}
        if data is None:
            missing_inputs.append(path)

    # target_portfolio.json опционален — его отсутствие не считаем фатальным,
    # но фатально, если нет НИ ОДНОГО источника кандидатов.
    has_candidate_source = any(
        loaded[n] is not None
        for n in ("universe_report", "audit_json", "coupon_json",
                  "floating_policy_json", "resolver_json"))
    if not has_candidate_source:
        raise OwnerDecisionError(
            "Не найден ни один входной отчёт для owner decision report. "
            "Сначала выполните smoke chain:\n"
            "  python main.py build-income-universe --force\n"
            "  python main.py income-universe-audit\n"
            "  python main.py income-coupon-validation\n"
            "  python main.py income-floating-coupon-policy\n"
            "  python main.py income-resolver-mapping-diagnostics\n"
            "затем повторите income-owner-decision-report."
        )

    report = build_report(
        builder=loaded["universe_report"], audit=loaded["audit_json"],
        coupon=loaded["coupon_json"], floating=loaded["floating_policy_json"],
        resolver=loaded["resolver_json"], target=loaded["target_json"],
        inputs=inputs, missing_inputs=missing_inputs,
        mode="offline" if offline else "report_join",
        max_candidates=max_candidates, min_score=min_score)

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
