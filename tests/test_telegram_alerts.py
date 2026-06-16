"""Тесты read-only Telegram-уведомлений. Реальный Telegram не вызывается."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from notifications import telegram as tg

NORMAL = "SECURITY_TRADING_STATUS_NORMAL_TRADING"
TOKEN = "123456:SECRET-TOKEN-DO-NOT-LOG"


def _ready_data():
    return {
        "status": "READY_DRY_RUN", "ticker": "LQDT", "verdict": "GOOD",
        "trading_status": NORMAL, "class_code": "SPBRU", "period": "2026-07",
        "check_date": "2027-07-01", "spread": "0.50",
        "side_notional": "127083.33", "broker_trade_count_missing": 4,
        "roundtrip_cycle_count_required": 2, "warnings": [],
    }


def _now():
    return datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)


# 1
def test_build_summary_ready():
    text = tg.build_summary_message(_ready_data(), today=date(2026, 6, 16))
    assert "T-Invest Kval Monitor" in text
    assert "🟢 READY_DRY_RUN" in text
    assert "LQDT: GOOD / NORMAL_TRADING" in text
    assert "Реальных заявок нет." in text


# 2
def test_build_summary_blocked_has_warning():
    data = _ready_data()
    data["status"] = "BLOCKED"
    data["warnings"] = ["side_within_max: side_notional=127083.33, max=100000"]
    text = tg.build_summary_message(data, today=date(2026, 6, 16))
    assert "🔴 BLOCKED" in text
    assert "⚠️ Причины:" in text
    assert "max=100000" in text


# 3
def test_read_reports_missing(tmp_path):
    data = tg.read_reports(tmp_path)
    assert data["status"] == "MISSING_REPORTS"
    text = tg.build_summary_message(data, today=date(2026, 6, 16))
    assert "MISSING_REPORTS" in text


# 4
def test_status_change_sends_immediately():
    state = dict(tg._DEFAULT_STATE)
    state["last_status"] = "READY_DRY_RUN"
    send, reason = tg.should_send_alert(
        "BLOCKED", state, _now(), min_interval_minutes=60, daily_summary_enabled=True)
    assert send is True and reason == "status_change"


# 5
def test_same_status_no_spam():
    now = _now()
    state = dict(tg._DEFAULT_STATE)
    state["last_status"] = "BLOCKED"
    state["last_sent_at_utc"] = (now - timedelta(minutes=5)).isoformat()
    send, reason = tg.should_send_alert(
        "BLOCKED", state, now, min_interval_minutes=60, daily_summary_enabled=True)
    assert send is False and reason == "antispam"


# 6
def test_daily_summary_once_per_day():
    now = _now()
    state = dict(tg._DEFAULT_STATE)
    state["last_status"] = "READY_DRY_RUN"
    # ещё не слали сегодня → шлём
    send, reason = tg.should_send_alert(
        "READY_DRY_RUN", state, now, min_interval_minutes=60, daily_summary_enabled=True)
    assert send is True and reason == "daily_summary"
    # уже слали сегодня → не шлём
    state["last_daily_summary_date"] = now.date().isoformat()
    send2, reason2 = tg.should_send_alert(
        "READY_DRY_RUN", state, now, min_interval_minutes=60, daily_summary_enabled=True)
    assert send2 is False and reason2 == "already_sent_today"


# 7
def test_month_deadline_alert():
    # 2026-07-29 → до конца месяца 2 дня → bucket 3
    bucket, key = tg.month_deadline_alert(date(2026, 7, 29))
    assert bucket == 3
    assert key.startswith("2026-07-31:")
    # далеко от конца месяца → нет алерта
    assert tg.month_deadline_alert(date(2026, 7, 1))[0] is None


# 8
def test_quarter_deadline_alert():
    # 2026-09-25 → конец Q3 2026-09-30, 5 дней → bucket 7
    bucket, key = tg.quarter_deadline_alert(date(2026, 9, 25))
    assert bucket == 7
    assert key.startswith("2026-09-30:")
    assert tg.quarter_deadline_alert(date(2026, 7, 1))[0] is None


# 9
def test_token_not_in_message_or_result(monkeypatch, tmp_path):
    text = tg.build_summary_message(_ready_data(), today=date(2026, 6, 16))
    assert TOKEN not in text
    result = tg.send_telegram_message(TOKEN, "999", text, enabled=True, dry_run=True)
    assert TOKEN not in json.dumps(result)


# 10
def test_requests_post_mocked(monkeypatch):
    calls = []

    class _Resp:
        status_code = 200

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        return _Resp()

    monkeypatch.setattr(tg.requests, "post", fake_post)
    result = tg.send_telegram_message(TOKEN, "999", "hi", enabled=True, dry_run=False)
    assert result["sent"] is True
    assert len(calls) == 1
    assert TOKEN in calls[0][0]            # токен в URL запроса, но не в result
    assert TOKEN not in json.dumps(result)


# 11
def test_alerts_disabled_blocks(monkeypatch):
    calls = []
    monkeypatch.setattr(tg.requests, "post",
                        lambda *a, **k: calls.append(1))
    result = tg.send_telegram_message(TOKEN, "999", "hi", enabled=False, dry_run=False)
    assert result["sent"] is False
    assert result["reason"] == "alerts_disabled"
    assert calls == []


# 12
def test_dry_run_sends_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(tg.requests, "post",
                        lambda *a, **k: calls.append(1))
    result = tg.send_telegram_message(TOKEN, "999", "hi", enabled=True, dry_run=True)
    assert result["sent"] is False
    assert result["dry_run"] is True
    assert calls == []


# 13
def test_no_order_endpoints_in_source():
    src = Path("notifications/telegram.py").read_text(encoding="utf-8")
    for forbidden in ("place_limit_order", "cancel_order", "OrdersService",
                      "order_client", "postOrder", "place_order", "submit_order",
                      "LIVE_EXECUTION_ENABLED"):
        assert forbidden not in src, forbidden


def test_force_overrides_disabled(monkeypatch):
    calls = []

    class _Resp:
        status_code = 200

    monkeypatch.setattr(tg.requests, "post",
                        lambda url, json=None, timeout=None: calls.append(url) or _Resp())
    result = tg.send_telegram_message(
        TOKEN, "999", "hi", enabled=False, dry_run=False, force=True)
    assert result["sent"] is True
    assert len(calls) == 1


def test_decide_notification_deadline_triggers(tmp_path):
    cfg = tg.TelegramConfig(enabled=False, daily_summary_enabled=True, min_interval_minutes=60)
    state = dict(tg._DEFAULT_STATE)
    state["last_status"] = "READY_DRY_RUN"
    state["last_daily_summary_date"] = "2026-07-29"
    now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    data = _ready_data()
    decision = tg.decide_notification(data, state, cfg, now)
    # дневную сводку уже слали, но дедлайн месяца (2 дня) должен заставить отправить
    assert decision.should_send is True
    assert any("month_deadline" in r for r in decision.reasons)
