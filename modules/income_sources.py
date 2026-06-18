"""
Автоматические read-only источники доходных данных для income_engine_v1.

Тянет из официального T-Invest API (read-only) дивиденды, купоны, НКД и цены
для оценки доходности. НИЧЕГО не покупает и не продаёт; никаких торговых
заявок, order-сервисов, full-токена и веб-скрапинга — только методы чтения.

Возвращает нормализованные словари (а не SDK-объекты), совместимые с REST-
контрактом проекта (Quotation/MoneyValue = {units, nano}). Если API не дал
данных — источник остаётся 'unknown', без падения.

ВАЖНО: историческая (trailing) доходность и trailing-дивиденды — это ОЦЕНКА,
а НЕ гарантия будущих выплат. Соответствующие риск-флаги ставятся здесь же.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from loguru import logger

from common.helpers import quotation_to_decimal

# Источники дохода (значения полей *_source в income engine)
SRC_MANUAL_OVERRIDE = "manual_override"
SRC_API_KNOWN_FUTURE = "api_known_future"
SRC_API_TRAILING_12M = "api_trailing_12m"
SRC_API_COUPON = "api_coupon_schedule"
SRC_TRAILING_30D = "trailing_30d"
SRC_UNKNOWN = "unknown"

# Confidence-токены
CONF_MANUAL = "manual"
CONF_API_KNOWN = "api_known"
CONF_ESTIMATED = "estimated"
CONF_UNKNOWN = "unknown"

# Бакеты классификации сырых событий (для income-source-audit)
DIV_BUCKET_FUTURE = "future_known"        # объявленная будущая выплата → используется
DIV_BUCKET_TRAILING = "trailing_12m"      # за последние trailing_months → оценка
DIV_BUCKET_OLDER = "older_lookback"       # внутри lookback, но старше trailing → НЕ в оценке
DIV_BUCKET_IGNORED = "ignored"            # без даты/суммы → пропущено

COUPON_BUCKET_HORIZON = "within_horizon"  # в горизонте → known_coupon_income_horizon
COUPON_BUCKET_ANNUAL = "annualized"       # в пределах 12м, но вне горизонта
COUPON_BUCKET_OUTSIDE = "outside_horizon" # прошлое/за пределами → не учитывается


def classify_dividend_event(pay_dt: datetime | None, per_share: Decimal | None,
                            now: datetime, trailing_cut: datetime) -> str:
    """Бакет дивидендного события (единый источник истины для движка и аудита)."""
    if pay_dt is None or per_share is None or per_share <= 0:
        return DIV_BUCKET_IGNORED
    if pay_dt > now:
        return DIV_BUCKET_FUTURE
    if pay_dt >= trailing_cut:
        return DIV_BUCKET_TRAILING
    return DIV_BUCKET_OLDER


def classify_coupon_event(pay_dt: datetime | None, amount: Decimal | None,
                          now: datetime, horizon_end: datetime,
                          annual_end: datetime) -> str:
    """Бакет купонного события (горизонт/год/вне) для аудита."""
    if pay_dt is None or amount is None or amount <= 0 or pay_dt < now:
        return COUPON_BUCKET_OUTSIDE
    if pay_dt <= horizon_end:
        return COUPON_BUCKET_HORIZON
    if pay_dt <= annual_end:
        return COUPON_BUCKET_ANNUAL
    return COUPON_BUCKET_OUTSIDE


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_dt(value: Any) -> datetime | None:
    """ISO-строка ('2025-07-10T00:00:00Z' и т.п.) → aware datetime (UTC). None при сбое."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        # пробуем только дату
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _date_str(dt: datetime | None) -> str:
    return dt.date().isoformat() if dt else ""


# ─── Дивиденды (акции) ────────────────────────────────────────────────────────

