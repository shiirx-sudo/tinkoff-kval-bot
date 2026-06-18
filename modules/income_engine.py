"""
income_engine_v1 — read-only аналитика доходного портфеля.

Считает ожидаемый доход от фондов денежного рынка, дивидендов и купонов, строит
календарь выплат и gap до целевого дохода. Ничего не покупает и не продаёт, не
скрапит интернет и не даёт инвестрекомендаций. Если данных нет — unknown, без падения.

Ручные оценки (manual) — это пометки пользователя, а НЕ гарантия выплат.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from loguru import logger

from modules.balance import _MM_FUND_TICKERS, available_cash_rub
from modules.fundamental_filter import evaluate_fundamental
from modules.income_policy import (
    PolicyEnv,
    classify_income_policy,
    load_policy_env,
)

_DEFAULT_PATH = "data/config/income_engine.yaml"
_FALLBACK_EXAMPLE = "config/income_engine.example.yaml"

CONCENTRATION_LIMIT_PCT = Decimal("25")

# Дисклеймеры (история/оценка ≠ гарантия будущего дохода)
NOTE_TRAILING_DIV = "Историческая оценка, не гарантия будущих дивидендов."
NOTE_TRAILING_MM = "Trailing yield фонда — оценка, доходность переменная."


def _b(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y", "да")


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


@dataclass
class IncomeEnv:
    target_monthly_rub: Decimal = Decimal("0")
    horizon_months: int = 12
    tax_rate_pct: Decimal = Decimal("13")
    money_market_yield_pct: Decimal = Decimal("0")
    include_unknown: bool = False
    # Автоматические read-only источники (T-Invest API). manual YAML остаётся
    # как override/fallback; основной источник — официальный API.
    auto_fetch_enabled: bool = True
    dividend_lookback_months: int = 24
    dividend_trailing_months: int = 12
    mm_trailing_days: int = 30
    use_trailing_dividends: bool = True
    use_known_future_dividends: bool = True
    use_bond_coupons: bool = True


@dataclass
class IncomeItem:
    ticker: str
    class_code: str = ""
    figi: str = ""
    instrument_name: str = ""
    source_type: str = "unknown"          # money_market|dividend|coupon|unknown
    position_quantity: Decimal | None = None
    position_value_rub: Decimal = Decimal("0")
    expected_annual_yield_pct: Decimal | None = None
    expected_annual_income_rub: Decimal = Decimal("0")
    expected_monthly_income_rub: Decimal = Decimal("0")
    gross_yield_pct: Decimal | None = None
    net_yield_pct: Decimal | None = None
    confidence: str = "unknown"           # api_known|estimated|manual|assumed|unknown
    next_payment_date: str = ""           # ISO | "month_unknown" | ""
    fundamental_verdict: str = ""
    risk_notes: list[str] = field(default_factory=list)
    income_verdict: str = "income_unknown"
    # источники дохода (manual_override|api_known_future|api_trailing_12m|trailing_30d|...)
    income_data_source: str = "unknown"
    dividend_source: str = ""
    coupon_source: str = ""
    yield_source: str = ""                # источник доходности фонда денежного рынка
    known_future_income_rub: Decimal = Decimal("0")
    trailing_income_rub: Decimal = Decimal("0")
    manual_income_rub: Decimal = Decimal("0")
    notes: str = ""
    # conservative income policy (income_quality_policy_v1)
    policy_bucket: str = "income_unknown"
    policy_confidence: str = "unknown"
    policy_reasons: list[str] = field(default_factory=list)
    base_annual_income_rub: Decimal = Decimal("0")
    base_monthly_income_rub: Decimal = Decimal("0")
    estimate_annual_income_rub: Decimal = Decimal("0")
    estimate_monthly_income_rub: Decimal = Decimal("0")
    excluded_annual_income_rub: Decimal = Decimal("0")
    conservative_yield_pct: Decimal | None = None
    # внутреннее: датированные события выплат для календаря (не в схеме отчёта)
    calendar_events: list[dict] = field(default_factory=list)


@dataclass
class IncomeSummary:
    total_value_rub: Decimal = Decimal("0")
    money_market_rub: Decimal = Decimal("0")
    shares_rub: Decimal = Decimal("0")
    bonds_rub: Decimal = Decimal("0")
    free_cash_rub: Decimal = Decimal("0")
    gross_annual_rub: Decimal = Decimal("0")
    net_annual_rub: Decimal = Decimal("0")
    gross_monthly_rub: Decimal = Decimal("0")
    net_monthly_rub: Decimal = Decimal("0")
    portfolio_gross_yield_pct: Decimal | None = None
    portfolio_net_yield_pct: Decimal | None = None
    target_monthly_rub: Decimal = Decimal("0")
    current_monthly_net_rub: Decimal = Decimal("0")
    gap_monthly_rub: Decimal = Decimal("0")
    required_capital_rub: Decimal | None = None
    tax_rate_pct: Decimal = Decimal("13")
    # conservative income policy: 3 слоя дохода (gross + net)
    policy_enabled: bool = True
    base_annual_gross_rub: Decimal = Decimal("0")
    base_annual_net_rub: Decimal = Decimal("0")
    base_monthly_net_rub: Decimal = Decimal("0")
    estimate_annual_gross_rub: Decimal = Decimal("0")
    estimate_annual_net_rub: Decimal = Decimal("0")
    estimate_monthly_net_rub: Decimal = Decimal("0")
    excluded_annual_gross_rub: Decimal = Decimal("0")
    excluded_monthly_net_rub: Decimal = Decimal("0")
    unknown_instruments: int = 0
    conservative_gross_yield_pct: Decimal | None = None
    conservative_net_yield_pct: Decimal | None = None
    gap_raw_monthly_rub: Decimal = Decimal("0")
    gap_base_monthly_rub: Decimal = Decimal("0")
    items: list[IncomeItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ─── конфиг ──────────────────────────────────────────────────────────────────

def load_income_config(path: str | None = None) -> dict:
    for p in [x for x in (path, _DEFAULT_PATH, _FALLBACK_EXAMPLE) if x]:
        fp = Path(p)
        if not fp.exists():
            continue
        try:
            import yaml
            data = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"income_engine: не удалось прочитать {p}: {exc}")
    return {}


def load_income_env(config: dict | None = None) -> IncomeEnv:
    cfg = config or {}
    targets = cfg.get("targets") or {}
    return IncomeEnv(
        target_monthly_rub=_dec(os.getenv("INCOME_TARGET_MONTHLY_RUB")
                                or targets.get("monthly_income_rub") or 0) or Decimal("0"),
        horizon_months=int(os.getenv("INCOME_HORIZON_MONTHS")
                           or targets.get("horizon_months") or 12),
        tax_rate_pct=_dec(os.getenv("INCOME_ASSUME_TAX_RATE_PCT")
                          or targets.get("tax_rate_pct") or 13) or Decimal("13"),
        money_market_yield_pct=_dec(os.getenv("INCOME_MONEY_MARKET_YIELD_PCT") or 0) or Decimal("0"),
        include_unknown=_b(os.getenv("INCOME_INCLUDE_UNKNOWN", "false")),
        auto_fetch_enabled=_b(os.getenv("INCOME_AUTO_FETCH_ENABLED", "true")),
        dividend_lookback_months=int(os.getenv("INCOME_DIVIDEND_LOOKBACK_MONTHS") or 24),
        dividend_trailing_months=int(os.getenv("INCOME_DIVIDEND_TRAILING_MONTHS") or 12),
        mm_trailing_days=int(os.getenv("INCOME_MM_TRAILING_DAYS") or 30),
        use_trailing_dividends=_b(os.getenv("INCOME_USE_TRAILING_DIVIDENDS", "true")),
        use_known_future_dividends=_b(os.getenv("INCOME_USE_KNOWN_FUTURE_DIVIDENDS", "true")),
        use_bond_coupons=_b(os.getenv("INCOME_USE_BOND_COUPONS", "true")),
    )


# ─── расчёт по одному инструменту ────────────────────────────────────────────

def _net(gross: Decimal, tax_pct: Decimal) -> Decimal:
    return gross * (Decimal("1") - tax_pct / Decimal("100"))


def classify_source(pos: dict, config: dict) -> str:
    ticker = str(pos.get("ticker", "")).upper()
    itype = str(pos.get("instrument_type", "")).lower()
    mm = config.get("manual_yields") or {}
    if ticker in mm or (itype == "etf" and ticker in _MM_FUND_TICKERS):
        return "money_market"
    if itype == "bond":
        return "coupon"
    if itype == "share":
        return "dividend"
    if itype == "etf":
        return "money_market"
    return "unknown"


def income_for_item(pos: dict, config: dict, env: IncomeEnv,
                    auto: dict | None = None) -> IncomeItem:
    """
    Доход по одной позиции. Приоритет источников:
        manual_override → api_known_future → api_trailing_12m →
        trailing_price_estimate (money market) → unknown.
    `auto` — авто-данные из read-only API (modules.income_sources.fetch_auto_income).
    """
    auto = auto or pos.get("auto_income") or {}
    ticker = str(pos.get("ticker", "")).upper()
    value = _dec(pos.get("position_value_rub")) or Decimal("0")
    qty = _dec(pos.get("position_quantity"))
    item = IncomeItem(
        ticker=ticker, class_code=str(pos.get("class_code", "")),
        figi=str(pos.get("figi", "")), instrument_name=str(pos.get("instrument_name", "")),
        position_quantity=qty, position_value_rub=value,
        source_type=classify_source(pos, config),
    )

    if item.source_type == "money_market":
        _income_money_market(item, config, env, auto.get("mm") or {}, value)
    elif item.source_type == "dividend":
        _income_dividend(item, config, env, auto.get("dividend") or {}, value, qty)
    elif item.source_type == "coupon":
        _income_coupon(item, config, env, auto.get("coupon") or {}, value, qty)

    if item.expected_annual_income_rub > 0:
        item.expected_monthly_income_rub = item.expected_annual_income_rub / Decimal("12")
        if item.gross_yield_pct is not None:
            item.net_yield_pct = _net(item.gross_yield_pct, env.tax_rate_pct)
    return item


def _income_money_market(item, config, env, auto_mm, value) -> None:
    rec = (config.get("manual_yields") or {}).get(item.ticker) or {}
    ypct = _dec(rec.get("expected_annual_yield_pct"))
    if ypct is not None:                       # manual override
        item.confidence = "manual"
        item.yield_source = "manual_override"
        item.income_data_source = "manual_override"
    elif auto_mm.get("expected_annual_yield_pct") is not None:   # trailing API
        ypct = _dec(auto_mm.get("expected_annual_yield_pct"))
        item.confidence = "estimated"
        item.yield_source = auto_mm.get("yield_source") or "trailing_30d"
        item.income_data_source = item.yield_source
        item.notes = NOTE_TRAILING_MM
        for note in auto_mm.get("risk_notes") or []:
            if note not in item.risk_notes:
                item.risk_notes.append(note)
    elif env.money_market_yield_pct > 0:       # ассумпция из env
        ypct = env.money_market_yield_pct
        item.confidence = "assumed"
        item.yield_source = "assumed"
        item.income_data_source = "assumed"
    if ypct is not None:
        item.expected_annual_yield_pct = ypct
        item.expected_annual_income_rub = value * ypct / Decimal("100")
        item.gross_yield_pct = ypct
        if item.income_data_source == "manual_override":
            item.manual_income_rub = item.expected_annual_income_rub
        elif item.yield_source == "trailing_30d":
            item.trailing_income_rub = item.expected_annual_income_rub


def _income_dividend(item, config, env, auto_div, value, qty) -> None:
    rec = (config.get("manual_dividends") or {}).get(item.ticker) or {}
    manual_dps = _dec(rec.get("expected_annual_dividend_rub_per_share"))
    src = str(auto_div.get("dividend_source") or "unknown")
    dps: Decimal | None = None

    if manual_dps is not None:                 # manual override
        dps = manual_dps
        item.confidence = str(rec.get("confidence") or "manual")
        item.dividend_source = "manual_override"
        item.income_data_source = "manual_override"
        nxt = rec.get("next_dividend_date") or rec.get("next_known_dividend_date")
        if nxt:
            item.next_payment_date = str(nxt)
        if qty is not None:
            item.manual_income_rub = manual_dps * qty
    elif src == "api_known_future" and env.use_known_future_dividends:
        dps = _dec(auto_div.get("expected_annual_dividend_rub_per_share"))
        item.confidence = "api_known"
        item.dividend_source = "api_known_future"
        item.income_data_source = "api_known_future"
        item.next_payment_date = auto_div.get("next_dividend_date") or item.next_payment_date
        if dps is not None and qty is not None:
            item.known_future_income_rub = dps * qty
        # датированные будущие выплаты → календарь
        for ev in auto_div.get("events") or []:
            if qty is not None and _dec(ev.get("per_share")) is not None:
                item.calendar_events.append({
                    "date": ev.get("date"),
                    "gross_amount": _dec(ev.get("per_share")) * qty})
    elif src == "api_trailing_12m" and env.use_trailing_dividends:
        dps = _dec(auto_div.get("trailing_12m_dividends_rub_per_share")) \
            or _dec(auto_div.get("expected_annual_dividend_rub_per_share"))
        item.confidence = "estimated"
        item.dividend_source = "api_trailing_12m"
        item.income_data_source = "api_trailing_12m"
        item.notes = NOTE_TRAILING_DIV
        if "trailing_not_guaranteed" not in item.risk_notes:
            item.risk_notes.append("trailing_not_guaranteed")
        if auto_div.get("last_dividend_date"):
            item.next_payment_date = item.next_payment_date or ""
        if dps is not None and qty is not None:
            item.trailing_income_rub = dps * qty

    if dps is not None and qty is not None:
        item.expected_annual_income_rub = dps * qty
        if value > 0:
            item.gross_yield_pct = item.expected_annual_income_rub / value * Decimal("100")


def _income_coupon(item, config, env, auto_coupon, value, qty) -> None:
    rec = (config.get("manual_bonds") or {}).get(item.ticker) \
        or (config.get("manual_bonds") or {}).get(item.figi) or {}
    coupon = _dec(rec.get("expected_coupon_rub"))
    freq = _dec(rec.get("coupon_frequency_per_year")) or Decimal("0")

    if coupon is not None and freq > 0:        # manual override
        item.confidence = str(rec.get("confidence") or "manual")
        item.coupon_source = "manual_override"
        item.income_data_source = "manual_override"
        per_unit = coupon * freq
        item.expected_annual_income_rub = per_unit * (qty or Decimal("1"))
        item.manual_income_rub = item.expected_annual_income_rub
        if rec.get("maturity_date"):
            item.next_payment_date = str(rec.get("maturity_date"))
    elif env.use_bond_coupons and auto_coupon.get("coupon_source") not in (None, "unknown"):
        item.confidence = "api_known"
        item.coupon_source = auto_coupon.get("coupon_source")
        item.income_data_source = auto_coupon.get("coupon_source")
        annual_per_unit = _dec(auto_coupon.get("known_coupon_income_annualized_rub")) \
            or Decimal("0")
        item.expected_annual_income_rub = annual_per_unit * (qty or Decimal("1"))
        item.known_future_income_rub = item.expected_annual_income_rub
        item.next_payment_date = auto_coupon.get("next_coupon_date") \
            or auto_coupon.get("maturity_date") or ""
        # купонные события в горизонте → календарь
        for ev in auto_coupon.get("events") or []:
            amt = _dec(ev.get("amount"))
            if amt is not None:
                item.calendar_events.append({
                    "date": ev.get("date"),
                    "gross_amount": amt * (qty or Decimal("1"))})

    if item.expected_annual_income_rub > 0 and value > 0:
        item.gross_yield_pct = item.expected_annual_income_rub / value * Decimal("100")


def add_risk_notes(item: IncomeItem, fundamental_verdict: str,
                   state_role: str, portfolio_total: Decimal) -> None:
    item.fundamental_verdict = fundamental_verdict or ""
    if portfolio_total > 0 and item.position_value_rub / portfolio_total * Decimal("100") \
            > CONCENTRATION_LIMIT_PCT:
        item.risk_notes.append("high_concentration")
    if fundamental_verdict == "quality_risk" or state_role in ("controller", "negative"):
        item.risk_notes.append("state_control_risk")
    if item.source_type == "dividend" and item.confidence in ("low", "manual", "assumed"):
        item.risk_notes.append("dividend_cut_risk")
    if item.source_type == "coupon" and item.confidence in ("low", "manual", "assumed"):
        item.risk_notes.append("coupon_default_risk")
    if item.expected_annual_income_rub <= 0:
        item.risk_notes.append("unknown_income_data")
    if item.confidence in ("manual", "assumed"):
        item.risk_notes.append("manual_estimate")


def income_verdict(item: IncomeItem) -> str:
    if item.expected_annual_income_rub <= 0 or item.confidence == "unknown":
        return "income_unknown"
    if "state_control_risk" in item.risk_notes or item.fundamental_verdict == "quality_risk":
        return "income_risk"
    if item.fundamental_verdict in ("", "quality_unknown", "quality_watch") \
            or item.confidence in ("assumed", "low", "estimated") \
            or "trailing_not_guaranteed" in item.risk_notes:
        return "income_watch"
    return "income_candidate"


def _has_future_date(date_str: str) -> bool:
    return bool(date_str) and date_str not in ("month_unknown", "")


def apply_item_policy(item: IncomeItem, policy_env: PolicyEnv) -> None:
    """Раскладывает доход инструмента по policy-слоям (base/estimate/excluded)."""
    res = classify_income_policy(
        income_data_source=item.income_data_source,
        source_type=item.source_type,
        raw_annual_income_rub=item.expected_annual_income_rub,
        gross_yield_pct=item.gross_yield_pct,
        has_future_date=_has_future_date(item.next_payment_date),
        env=policy_env,
    )
    item.policy_bucket = res.policy_bucket
    item.policy_confidence = res.policy_confidence
    item.policy_reasons = res.policy_reasons
    item.base_annual_income_rub = res.base_annual_income_rub
    item.base_monthly_income_rub = res.base_annual_income_rub / Decimal("12")
    item.estimate_annual_income_rub = res.estimate_annual_income_rub
    item.estimate_monthly_income_rub = res.estimate_annual_income_rub / Decimal("12")
    item.excluded_annual_income_rub = res.excluded_annual_income_rub
    item.conservative_yield_pct = res.conservative_yield_pct


# ─── агрегация портфеля ──────────────────────────────────────────────────────

def compute_income(positions: list[dict], config: dict, env: IncomeEnv,
                   fundamental_data: dict | None = None,
                   free_cash_rub: Decimal | None = None,
                   policy_env: PolicyEnv | None = None) -> IncomeSummary:
    fundamental_data = fundamental_data or {}
    policy_env = policy_env or load_policy_env()
    s = IncomeSummary(tax_rate_pct=env.tax_rate_pct,
                      target_monthly_rub=env.target_monthly_rub,
                      free_cash_rub=free_cash_rub or Decimal("0"),
                      policy_enabled=policy_env.enabled)

    items: list[IncomeItem] = []
    total = s.free_cash_rub
    for pos in positions:
        item = income_for_item(pos, config, env)
        total += item.position_value_rub
        items.append(item)

    s.total_value_rub = total
    for item in items:
        fr = evaluate_fundamental(item.ticker, item.class_code, fundamental_data)
        add_risk_notes(item, fr.verdict, fr.state_role, total)
        item.income_verdict = income_verdict(item)

        if policy_env.enabled:
            apply_item_policy(item, policy_env)
        else:
            # policy выключен → весь доход считаем опорным (как раньше)
            item.policy_bucket = "income_reliable" if item.expected_annual_income_rub > 0 \
                else "income_unknown"
            item.base_annual_income_rub = item.expected_annual_income_rub
            item.base_monthly_income_rub = item.expected_annual_income_rub / Decimal("12")
            item.conservative_yield_pct = item.gross_yield_pct

        if item.source_type == "money_market":
            s.money_market_rub += item.position_value_rub
        elif item.source_type == "dividend":
            s.shares_rub += item.position_value_rub
        elif item.source_type == "coupon":
            s.bonds_rub += item.position_value_rub

        s.gross_annual_rub += item.expected_annual_income_rub
        s.base_annual_gross_rub += item.base_annual_income_rub
        s.estimate_annual_gross_rub += item.estimate_annual_income_rub
        s.excluded_annual_gross_rub += item.excluded_annual_income_rub
        if item.policy_bucket == "income_unknown":
            s.unknown_instruments += 1
        if item.expected_annual_income_rub <= 0 and not env.include_unknown:
            s.warnings.append(f"{item.ticker or item.figi}: доход неизвестен (unknown)")

    s.items = items
    # raw слой
    s.net_annual_rub = _net(s.gross_annual_rub, env.tax_rate_pct)
    s.gross_monthly_rub = s.gross_annual_rub / Decimal("12")
    s.net_monthly_rub = s.net_annual_rub / Decimal("12")
    s.current_monthly_net_rub = s.net_monthly_rub
    # conservative слои (net)
    s.base_annual_net_rub = _net(s.base_annual_gross_rub, env.tax_rate_pct)
    s.base_monthly_net_rub = s.base_annual_net_rub / Decimal("12")
    s.estimate_annual_net_rub = _net(s.estimate_annual_gross_rub, env.tax_rate_pct)
    s.estimate_monthly_net_rub = s.estimate_annual_net_rub / Decimal("12")
    s.excluded_monthly_net_rub = _net(s.excluded_annual_gross_rub, env.tax_rate_pct) / Decimal("12")

    if s.total_value_rub > 0:
        if s.gross_annual_rub > 0:
            s.portfolio_gross_yield_pct = s.gross_annual_rub / s.total_value_rub * Decimal("100")
            s.portfolio_net_yield_pct = s.net_annual_rub / s.total_value_rub * Decimal("100")
        if s.base_annual_gross_rub > 0:
            s.conservative_gross_yield_pct = s.base_annual_gross_rub / s.total_value_rub * Decimal("100")
            s.conservative_net_yield_pct = s.base_annual_net_rub / s.total_value_rub * Decimal("100")

    if env.target_monthly_rub > 0:
        s.gap_raw_monthly_rub = max(Decimal("0"), env.target_monthly_rub - s.net_monthly_rub)
        s.gap_base_monthly_rub = max(Decimal("0"), env.target_monthly_rub - s.base_monthly_net_rub)
        s.gap_monthly_rub = s.gap_raw_monthly_rub  # обратная совместимость
        # required_capital по КОНСЕРВАТИВНОЙ (base) net-доходности
        if s.conservative_net_yield_pct and s.conservative_net_yield_pct > 0:
            target_annual_net = env.target_monthly_rub * Decimal("12")
            s.required_capital_rub = (target_annual_net
                                      / (s.conservative_net_yield_pct / Decimal("100")))
        else:
            s.warnings.append(
                "required_capital: n/a (консервативная доходность не определена)")

    return s


def build_calendar(items: list[IncomeItem], horizon_months: int,
                   tax_pct: Decimal) -> list[dict]:
    """
    Календарь ожидаемых выплат. Будущие известные дивиденды и купоны строятся
    автоматически из датированных событий (calendar_events); фонды денежного
    рынка размазываются помесячно; manual без даты — month_unknown.
    """
    rows: list[dict] = []
    for it in items:
        if it.expected_annual_income_rub <= 0:
            continue
        gross = it.expected_annual_income_rub
        source = it.income_data_source or it.confidence

        if it.calendar_events:                 # датированные будущие выплаты из API
            for ev in it.calendar_events:
                date = str(ev.get("date") or "")
                amt = _dec(ev.get("gross_amount")) or Decimal("0")
                rows.append({
                    "month": date[:7] if date else "month_unknown",
                    "ticker": it.ticker, "source_type": it.source_type,
                    "source": source,
                    "expected_payment_date": date or "month_unknown",
                    "gross_amount": amt, "net_amount": _net(amt, tax_pct),
                    "confidence": it.confidence, "notes": it.notes})
        elif it.source_type == "money_market":
            per_month = gross / Decimal("12")
            for m in range(1, min(horizon_months, 12) + 1):
                rows.append({
                    "month": f"M+{m}", "ticker": it.ticker,
                    "source_type": "money_market", "source": source,
                    "expected_payment_date": "month_unknown",
                    "gross_amount": per_month, "net_amount": _net(per_month, tax_pct),
                    "confidence": it.confidence, "notes": it.notes})
        else:
            rows.append({
                "month": (it.next_payment_date[:7] if it.next_payment_date
                          and it.next_payment_date != "month_unknown" else "month_unknown"),
                "ticker": it.ticker, "source_type": it.source_type, "source": source,
                "expected_payment_date": it.next_payment_date or "month_unknown",
                "gross_amount": gross, "net_amount": _net(gross, tax_pct),
                "confidence": it.confidence, "notes": it.notes})
    return rows


# ─── read-only сбор позиций со счёта ─────────────────────────────────────────

def build_positions(client, account_id: str | None, config: dict | None = None,
                    env: IncomeEnv | None = None) -> list[dict]:
    """
    Нормализует позиции портфеля (read-only), резолвит ticker/class по figi и
    подтягивает авто-данные дохода (дивиденды/купоны/trailing yield) из API.
    """
    from modules.balance import _pos_value, _resolve_account_id
    from common.helpers import quotation_to_decimal

    config = config or {}
    acc = _resolve_account_id(client, account_id)
    if not acc:
        return []
    try:
        pf = client.get_portfolio(acc)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"GetPortfolio недоступен: {exc}")
        return []

    out: list[dict] = []
    for pos in pf.get("positions") or []:
        itype = str(pos.get("instrumentType", "")).lower()
        if itype == "currency":
            continue  # свободный кэш учитываем отдельно
        figi = str(pos.get("figi", ""))
        uid = str(pos.get("instrumentUid") or pos.get("instrument_uid") or "")
        ticker = str(pos.get("ticker", "")).upper()
        class_code = str(pos.get("classCode") or pos.get("class_code") or "")
        name = ""
        maturity = ""
        if (not ticker or not class_code) and figi:
            try:
                meta = client.get_instrument_by_figi(figi) or {}
                ticker = ticker or str(meta.get("ticker", "")).upper()
                class_code = class_code or str(meta.get("classCode") or meta.get("class_code") or "")
                name = str(meta.get("name", ""))
                uid = uid or str(meta.get("uid") or meta.get("instrumentUid") or "")
                maturity = str(meta.get("maturityDate") or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"get_instrument_by_figi({figi}) недоступен: {exc}")
        record = {
            "ticker": ticker, "class_code": class_code, "figi": figi,
            "instrument_uid": uid, "instrument_name": name, "instrument_type": itype,
            "position_quantity": quotation_to_decimal(pos.get("quantity")),
            "position_value_rub": _pos_value(pos),
        }
        record["auto_income"] = _auto_income_for_position(
            client, record, config, env, maturity)
        out.append(record)
    return out


def _auto_income_for_position(client, record: dict, config: dict,
                              env: IncomeEnv | None, maturity_date: str) -> dict:
    """Авто-данные дохода по позиции (read-only). Пустой словарь, если выключено."""
    if env is None or not env.auto_fetch_enabled:
        return {}
    source_type = classify_source(record, config)
    # manual override полностью перекрывает API → авто-запрос не нужен
    ticker = record["ticker"]
    if source_type == "money_market" and ticker in (config.get("manual_yields") or {}):
        return {}
    if source_type == "dividend" and ticker in (config.get("manual_dividends") or {}):
        return {}
    if source_type == "coupon" and (
            ticker in (config.get("manual_bonds") or {})
            or record.get("figi") in (config.get("manual_bonds") or {})):
        return {}
    instrument_id = record.get("figi") or record.get("instrument_uid")
    if not instrument_id:
        return {}
    from modules.income_sources import fetch_auto_income
    try:
        return fetch_auto_income(client, source_type=source_type,
                                 instrument_id=instrument_id, env=env,
                                 maturity_date=maturity_date)
    except Exception as exc:  # noqa: BLE001 — авто-данные опциональны
        logger.warning(f"income_sources недоступны для {ticker}: {exc}")
        return {}


def summarize_account(client, account_id: str | None, config: dict, env: IncomeEnv,
                      fundamental_data: dict | None = None) -> IncomeSummary:
    positions = build_positions(client, account_id, config, env)
    free_cash = available_cash_rub(client, account_id) or Decimal("0")
    return compute_income(positions, config, env, fundamental_data, free_cash)


# ─── watchlist: доходность по текущей цене (read-only, без позиций) ───────────
#
# В отличие от income-summary (считает доход от стоимости позиции в портфеле),
# watchlist резолвит инструмент и тянет ТЕКУЩУЮ цену read-only, чтобы посчитать
# дивдоходность/купонную доходность на акцию. Заявок не отправляет.

DEFAULT_CLASS_CODE_PRIORITY = ["TQBR", "TQTF", "SPBRU"]

# «серьёзные» риск-флаги: переводят вердикт в income_risk (см. watchlist_verdict)
_SERIOUS_RISK = {"state_control_risk", "dividend_cut_risk", "coupon_default_risk"}


@dataclass
class WatchlistItem:
    ticker: str
    class_code: str = ""
    figi: str = ""
    instrument_uid: str = ""
    instrument_name: str = ""
    instrument_type: str = ""
    current_price: Decimal | None = None
    price_source: str = "price_unknown"   # last_price|orderbook_mid|candle_close|price_unknown
    source_type: str = "unknown"          # money_market|dividend|coupon|unknown
    expected_annual_yield_pct: Decimal | None = None
    gross_yield_pct: Decimal | None = None
    net_yield_pct: Decimal | None = None
    confidence: str = "unknown"           # api_known|estimated|manual|medium|low|assumed|unknown
    fundamental_verdict: str = ""
    income_verdict: str = "income_unknown"
    risk_notes: list[str] = field(default_factory=list)
    # источники дохода (автоматические read-only API + manual override)
    income_data_source: str = "unknown"
    dividend_source: str = ""
    coupon_source: str = ""
    yield_source: str = ""
    trailing_12m_dividend: Decimal | None = None
    known_future_dividend: Decimal | None = None
    notes: str = ""
    # conservative income policy (income_quality_policy_v1)
    policy_bucket: str = "income_unknown"
    policy_confidence: str = "unknown"
    policy_reasons: list[str] = field(default_factory=list)
    conservative_yield_pct: Decimal | None = None
    # для CLI-вывода (не входит в фиксированную схему отчёта)
    expected_annual_dividend_rub_per_share: Decimal | None = None


def fetch_current_price(client, instrument_id: str) -> tuple[Decimal | None, str]:
    """Текущая цена read-only: last price → mid стакана → close свечи → unknown."""
    if not instrument_id:
        return None, "price_unknown"
    try:
        lp = client.get_last_price(instrument_id)
        if lp:
            from common.helpers import quotation_to_decimal
            p = quotation_to_decimal(lp.get("price"))
            if p > 0:
                return p, "last_price"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"get_last_price({instrument_id}) недоступен: {exc}")
    try:
        from common.helpers import quotation_to_decimal
        ob = client.get_order_book(instrument_id, depth=1)
        bid = quotation_to_decimal((ob.get("bids") or [{}])[0].get("price"))
        ask = quotation_to_decimal((ob.get("asks") or [{}])[0].get("price"))
        if bid > 0 and ask > 0:
            return (bid + ask) / Decimal("2"), "orderbook_mid"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"get_order_book({instrument_id}) недоступен: {exc}")
    try:
        from datetime import datetime, timedelta, timezone

        from common.helpers import quotation_to_decimal
        now = datetime.now(timezone.utc)
        frm = now - timedelta(days=7)
        cs = client.get_candles(instrument_id, frm.isoformat(), now.isoformat(),
                                "CANDLE_INTERVAL_DAY")
        candles = cs.get("candles") or []
        if candles:
            c = quotation_to_decimal(candles[-1].get("close"))
            if c > 0:
                return c, "candle_close"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"get_candles({instrument_id}) недоступен: {exc}")
    return None, "price_unknown"


def resolve_watchlist_meta(client, ticker: str, explicit_class: str | None,
                           priority: list[str]) -> dict:
    """Read-only резолв инструмента → figi/uid/name/type/class_code (или минимум)."""
    from strategies.trend_signal_v1 import resolve_instrument
    try:
        found = client.find_instruments(ticker)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"find_instruments({ticker}) недоступен: {exc}")
        found = []
    chosen, _selected_by, _classes = resolve_instrument(
        found, ticker, explicit_class, priority)
    if chosen is None:
        return {"ticker": ticker, "class_code": (explicit_class or ""), "figi": "",
                "instrument_uid": "", "instrument_name": "", "instrument_type": ""}
    return {
        "ticker": ticker, "class_code": chosen["class_code"], "figi": chosen["figi"],
        "instrument_uid": chosen["uid"], "instrument_name": chosen["name"],
        "instrument_type": chosen["instrument_type"],
    }


def _watchlist_risk_notes(item: WatchlistItem, fr) -> list[str]:
    # сохраняем флаги, уже выставленные авто-источниками (trailing_not_guaranteed,
    # variable_yield), и добавляем флаги по фундаменталу/цене/уверенности
    notes: list[str] = list(item.risk_notes)
    if item.price_source == "price_unknown" and item.source_type in ("dividend", "coupon"):
        notes.append("price_unknown")
    if getattr(fr, "verdict", "") == "quality_risk" \
            or getattr(fr, "state_role", "") in ("controller", "negative"):
        notes.append("state_control_risk")
    # low/assumed — это шаткая оценка дохода (manual/medium — осознанная ручная)
    if item.source_type == "dividend" and item.confidence in ("low", "assumed"):
        notes.append("dividend_cut_risk")
    if item.source_type == "coupon" and item.confidence in ("low", "assumed"):
        notes.append("coupon_default_risk")
    if item.confidence in ("manual", "assumed"):
        notes.append("manual_estimate")
    # доходность фонда денежного рынка переменная (manual или trailing)
    if item.source_type == "money_market" and item.gross_yield_pct is not None:
        notes.append("variable_yield")
    # дедупликация с сохранением порядка
    seen: set[str] = set()
    return [n for n in notes if not (n in seen or seen.add(n))]


def _wl_money_market(item: WatchlistItem, config: dict, env: IncomeEnv,
                     auto_mm: dict) -> None:
    rec = (config.get("manual_yields") or {}).get(item.ticker) or {}
    ypct = _dec(rec.get("expected_annual_yield_pct"))
    if ypct is not None:                       # manual override
        item.confidence = "manual"
        item.yield_source = "manual_override"
        item.income_data_source = "manual_override"
    elif auto_mm.get("expected_annual_yield_pct") is not None:   # trailing API
        ypct = _dec(auto_mm.get("expected_annual_yield_pct"))
        item.confidence = "estimated"
        item.yield_source = auto_mm.get("yield_source") or "trailing_30d"
        item.income_data_source = item.yield_source
        item.notes = NOTE_TRAILING_MM
        for note in auto_mm.get("risk_notes") or []:
            if note not in item.risk_notes:
                item.risk_notes.append(note)
    elif env.money_market_yield_pct > 0:
        ypct = env.money_market_yield_pct
        item.confidence = "assumed"
        item.yield_source = "assumed"
        item.income_data_source = "assumed"
    if ypct is not None:
        item.expected_annual_yield_pct = ypct
        item.gross_yield_pct = ypct


def _wl_dividend(item: WatchlistItem, config: dict, env: IncomeEnv,
                 auto_div: dict, price: Decimal | None) -> None:
    rec = (config.get("manual_dividends") or {}).get(item.ticker) or {}
    manual_dps = _dec(rec.get("expected_annual_dividend_rub_per_share"))
    src = str(auto_div.get("dividend_source") or "unknown")
    dps: Decimal | None = None
    if manual_dps is not None:                 # manual override
        dps = manual_dps
        item.confidence = str(rec.get("confidence") or "manual")
        item.dividend_source = "manual_override"
        item.income_data_source = "manual_override"
    elif src == "api_known_future" and env.use_known_future_dividends:
        dps = _dec(auto_div.get("expected_annual_dividend_rub_per_share"))
        item.known_future_dividend = dps
        item.confidence = "api_known"
        item.dividend_source = "api_known_future"
        item.income_data_source = "api_known_future"
    elif src == "api_trailing_12m" and env.use_trailing_dividends:
        dps = _dec(auto_div.get("trailing_12m_dividends_rub_per_share")) \
            or _dec(auto_div.get("expected_annual_dividend_rub_per_share"))
        item.trailing_12m_dividend = dps
        item.confidence = "estimated"
        item.dividend_source = "api_trailing_12m"
        item.income_data_source = "api_trailing_12m"
        item.notes = NOTE_TRAILING_DIV
        if "trailing_not_guaranteed" not in item.risk_notes:
            item.risk_notes.append("trailing_not_guaranteed")
    if dps is not None:
        item.expected_annual_dividend_rub_per_share = dps
        if price is not None and price > 0:
            item.gross_yield_pct = dps / price * Decimal("100")


def _wl_coupon(item: WatchlistItem, config: dict, env: IncomeEnv,
               auto_coupon: dict, price: Decimal | None) -> None:
    rec = (config.get("manual_bonds") or {}).get(item.ticker) \
        or (config.get("manual_bonds") or {}).get(item.figi) or {}
    coupon = _dec(rec.get("expected_coupon_rub"))
    freq = _dec(rec.get("coupon_frequency_per_year")) or Decimal("0")
    annual: Decimal | None = None
    if coupon is not None and freq > 0:        # manual override
        item.confidence = str(rec.get("confidence") or "manual")
        item.coupon_source = "manual_override"
        item.income_data_source = "manual_override"
        annual = coupon * freq
    elif env.use_bond_coupons and auto_coupon.get("coupon_source") not in (None, "unknown"):
        item.confidence = "api_known"
        item.coupon_source = auto_coupon.get("coupon_source")
        item.income_data_source = auto_coupon.get("coupon_source")
        annual = _dec(auto_coupon.get("known_coupon_income_annualized_rub"))
    if annual is not None and price is not None and price > 0:
        item.gross_yield_pct = annual / price * Decimal("100")


def watchlist_verdict(item: WatchlistItem) -> str:
    """Вердикт по watchlist-инструменту (read-only аналитика, не рекомендация)."""
    has_yield = item.gross_yield_pct is not None or item.expected_annual_yield_pct is not None
    # нет дохода вовсе, либо для дивиденда/облигации нет цены → unknown
    if not has_yield:
        return "income_unknown"
    if item.price_source == "price_unknown" and item.source_type in ("dividend", "coupon"):
        return "income_unknown"
    # серьёзные риски
    if item.fundamental_verdict == "quality_risk" or (_SERIOUS_RISK & set(item.risk_notes)):
        return "income_risk"
    # доходность известна; money market — переменная и ручная → всегда watch
    soft = (item.source_type == "money_market"
            or item.confidence in ("low", "medium", "manual", "assumed")
            or bool(item.risk_notes))
    return "income_watch" if soft else "income_candidate"


def compute_watchlist_item(ticker: str, explicit_class: str | None, meta: dict,
                           current_price: Decimal | None, price_source: str,
                           config: dict, env: IncomeEnv, fundamental_result,
                           auto: dict | None = None,
                           policy_env: PolicyEnv | None = None) -> WatchlistItem:
    auto = auto or {}
    ticker = ticker.upper()
    item = WatchlistItem(
        ticker=ticker,
        class_code=str(meta.get("class_code") or explicit_class or ""),
        figi=str(meta.get("figi", "")), instrument_uid=str(meta.get("instrument_uid", "")),
        instrument_name=str(meta.get("instrument_name", "")),
        instrument_type=str(meta.get("instrument_type", "")),
        current_price=current_price, price_source=price_source,
    )
    item.source_type = classify_source(
        {"ticker": ticker, "instrument_type": item.instrument_type}, config)

    if item.source_type == "money_market":
        _wl_money_market(item, config, env, auto.get("mm") or {})
    elif item.source_type == "dividend":
        _wl_dividend(item, config, env, auto.get("dividend") or {}, current_price)
    elif item.source_type == "coupon":
        _wl_coupon(item, config, env, auto.get("coupon") or {}, current_price)

    if item.gross_yield_pct is not None:
        item.net_yield_pct = _net(item.gross_yield_pct, env.tax_rate_pct)

    item.fundamental_verdict = getattr(fundamental_result, "verdict", "") or ""
    item.risk_notes = _watchlist_risk_notes(item, fundamental_result)
    item.income_verdict = watchlist_verdict(item)

    if policy_env is None:
        policy_env = load_policy_env()
    if policy_env.enabled:
        res = classify_income_policy(
            income_data_source=item.income_data_source,
            source_type=item.source_type,
            raw_annual_income_rub=Decimal("0"),     # watchlist без позиции
            gross_yield_pct=item.gross_yield_pct,
            has_future_date=(item.income_data_source == "api_known_future"),
            env=policy_env,
        )
        item.policy_bucket = res.policy_bucket
        item.policy_confidence = res.policy_confidence
        item.policy_reasons = res.policy_reasons
        item.conservative_yield_pct = res.conservative_yield_pct
    return item


def build_watchlist(client, raw_items: list[str], config: dict, env: IncomeEnv,
                    fundamental_data: dict | None = None,
                    priority: list[str] | None = None,
                    policy_env: PolicyEnv | None = None) -> list[WatchlistItem]:
    """Прогоняет watchlist через read-only резолв + текущую цену. Заявок нет."""
    from modules.fundamental_filter import evaluate_fundamental
    from strategies.trend_signal_v1 import parse_watchlist_item

    fundamental_data = fundamental_data or {}
    priority = priority or DEFAULT_CLASS_CODE_PRIORITY
    policy_env = policy_env or load_policy_env()
    out: list[WatchlistItem] = []
    for raw in raw_items:
        ticker, explicit = parse_watchlist_item(raw)
        meta = resolve_watchlist_meta(client, ticker, explicit, priority)
        instrument_id = meta.get("figi") or meta.get("instrument_uid")
        price, price_source = fetch_current_price(client, instrument_id)
        fr = evaluate_fundamental(ticker, meta.get("class_code") or explicit or "",
                                  fundamental_data)
        auto = _watchlist_auto_income(client, ticker, meta, config, env)
        out.append(compute_watchlist_item(ticker, explicit, meta, price, price_source,
                                           config, env, fr, auto, policy_env))
    return out


def _watchlist_auto_income(client, ticker: str, meta: dict, config: dict,
                           env: IncomeEnv) -> dict:
    """Авто-данные дохода для watchlist-инструмента (read-only). {} если выключено."""
    if not getattr(env, "auto_fetch_enabled", True):
        return {}
    source_type = classify_source(
        {"ticker": ticker.upper(), "instrument_type": meta.get("instrument_type", "")},
        config)
    tkr = ticker.upper()
    figi = meta.get("figi", "")
    if source_type == "money_market" and tkr in (config.get("manual_yields") or {}):
        return {}
    if source_type == "dividend" and tkr in (config.get("manual_dividends") or {}):
        return {}
    if source_type == "coupon" and (
            tkr in (config.get("manual_bonds") or {})
            or figi in (config.get("manual_bonds") or {})):
        return {}
    instrument_id = figi or meta.get("instrument_uid")
    if not instrument_id:
        return {}
    from modules.income_sources import fetch_auto_income
    try:
        return fetch_auto_income(client, source_type=source_type,
                                 instrument_id=instrument_id, env=env)
    except Exception as exc:  # noqa: BLE001 — авто-данные опциональны
        logger.warning(f"income_sources недоступны для {tkr}: {exc}")
        return {}
