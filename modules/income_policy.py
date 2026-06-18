"""
income_quality_policy_v1 — conservative income mode (read-only, чистая логика).

Слой правил, который отделяет НАДЁЖНЫЙ доход (объявленные будущие выплаты) от
оценочного (trailing), переменного (денежный рынок), ручного (manual override),
разового/сомнительного и неизвестного. Используется, чтобы планировать «жизнь на
доход» только по той части, которую можно считать опорной (base income).

Модуль pure/testable: НИКАКИХ обращений к API, заявок, order-сервисов, full-токена
и live-исполнения. Это аналитика, не рекомендация.

Раскладка дохода (годовой gross на инструмент):
    base_annual     — опорная часть (с haircut для переменной доходности);
    estimate_annual — возможная, но не гарантированная;
    excluded_annual — не используем для базового плана.
Сумма слоёв может быть меньше raw на величину haircut-margin — это намеренный
консервативный запас, а не «потерянный» доход.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

# ─── policy buckets ───────────────────────────────────────────────────────────

BUCKET_RELIABLE = "income_reliable"
BUCKET_ESTIMATED = "income_estimated"
BUCKET_MANUAL = "income_manual"
BUCKET_VARIABLE = "income_variable"
BUCKET_EXCLUDED = "income_excluded"
BUCKET_UNKNOWN = "income_unknown"

# confidence-токены policy-слоя
CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"
CONF_UNKNOWN = "unknown"


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _b(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y", "да")


@dataclass
class PolicyEnv:
    enabled: bool = True
    trailing_yield_cap_pct: Decimal = Decimal("15")
    mm_haircut_pct: Decimal = Decimal("20")
    use_trailing_dividends_in_base: bool = False
    use_manual_dividends_in_base: bool = False
    use_manual_mm_in_base: bool = True


def load_policy_env() -> PolicyEnv:
    return PolicyEnv(
        enabled=_b(os.getenv("INCOME_POLICY_ENABLED", "true")),
        trailing_yield_cap_pct=_dec(os.getenv("INCOME_POLICY_TRAILING_YIELD_CAP_PCT")
                                    or 15) or Decimal("15"),
        mm_haircut_pct=_dec(os.getenv("INCOME_POLICY_MM_HAIRCUT_PCT") or 20) or Decimal("20"),
        use_trailing_dividends_in_base=_b(
            os.getenv("INCOME_POLICY_USE_TRAILING_DIVIDENDS_IN_BASE", "false")),
        use_manual_dividends_in_base=_b(
            os.getenv("INCOME_POLICY_USE_MANUAL_DIVIDENDS_IN_BASE", "false")),
        use_manual_mm_in_base=_b(
            os.getenv("INCOME_POLICY_USE_MANUAL_MM_IN_BASE", "true")),
    )


@dataclass
class PolicyResult:
    policy_bucket: str = BUCKET_UNKNOWN
    policy_confidence: str = CONF_UNKNOWN
    policy_reasons: list[str] = field(default_factory=list)
    base_annual_income_rub: Decimal = Decimal("0")
    estimate_annual_income_rub: Decimal = Decimal("0")
    excluded_annual_income_rub: Decimal = Decimal("0")
    conservative_yield_pct: Decimal | None = None

    @property
    def count_in_base(self) -> bool:
        return self.base_annual_income_rub > 0

    @property
    def count_in_estimate(self) -> bool:
        return self.estimate_annual_income_rub > 0


# ─── классификация одного источника дохода ───────────────────────────────────

def _haircut_fraction(env: PolicyEnv) -> Decimal:
    return Decimal("1") - (env.mm_haircut_pct / Decimal("100"))


def classify_income_policy(
    *, income_data_source: str, source_type: str,
    raw_annual_income_rub: Decimal | None = None,
    gross_yield_pct: Decimal | None = None,
    has_future_date: bool = True,
    env: PolicyEnv | None = None,
) -> PolicyResult:
    """
    Классифицирует доход инструмента по policy-правилам v1.

    raw_annual_income_rub — gross годовой доход (для watchlist без позиции = 0;
    тогда раскладываются нулевые суммы, но bucket/confidence/conservative_yield
    остаются осмысленными).
    """
    env = env or PolicyEnv()
    raw = raw_annual_income_rub or Decimal("0")
    src = (income_data_source or "unknown").strip() or "unknown"
    res = PolicyResult()

    if src in ("unknown", "assumed_unknown") or source_type == "unknown":
        res.policy_bucket = BUCKET_UNKNOWN
        res.policy_confidence = CONF_UNKNOWN
        res.policy_reasons = ["unknown_income_data"]
        return res

    if src == "api_known_future":
        if has_future_date:
            res.policy_bucket = BUCKET_RELIABLE
            res.policy_confidence = CONF_HIGH
            res.policy_reasons = ["announced_future_payment"]
            res.base_annual_income_rub = raw
            res.conservative_yield_pct = gross_yield_pct
        else:
            res.policy_bucket = BUCKET_ESTIMATED
            res.policy_confidence = CONF_LOW
            res.policy_reasons = ["announced_future_payment", "missing_future_date"]
            res.estimate_annual_income_rub = raw
        return res

    if src == "api_coupon_schedule":
        # известный купонный график облигации надёжнее trailing-дивидендов
        res.policy_bucket = BUCKET_RELIABLE
        res.policy_confidence = CONF_HIGH
        res.policy_reasons = ["known_coupon_schedule"]
        res.base_annual_income_rub = raw
        res.conservative_yield_pct = gross_yield_pct
        return res

    if src == "api_trailing_12m":
        reasons = ["trailing_not_guaranteed"]
        over_cap = (gross_yield_pct is not None and env.trailing_yield_cap_pct > 0
                    and gross_yield_pct > env.trailing_yield_cap_pct)
        if over_cap:
            res.policy_bucket = BUCKET_EXCLUDED
            res.policy_confidence = CONF_LOW
            res.policy_reasons = reasons + ["trailing_yield_above_cap"]
            res.excluded_annual_income_rub = raw
        elif env.use_trailing_dividends_in_base:
            res.policy_bucket = BUCKET_ESTIMATED
            res.policy_confidence = CONF_MEDIUM
            res.policy_reasons = reasons
            res.base_annual_income_rub = raw
            res.conservative_yield_pct = gross_yield_pct
        else:
            res.policy_bucket = BUCKET_ESTIMATED
            res.policy_confidence = CONF_MEDIUM
            res.policy_reasons = reasons
            res.estimate_annual_income_rub = raw
        return res

    if src == "manual_override":
        if source_type == "money_market":
            reasons = ["manual_money_market_yield"]
            if env.use_manual_mm_in_base:
                hf = _haircut_fraction(env)
                res.policy_bucket = BUCKET_VARIABLE
                res.policy_confidence = CONF_MEDIUM
                res.policy_reasons = reasons + ["haircut_applied"]
                res.base_annual_income_rub = raw * hf
                if gross_yield_pct is not None:
                    res.conservative_yield_pct = gross_yield_pct * hf
            else:
                res.policy_bucket = BUCKET_VARIABLE
                res.policy_confidence = CONF_MEDIUM
                res.policy_reasons = reasons
                res.estimate_annual_income_rub = raw
        else:  # dividend / coupon
            reasons = ["manual_estimate"]
            res.policy_bucket = BUCKET_MANUAL
            res.policy_confidence = CONF_LOW
            res.policy_reasons = reasons
            if env.use_manual_dividends_in_base:
                res.base_annual_income_rub = raw
                res.conservative_yield_pct = gross_yield_pct
            else:
                res.estimate_annual_income_rub = raw
        return res

    if src == "trailing_30d":
        hf = _haircut_fraction(env)
        res.policy_bucket = BUCKET_VARIABLE
        res.policy_confidence = CONF_MEDIUM
        res.policy_reasons = ["variable_yield_trailing", "haircut_applied"]
        res.base_annual_income_rub = raw * hf
        if gross_yield_pct is not None:
            res.conservative_yield_pct = gross_yield_pct * hf
        return res

    if src == "assumed":
        res.policy_bucket = BUCKET_ESTIMATED
        res.policy_confidence = CONF_LOW
        res.policy_reasons = ["assumed_yield"]
        res.estimate_annual_income_rub = raw
        return res

    # неизвестный/непокрытый источник → не планируем
    res.policy_bucket = BUCKET_UNKNOWN
    res.policy_confidence = CONF_UNKNOWN
    res.policy_reasons = ["unknown_income_data"]
    return res