def fetch_dividend_data(
    client, instrument_id: str, *, now: datetime | None = None,
    lookback_months: int = 24, trailing_months: int = 12,
) -> dict[str, Any]:
    """
    Read-only оценка дивидендов по акции.

    Возвращает словарь:
        dividend_source, dividend_confidence,
        expected_annual_dividend_rub_per_share,
        known_future_dividends_rub_per_share,
        trailing_12m_dividends_rub_per_share,
        last_dividend_date, next_dividend_date,
        events (будущие выплаты для календаря), risk_notes
    """
    out = _empty_dividend()
    if not instrument_id:
        return out
    n = _now(now)
    frm = _iso(n - timedelta(days=int(lookback_months * 31)))
    to = _iso(n + timedelta(days=400))  # охватить уже объявленные будущие выплаты
    try:
        rows = client.get_dividends(instrument_id, frm, to)
    except Exception as exc:  # noqa: BLE001 — дивиденды опциональны
        logger.warning(f"income_sources: дивиденды {instrument_id} недоступны: {exc}")
        return out

    trailing_cut = n - timedelta(days=int(trailing_months * 31))
    known_future = Decimal("0")
    trailing = Decimal("0")
    future_events: list[dict] = []
    next_dt: datetime | None = None
    last_dt: datetime | None = None

    for row in rows or []:
        pay_dt = _parse_dt(row.get("paymentDate") or row.get("recordDate"))
        per_share = quotation_to_decimal(row.get("dividendNet"))
        bucket = classify_dividend_event(pay_dt, per_share, n, trailing_cut)
        if bucket == DIV_BUCKET_FUTURE:
            known_future += per_share
            future_events.append({"date": pay_dt, "per_share": per_share})
            if next_dt is None or pay_dt < next_dt:
                next_dt = pay_dt
        elif bucket == DIV_BUCKET_TRAILING:
            trailing += per_share
            if last_dt is None or pay_dt > last_dt:
                last_dt = pay_dt
        elif bucket == DIV_BUCKET_OLDER:
            if last_dt is None or pay_dt > last_dt:
                last_dt = pay_dt

    out["known_future_dividends_rub_per_share"] = known_future or None
    out["trailing_12m_dividends_rub_per_share"] = trailing or None
    out["last_dividend_date"] = _date_str(last_dt)
    out["next_dividend_date"] = _date_str(next_dt)

    if known_future > 0:
        out["dividend_source"] = SRC_API_KNOWN_FUTURE
        out["dividend_confidence"] = CONF_API_KNOWN
        out["expected_annual_dividend_rub_per_share"] = known_future
        out["events"] = [
            {"date": _date_str(e["date"]), "per_share": e["per_share"]}
            for e in sorted(future_events, key=lambda e: e["date"])
        ]
    elif trailing > 0:
        out["dividend_source"] = SRC_API_TRAILING_12M
        out["dividend_confidence"] = CONF_ESTIMATED
        out["expected_annual_dividend_rub_per_share"] = trailing
        out["risk_notes"].append("trailing_not_guaranteed")
    return out


def _empty_dividend() -> dict[str, Any]:
    return {
        "dividend_source": SRC_UNKNOWN,
        "dividend_confidence": CONF_UNKNOWN,
        "expected_annual_dividend_rub_per_share": None,
        "known_future_dividends_rub_per_share": None,
        "trailing_12m_dividends_rub_per_share": None,
        "last_dividend_date": "",
        "next_dividend_date": "",
        "events": [],
        "risk_notes": [],
    }


# ─── Купоны (облигации) ───────────────────────────────────────────────────────

def fetch_coupon_data(
    client, instrument_id: str, *, now: datetime | None = None,
    horizon_months: int = 12, maturity_date: str = "",
) -> dict[str, Any]:
    """
    Read-only график купонов облигации в горизонте horizon_months.

    Суммы рассчитываются на ОДНУ бумагу (умножение на количество — в income engine).
    Возвращает словарь с coupon_source/next_coupon_date/coupon_amount_rub/
    coupon_frequency_per_year/known_coupon_income_horizon_rub/
    known_coupon_income_annualized_rub/maturity_date/coupon_confidence/events.
    """
    out = _empty_coupon()
    out["maturity_date"] = maturity_date or ""
    if not instrument_id:
        return out
    n = _now(now)
    frm = _iso(n - timedelta(days=7))
    to = _iso(n + timedelta(days=400))
    try:
        events = client.get_bond_coupons(instrument_id, frm, to)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"income_sources: купоны {instrument_id} недоступны: {exc}")
        return out

    horizon_end = n + timedelta(days=int(horizon_months * 31))
    annual_end = n + timedelta(days=365)
    horizon_sum = Decimal("0")
    annual_sum = Decimal("0")
    next_dt: datetime | None = None
    next_amount: Decimal | None = None
    period_days: int | None = None
    cal_events: list[dict] = []

    for ev in events or []:
        pay_dt = _parse_dt(ev.get("couponDate"))
        amount = quotation_to_decimal(ev.get("payOneBond"))
        if pay_dt is None or amount <= 0 or pay_dt < n:
            continue
        if next_dt is None or pay_dt < next_dt:
            next_dt, next_amount = pay_dt, amount
            try:
                period_days = int(ev.get("couponPeriod") or 0) or None
            except (TypeError, ValueError):
                period_days = None
        if pay_dt <= horizon_end:
            horizon_sum += amount
            cal_events.append({"date": _date_str(pay_dt), "amount": amount})
        if pay_dt <= annual_end:
            annual_sum += amount

    if next_amount is None:
        return out

    freq: Decimal | None = None
    if period_days and period_days > 0:
        freq = (Decimal("365") / Decimal(period_days)).quantize(Decimal("1"))

    out["coupon_source"] = SRC_API_COUPON
    out["coupon_confidence"] = CONF_API_KNOWN
    out["next_coupon_date"] = _date_str(next_dt)
    out["coupon_amount_rub"] = next_amount
    out["coupon_frequency_per_year"] = freq
    out["known_coupon_income_horizon_rub"] = horizon_sum or None
    out["known_coupon_income_annualized_rub"] = annual_sum or None
    out["events"] = sorted(cal_events, key=lambda e: e["date"])
    return out


