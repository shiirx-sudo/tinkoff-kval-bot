"""Тесты read-only income-watchlist (доходность по текущей цене). Никаких заявок."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from modules.fundamental_filter import FundamentalResult
from modules.income_engine import (
    IncomeEnv,
    build_watchlist,
    compute_watchlist_item,
    fetch_current_price,
)
from reports import income_reports
from reports.income_reports import WATCHLIST_COLUMNS

CONFIG = {
    "manual_yields": {"LQDT": {"class_code": "TQTF", "type": "money_market_fund",
                               "expected_annual_yield_pct": 14.0}},
    "manual_dividends": {"SBER": {"class_code": "TQBR",
                                  "expected_annual_dividend_rub_per_share": 35,
                                  "confidence": "medium"}},
}
ENV = IncomeEnv(target_monthly_rub=Decimal("100000"), tax_rate_pct=Decimal("13"))
FUND = {"GAZP": {"class_code": "TQBR", "management_alignment": "negative",
                 "cash_return": "weak", "state_role": "negative", "market_growth": "weak"}}


def _q(value) -> dict:
    d = Decimal(str(value))
    units = int(d)
    nano = int((d - units) * Decimal("1000000000"))
    return {"units": str(units), "nano": nano}


def _meta(ticker, cls, itype, figi="F", uid="U", name=""):
    return {"class_code": cls, "figi": figi, "instrument_uid": uid,
            "instrument_name": name or ticker, "instrument_type": itype}


def _fr(verdict, state_role=""):
    return FundamentalResult(ticker="X", verdict=verdict, state_role=state_role)


# ─── расчёт по одному инструменту ────────────────────────────────────────────

def test_dividend_watchlist_with_price():
    it = compute_watchlist_item(
        "SBER", "TQBR", _meta("SBER", "TQBR", "share"),
        Decimal("319.21"), "last_price", CONFIG, ENV, _fr("quality_pass"))
    assert it.source_type == "dividend"
    assert it.gross_yield_pct == Decimal("35") / Decimal("319.21") * Decimal("100")
    assert it.net_yield_pct is not None and it.net_yield_pct < it.gross_yield_pct
    assert it.confidence == "medium"
    assert it.income_verdict == "income_watch"
    assert "price_unknown" not in it.risk_notes


def test_dividend_watchlist_without_price():
    it = compute_watchlist_item(
        "SBER", "TQBR", _meta("SBER", "TQBR", "share"),
        None, "price_unknown", CONFIG, ENV, _fr("quality_pass"))
    assert it.gross_yield_pct is None
    assert "price_unknown" in it.risk_notes
    assert it.income_verdict == "income_unknown"


def test_money_market_manual_yield_not_unknown():
    it = compute_watchlist_item(
        "LQDT", "TQTF", _meta("LQDT", "TQTF", "etf"),
        Decimal("2.01"), "last_price", CONFIG, ENV, _fr("quality_unknown"))
    assert it.source_type == "money_market"
    assert it.expected_annual_yield_pct == Decimal("14.0")
    assert it.confidence == "manual"
    assert it.income_verdict != "income_unknown"
    assert it.income_verdict == "income_watch"


def test_money_market_quality_unknown_does_not_force_unknown():
    # quality_unknown по фундаменталу не должен превращать MM в income_unknown
    it = compute_watchlist_item(
        "LQDT", "TQTF", _meta("LQDT", "TQTF", "etf"),
        None, "price_unknown", CONFIG, ENV, _fr("quality_unknown"))
    # у MM доходность задана manual, цена не нужна
    assert it.income_verdict == "income_watch"


def test_quality_risk_with_known_yield_is_risk():
    it = compute_watchlist_item(
        "SBER", "TQBR", _meta("SBER", "TQBR", "share"),
        Decimal("300"), "last_price", CONFIG, ENV, _fr("quality_risk", "negative"))
    assert it.gross_yield_pct is not None
    assert it.income_verdict == "income_risk"


def test_no_income_data_is_unknown():
    it = compute_watchlist_item(
        "GAZP", "TQBR", _meta("GAZP", "TQBR", "share"),
        Decimal("110.05"), "last_price", CONFIG, ENV, _fr("quality_risk"))
    assert it.gross_yield_pct is None
    assert it.income_verdict == "income_unknown"
    assert it.confidence == "unknown"


# ─── текущая цена: last → стакан → свеча → unknown ───────────────────────────

class _PriceClient:
    def __init__(self, last=None, book=None, candle=None):
        self._last, self._book, self._candle = last, book, candle

    def get_last_price(self, instrument_id):
        return {"price": _q(self._last)} if self._last is not None else None

    def get_order_book(self, instrument_id, depth=1):
        if self._book is None:
            return {"bids": [], "asks": []}
        bid, ask = self._book
        return {"bids": [{"price": _q(bid)}], "asks": [{"price": _q(ask)}]}

    def get_candles(self, instrument_id, frm, to, interval="CANDLE_INTERVAL_DAY"):
        if self._candle is None:
            return {"candles": []}
        return {"candles": [{"close": _q(self._candle)}]}


def test_price_prefers_last():
    p, src = fetch_current_price(_PriceClient(last="319.21"), "F")
    assert p == Decimal("319.21") and src == "last_price"


def test_price_falls_back_to_orderbook_mid():
    p, src = fetch_current_price(_PriceClient(book=("100", "102")), "F")
    assert p == Decimal("101") and src == "orderbook_mid"


def test_price_falls_back_to_candle_close():
    p, src = fetch_current_price(_PriceClient(candle="55.5"), "F")
    assert p == Decimal("55.5") and src == "candle_close"


def test_price_unknown_when_nothing():
    p, src = fetch_current_price(_PriceClient(), "F")
    assert p is None and src == "price_unknown"


def test_price_unknown_when_no_instrument_id():
    p, src = fetch_current_price(_PriceClient(last="1"), "")
    assert p is None and src == "price_unknown"


# ─── оркестрация build_watchlist с фейковым read-only клиентом ────────────────

class _FakeClient:
    CANDS = {
        "SBER": [{"ticker": "SBER", "classCode": "TQBR", "figi": "FSBER",
                  "uid": "USBER", "name": "Сбербанк", "instrumentType": "share"}],
        "LQDT": [{"ticker": "LQDT", "classCode": "TQTF", "figi": "FLQDT",
                  "uid": "ULQDT", "name": "Ликвидность", "instrumentType": "etf"}],
        "GAZP": [{"ticker": "GAZP", "classCode": "TQBR", "figi": "FGAZP",
                  "uid": "UGAZP", "name": "Газпром", "instrumentType": "share"}],
    }
    PRICES = {"FSBER": "319.21", "FLQDT": "2.01", "FGAZP": "110.05"}

    def find_instruments(self, query):
        return self.CANDS.get(query.upper(), [])

    def get_last_price(self, instrument_id):
        p = self.PRICES.get(instrument_id)
        return {"price": _q(p)} if p else None

    def get_order_book(self, instrument_id, depth=1):
        return {"bids": [], "asks": []}

    def get_candles(self, instrument_id, frm, to, interval="CANDLE_INTERVAL_DAY"):
        return {"candles": []}


def test_build_watchlist_orchestration():
    items = build_watchlist(_FakeClient(),
                            ["TQBR:SBER", "TQTF:LQDT", "TQBR:GAZP"],
                            CONFIG, ENV, FUND)
    by = {it.ticker: it for it in items}
    assert by["SBER"].current_price == Decimal("319.21")
    assert by["SBER"].price_source == "last_price"
    assert by["SBER"].income_verdict == "income_watch"          # yield известен, conf medium
    assert by["LQDT"].income_verdict == "income_watch"          # MM manual
    assert by["GAZP"].income_verdict == "income_unknown"        # нет дивиденда в YAML
    # больше нет «yield=n/a -> income_candidate»
    for it in items:
        if it.income_verdict == "income_candidate":
            assert it.gross_yield_pct is not None or it.expected_annual_yield_pct is not None


def test_build_watchlist_no_instrument_match():
    items = build_watchlist(_FakeClient(), ["TQBR:NOPE"], CONFIG, ENV, {})
    assert items[0].income_verdict == "income_unknown"
    assert items[0].current_price is None


# ─── отчёты ──────────────────────────────────────────────────────────────────

def test_watchlist_reports_generated(tmp_path):
    items = build_watchlist(_FakeClient(), ["TQBR:SBER", "TQTF:LQDT"], CONFIG, ENV, FUND)
    income_reports.write_watchlist(items, tmp_path)
    header = (tmp_path / "income_watchlist.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    for col in WATCHLIST_COLUMNS:
        assert col in header
    assert (tmp_path / "income_watchlist.md").read_text(
        encoding="utf-8").startswith("# Income watchlist — READ ONLY")
    assert (tmp_path / "income_watchlist.json").exists()


def test_watchlist_cli_line_no_na_candidate():
    it = compute_watchlist_item(
        "SBER", "TQBR", _meta("SBER", "TQBR", "share"),
        Decimal("319.21"), "last_price", CONFIG, ENV, _fr("quality_pass"))
    line = income_reports.render_watchlist_line(it)
    assert "income_watch" in line
    assert "div=35 ₽" in line
    assert "yield=n/a" not in line


# ─── safety scan ─────────────────────────────────────────────────────────────

def test_no_order_endpoints_in_watchlist_sources():
    files = ["modules/income_engine.py", "reports/income_reports.py"]
    for f in files:
        src = Path(f).read_text(encoding="utf-8")
        for forbidden in ("OrdersService", "postOrder", "cancelOrder", "place_order",
                          "submit_order", "place_limit_order", "order_client",
                          "LIVE_EXECUTION", "full_token"):
            assert forbidden not in src, f"{f}: {forbidden}"
