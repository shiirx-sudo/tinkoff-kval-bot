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

_DEFAULT_PATH = "data/config/income_engine.yaml"
_FALLBACK_EXAMPLE = "config/income_engine.example.yaml"

CONCENTRATION_LIMIT_PCT = Decimal("25")


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
    confidence: str = "unknown"           # api|manual|assumed|unknown
    next_payment_date: str = ""           # ISO | "month_unknown" | ""
    fundamental_verdict: str = ""
    risk_notes: list[str] = field(default_factory=list)
    income_verdict: str = "income_unknown"


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


def income_for_item(pos: dict, config: dict, env: IncomeEnv) -> IncomeItem:
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
        rec = (config.get("manual_yields") or {}).get(ticker) or {}
        ypct = _dec(rec.get("expected_annual_yield_pct"))
        if ypct is not None:
            item.confidence = "manual"
        elif env.money_market_yield_pct > 0:
            ypct = env.money_market_yield_pct
            item.confidence = "assumed"
        if ypct is not None:
            item.expected_annual_yield_pct = ypct
            item.expected_annual_income_rub = value * ypct / Decimal("100")
            item.gross_yield_pct = ypct

    elif item.source_type == "dividend":
        rec = (config.get("manual_dividends") or {}).get(ticker) or {}
        dps = _dec(rec.get("expected_annual_dividend_rub_per_share"))
        if dps is not None and qty is not None:
            item.confidence = str(rec.get("confidence") or "manual")
            item.expected_annual_income_rub = dps * qty
            if value > 0:
                item.gross_yield_pct = item.expected_annual_income_rub / value * Decimal("100")
        nxt = rec.get("next_dividend_date") or rec.get("next_known_dividend_date")
        if nxt:
            item.next_payment_date = str(nxt)

    elif item.source_type == "coupon":
        rec = (config.get("manual_bonds") or {}).get(ticker) \
            or (config.get("manual_bonds") or {}).get(item.figi) or {}
        coupon = _dec(rec.get("expected_coupon_rub"))
        freq = _dec(rec.get("coupon_frequency_per_year")) or Decimal("0")
        if coupon is not None and freq > 0:
            item.confidence = str(rec.get("confidence") or "manual")
            per_unit = coupon * freq
            item.expected_annual_income_rub = per_unit * (qty or Decimal("1"))
            if value > 0:
                item.gross_yield_pct = item.expected_annual_income_rub / value * Decimal("100")
        if rec.get("maturity_date"):
            item.next_payment_date = str(rec.get("maturity_date"))

    if item.expected_annual_income_rub > 0:
        item.expected_monthly_income_rub = item.expected_annual_income_rub / Decimal("12")
        if item.gross_yield_pct is not None:
            item.net_yield_pct = _net(item.gross_yield_pct, env.tax_rate_pct)
    else:
        item.confidence = item.confidence if item.confidence != "unknown" else "unknown"

    return item


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
            or item.confidence in ("assumed", "low"):
        return "income_watch"
    return "income_candidate"


# ─── агрегация портфеля ──────────────────────────────────────────────────────

