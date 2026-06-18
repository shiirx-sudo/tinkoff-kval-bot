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
    confidence: str = "unknown"           # api|manual|medium|low|assumed|unknown
    fundamental_verdict: str = ""
    income_verdict: str = "income_unknown"
    risk_notes: list[str] = field(default_factory=list)
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
    notes: list[str] = []
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
    return notes


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
                           config: dict, env: IncomeEnv, fundamental_result) -> WatchlistItem:
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
        rec = (config.get("manual_yields") or {}).get(ticker) or {}
        ypct = _dec(rec.get("expected_annual_yield_pct"))
        if ypct is not None:
            item.confidence = "manual"
        elif env.money_market_yield_pct > 0:
            ypct = env.money_market_yield_pct
            item.confidence = "assumed"
        if ypct is not None:
            item.expected_annual_yield_pct = ypct
            item.gross_yield_pct = ypct

    elif item.source_type == "dividend":
        rec = (config.get("manual_dividends") or {}).get(ticker) or {}
        dps = _dec(rec.get("expected_annual_dividend_rub_per_share"))
        if dps is not None:
            item.expected_annual_dividend_rub_per_share = dps
            item.confidence = str(rec.get("confidence") or "manual")
            if current_price is not None and current_price > 0:
                item.gross_yield_pct = dps / current_price * Decimal("100")

    elif item.source_type == "coupon":
        rec = (config.get("manual_bonds") or {}).get(ticker) \
            or (config.get("manual_bonds") or {}).get(item.figi) or {}
        coupon = _dec(rec.get("expected_coupon_rub"))
        freq = _dec(rec.get("coupon_frequency_per_year")) or Decimal("0")
        if coupon is not None and freq > 0:
            item.confidence = str(rec.get("confidence") or "manual")
            annual = coupon * freq
            if current_price is not None and current_price > 0:
                item.gross_yield_pct = annual / current_price * Decimal("100")

    if item.gross_yield_pct is not None:
        item.net_yield_pct = _net(item.gross_yield_pct, env.tax_rate_pct)

    item.fundamental_verdict = getattr(fundamental_result, "verdict", "") or ""
    item.risk_notes = _watchlist_risk_notes(item, fundamental_result)
    item.income_verdict = watchlist_verdict(item)
    return item


def build_watchlist(client, raw_items: list[str], config: dict, env: IncomeEnv,
                    fundamental_data: dict | None = None,
                    priority: list[str] | None = None) -> list[WatchlistItem]:
    """Прогоняет watchlist через read-only резолв + текущую цену. Заявок нет."""
    from modules.fundamental_filter import evaluate_fundamental
    from strategies.trend_signal_v1 import parse_watchlist_item

    fundamental_data = fundamental_data or {}
    priority = priority or DEFAULT_CLASS_CODE_PRIORITY
    out: list[WatchlistItem] = []
    for raw in raw_items:
        ticker, explicit = parse_watchlist_item(raw)
        meta = resolve_watchlist_meta(client, ticker, explicit, priority)
        instrument_id = meta.get("figi") or meta.get("instrument_uid")
        price, price_source = fetch_current_price(client, instrument_id)
        fr = evaluate_fundamental(ticker, meta.get("class_code") or explicit or "",
                                  fundamental_data)
        out.append(compute_watchlist_item(ticker, explicit, meta, price, price_source,
                                           config, env, fr))
    return out