def _empty_coupon() -> dict[str, Any]:
    return {
        "coupon_source": SRC_UNKNOWN,
        "coupon_confidence": CONF_UNKNOWN,
        "next_coupon_date": "",
        "coupon_amount_rub": None,
        "coupon_frequency_per_year": None,
        "known_coupon_income_horizon_rub": None,
        "known_coupon_income_annualized_rub": None,
        "maturity_date": "",
        "accrued_interest": None,
        "events": [],
    }


def fetch_accrued_interest(
    client, instrument_id: str, *, now: datetime | None = None,
) -> Decimal | None:
    """Последний НКД на бумагу (read-only). None при отсутствии данных."""
    if not instrument_id:
        return None
    n = _now(now)
    frm = _iso(n - timedelta(days=10))
    to = _iso(n + timedelta(days=1))
    try:
        rows = client.get_accrued_interests(instrument_id, frm, to)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"income_sources: НКД {instrument_id} недоступен: {exc}")
        return None
    latest: tuple[datetime, Decimal] | None = None
    for row in rows or []:
        dt = _parse_dt(row.get("date"))
        val = quotation_to_decimal(row.get("value"))
        if dt is None:
            continue
        if latest is None or dt > latest[0]:
            latest = (dt, val)
    return latest[1] if latest else None


# ─── Фонды денежного рынка: trailing-доходность по свечам ─────────────────────

def fetch_mm_trailing_yield(
    client, instrument_id: str, *, now: datetime | None = None,
    trailing_days: int = 30,
) -> dict[str, Any]:
    """
    Read-only оценка годовой доходности фонда денежного рынка по росту цены за
    trailing_days дней (свечи). ОЦЕНКА, не гарантия (доходность переменная).

    Возвращает {yield_source, expected_annual_yield_pct, confidence, risk_notes}.
    """
    out = {
        "yield_source": SRC_UNKNOWN,
        "expected_annual_yield_pct": None,
        "confidence": CONF_UNKNOWN,
        "risk_notes": [],
    }
    if not instrument_id:
        return out
    n = _now(now)
    frm = n - timedelta(days=int(trailing_days) + 3)
    try:
        resp = client.get_candles(instrument_id, _iso(frm), _iso(n),
                                  "CANDLE_INTERVAL_DAY")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"income_sources: свечи {instrument_id} недоступны: {exc}")
        return out
    candles = (resp or {}).get("candles") or []
    closes: list[tuple[datetime, Decimal]] = []
    for c in candles:
        dt = _parse_dt(c.get("time") or c.get("date"))
        close = quotation_to_decimal(c.get("close"))
        if close > 0:
            closes.append((dt or n, close))
    if len(closes) < 2:
        return out
    closes.sort(key=lambda x: x[0])
    p_start = closes[0][1]
    p_end = closes[-1][1]
    span_days = (closes[-1][0] - closes[0][0]).days or trailing_days
    if p_start <= 0 or span_days <= 0 or p_end == p_start:
        return out
    growth = (p_end / p_start) - Decimal("1")
    annualized = growth * (Decimal("365") / Decimal(span_days)) * Decimal("100")
    if annualized <= 0:
        return out
    out["yield_source"] = SRC_TRAILING_30D
    out["expected_annual_yield_pct"] = annualized
    out["confidence"] = CONF_ESTIMATED
    out["risk_notes"].append("variable_yield")
    return out


# ─── Единая точка сбора авто-источников по инструменту ───────────────────────

def fetch_auto_income(
    client, *, source_type: str, instrument_id: str, env, now: datetime | None = None,
    maturity_date: str = "",
) -> dict[str, Any]:
    """
    Собирает авто-данные дохода по типу инструмента (read-only).

    Возвращает {'dividend': {...}|None, 'coupon': {...}|None, 'mm': {...}|None}.
    Учитывает фиче-флаги env (use_*). Заявок не отправляет.
    """
    auto: dict[str, Any] = {"dividend": None, "coupon": None, "mm": None}
    if not getattr(env, "auto_fetch_enabled", True) or not instrument_id:
        return auto

    if source_type == "dividend" and (
            getattr(env, "use_known_future_dividends", True)
            or getattr(env, "use_trailing_dividends", True)):
        auto["dividend"] = fetch_dividend_data(
            client, instrument_id, now=now,
            lookback_months=getattr(env, "dividend_lookback_months", 24),
            trailing_months=getattr(env, "dividend_trailing_months", 12),
        )
    elif source_type == "coupon" and getattr(env, "use_bond_coupons", True):
        coupon = fetch_coupon_data(
            client, instrument_id, now=now,
            horizon_months=getattr(env, "horizon_months", 12),
            maturity_date=maturity_date,
        )
        coupon["accrued_interest"] = fetch_accrued_interest(
            client, instrument_id, now=now)
        auto["coupon"] = coupon
    elif source_type == "money_market":
        auto["mm"] = fetch_mm_trailing_yield(
            client, instrument_id, now=now,
            trailing_days=getattr(env, "mm_trailing_days", 30),
        )
    return auto