def compute_income(positions: list[dict], config: dict, env: IncomeEnv,
                   fundamental_data: dict | None = None,
                   free_cash_rub: Decimal | None = None) -> IncomeSummary:
    fundamental_data = fundamental_data or {}
    s = IncomeSummary(tax_rate_pct=env.tax_rate_pct,
                      target_monthly_rub=env.target_monthly_rub,
                      free_cash_rub=free_cash_rub or Decimal("0"))

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

        if item.source_type == "money_market":
            s.money_market_rub += item.position_value_rub
        elif item.source_type == "dividend":
            s.shares_rub += item.position_value_rub
        elif item.source_type == "coupon":
            s.bonds_rub += item.position_value_rub

        s.gross_annual_rub += item.expected_annual_income_rub
        if item.expected_annual_income_rub <= 0 and not env.include_unknown:
            s.warnings.append(f"{item.ticker or item.figi}: доход неизвестен (unknown)")

    s.items = items
    s.net_annual_rub = _net(s.gross_annual_rub, env.tax_rate_pct)
    s.gross_monthly_rub = s.gross_annual_rub / Decimal("12")
    s.net_monthly_rub = s.net_annual_rub / Decimal("12")
    s.current_monthly_net_rub = s.net_monthly_rub

    if s.total_value_rub > 0 and s.gross_annual_rub > 0:
        s.portfolio_gross_yield_pct = s.gross_annual_rub / s.total_value_rub * Decimal("100")
        s.portfolio_net_yield_pct = s.net_annual_rub / s.total_value_rub * Decimal("100")

    if env.target_monthly_rub > 0:
        s.gap_monthly_rub = max(Decimal("0"), env.target_monthly_rub - s.net_monthly_rub)
        if s.portfolio_net_yield_pct and s.portfolio_net_yield_pct > 0:
            target_annual_net = env.target_monthly_rub * Decimal("12")
            s.required_capital_rub = (target_annual_net
                                      / (s.portfolio_net_yield_pct / Decimal("100")))
        else:
            s.warnings.append("required_capital: n/a (доходность портфеля неизвестна)")

    return s


def build_calendar(items: list[IncomeItem], horizon_months: int,
                   tax_pct: Decimal) -> list[dict]:
    """Календарь ожидаемых выплат. Без точной даты — month_unknown."""
    rows: list[dict] = []
    for it in items:
        if it.expected_annual_income_rub <= 0:
            continue
        gross = it.expected_annual_income_rub
        if it.source_type == "money_market":
            # равномерно по месяцам горизонта
            per_month = gross / Decimal("12")
            for m in range(1, min(horizon_months, 12) + 1):
                rows.append({
                    "month": f"M+{m}", "ticker": it.ticker,
                    "source_type": "money_market",
                    "expected_payment_date": "month_unknown",
                    "gross_amount": per_month, "net_amount": _net(per_month, tax_pct),
                    "confidence": it.confidence})
        else:
            rows.append({
                "month": (it.next_payment_date[:7] if it.next_payment_date
                          and it.next_payment_date != "month_unknown" else "month_unknown"),
                "ticker": it.ticker, "source_type": it.source_type,
                "expected_payment_date": it.next_payment_date or "month_unknown",
                "gross_amount": gross, "net_amount": _net(gross, tax_pct),
                "confidence": it.confidence})
    return rows


# ─── read-only сбор позиций со счёта ─────────────────────────────────────────

def build_positions(client, account_id: str | None) -> list[dict]:
    """Нормализует позиции портфеля (read-only), резолвит ticker/class по figi."""
    from modules.balance import _pos_value, _resolve_account_id
    from common.helpers import quotation_to_decimal

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
        ticker = str(pos.get("ticker", "")).upper()
        class_code = str(pos.get("classCode") or pos.get("class_code") or "")
        name = ""
        if (not ticker or not class_code) and figi:
            try:
                meta = client.get_instrument_by_figi(figi) or {}
                ticker = ticker or str(meta.get("ticker", "")).upper()
                class_code = class_code or str(meta.get("classCode") or meta.get("class_code") or "")
                name = str(meta.get("name", ""))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"get_instrument_by_figi({figi}) недоступен: {exc}")
        out.append({
            "ticker": ticker, "class_code": class_code, "figi": figi,
            "instrument_name": name, "instrument_type": itype,
            "position_quantity": quotation_to_decimal(pos.get("quantity")),
            "position_value_rub": _pos_value(pos),
        })
    return out


def summarize_account(client, account_id: str | None, config: dict, env: IncomeEnv,
                      fundamental_data: dict | None = None) -> IncomeSummary:
    positions = build_positions(client, account_id)
    free_cash = available_cash_rub(client, account_id) or Decimal("0")
    return compute_income(positions, config, env, fundamental_data, free_cash)
