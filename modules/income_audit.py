"""
income-source-audit — read-only проверка, какие именно события T-Invest API
легли в расчёт доходности (дивиденды/купоны/свечи денежного рынка).

Назначение: увидеть СЫРЫЕ события и их бакет (future_known / trailing_12m /
older_lookback / ignored для дивидендов; within_horizon / annualized /
outside_horizon для купонов) и убедиться, что в оценку не попали ошибочные,
разовые или нерепрезентативные выплаты.

СТРОГО read-only: только методы чтения T-Invest API. Никаких торговых заявок,
order-сервисов, full-токена, live-исполнения и веб-скрапинга. Это аналитика,
а не рекомендация. Портфель не изменяется.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger

from common.helpers import quotation_to_decimal
from modules.income_engine import (
    DEFAULT_CLASS_CODE_PRIORITY,
    IncomeEnv,
    _dec,
    classify_source,
    fetch_current_price,
    resolve_watchlist_meta,
)
from modules.income_sources import (
    CONF_MANUAL,
    SRC_MANUAL_OVERRIDE,
    _date_str,
    _iso,
    _now,
    _parse_dt,
    classify_coupon_event,
    classify_dividend_event,
    fetch_accrued_interest,
    fetch_coupon_data,
    fetch_dividend_data,
    fetch_mm_trailing_yield,
)


# ─── строки сырых событий ─────────────────────────────────────────────────────

@dataclass
class DividendEventRow:
    payment_date: str = ""
    record_date: str = ""
    last_buy_date: str = ""
    declared_date: str = ""
    created_at: str = ""
    dividend_net: Decimal | None = None
    dividend_gross: Decimal | None = None
    yield_value: Decimal | None = None
    source_bucket: str = ""


@dataclass
class CouponEventRow:
    coupon_date: str = ""
    pay_one_bond: Decimal | None = None
    coupon_number: str = ""
    coupon_period: str = ""
    coupon_type: str = ""
    source_bucket: str = ""


@dataclass
class CandleBasis:
    start_date: str = ""
    start_close: Decimal | None = None
    end_date: str = ""
    end_close: Decimal | None = None
    span_days: int | None = None
    growth_pct: Decimal | None = None
    annualized_yield_pct: Decimal | None = None


@dataclass
class AuditItem:
    ticker: str
    class_code: str = ""
    figi: str = ""
    instrument_uid: str = ""
    instrument_name: str = ""
    instrument_type: str = ""
    source_type: str = "unknown"
    current_price: Decimal | None = None
    price_source: str = "price_unknown"
    origin: str = "watchlist"             # watchlist | portfolio
    # сводка источника (как её увидел движок)
    income_data_source: str = "unknown"
    dividend_source: str = ""
    coupon_source: str = ""
    yield_source: str = ""
    confidence: str = "unknown"
    manual_override_active: bool = False
    # дивиденды
    known_future_dividends_rub_per_share: Decimal | None = None
    trailing_12m_dividends_rub_per_share: Decimal | None = None
    last_dividend_date: str = ""
    next_dividend_date: str = ""
    # облигации
    next_coupon_date: str = ""
    coupon_amount_rub: Decimal | None = None
    coupon_frequency_per_year: Decimal | None = None
    known_coupon_income_horizon_rub: Decimal | None = None
    known_coupon_income_annualized_rub: Decimal | None = None
    accrued_interest: Decimal | None = None
    maturity_date: str = ""
    # денежный рынок
    expected_annual_yield_pct: Decimal | None = None
    risk_notes: list[str] = field(default_factory=list)
    # сырые события
    dividend_events: list[DividendEventRow] = field(default_factory=list)
    coupon_events: list[CouponEventRow] = field(default_factory=list)
    candle_basis: CandleBasis | None = None


# ─── сырые события + бакеты (read-only) ──────────────────────────────────────

def _opt_dec(value: Any) -> Decimal | None:
    """Quotation/MoneyValue → Decimal, либо None если поля нет."""
    if value in (None, "", {}):
        return None
    return quotation_to_decimal(value)


def audit_dividend_events(client, instrument_id: str, now: datetime,
                          lookback_months: int, trailing_months: int
                          ) -> list[DividendEventRow]:
    """Сырые дивидендные события с бакетом (read-only)."""
    if not instrument_id:
        return []
    frm = _iso(now - timedelta(days=int(lookback_months * 31)))
    to = _iso(now + timedelta(days=400))
    try:
        rows = client.get_dividends(instrument_id, frm, to)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"income_audit: дивиденды {instrument_id} недоступны: {exc}")
        return []
    trailing_cut = now - timedelta(days=int(trailing_months * 31))
    out: list[DividendEventRow] = []
    for r in rows or []:
        pay_dt = _parse_dt(r.get("paymentDate") or r.get("recordDate"))
        net = quotation_to_decimal(r.get("dividendNet"))
        bucket = classify_dividend_event(pay_dt, net, now, trailing_cut)
        out.append(DividendEventRow(
            payment_date=_fmt_date(r.get("paymentDate")),
            record_date=_fmt_date(r.get("recordDate")),
            last_buy_date=_fmt_date(r.get("lastBuyDate")),
            declared_date=_fmt_date(r.get("declaredDate")),
            created_at=_fmt_date(r.get("createdAt")),
            dividend_net=net,
            dividend_gross=_opt_dec(r.get("dividendGross")),
            yield_value=_opt_dec(r.get("yieldValue")),
            source_bucket=bucket,
        ))
    out.sort(key=lambda e: e.payment_date or e.record_date)
    return out


def audit_coupon_events(client, instrument_id: str, now: datetime,
                        horizon_months: int) -> list[CouponEventRow]:
    """Сырые купонные события с бакетом (read-only)."""
    if not instrument_id:
        return []
    frm = _iso(now - timedelta(days=7))
    to = _iso(now + timedelta(days=400))
    try:
        rows = client.get_bond_coupons(instrument_id, frm, to)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"income_audit: купоны {instrument_id} недоступны: {exc}")
        return []
    horizon_end = now + timedelta(days=int(horizon_months * 31))
    annual_end = now + timedelta(days=365)
    out: list[CouponEventRow] = []
    for ev in rows or []:
        pay_dt = _parse_dt(ev.get("couponDate"))
        amount = quotation_to_decimal(ev.get("payOneBond"))
        bucket = classify_coupon_event(pay_dt, amount, now, horizon_end, annual_end)
        out.append(CouponEventRow(
            coupon_date=_fmt_date(ev.get("couponDate")),
            pay_one_bond=amount,
            coupon_number=str(ev.get("couponNumber") or ""),
            coupon_period=str(ev.get("couponPeriod") or ""),
            coupon_type=str(ev.get("couponType") or ""),
            source_bucket=bucket,
        ))
    out.sort(key=lambda e: e.coupon_date)
    return out


def audit_candle_basis(client, instrument_id: str, now: datetime,
                       trailing_days: int) -> CandleBasis | None:
    """Базис trailing-доходности денежного фонда: старт/конец/рост/annualized."""
    if not instrument_id:
        return None
    frm = now - timedelta(days=int(trailing_days) + 3)
    try:
        resp = client.get_candles(instrument_id, _iso(frm), _iso(now),
                                  "CANDLE_INTERVAL_DAY")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"income_audit: свечи {instrument_id} недоступны: {exc}")
        return None
    candles = (resp or {}).get("candles") or []
    closes: list[tuple[datetime, Decimal]] = []
    for c in candles:
        dt = _parse_dt(c.get("time") or c.get("date"))
        close = quotation_to_decimal(c.get("close"))
        if close > 0:
            closes.append((dt or now, close))
    if len(closes) < 2:
        return None
    closes.sort(key=lambda x: x[0])
    start_dt, start_close = closes[0]
    end_dt, end_close = closes[-1]
    span_days = (end_dt - start_dt).days or int(trailing_days)
    growth_pct: Decimal | None = None
    annualized: Decimal | None = None
    if start_close > 0 and span_days > 0:
        growth_pct = (end_close / start_close - Decimal("1")) * Decimal("100")
        annualized = growth_pct * (Decimal("365") / Decimal(span_days))
    return CandleBasis(
        start_date=_date_str(start_dt), start_close=start_close,
        end_date=_date_str(end_dt), end_close=end_close, span_days=span_days,
        growth_pct=growth_pct, annualized_yield_pct=annualized,
    )


def _fmt_date(value: Any) -> str:
    """ISO-дата (только день) из произвольного поля API; '' если нет/невалидно."""
    return _date_str(_parse_dt(value))


# ─── аудит одного инструмента ────────────────────────────────────────────────

def audit_one(client, meta: dict, config: dict, env: IncomeEnv, *,
              origin: str, now: datetime | None = None) -> AuditItem:
    """Полный read-only аудит одного инструмента (сводка + сырые события)."""
    n = _now(now)
    ticker = str(meta.get("ticker", "")).upper()
    item = AuditItem(
        ticker=ticker,
        class_code=str(meta.get("class_code", "")),
        figi=str(meta.get("figi", "")),
        instrument_uid=str(meta.get("instrument_uid", "")),
        instrument_name=str(meta.get("instrument_name", "")),
        instrument_type=str(meta.get("instrument_type", "")),
        origin=origin,
    )
    item.source_type = classify_source(
        {"ticker": ticker, "instrument_type": item.instrument_type}, config)

    instrument_id = item.figi or item.instrument_uid
    price = meta.get("current_price")
    if price is None:
        price, price_source = fetch_current_price(client, instrument_id)
        item.current_price = price
        item.price_source = price_source
    else:
        item.current_price = price
        item.price_source = str(meta.get("price_source") or "portfolio")

    if item.source_type == "dividend":
        _audit_dividends(client, item, config, env, instrument_id, n)
    elif item.source_type == "coupon":
        _audit_coupons(client, item, config, env, instrument_id, n)
    elif item.source_type == "money_market":
        _audit_money_market(client, item, config, env, instrument_id, n)
    return item


def _audit_dividends(client, item: AuditItem, config: dict, env: IncomeEnv,
                     instrument_id: str, now: datetime) -> None:
    div = fetch_dividend_data(
        client, instrument_id, now=now,
        lookback_months=env.dividend_lookback_months,
        trailing_months=env.dividend_trailing_months)
    item.dividend_source = str(div.get("dividend_source") or "unknown")
    item.confidence = str(div.get("dividend_confidence") or "unknown")
    item.income_data_source = item.dividend_source
    item.known_future_dividends_rub_per_share = div.get("known_future_dividends_rub_per_share")
    item.trailing_12m_dividends_rub_per_share = div.get("trailing_12m_dividends_rub_per_share")
    item.last_dividend_date = str(div.get("last_dividend_date") or "")
    item.next_dividend_date = str(div.get("next_dividend_date") or "")
    item.risk_notes = list(div.get("risk_notes") or [])
    item.dividend_events = audit_dividend_events(
        client, instrument_id, now, env.dividend_lookback_months,
        env.dividend_trailing_months)
    rec = (config.get("manual_dividends") or {}).get(item.ticker)
    if rec is not None:
        item.manual_override_active = True
        item.income_data_source = SRC_MANUAL_OVERRIDE
        item.confidence = str(rec.get("confidence") or CONF_MANUAL)


def _audit_coupons(client, item: AuditItem, config: dict, env: IncomeEnv,
                   instrument_id: str, now: datetime) -> None:
    coupon = fetch_coupon_data(client, instrument_id, now=now,
                               horizon_months=env.horizon_months)
    item.coupon_source = str(coupon.get("coupon_source") or "unknown")
    item.confidence = str(coupon.get("coupon_confidence") or "unknown")
    item.income_data_source = item.coupon_source
    item.next_coupon_date = str(coupon.get("next_coupon_date") or "")
    item.coupon_amount_rub = coupon.get("coupon_amount_rub")
    item.coupon_frequency_per_year = coupon.get("coupon_frequency_per_year")
    item.known_coupon_income_horizon_rub = coupon.get("known_coupon_income_horizon_rub")
    item.known_coupon_income_annualized_rub = coupon.get("known_coupon_income_annualized_rub")
    item.maturity_date = str(coupon.get("maturity_date") or "")
    item.accrued_interest = fetch_accrued_interest(client, instrument_id, now=now)
    item.coupon_events = audit_coupon_events(
        client, instrument_id, now, env.horizon_months)
    if (item.ticker in (config.get("manual_bonds") or {})
            or item.figi in (config.get("manual_bonds") or {})):
        item.manual_override_active = True
        item.income_data_source = SRC_MANUAL_OVERRIDE
        item.confidence = CONF_MANUAL


def _audit_money_market(client, item: AuditItem, config: dict, env: IncomeEnv,
                        instrument_id: str, now: datetime) -> None:
    mm = fetch_mm_trailing_yield(client, instrument_id, now=now,
                                 trailing_days=env.mm_trailing_days)
    item.yield_source = str(mm.get("yield_source") or "unknown")
    item.confidence = str(mm.get("confidence") or "unknown")
    item.income_data_source = item.yield_source
    item.expected_annual_yield_pct = mm.get("expected_annual_yield_pct")
    item.risk_notes = list(mm.get("risk_notes") or [])
    # trailing-базис свечей всегда собираем как raw/audit basis — даже если
    # активен manual override (candle_basis.annualized_yield_pct ≠ ручной yield).
    item.candle_basis = audit_candle_basis(
        client, instrument_id, now, env.mm_trailing_days)
    rec = (config.get("manual_yields") or {}).get(item.ticker)
    if rec is not None:
        # manual override приоритетен: основной yield в сводке — ручной, а
        # trailing остаётся виден в candle_basis (не подменяет override).
        item.manual_override_active = True
        item.income_data_source = SRC_MANUAL_OVERRIDE
        item.yield_source = SRC_MANUAL_OVERRIDE
        item.confidence = CONF_MANUAL
        manual_yield = _dec(rec.get("expected_annual_yield_pct"))
        if manual_yield is not None:
            item.expected_annual_yield_pct = manual_yield


# ─── оркестрация: watchlist и/или портфель ───────────────────────────────────

def build_audit(client, *, raw_items: list[str] | None = None,
                account_id: str | None = None, config: dict, env: IncomeEnv,
                priority: list[str] | None = None,
                now: datetime | None = None) -> list[AuditItem]:
    """
    Read-only аудит источников дохода по watchlist и/или позициям портфеля.

    raw_items — элементы watchlist ('TQBR:SBER' / 'SBER@TQBR' / 'SBER').
    account_id — если задан, аудит покрывает текущие позиции портфеля.
    """
    from strategies.trend_signal_v1 import parse_watchlist_item

    priority = priority or DEFAULT_CLASS_CODE_PRIORITY
    out: list[AuditItem] = []
    seen: set[str] = set()

    for raw in raw_items or []:
        ticker, explicit = parse_watchlist_item(raw)
        meta = resolve_watchlist_meta(client, ticker, explicit, priority)
        key = f"WL:{ticker}:{meta.get('figi') or meta.get('instrument_uid')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(audit_one(client, meta, config, env, origin="watchlist", now=now))

    if account_id is not None:
        from modules.income_engine import build_positions
        for pos in build_positions(client, account_id, config, env):
            meta = {
                "ticker": pos.get("ticker", ""), "class_code": pos.get("class_code", ""),
                "figi": pos.get("figi", ""), "instrument_uid": pos.get("instrument_uid", ""),
                "instrument_name": pos.get("instrument_name", ""),
                "instrument_type": pos.get("instrument_type", ""),
            }
            key = f"PF:{meta['ticker']}:{meta.get('figi') or meta.get('instrument_uid')}"
            if key in seen:
                continue
            seen.add(key)
            out.append(audit_one(client, meta, config, env, origin="portfolio", now=now))

    return out
