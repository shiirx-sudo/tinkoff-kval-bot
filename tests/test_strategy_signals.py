"""Тесты read-only стратегии trend_signal_v1. Никаких заявок."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from notifications import signals as sg
from strategies.trend_signal_v1 import NORMAL, SignalConfig, evaluate

CFG = SignalConfig()


def _candle(c, spread=1.0):
    return {"o": Decimal(str(c)), "h": Decimal(str(c + spread)),
            "l": Decimal(str(c - spread)), "c": Decimal(str(c)),
            "v": Decimal("100000")}


def _uptrend(n=252):
    # мягкий тренд + сильные колебания → бычий стек EMA и RSI в рабочем диапазоне
    out = []
    for i in range(n):
        c = 100 + i * 0.15 + math.sin(i / 5.0) * 6.0
        out.append(_candle(c))
    return out


def _downtrend(n=260):
    out = []
    for i in range(n):
        c = 200 - i * 0.35 + math.sin(i / 6.0) * 3.0
        out.append(_candle(c))
    return out


def _meta(ticker="SBER", spread_bps=3.0, liq="50000000", status=NORMAL):
    return {"ticker": ticker, "class_code": "SPBRU", "spread_bps": spread_bps,
            "liquidity_value_rub": liq, "trading_status": status}


def test_buy_signal():
    sig = evaluate(_uptrend(), _meta(), CFG)
    assert sig.action == "BUY"
    assert sig.score >= CFG.min_score
    assert Decimal("45") <= sig.rsi <= Decimal("70")
    assert sig.entry and sig.stop and sig.take_profit
    assert sig.stop < sig.entry < sig.take_profit
    assert "close > EMA200" in sig.reasons


def test_sell_signal():
    sig = evaluate(_downtrend(), _meta(), CFG)
    assert sig.action == "SELL"
    assert any("EMA50" in r for r in sig.reasons)


def test_skip_few_candles():
    sig = evaluate([_candle(100) for _ in range(10)], _meta(), CFG)
    assert sig.action == "SKIP"
    assert any("мало истории" in r for r in sig.blocked_reasons)


def test_skip_not_trading():
    sig = evaluate(_uptrend(), _meta(status="SECURITY_TRADING_STATUS_CLOSE"), CFG)
    assert sig.action == "SKIP"


def test_skip_wide_spread():
    sig = evaluate(_uptrend(), _meta(spread_bps=50.0), CFG)
    assert sig.action == "SKIP"
    assert any("spread" in r for r in sig.blocked_reasons)


def test_dedup_suppresses_repeat():
    sig = evaluate(_uptrend(), _meta(), CFG)
    assert sig.action == "BUY"
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
    state = {}
    send, _ = sg.should_notify(sig, state, now, dedup_hours=12)
    assert send is True                     # первый раз
    sg.update_state(state, sig, now)
    send2, reason2 = sg.should_notify(sig, state, now + timedelta(hours=2), dedup_hours=12)
    assert send2 is False and reason2 == "dedup_suppressed"
    # окно прошло → снова можно
    send3, _ = sg.should_notify(sig, state, now + timedelta(hours=13), dedup_hours=12)
    assert send3 is True


def test_dedup_action_change_sends():
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
    buy = evaluate(_uptrend(), _meta(), CFG)
    state = {}
    sg.update_state(state, buy, now)
    sell = evaluate(_downtrend(), _meta(), CFG)
    send, reason = sg.should_notify(sell, state, now + timedelta(hours=1), dedup_hours=12)
    assert send is True and reason == "action_changed"


def test_hold_and_skip_not_notified():
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
    skip = evaluate([_candle(100) for _ in range(10)], _meta(), CFG)
    assert sg.should_notify(skip, {}, now, dedup_hours=12)[0] is False


def test_buy_message_format():
    sig = evaluate(_uptrend(), _meta(), CFG)
    text = sg.build_signal_message(sig)
    assert "SIGNAL: BUY" in text
    assert "SIGNAL_ONLY / READ_ONLY" in text
    assert "Заявки не отправляются." in text


def test_no_order_endpoints_in_signal_sources():
    files = ["strategies/trend_signal_v1.py", "modules/strategy_signals.py",
             "notifications/signals.py", "reports/strategy_signals_reports.py"]
    for f in files:
        src = Path(f).read_text(encoding="utf-8")
        for forbidden in ("OrdersService", "postOrder", "cancelOrder", "place_order",
                          "submit_order", "place_limit_order", "order_client",
                          "LIVE_EXECUTION", "TINKOFF_TOKEN", "full_token"):
            assert forbidden not in src, f"{f}: {forbidden}"
