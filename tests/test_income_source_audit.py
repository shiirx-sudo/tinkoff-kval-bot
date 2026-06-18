"""
Тесты read-only income-source-audit: классификация сырых событий API в бакеты
и генерация отчётов. Никаких заявок: только чтение.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from modules.income_audit import AuditItem, audit_one, build_audit
from modules.income_engine import IncomeEnv

NOW = datetime(2026, 6, 18, tzinfo=timezone.utc)
ENV = IncomeEnv(horizon_months=12, dividend_lookback_months=24,
                dividend_trailing_months=12, mm_trailing_days=30)


def _q(value, currency="rub") -> dict:
    d = Decimal(str(value))
    units = int(d)
    nano = int((d - units) * Decimal("1000000000"))
    return {"units": str(units), "nano": nano, "currency": currency}


def _meta(ticker, itype, cls="TQBR", figi="F", uid="U", price=Decimal("300")):
    return {"ticker": ticker, "class_code": cls, "figi": figi, "instrument_uid": uid,
            "instrument_name": ticker, "instrument_type": itype,
            "current_price": price, "price_source": "last_price"}


class FakeAuditClient:
    def __init__(self, dividends=None, coupons=None, accrued=None, candles=None,
                 cands=None, prices=None):
        self._dividends = dividends or []
        self._coupons = coupons or []
        self._accrued = accrued or []
        self._candles = candles or []
        self._cands = cands or {}
        self._prices = prices or {}

    def get_dividends(self, iid, frm, to):
        return list(self._dividends)

    def get_bond_coupons(self, iid, frm, to):
        return list(self._coupons)

    def get_accrued_interests(self, iid, frm, to):
        return list(self._accrued)

    def get_candles(self, iid, frm, to, interval="CANDLE_INTERVAL_DAY"):
        return {"candles": list(self._candles)}

    def find_instruments(self, query):
        return self._cands.get(query.upper(), [])

    def get_last_price(self, iid):
        p = self._prices.get(iid)
        return {"price": _q(p)} if p else None

    def get_order_book(self, iid, depth=1):
        return {"bids": [], "asks": []}


# ─── 1. Дивиденд классифицирован как future_known ────────────────────────────

def test_dividend_future_known_bucket():
    client = FakeAuditClient(dividends=[
        {"paymentDate": "2026-07-10T00:00:00Z", "recordDate": "2026-07-05T00:00:00Z",
         "lastBuyDate": "2026-07-03T00:00:00Z", "dividendNet": _q(35)},
    ])
    it = audit_one(client, _meta("SBER", "share"), {}, ENV, origin="watchlist", now=NOW)
    assert it.source_type == "dividend"
    assert it.income_data_source == "api_known_future"
    assert len(it.dividend_events) == 1
    ev = it.dividend_events[0]
    assert ev.source_bucket == "future_known"
    assert ev.payment_date == "2026-07-10"
    assert ev.last_buy_date == "2026-07-03"
    assert it.known_future_dividends_rub_per_share == Decimal("35")


# ─── 2. Дивиденд классифицирован как trailing_12m ────────────────────────────

def test_dividend_trailing_12m_bucket():
    client = FakeAuditClient(dividends=[
        {"paymentDate": "2025-08-15T00:00:00Z", "dividendNet": _q(30)},
    ])
    it = audit_one(client, _meta("LKOH", "share"), {}, ENV, origin="watchlist", now=NOW)
    assert it.income_data_source == "api_trailing_12m"
    assert it.dividend_events[0].source_bucket == "trailing_12m"
    assert it.trailing_12m_dividends_rub_per_share == Decimal("30")
    assert "trailing_not_guaranteed" in it.risk_notes


# ─── 3. Старый дивиденд: виден как older_lookback, но НЕ в оценке ─────────────

def test_older_dividend_visible_but_not_in_estimate():
    client = FakeAuditClient(dividends=[
        {"paymentDate": "2024-12-15T00:00:00Z", "dividendNet": _q(50)},
    ])
    it = audit_one(client, _meta("NVTK", "share"), {}, ENV, origin="watchlist", now=NOW)
    # событие видно
    assert len(it.dividend_events) == 1
    assert it.dividend_events[0].source_bucket == "older_lookback"
    # но в оценку не попало: нет ни future, ни trailing
    assert it.known_future_dividends_rub_per_share is None
    assert it.trailing_12m_dividends_rub_per_share is None
    assert it.income_data_source == "unknown"


def test_mixed_dividends_only_trailing_used():
    client = FakeAuditClient(dividends=[
        {"paymentDate": "2025-08-15T00:00:00Z", "dividendNet": _q(30)},  # trailing
        {"paymentDate": "2024-12-15T00:00:00Z", "dividendNet": _q(50)},  # older
    ])
    it = audit_one(client, _meta("NVTK", "share"), {}, ENV, origin="watchlist", now=NOW)
    buckets = {e.payment_date: e.source_bucket for e in it.dividend_events}
    assert buckets["2025-08-15"] == "trailing_12m"
    assert buckets["2024-12-15"] == "older_lookback"
    # старый (50) не учтён: trailing-оценка = только 30
    assert it.trailing_12m_dividends_rub_per_share == Decimal("30")


# ─── 4. Купонное событие в горизонте видно в аудите ──────────────────────────

def test_bond_coupon_within_horizon_in_audit():
    client = FakeAuditClient(
        coupons=[
            {"couponDate": "2026-09-15T00:00:00Z", "payOneBond": _q(40),
             "couponNumber": "5", "couponPeriod": 182, "couponType": "COUPON_TYPE_CONSTANT"},
        ],
        accrued=[{"date": "2026-06-17T00:00:00Z", "value": _q("12.5")}],
    )
    it = audit_one(client, _meta("RU000TEST", "bond", cls="TQCB"), {}, ENV,
                   origin="portfolio", now=NOW)
    assert it.source_type == "coupon"
    assert it.income_data_source == "api_coupon_schedule"
    assert len(it.coupon_events) == 1
    ev = it.coupon_events[0]
    assert ev.source_bucket == "within_horizon"
    assert ev.coupon_number == "5"
    assert it.next_coupon_date == "2026-09-15"
    assert it.accrued_interest == Decimal("12.5")


# ─── 5. Денежный рынок: базис свечей со всеми полями ─────────────────────────

def test_money_market_candle_basis():
    client = FakeAuditClient(candles=[
        {"time": "2026-05-19T00:00:00Z", "close": _q("100")},
        {"time": "2026-06-18T00:00:00Z", "close": _q("101")},
    ])
    it = audit_one(client, _meta("LQDT", "etf", cls="TQTF"), {}, ENV,
                   origin="watchlist", now=NOW)
    assert it.source_type == "money_market"
    cb = it.candle_basis
    assert cb is not None
    assert cb.start_date == "2026-05-19" and cb.start_close == Decimal("100")
    assert cb.end_date == "2026-06-18" and cb.end_close == Decimal("101")
    assert cb.span_days == 30
    assert cb.growth_pct == Decimal("1")            # (101/100 - 1)*100
    assert cb.annualized_yield_pct is not None and cb.annualized_yield_pct > 0
    assert it.income_data_source == "trailing_30d"


def test_money_market_manual_override_flag():
    config = {"manual_yields": {"LQDT": {"expected_annual_yield_pct": 14.0}}}
    client = FakeAuditClient(candles=[
        {"time": "2026-05-19T00:00:00Z", "close": _q("100")},
        {"time": "2026-06-18T00:00:00Z", "close": _q("101")},
    ])
    it = audit_one(client, _meta("LQDT", "etf", cls="TQTF"), config, ENV,
                   origin="watchlist", now=NOW)
    assert it.manual_override_active is True
    assert it.income_data_source == "manual_override"
    # сырые свечи всё равно собраны для проверки
    assert it.candle_basis is not None


# ─── 6. Отчёты генерируются ──────────────────────────────────────────────────

def test_audit_reports_generated(tmp_path):
    from reports import income_audit_reports as rep
    client = FakeAuditClient(dividends=[
        {"paymentDate": "2026-07-10T00:00:00Z", "dividendNet": _q(35)},
        {"paymentDate": "2024-12-15T00:00:00Z", "dividendNet": _q(50)},
    ])
    items = [audit_one(client, _meta("SBER", "share"), {}, ENV,
                       origin="watchlist", now=NOW)]
    rep.write_audit(items, tmp_path)
    assert (tmp_path / "income_source_audit.json").exists()
    assert (tmp_path / "income_source_audit.csv").exists()
    assert (tmp_path / "income_source_audit.md").exists()

    header = (tmp_path / "income_source_audit.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    for col in ("ticker", "kind", "source_bucket", "income_data_source", "confidence"):
        assert col in header, col
    # есть событийные строки с бакетами
    csv_text = (tmp_path / "income_source_audit.csv").read_text(encoding="utf-8-sig")
    assert "future_known" in csv_text and "older_lookback" in csv_text

    md = (tmp_path / "income_source_audit.md").read_text(encoding="utf-8")
    assert md.startswith("# Income source audit — READ ONLY")
    assert "не гарантируют будущий доход" in md
    assert "Заявки не отправляются" in md


# ─── оркестрация build_audit (watchlist) ─────────────────────────────────────

def test_build_audit_watchlist_orchestration():
    client = FakeAuditClient(
        dividends=[{"paymentDate": "2026-07-10T00:00:00Z", "dividendNet": _q(35)}],
        cands={"SBER": [{"ticker": "SBER", "classCode": "TQBR", "figi": "FSBER",
                         "uid": "USBER", "name": "Сбербанк", "instrumentType": "share"}]},
        prices={"FSBER": "319.21"},
    )
    items = build_audit(client, raw_items=["TQBR:SBER"], config={}, env=ENV, now=NOW)
    assert len(items) == 1
    assert items[0].ticker == "SBER"
    assert items[0].origin == "watchlist"
    assert items[0].income_data_source == "api_known_future"
    assert items[0].current_price == Decimal("319.21")


def test_build_audit_requires_some_input():
    # build_audit без входа возвращает пустой список (без падения)
    assert build_audit(FakeAuditClient(), config={}, env=ENV, now=NOW) == []


# ─── 7. Safety scan ──────────────────────────────────────────────────────────

def test_no_order_endpoints_in_audit_sources():
    files = ["modules/income_audit.py", "reports/income_audit_reports.py"]
    forbidden = ("OrdersService", "postOrder", "cancelOrder", "place_order",
                 "submit_order", "place_limit_order", "order_client",
                 "LIVE_EXECUTION", "full_token")
    for f in files:
        src = Path(f).read_text(encoding="utf-8")
        for tok in forbidden:
            assert tok not in src, f"{f}: {tok}"


def test_audit_item_dataclass_fields():
    # контракт полей по акциям/облигациям/фондам присутствует
    it = AuditItem(ticker="X")
    for field_name in ("income_data_source", "dividend_source", "coupon_source",
                       "yield_source", "known_future_dividends_rub_per_share",
                       "trailing_12m_dividends_rub_per_share", "next_coupon_date",
                       "coupon_frequency_per_year", "accrued_interest",
                       "expected_annual_yield_pct", "dividend_events",
                       "coupon_events", "candle_basis"):
        assert hasattr(it, field_name), field_name
