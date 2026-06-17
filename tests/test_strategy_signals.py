"""Тесты read-only стратегии trend_signal_v1. Никаких заявок."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from notifications import signals as sg
from strategies.trend_signal_v1 import (
    NORMAL,
    SignalConfig,
    evaluate,
    parse_watchlist_item,
    resolve_instrument,
)

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


# ─── resolver / class_code (focused fix) ─────────────────────────────────────

PRIORITY = ["TQBR", "TQTF", "SPBRU"]


def test_watchlist_parser():
    assert parse_watchlist_item("TQBR:SBER") == ("SBER", "TQBR")
    assert parse_watchlist_item("SBER@TQBR") == ("SBER", "TQBR")
    assert parse_watchlist_item("SBER") == ("SBER", None)
    assert parse_watchlist_item(" tqbr:sber ") == ("SBER", "TQBR")


def test_resolver_priority_picks_tqbr():
    cands = [{"ticker": "SBER", "classCode": "37M", "figi": "F37"},
             {"ticker": "SBER", "classCode": "TQBR", "figi": "FTQ"}]
    chosen, by, classes = resolve_instrument(cands, "SBER", None, PRIORITY)
    assert chosen["class_code"] == "TQBR"
    assert chosen["figi"] == "FTQ"
    assert by == "priority_fallback"
    assert set(classes) == {"37M", "TQBR"}


def test_resolver_explicit_class():
    cands = [{"ticker": "SBER", "classCode": "37M", "figi": "F37"},
             {"ticker": "SBER", "classCode": "TQBR", "figi": "FTQ"}]
    chosen, by, _ = resolve_instrument(cands, "SBER", "TQBR", PRIORITY)
    assert chosen["class_code"] == "TQBR" and by == "explicit_class_code"
    # явный, которого нет → no_allowed_match
    chosen2, by2, _ = resolve_instrument(cands, "SBER", "SPBRU", PRIORITY)
    assert chosen2 is None and by2 == "no_allowed_match"


def test_resolver_no_allowed_class():
    cands = [{"ticker": "SBER", "classCode": "37M", "figi": "F37"}]
    chosen, by, classes = resolve_instrument(cands, "SBER", None, PRIORITY)
    assert chosen is None
    assert by == "no_allowed_match"
    assert classes == ["37M"]


def test_sell_message_exit_watch():
    sig = evaluate(_downtrend(), _meta(), CFG)
    assert sig.action == "SELL"
    text = sg.build_signal_message(sig)
    assert "SELL / EXIT WATCH" in text
    assert "не команда открывать шорт" in text
    assert "выход/снижение риска" in text


def test_telegram_only_called_with_notify(monkeypatch, tmp_path):
    import types
    import main as m
    from notifications import telegram as tgmod

    buy = evaluate(_uptrend(), _meta(), CFG)
    skip = evaluate([_candle(100) for _ in range(5)], _meta(), CFG)

    monkeypatch.setattr("api.client.ReadOnlyClient", lambda *a, **k: object())
    monkeypatch.setattr("modules.strategy_signals.scan", lambda *a, **k: [buy, skip])

    calls = []
    monkeypatch.setattr(tgmod, "send_telegram_message",
                        lambda *a, **k: calls.append(1) or {"sent": False})
    monkeypatch.chdir(tmp_path)

    def _args(notify):
        return types.SimpleNamespace(
            strategy="trend_signal_v1", watchlist=None, min_score=None,
            notify=notify, as_of=None, timeframe=None, max_signals=None)

    m.cmd_strategy_scan(_args(False))
    assert calls == []                      # без --notify Telegram не трогаем
    m.cmd_strategy_scan(_args(True))
    assert len(calls) == 1                  # только BUY (SKIP не шлём)
