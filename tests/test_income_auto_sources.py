"""
Тесты автоматических read-only источников доходных данных (income_sources +
интеграция в income_engine). Никаких заявок: только чтение API.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from modules.fundamental_filter import FundamentalResult
from modules.income_engine import (
    IncomeEnv,
    build_calendar,
    compute_income,
    compute_watchlist_item,
    income_for_item,
)
from modules.income_sources import (
    fetch_auto_income,
    fetch_coupon_data,
    fetch_dividend_data,
    fetch_mm_trailing_yield,
)

NOW = datetime(2026, 6, 18, tzinfo=timezone.utc)
ENV = IncomeEnv(target_monthly_rub=Decimal("100000"), horizon_months=12,
                tax_rate_pct=Decimal("13"))


def _q(value, currency="rub") -> dict:
    d = Decimal(str(value))
    units = int(d)
    nano = int((d - units) * Decimal("1000000000"))
    return {"units": str(units), "nano": nano, "currency": currency}


def _pos(ticker, itype, qty, value, auto=None, cls="TQBR", figi="F"):
    return {"ticker": ticker, "class_code": cls, "figi": figi,
            "instrument_name": ticker, "instrument_type": itype,
            "position_quantity": Decimal(str(qty)),
            "position_value_rub": Decimal(str(value)),
            "auto_income": auto or {}}


class FakeAPIClient:
    """Минимальный read-only фейк фасада ReadOnlyClient (только чтение)."""

    def __init__(self, dividends=None, coupons=None, accrued=None, candles=None):
        self._dividends = dividends or []
        self._coupons = coupons or []
        self._accrued = accrued or []
        self._candles = candles or []

    def get_dividends(self, instrument_id, from_iso, to_iso):
        return list(self._dividends)

    def get_bond_coupons(self, instrument_id, from_iso, to_iso):
        return list(self._coupons)

    def get_accrued_interests(self, instrument_id, from_iso, to_iso):
        return list(self._accrued)

    def get_candles(self, instrument_id, from_iso, to_iso, interval="CANDLE_INTERVAL_DAY"):
        return {"candles": list(self._candles)}


# ─── 1. Акция с будущим объявленным дивидендом ───────────────────────────────

def test_share_with_future_dividend_known_future():
    client = FakeAPIClient(dividends=[
        {"paymentDate": "2026-07-10T00:00:00Z", "dividendNet": _q(35)},
    ])
    div = fetch_dividend_data(client, "FIGI", now=NOW)
    assert div["dividend_source"] == "api_known_future"
    assert div["expected_annual_dividend_rub_per_share"] == Decimal("35")
    assert div["next_dividend_date"] == "2026-07-10"

    item = income_for_item(_pos("SBER", "share", 100, 30000,
                                auto={"dividend": div}), {}, ENV)
    assert item.dividend_source == "api_known_future"
    assert item.income_data_source == "api_known_future"
    assert item.confidence == "api_known"
    assert item.expected_annual_income_rub == Decimal("3500")
    # календарь содержит будущую выплату
    rows = build_calendar([item], 12, ENV.tax_rate_pct)
    assert any(r["month"].startswith("2026-07") for r in rows)
    assert any(r["source"] == "api_known_future" for r in rows)


# ─── 2. Акция без будущих, но с trailing 12m ─────────────────────────────────

def test_share_trailing_12m_marks_not_guaranteed():
    client = FakeAPIClient(dividends=[
        {"paymentDate": "2025-08-10T00:00:00Z", "dividendNet": _q(30)},
    ])
    div = fetch_dividend_data(client, "FIGI", now=NOW)
    assert div["dividend_source"] == "api_trailing_12m"
    assert "trailing_not_guaranteed" in div["risk_notes"]

    pos = _pos("LKOH", "share", 10, 70000, auto={"dividend": div})
    item = income_for_item(pos, {}, ENV)
    assert item.dividend_source == "api_trailing_12m"
    assert item.confidence == "estimated"
    assert "trailing_not_guaranteed" in item.risk_notes
    assert item.notes  # дисклеймер проставлен
    # вердикт проставляется в агрегации compute_income
    s = compute_income([pos], {}, ENV, {})
    assert s.items[0].income_verdict == "income_watch"


# ─── 3. Акция без дивидендов ─────────────────────────────────────────────────

def test_share_without_dividends_is_unknown():
    client = FakeAPIClient(dividends=[])
    div = fetch_dividend_data(client, "FIGI", now=NOW)
    assert div["dividend_source"] == "unknown"
    item = income_for_item(_pos("XXXX", "share", 5, 5000,
                                auto={"dividend": div}), {}, ENV)
    assert item.expected_annual_income_rub == Decimal("0")
    assert item.income_verdict == "income_unknown"


# ─── 4. Облигация с графиком купонов ─────────────────────────────────────────

def test_bond_coupon_schedule_calculated():
    client = FakeAPIClient(coupons=[
        {"couponDate": "2026-09-15T00:00:00Z", "payOneBond": _q(40), "couponPeriod": 182},
        {"couponDate": "2027-03-15T00:00:00Z", "payOneBond": _q(40), "couponPeriod": 182},
    ])
    coupon = fetch_coupon_data(client, "FIGI", now=NOW, horizon_months=12)
    assert coupon["coupon_source"] == "api_coupon_schedule"
    assert coupon["next_coupon_date"] == "2026-09-15"
    assert coupon["coupon_frequency_per_year"] == Decimal("2")
    assert coupon["known_coupon_income_annualized_rub"] == Decimal("80")

    item = income_for_item(_pos("RU000TEST", "bond", 10, 9000,
                                auto={"coupon": coupon}), {}, ENV)
    assert item.coupon_source == "api_coupon_schedule"
    assert item.expected_annual_income_rub == Decimal("800")   # 80 * 10
    rows = build_calendar([item], 12, ENV.tax_rate_pct)
    assert any(r["month"].startswith("2026-09") for r in rows)


# ─── 5. Фонд денежного рынка с manual yield ──────────────────────────────────

def test_money_market_manual_override():
    config = {"manual_yields": {"LQDT": {"expected_annual_yield_pct": 14.0}}}
    # даже если есть авто-trailing, manual override должен победить
    auto = {"mm": {"yield_source": "trailing_30d",
                   "expected_annual_yield_pct": Decimal("9"),
                   "confidence": "estimated", "risk_notes": ["variable_yield"]}}
    item = income_for_item(_pos("LQDT", "etf", 1000, 100000, auto=auto, cls="TQTF"),
                           config, ENV)
    assert item.yield_source == "manual_override"
    assert item.income_data_source == "manual_override"
    assert item.expected_annual_income_rub == Decimal("14000.0")
    assert item.confidence == "manual"


# ─── 6. Фонд денежного рынка без manual, но с ростом цены ─────────────────────

def test_money_market_trailing_yield_from_candles():
    client = FakeAPIClient(candles=[
        {"time": "2026-05-19T00:00:00Z", "close": _q("100")},
        {"time": "2026-06-18T00:00:00Z", "close": _q("101")},
    ])
    mm = fetch_mm_trailing_yield(client, "FIGI", now=NOW, trailing_days=30)
    assert mm["yield_source"] == "trailing_30d"
    assert mm["expected_annual_yield_pct"] is not None
    assert mm["expected_annual_yield_pct"] > 0
    assert "variable_yield" in mm["risk_notes"]

    item = income_for_item(_pos("AKMM", "etf", 1000, 100000, auto={"mm": mm}, cls="TQTF"),
                           {}, ENV)
    assert item.yield_source == "trailing_30d"
    assert item.income_data_source == "trailing_30d"
    assert "variable_yield" in item.risk_notes
    assert item.expected_annual_income_rub > 0


# ─── 7. Manual override приоритетнее API-оценки (дивиденды) ──────────────────

def test_manual_override_beats_api_estimate():
    config = {"manual_dividends": {"SBER": {
        "expected_annual_dividend_rub_per_share": 35, "confidence": "medium"}}}
    auto = {"dividend": {"dividend_source": "api_trailing_12m",
                         "trailing_12m_dividends_rub_per_share": Decimal("20"),
                         "expected_annual_dividend_rub_per_share": Decimal("20"),
                         "risk_notes": ["trailing_not_guaranteed"]}}
    item = income_for_item(_pos("SBER", "share", 100, 30000, auto=auto), config, ENV)
    assert item.dividend_source == "manual_override"
    assert item.income_data_source == "manual_override"
    assert item.expected_annual_income_rub == Decimal("3500")   # 35 * 100, не 20
    assert "trailing_not_guaranteed" not in item.risk_notes


# ─── 7b. Приоритет в watchlist ───────────────────────────────────────────────

def test_watchlist_api_trailing_source():
    auto = {"dividend": {"dividend_source": "api_trailing_12m",
                         "trailing_12m_dividends_rub_per_share": Decimal("35"),
                         "expected_annual_dividend_rub_per_share": Decimal("35"),
                         "risk_notes": ["trailing_not_guaranteed"]}}
    meta = {"class_code": "TQBR", "figi": "F", "instrument_uid": "U",
            "instrument_name": "SBER", "instrument_type": "share"}
    fr = FundamentalResult(ticker="SBER", verdict="quality_unknown")
    it = compute_watchlist_item("SBER", "TQBR", meta, Decimal("319.21"),
                                "last_price", {}, ENV, fr, auto)
    assert it.dividend_source == "api_trailing_12m"
    assert it.income_data_source == "api_trailing_12m"
    assert it.confidence == "estimated"
    assert "trailing_not_guaranteed" in it.risk_notes
    assert it.income_verdict == "income_watch"


# ─── 8. Отчёты содержат поля источников/confidence ───────────────────────────

def test_reports_have_source_fields(tmp_path):
    from reports import income_reports
    div = {"dividend_source": "api_trailing_12m",
           "trailing_12m_dividends_rub_per_share": Decimal("35"),
           "expected_annual_dividend_rub_per_share": Decimal("35"),
           "risk_notes": ["trailing_not_guaranteed"]}
    positions = [_pos("SBER", "share", 100, 30000, auto={"dividend": div})]
    s = compute_income(positions, {}, ENV, {})
    income_reports.write_summary(s, tmp_path)
    header = (tmp_path / "income_summary.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    for col in ("income_data_source", "dividend_source", "coupon_source",
                "yield_source", "known_future_income_rub", "trailing_income_rub",
                "manual_income_rub", "confidence", "notes"):
        assert col in header, col

    rows = build_calendar(s.items, 12, ENV.tax_rate_pct)
    income_reports.write_calendar(rows, tmp_path)
    cal_header = (tmp_path / "income_calendar.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    assert "source" in cal_header and "notes" in cal_header


# ─── 9. Telegram текст содержит источник и дисклеймер ────────────────────────

def test_telegram_text_has_source_and_disclaimer():
    from reports import income_reports
    div = {"dividend_source": "api_trailing_12m",
           "trailing_12m_dividends_rub_per_share": Decimal("35"),
           "expected_annual_dividend_rub_per_share": Decimal("35"),
           "risk_notes": ["trailing_not_guaranteed"]}
    positions = [_pos("SBER", "share", 100, 30000, auto={"dividend": div})]
    s = compute_income(positions, {}, ENV, {})
    text = income_reports.build_summary_telegram(s)
    assert "source=api_trailing_12m" in text
    assert "confidence=estimated" in text
    assert "не гарантируют будущий доход" in text
    assert "Заявки не отправляются" in text


# ─── fetch_auto_income: единая точка, учитывает фиче-флаги ────────────────────

def test_fetch_auto_income_dispatches_by_type():
    client = FakeAPIClient(dividends=[
        {"paymentDate": "2026-07-10T00:00:00Z", "dividendNet": _q(35)}])
    auto = fetch_auto_income(client, source_type="dividend",
                             instrument_id="FIGI", env=ENV, now=NOW)
    assert auto["dividend"]["dividend_source"] == "api_known_future"
    assert auto["coupon"] is None and auto["mm"] is None


def test_fetch_auto_income_disabled_returns_empty():
    env = IncomeEnv(auto_fetch_enabled=False)
    client = FakeAPIClient(dividends=[
        {"paymentDate": "2026-07-10T00:00:00Z", "dividendNet": _q(35)}])
    auto = fetch_auto_income(client, source_type="dividend",
                             instrument_id="FIGI", env=env, now=NOW)
    assert auto == {"dividend": None, "coupon": None, "mm": None}


# ─── 10. Safety scan: нет торговых эндпоинтов в авто-источниках ───────────────

def test_no_order_endpoints_in_income_sources():
    # income-слой не должен содержать торговых эндпоинтов даже в виде литералов
    files = ["modules/income_sources.py", "modules/income_engine.py",
             "reports/income_reports.py"]
    forbidden = ("OrdersService", "postOrder", "cancelOrder", "place_order",
                 "submit_order", "place_limit_order", "order_client",
                 "LIVE_EXECUTION", "full_token")
    for f in files:
        src = Path(f).read_text(encoding="utf-8")
        for tok in forbidden:
            assert tok not in src, f"{f}: {tok}"


def test_income_api_methods_are_read_only():
    # новые методы фасада/REST — только чтение; никаких post/cancel-order
    import inspect

    from api import client as facade
    from brokers.tinkoff import rest_client as rc
    for mod in (facade, rc):
        src = inspect.getsource(mod)
        for tok in ("place_order", "submit_order", "place_limit_order",
                    "OrdersService", "order_client"):
            assert tok not in src, f"{mod.__name__}: {tok}"
    # запрошенные read-only методы реально добавлены
    for name in ("get_dividends", "get_bond_coupons", "get_accrued_interests",
                 "get_asset_fundamentals", "get_asset_reports"):
        assert hasattr(facade.ReadOnlyClient, name), name
        assert hasattr(rc.TinkoffReadOnlyClient, name), name
