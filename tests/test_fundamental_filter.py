"""Тесты read-only фундаментального фильтра. Никаких заявок."""
from __future__ import annotations

from pathlib import Path

from modules.fundamental_filter import (
    apply_to_signal,
    evaluate_fundamental,
    load_fundamental_filter,
)
from strategies.trend_signal_v1 import Signal

DATA = {
    "AAA": {"class_code": "TQBR", "management_alignment": "positive",
            "cash_return": "positive", "state_role": "positive",
            "market_growth": "neutral", "notes": ["сильный менеджмент"]},
    "BBB": {"class_code": "TQBR", "management_alignment": "positive",
            "cash_return": "neutral", "state_role": "mixed",
            "market_growth": "neutral"},
    "CCC": {"class_code": "TQBR", "management_alignment": "negative",
            "cash_return": "weak", "state_role": "negative",
            "market_growth": "weak", "notes": ["государственный контроль"]},
}


def test_score_pass():
    r = evaluate_fundamental("AAA", "TQBR", DATA)
    # 1 + 1 + 1 + 0.5 = 3.5
    assert r.score_0_4 == 3.5
    assert r.verdict == "quality_pass"


def test_score_watch():
    r = evaluate_fundamental("BBB", "TQBR", DATA)
    # 1 + 0.5 + 0.5 + 0.5 = 2.5
    assert r.score_0_4 == 2.5
    assert r.verdict == "quality_watch"


def test_score_risk():
    r = evaluate_fundamental("CCC", "TQBR", DATA)
    # 0 + 0 + 0 + 0 = 0
    assert r.score_0_4 == 0.0
    assert r.verdict == "quality_risk"
    assert any("государственный контроль" in x for x in r.reasons)


def test_no_data_unknown():
    r = evaluate_fundamental("ZZZ", "TQBR", DATA)
    assert r.verdict == "quality_unknown"
    assert r.score_0_4 is None


def test_class_code_mismatch_unknown():
    r = evaluate_fundamental("AAA", "SPBRU", DATA)
    assert r.verdict == "quality_unknown"


def test_buy_blocked_when_require_pass_and_risk():
    sig = Signal(ticker="CCC", class_code="TQBR", action="BUY", raw_action="BUY")
    r = evaluate_fundamental("CCC", "TQBR", DATA)
    apply_to_signal(sig, r, require_pass=True)
    assert sig.raw_action == "BUY"
    assert sig.action == "HOLD"
    assert "fundamental_quality_risk" in sig.blocked_reasons


def test_buy_kept_when_not_require_pass():
    sig = Signal(ticker="CCC", class_code="TQBR", action="BUY", raw_action="BUY")
    r = evaluate_fundamental("CCC", "TQBR", DATA)
    apply_to_signal(sig, r, require_pass=False)
    assert sig.action == "BUY"
    assert sig.fundamental_verdict == "quality_risk"


def test_sell_held_keeps_sell_with_fundamental_risk():
    sig = Signal(ticker="CCC", class_code="TQBR", action="SELL", raw_action="SELL",
                 held=True)
    r = evaluate_fundamental("CCC", "TQBR", DATA)
    apply_to_signal(sig, r, require_pass=True)
    assert sig.action == "SELL"                 # held SELL не отменяется фундаменталом
    assert sig.fundamental_verdict == "quality_risk"


def test_sell_message_shows_fundamental_risk():
    from notifications import signals as sg
    sig = Signal(ticker="CCC", class_code="TQBR", action="SELL", raw_action="SELL",
                 held=True)
    apply_to_signal(sig, evaluate_fundamental("CCC", "TQBR", DATA), require_pass=True)
    text = sg.build_signal_message(sig)
    assert "SELL / EXIT WATCH" in text
    assert "Фундаментальный риск" in text


def test_reports_have_fundamental_fields(tmp_path):
    from reports import strategy_signals_reports as rep
    sig = Signal(ticker="AAA", class_code="TQBR", action="HOLD", raw_action="HOLD")
    apply_to_signal(sig, evaluate_fundamental("AAA", "TQBR", DATA), require_pass=False)
    rep.write_all([sig], "trend_signal_v1", tmp_path)
    header = (tmp_path / "strategy_signals.csv").read_text(
        encoding="utf-8-sig").splitlines()[0].split(";")
    for col in ("fundamental_score", "fundamental_verdict", "management_alignment",
                "cash_return", "state_role", "market_growth", "fundamental_reasons"):
        assert col in header


def test_example_yaml_loads():
    data = load_fundamental_filter("config/fundamental_filter.example.yaml")
    assert "SBER" in data and "GAZP" in data


def test_no_order_endpoints_in_fundamental_sources():
    files = ["modules/fundamental_filter.py", "config/fundamental_filter.example.yaml"]
    for f in files:
        src = Path(f).read_text(encoding="utf-8")
        for forbidden in ("OrdersService", "postOrder", "cancelOrder", "place_order",
                          "submit_order", "place_limit_order", "order_client",
                          "LIVE_EXECUTION", "full_token"):
            assert forbidden not in src, f"{f}: {forbidden}"
