"""
target_portfolio_v1 — read-only планировщик целевого доходного портфеля.

Отвечает на вопрос «что и в каких долях держать/докупать, чтобы выйти на целевой
доход», опираясь на conservative income policy ([[income-policy]]). Использует ТОЛЬКО
read-only данные (income-summary по счёту + income-watchlist по вселенной) и не
отправляет заявок, не трогает портфель, не даёт инвестрекомендаций.

Никаких торговых заявок, order-сервисов, full-токена и live-исполнения. План —
это аналитика: вместо «купить/продать» используются нейтральные формулировки
(planned_add_rub, underweight_by_rub, action_hint).

Чистое ядро (eligibility/allocation/plan) тестируется без сети; данные приходят
через уже существующие read-only функции income_engine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

# policy-бакеты, допустимые в base target по умолчанию
BASE_BUCKETS = ("income_reliable", "income_variable")

# ─── low-yield диагностика (read-only) ──────────────────────────────────────────
# Слот считается «низкодоходным», если он занимает существенную долю капитала, но
# его консервативная доходность сильно ниже смешанной доходности портфеля. Это
# только аналитический флаг: веса распределения он НЕ меняет.
LOW_YIELD_MIN_CAPITAL_SHARE_PCT = Decimal("10")
LOW_YIELD_TO_BLENDED_RATIO = Decimal("0.30")


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _b(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y", "да")


def _net_pct(gross: Decimal | None, tax_pct: Decimal) -> Decimal | None:
    if gross is None:
        return None
    return gross * (Decimal("1") - tax_pct / Decimal("100"))


# ─── окружение ────────────────────────────────────────────────────────────────

@dataclass
class TargetEnv:
    target_monthly_rub: Decimal = Decimal("0")
    tax_rate_pct: Decimal = Decimal("13")
    max_position_pct: Decimal = Decimal("25")
    max_issuer_pct: Decimal = Decimal("30")
    max_money_market_pct: Decimal = Decimal("40")
    include_estimated: bool = False
    include_variable: bool = True
    exclude_unknown: bool = True
    min_policy_bucket: str = "income_variable"   # income_reliable | income_variable
    cash_reserve_rub: Decimal = Decimal("5000")
    min_order_plan_rub: Decimal = Decimal("1000")
    new_capital_rub: Decimal = Decimal("0")
    monthly_contribution_rub: Decimal = Decimal("0")
    months: int = 0


def load_target_env(income_env=None) -> TargetEnv:
    tax = getattr(income_env, "tax_rate_pct", None) or Decimal("13")
    target = getattr(income_env, "target_monthly_rub", None) or Decimal("0")
    return TargetEnv(
        target_monthly_rub=target,
        tax_rate_pct=tax,
        max_position_pct=_dec(os.getenv("TARGET_MAX_POSITION_PCT") or 25) or Decimal("25"),
        max_issuer_pct=_dec(os.getenv("TARGET_MAX_ISSUER_PCT") or 30) or Decimal("30"),
        max_money_market_pct=_dec(os.getenv("TARGET_MAX_MONEY_MARKET_PCT") or 40) or Decimal("40"),
        include_estimated=_b(os.getenv("TARGET_INCLUDE_ESTIMATED", "false")),
        include_variable=_b(os.getenv("TARGET_INCLUDE_VARIABLE", "true")),
        exclude_unknown=_b(os.getenv("TARGET_EXCLUDE_UNKNOWN", "true")),
        min_policy_bucket=os.getenv("TARGET_MIN_POLICY_BUCKET", "income_variable").strip()
        or "income_variable",
        cash_reserve_rub=_dec(os.getenv("TARGET_CASH_RESERVE_RUB") or 5000) or Decimal("5000"),
        min_order_plan_rub=_dec(os.getenv("TARGET_MIN_ORDER_PLAN_RUB") or 1000) or Decimal("1000"),
    )


# ─── модели ───────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    ticker: str
    class_code: str = ""
    issuer: str = ""
    source_type: str = ""
    income_data_source: str = ""
    policy_bucket: str = "income_unknown"
    policy_reasons: list[str] = field(default_factory=list)
    conservative_yield_pct: Decimal | None = None
    conservative_net_yield_pct: Decimal | None = None
    net_yield_pct: Decimal | None = None
    fundamental_verdict: str = ""
    income_verdict: str = ""
    risk_notes: list[str] = field(default_factory=list)
    current_price: Decimal | None = None
    eligible: bool = False
    target_layer: str = ""            # base | estimate
    excluded_reason: str = ""

    def yield_for_layer(self) -> Decimal | None:
        if self.target_layer == "estimate":
            return self.conservative_net_yield_pct or self.net_yield_pct
        return self.conservative_net_yield_pct


@dataclass
class Allocation:
    ticker: str
    target_layer: str = "base"
    target_weight_pct: Decimal = Decimal("0")
    target_capital_rub: Decimal = Decimal("0")
    expected_base_income_month_rub: Decimal = Decimal("0")
    net_yield_pct: Decimal | None = None
    reason: str = ""
    # read-only диагностика эффективности слота (веса не меняет)
    capital_share_pct: Decimal = Decimal("0")
    income_share_pct: Decimal = Decimal("0")
    income_efficiency_ratio: Decimal | None = None
    yield_vs_blended_ratio: Decimal | None = None
    low_yield_slot: bool = False


@dataclass
class CurrentVsTarget:
    ticker: str
    current_value_rub: Decimal = Decimal("0")
    target_value_rub: Decimal = Decimal("0")
    diff_value_rub: Decimal = Decimal("0")
    action_hint: str = "hold"


@dataclass
class PlanRow:
    ticker: str
    planned_add_rub: Decimal = Decimal("0")
    expected_extra_base_income_month_rub: Decimal = Decimal("0")
    reason: str = ""


@dataclass
class MonthlyPlanRow:
    month: int = 0
    contribution_rub: Decimal = Decimal("0")
    target_tickers: list[str] = field(default_factory=list)
    expected_base_income_after_rub: Decimal = Decimal("0")


@dataclass
class TargetPortfolio:
    target_monthly_net_rub: Decimal = Decimal("0")
    target_annual_net_rub: Decimal = Decimal("0")
    target_status: str = "ok"          # ok | insufficient_universe
    required_capital_rub: Decimal | None = None
    # источник вселенной (income_universe_v1)
    universe_profile: str = ""
    universe_path: str = ""
    universe_watchlist_count: int = 0
    current_total_value_rub: Decimal = Decimal("0")
    current_base_month_net_rub: Decimal = Decimal("0")
    current_estimate_month_net_rub: Decimal = Decimal("0")
    gap_base_month_rub: Decimal = Decimal("0")
    eligible_universe: list[Candidate] = field(default_factory=list)
    excluded_universe: list[Candidate] = field(default_factory=list)
    target_allocation: list[Allocation] = field(default_factory=list)
    current_vs_target: list[CurrentVsTarget] = field(default_factory=list)
    new_capital_plan: list[PlanRow] = field(default_factory=list)
    monthly_plan: list[MonthlyPlanRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ─── eligibility ──────────────────────────────────────────────────────────────

def classify_eligibility(c: Candidate, env: TargetEnv) -> None:
    """Помечает кандидата eligible/target_layer или excluded_reason (mutate)."""
    if "state_control_risk" in c.risk_notes:
        c.eligible = False
        c.excluded_reason = "state_control_risk"
        return
    b = c.policy_bucket
    if b == "income_unknown":
        c.excluded_reason = "unknown_income_data"
        return
    if b == "income_excluded":
        c.excluded_reason = ("trailing_yield_above_cap"
                             if "trailing_yield_above_cap" in c.policy_reasons
                             else "not_allowed_by_policy")
        return

    if b == "income_reliable":
        layer = "base"
    elif b == "income_variable":
        if not env.include_variable or env.min_policy_bucket == "income_reliable":
            c.excluded_reason = "not_allowed_by_policy"
            return
        layer = "base"
    elif b in ("income_estimated", "income_manual"):
        if not env.include_estimated:
            c.excluded_reason = "not_allowed_by_policy"
            return
        layer = "estimate"
    else:
        c.excluded_reason = "not_allowed_by_policy"
        return

    c.target_layer = layer
    yld = c.yield_for_layer()
    if yld is None or yld <= 0:
        c.eligible = False
        c.target_layer = ""
        c.excluded_reason = "no_conservative_yield"
        return
    c.eligible = True


def _bucket_rank(b: str) -> int:
    return {"income_reliable": 0, "income_variable": 1,
            "income_estimated": 2, "income_manual": 3}.get(b, 9)


def _fund_rank(v: str) -> int:
    if v == "quality_pass":
        return 0
    if v == "quality_risk":
        return 2
    return 1


def sort_eligible(cands: list[Candidate]) -> list[Candidate]:
    """reliable > variable; выше conservative net yield; quality_pass выше; риск ниже."""
    return sorted(cands, key=lambda c: (
        0 if c.target_layer == "base" else 1,
        _bucket_rank(c.policy_bucket),
        _fund_rank(c.fundamental_verdict),
        1 if "state_control_risk" in c.risk_notes else 0,
        -(c.yield_for_layer() or Decimal("0")),
    ))


# ─── распределение target capital ─────────────────────────────────────────────

def allocate_target(eligible: list[Candidate], env: TargetEnv
                    ) -> tuple[list[Allocation], Decimal | None, str, list[str]]:
    """
    Простое прозрачное распределение base-target: равные веса под cap, отдельный
    cap на денежный рынок, затем required_capital по взвешенной консервативной
    net-доходности. Возвращает (allocations, required_capital, status, warnings).
    """
    warnings: list[str] = []
    base = [c for c in eligible if c.target_layer == "base"]
    if not base:
        return [], None, "insufficient_universe", [
            "target universe пуст: нет base-eligible инструментов"]

    cap = env.max_position_pct
    issuer_cap = env.max_issuer_pct
    equal = Decimal("100") / Decimal(len(base))
    weights: dict[str, Decimal] = {c.ticker: min(equal, cap) for c in base}

    def _issuer(c: Candidate) -> str:
        return c.issuer or c.ticker

    # cap по эмитенту: суммарный вес одного issuer не выше max_issuer_pct
    issuer_capped: set[str] = set()
    by_issuer: dict[str, list[Candidate]] = {}
    for c in base:
        by_issuer.setdefault(_issuer(c), []).append(c)
    for grp in by_issuer.values():
        grp_total = sum((weights[c.ticker] for c in grp), Decimal("0"))
        if grp_total > issuer_cap and grp_total > 0:
            scale = issuer_cap / grp_total
            for c in grp:
                weights[c.ticker] *= scale
                issuer_capped.add(c.ticker)

    # cap на денежный рынок
    mm = [c for c in base if c.source_type == "money_market"]
    mm_total = sum((weights[c.ticker] for c in mm), Decimal("0"))
    mm_capped = mm_total > env.max_money_market_pct and mm_total > 0
    if mm_capped:
        scale = env.max_money_market_pct / mm_total
        for c in mm:
            weights[c.ticker] *= scale

    # перераспределить освободившийся вес на не-MM, соблюдая position- и issuer-cap
    leftover = Decimal("100") - sum(weights.values(), Decimal("0"))
    if leftover > 0:
        issuer_sum: dict[str, Decimal] = {}
        for c in base:
            issuer_sum[_issuer(c)] = issuer_sum.get(_issuer(c), Decimal("0")) + weights[c.ticker]
        for c in base:
            if leftover <= 0:
                break
            if c.source_type == "money_market":
                continue
            room = min(cap - weights[c.ticker], issuer_cap - issuer_sum[_issuer(c)])
            if room <= 0:
                continue
            add = min(room, leftover)
            weights[c.ticker] += add
            issuer_sum[_issuer(c)] += add
            leftover -= add
    if leftover > Decimal("0.01"):
        warnings.append(
            f"диверсификация: {leftover:.1f}% не распределено "
            f"(лимиты позиции/эмитента/денежного рынка)")

    blended = sum(
        ((weights[c.ticker] / Decimal("100")) * ((c.conservative_net_yield_pct or Decimal("0"))
                                                  / Decimal("100")) for c in base),
        Decimal("0"))
    if blended <= 0:
        return [], None, "insufficient_universe", warnings + [
            "консервативная доходность eligible-инструментов не определена"]

    target_annual_net = env.target_monthly_rub * Decimal("12")
    required_capital = (target_annual_net / blended) if target_annual_net > 0 else Decimal("0")

    allocations: list[Allocation] = []
    for c in base:
        w = weights[c.ticker]
        tcap = required_capital * w / Decimal("100")
        ynet = c.conservative_net_yield_pct or Decimal("0")
        inc_m = tcap * (ynet / Decimal("100")) / Decimal("12")
        reasons = [c.policy_bucket]
        if mm_capped and c.source_type == "money_market":
            reasons.append("money_market_capped")
        if c.ticker in issuer_capped:
            reasons.append("max_issuer_capped")
        if w >= cap:
            reasons.append("max_position_capped")
        allocations.append(Allocation(
            ticker=c.ticker, target_layer="base", target_weight_pct=w,
            target_capital_rub=tcap, expected_base_income_month_rub=inc_m,
            net_yield_pct=ynet, reason="|".join(reasons)))

    # read-only диагностика низкодоходных слотов; allocation math не меняется
    warnings.extend(annotate_low_yield_diagnostics(allocations, blended * Decimal("100")))
    return allocations, required_capital, "ok", warnings


# ─── low-yield диагностика (read-only, веса не меняет) ──────────────────────────

def annotate_low_yield_diagnostics(allocations: list[Allocation],
                                   blended_yield_pct: Decimal | None) -> list[str]:
    """
    Проставляет на каждом allocation метрики эффективности слота и помечает
    низкодоходные слоты. Возвращает список user-facing предупреждений (рус.).

    Это аналитика, не рекомендация: веса распределения здесь НЕ пересчитываются.
    Деление на ноль безопасно — при нулевом доходе/доходности ratios = None.
    """
    blended = blended_yield_pct or Decimal("0")
    total_income = sum((a.expected_base_income_month_rub for a in allocations), Decimal("0"))
    warnings: list[str] = []
    for a in allocations:
        cap_share = a.target_weight_pct
        a.capital_share_pct = cap_share
        a.income_share_pct = (
            a.expected_base_income_month_rub / total_income * Decimal("100")
            if total_income > 0 else Decimal("0"))
        a.income_efficiency_ratio = (
            a.income_share_pct / cap_share if cap_share > 0 else None)
        ynet = a.net_yield_pct or Decimal("0")
        a.yield_vs_blended_ratio = (ynet / blended if blended > 0 else None)
        a.low_yield_slot = bool(
            cap_share >= LOW_YIELD_MIN_CAPITAL_SHARE_PCT
            and a.yield_vs_blended_ratio is not None
            and a.yield_vs_blended_ratio < LOW_YIELD_TO_BLENDED_RATIO)
        if a.low_yield_slot:
            warnings.append(
                f"Низкодоходный слот: {a.ticker} занимает {cap_share:.2f}% капитала, "
                f"но даёт лишь ~{a.income_share_pct:.2f}% ожидаемого дохода; его "
                f"консервативная доходность {ynet:.2f}% существенно ниже смешанной "
                f"{blended:.2f}%. Это диагностическое предупреждение, не рекомендация. "
                f"Веса распределения не изменялись.")
    return warnings


# ─── current vs target ────────────────────────────────────────────────────────

def build_current_vs_target(allocations: list[Allocation], holdings: dict[str, Decimal],
                            env: TargetEnv) -> list[CurrentVsTarget]:
    tol = env.min_order_plan_rub
    rows: list[CurrentVsTarget] = []
    seen: set[str] = set()
    for a in allocations:
        cur = holdings.get(a.ticker, Decimal("0"))
        diff = a.target_capital_rub - cur
        if diff > tol:
            hint = "underweight"
        elif diff < -tol:
            hint = "overweight"
        else:
            hint = "hold"
        rows.append(CurrentVsTarget(a.ticker, cur, a.target_capital_rub, diff, hint))
        seen.add(a.ticker)
    for ticker, cur in holdings.items():
        if ticker in seen or cur <= 0:
            continue
        rows.append(CurrentVsTarget(ticker, cur, Decimal("0"), -cur, "not_in_target"))
    return rows


# ─── план для нового капитала ─────────────────────────────────────────────────

def build_new_capital_plan(allocations: list[Allocation], holdings: dict[str, Decimal],
                           new_capital: Decimal, env: TargetEnv) -> list[PlanRow]:
    # cash_reserve_rub оставляем нераспределённым: --new-capital-rub — это общий
    # новый капитал ДО резерва, planned_add суммарно не превышает (capital - reserve).
    available = max(Decimal("0"), new_capital - max(Decimal("0"), env.cash_reserve_rub))
    if available <= 0 or not allocations:
        return []
    underweights = [(a, max(Decimal("0"), a.target_capital_rub - holdings.get(a.ticker, Decimal("0"))))
                    for a in allocations]
    underweights = [(a, u) for a, u in underweights if u > 0]
    total_u = sum((u for _, u in underweights), Decimal("0"))
    if total_u <= 0:
        return []
    rows: list[PlanRow] = []
    remaining = available
    for a, u in sorted(underweights, key=lambda x: -x[1]):
        if remaining <= 0:
            break
        share = available * (u / total_u)
        add = min(share, u, remaining)
        if add < env.min_order_plan_rub:
            continue
        ynet = a.net_yield_pct or Decimal("0")
        inc = add * (ynet / Decimal("100")) / Decimal("12")
        rows.append(PlanRow(a.ticker, add, inc, "underweight"))
        remaining -= add
    return rows


# ─── помесячный DCA-план ──────────────────────────────────────────────────────

def build_monthly_plan(allocations: list[Allocation], holdings: dict[str, Decimal],
                       monthly: Decimal, months: int, env: TargetEnv) -> list[MonthlyPlanRow]:
    if monthly <= 0 or months <= 0 or not allocations:
        return []
    hold = {a.ticker: holdings.get(a.ticker, Decimal("0")) for a in allocations}
    rows: list[MonthlyPlanRow] = []
    for m in range(1, months + 1):
        remaining = monthly
        funded: list[str] = []
        under = sorted(
            ((a, a.target_capital_rub - hold[a.ticker]) for a in allocations),
            key=lambda x: -x[1])
        for a, gap in under:
            if remaining <= 0 or gap <= 0:
                continue
            add = min(remaining, gap)
            hold[a.ticker] += add
            funded.append(a.ticker)
            remaining -= add
        if remaining > 0:  # все добиты — кладём остаток в самый доходный target
            best = max(allocations, key=lambda a: a.net_yield_pct or Decimal("0"))
            hold[best.ticker] += remaining
            if best.ticker not in funded:
                funded.append(best.ticker)
        income_m = sum(
            (hold[a.ticker] * ((a.net_yield_pct or Decimal("0")) / Decimal("100")) / Decimal("12")
             for a in allocations), Decimal("0"))
        rows.append(MonthlyPlanRow(m, monthly, funded, income_m))
    return rows


# ─── кандидаты из watchlist ──────────────────────────────────────────────────

def candidate_from_watchlist(it, tax_rate_pct: Decimal) -> Candidate:
    cons = it.conservative_yield_pct
    return Candidate(
        ticker=it.ticker, class_code=it.class_code, issuer=it.ticker,
        source_type=it.source_type, income_data_source=it.income_data_source,
        policy_bucket=it.policy_bucket, policy_reasons=list(it.policy_reasons),
        conservative_yield_pct=cons,
        conservative_net_yield_pct=_net_pct(cons, tax_rate_pct),
        net_yield_pct=it.net_yield_pct, fundamental_verdict=it.fundamental_verdict,
        income_verdict=it.income_verdict, risk_notes=list(it.risk_notes),
        current_price=it.current_price)


# ─── оркестрация (read-only) ──────────────────────────────────────────────────

def build_target_portfolio(client, *, raw_watchlist: list[str], account_id: str | None,
                           config: dict, income_env, target_env: TargetEnv,
                           fundamental_data: dict | None = None,
                           policy_env=None, priority: list[str] | None = None
                           ) -> TargetPortfolio:
    """Полный read-only расчёт целевого портфеля и планов докупки. Заявок нет."""
    from modules.income_engine import build_watchlist, summarize_account

    fundamental_data = fundamental_data or {}
    result = TargetPortfolio(
        target_monthly_net_rub=target_env.target_monthly_rub,
        target_annual_net_rub=target_env.target_monthly_rub * Decimal("12"))

    summary = None
    if account_id is not None:
        summary = summarize_account(client, account_id, config, income_env, fundamental_data)
    if summary is not None:
        result.current_total_value_rub = summary.total_value_rub
        result.current_base_month_net_rub = summary.base_monthly_net_rub
        result.current_estimate_month_net_rub = summary.estimate_monthly_net_rub
        result.gap_base_month_rub = summary.gap_base_monthly_rub

    watch = build_watchlist(client, raw_watchlist or [], config, income_env,
                            fundamental_data, priority=priority, policy_env=policy_env)
    candidates = [candidate_from_watchlist(it, target_env.tax_rate_pct) for it in watch]
    for c in candidates:
        classify_eligibility(c, target_env)

    eligible = sort_eligible([c for c in candidates if c.eligible])
    excluded = [c for c in candidates if not c.eligible]
    result.eligible_universe = eligible
    result.excluded_universe = excluded

    allocations, required_capital, status, warns = allocate_target(eligible, target_env)
    result.target_allocation = allocations
    result.required_capital_rub = required_capital
    result.target_status = status
    result.warnings.extend(warns)

    holdings = {it.ticker: it.position_value_rub
                for it in (summary.items if summary else []) if it.ticker}
    result.current_vs_target = build_current_vs_target(allocations, holdings, target_env)
    result.new_capital_plan = build_new_capital_plan(
        allocations, holdings, target_env.new_capital_rub, target_env)
    if target_env.new_capital_rub > 0 and target_env.cash_reserve_rub > 0:
        result.warnings.append(
            f"cash_reserve_applied: {target_env.cash_reserve_rub} ₽ оставлено вне "
            f"распределения нового капитала")
    result.monthly_plan = build_monthly_plan(
        allocations, holdings, target_env.monthly_contribution_rub, target_env.months, target_env)
    return result
