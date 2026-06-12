"""Тесты read-only Instrument Scanner."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from brokers.tinkoff.rest_client import SECURITY_TRADING_STATUS_NORMAL
from modules.instrument_scanner import (
    InstrumentScanner,
    load_candidates,
    target_from_kval_plan,
)
from modules.instrument_scanner import Candidate
from tests.conftest import quotation

NORMAL = SECURITY_TRADING_STATUS_NORMAL


def _instr(ticker, uid):
    return {
        "figi": f"FG-{ticker}", "uid": uid, "name": ticker, "ticker": ticker,
        "instrumentType": "share", "classCode": "TQBR", "lot": 1, "currency": "rub",
    }


def _book(bid, ask, bid_qty, ask_qty):
    return {
        "bids": [{"price": quotation(str(bid)), "quantity": str(bid_qty)}],
        "asks": [{"price": quotation(str(ask)), "quantity": str(ask_qty)}],
    }


class FakeClient:
    def __init__(self, instruments, books, statuses=None, fail_find=None):
        self._instruments = instruments      # ticker -> instr | None
        self._books = books                  # uid -> book
        self._statuses = statuses or {}
        self._fail_find = set(fail_find or [])

    def find_instrument(self, ticker, class_code):
        if ticker in self._fail_find:
            raise RuntimeError("api down")
        return self._instruments.get(ticker)

    def get_trading_status(self, instrument_id):
        return {"tradingStatus": self._statuses.get(instrument_id, NORMAL)}

    def get_last_price(self, instrument_id):
        return None

    def get_order_book(self, instrument_id, depth):
        return self._books.get(instrument_id, {"bids": [], "asks": []})


def _scan(client, symbols, commission=Decimal("0"), target=Decimal("0"), filters=None):
    cands = [Candidate(ticker=t, class_code="TQBR") for t in symbols]
    return InstrumentScanner(client=client).scan(
        cands, as_of=date(2026, 7, 1), commission_bps=commission,
        target_monthly_turnover=target, filters=filters,
    )


def test_narrow_spread_good():
    client = FakeClient(
        {"TMON": _instr("TMON", "u-tmon")},
        {"u-tmon": _book("100.00", "100.02", 2000, 2000)},
    )
    rep = _scan(client, ["TMON"])
    r = rep.results[0]
    assert r.data_ok is True
    assert r.spread_ok and r.depth_ok and r.trading_status_ok
    assert r.suitable_for_turnover is True
    assert r.verdict == "GOOD"
    assert r.score >= 70


def test_wide_spread_not_good():
    client = FakeClient(
        {"WIDE": _instr("WIDE", "u-wide")},
        {"u-wide": _book("100.00", "110.00", 2000, 2000)},
    )
    r = _scan(client, ["WIDE"]).results[0]
    assert r.spread_ok is False
    assert r.suitable_for_turnover is False
    assert r.verdict in ("BAD", "WATCH")
    assert r.verdict != "GOOD"


def test_empty_book_no_data():
    client = FakeClient(
        {"EMPT": _instr("EMPT", "u-empt")},
        {"u-empt": {"bids": [], "asks": []}},
    )
    r = _scan(client, ["EMPT"]).results[0]
    assert r.data_ok is False
    assert r.verdict == "NO_DATA"
    assert r.score == 0


def test_one_failing_instrument_does_not_break_scan():
    client = FakeClient(
        {"OK": _instr("OK", "u-ok")},
        {"u-ok": _book("100.00", "100.02", 2000, 2000)},
        fail_find=["BAD"],
    )
    rep = _scan(client, ["BAD", "OK"])
    assert len(rep.results) == 2
    assert rep.results[0].verdict == "NO_DATA"
    assert rep.results[0].warnings
    assert rep.results[1].verdict == "GOOD"


def test_commission_increases_roundtrip():
    client = FakeClient(
        {"TMON": _instr("TMON", "u-tmon")},
        {"u-tmon": _book("100.00", "100.02", 2000, 2000)},
    )
    r0 = _scan(client, ["TMON"], commission=Decimal("0")).results[0]
    r5 = _scan(client, ["TMON"], commission=Decimal("5")).results[0]
    assert r5.estimated_roundtrip_cost_bps == r0.estimated_roundtrip_cost_bps + 10


def test_target_from_kval_plan(tmp_path):
    plan = {"monthly_plan": [
        {"month": "2026-07", "status": "future_required",
         "suggested_turnover": "508333.33"},
    ]}
    (tmp_path / "kval_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    assert target_from_kval_plan(tmp_path) == Decimal("508333.33")
    # отсутствие файла — не падаем
    assert target_from_kval_plan(tmp_path / "nope") is None


def test_reports_written_with_expected_columns(tmp_path):
    from reports import instrument_scan_reports
    client = FakeClient(
        {"TMON": _instr("TMON", "u-tmon")},
        {"u-tmon": _book("100.00", "100.02", 2000, 2000)},
    )
    rep = _scan(client, ["TMON"], commission=Decimal("5"), target=Decimal("508333"))
    written = instrument_scan_reports.write_all(rep, tmp_path)
    assert set(written) == {"instrument_scan.json", "instrument_scan.csv"}

    header = (tmp_path / "instrument_scan.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    for col in ("ticker", "figi", "spread_bps", "estimated_roundtrip_cost_bps",
                "estimated_monthly_cost_rub", "score", "verdict",
                "suitable_for_turnover", "warnings"):
        assert col in header

    data = json.loads((tmp_path / "instrument_scan.json").read_text(encoding="utf-8"))
    for key in ("generated_at_utc", "as_of", "commission_bps",
                "target_monthly_turnover", "filters", "candidates",
                "results", "warnings"):
        assert key in data


def test_load_candidates_from_symbols_and_yaml(tmp_path):
    cands = load_candidates("tmon, lqdt", class_code="TQBR")
    assert [c.ticker for c in cands] == ["TMON", "LQDT"]

    cfg = tmp_path / "instrument_candidates.yaml"
    cfg.write_text(
        "candidates:\n"
        "  - ticker: TMON\n    class_code: TQBR\n    note: \"x\"\n"
        "  - ticker: LQDT\n    class_code: TQBR\n",
        encoding="utf-8",
    )
    cands2 = load_candidates(None, config_path=cfg)
    assert [c.ticker for c in cands2] == ["TMON", "LQDT"]
    # отсутствие конфига → пустой список (CLI подскажет --symbols)
    assert load_candidates(None, config_path=tmp_path / "nope.yaml") == []


def test_scanner_is_read_only_no_orders():
    # В файлах сканера не должно быть никаких следов заявок
    for mod in ("modules/instrument_scanner.py",
                "reports/instrument_scan_reports.py",
                "reports/console_scan.py"):
        src = Path(mod).read_text(encoding="utf-8")
        assert "postOrder" not in src
        assert "cancelOrder" not in src
        assert "OrdersService" not in src
    # REST-клиент/фасад не обращаются к endpoint OrdersService
    for mod in ("brokers/tinkoff/rest_client.py", "api/client.py"):
        assert "OrdersService" not in Path(mod).read_text(encoding="utf-8")
