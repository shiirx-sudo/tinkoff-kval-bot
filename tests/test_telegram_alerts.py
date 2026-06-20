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


def test_load_config_reads_dotenv(tmp_path, monkeypatch):
    # .env во временном каталоге + чистим OS env → load_config должен взять .env
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TELEGRAM_ALERTS_ENABLED=true\n"
        "TELEGRAM_BOT_TOKEN=123456:TEST\n"
        "TELEGRAM_CHAT_ID=123456789\n",
        encoding="utf-8",
    )
    for var in ("TELEGRAM_ALERTS_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)

    cfg = tg.load_config()
    assert cfg.enabled is True
    assert cfg.bot_token == "123456:TEST"
    assert cfg.chat_id == "123456789"
    # токен не должен светиться в repr
    assert "123456:TEST" not in repr(cfg)


def test_load_config_does_not_override_os_env(tmp_path, monkeypatch):
    # реальная OS env-переменная имеет приоритет над .env
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=FROM_DOTENV\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FROM_OS_ENV")
    cfg = tg.load_config()
    assert cfg.bot_token == "FROM_OS_ENV"


# ─── target-portfolio блок в digest (read-only) ──────────────────────────────

# Запрещённые рекомендационные формулировки в новом блоке digest.
_FORBIDDEN_WORDS = ("купить", "продать", "исключить", "buy", "sell")


def _target_portfolio_report():
    return {
        "target": {
            "monthly_net_rub": "100000.0",
            "annual_net_rub": "1200000.0",
            "status": "ok",
            "required_capital_rub": "16796234.73616309917239559702",
        },
        "target_allocation": [
            {"ticker": "VTBR", "target_weight_pct": "25", "low_yield_slot": False},
            {"ticker": "T", "target_weight_pct": "25", "low_yield_slot": True},
            {"ticker": "SBMM", "target_weight_pct": "20.0", "low_yield_slot": False},
            {"ticker": "LQDT", "target_weight_pct": "20.0", "low_yield_slot": False},
        ],
        "warnings": [
            "диверсификация: 10.0% не распределено (лимиты позиции/эмитента/денежного рынка)",
            "Низкодоходный слот: T занимает 25.00% капитала, но даёт лишь ~4.99% "
            "ожидаемого дохода; его консервативная доходность 1.43% существенно ниже "
            "смешанной 7.14%. Это диагностическое предупреждение, не рекомендация. "
            "Веса распределения не изменялись.",
            "cash_reserve_applied: 5000 ₽ оставлено вне распределения нового капитала",
        ],
    }


def _write_full_reports(d: Path):
    """Минимальный preflight, чтобы read_reports не вернул MISSING_REPORTS."""
    (d / "execution_preflight.json").write_text(json.dumps({
        "status": "READY_DRY_RUN", "instrument": {"ticker": "LQDT"},
        "period": "2026-07", "checks": [], "errors": [],
    }), encoding="utf-8")


# A: без target_portfolio.json digest работает как раньше.
def test_summary_without_target_portfolio_unchanged(tmp_path):
    _write_full_reports(tmp_path)
    data = tg.read_reports(tmp_path)
    assert data.get("target_portfolio") is None
    text = tg.build_summary_message(data, today=date(2026, 6, 16))
    assert "🎯 Целевой портфель" not in text
    assert "Реальных заявок нет." in text


# B: с target_portfolio.json появляется target-блок.
def test_summary_with_target_portfolio_block(tmp_path):
    _write_full_reports(tmp_path)
    (tmp_path / "target_portfolio.json").write_text(
        json.dumps(_target_portfolio_report()), encoding="utf-8")
    data = tg.read_reports(tmp_path)
    assert data["target_portfolio"]["status"] == "ok"
    text = tg.build_summary_message(data, today=date(2026, 6, 16))
    assert "🎯 Целевой портфель (план)" in text
    assert "Статус: ok" in text
    assert "100 000 ₽/мес" in text
    assert "Требуемый капитал:" in text
    assert "VTBR 25%" in text
    assert "T 25% ⚠️" in text  # маркер низкодоходного слота
    assert "не рекомендация" in text
    assert "Заявки не отправляются." in text


# C: low-yield warning из target_portfolio.json попадает в summary.
def test_summary_surfaces_low_yield_warning(tmp_path):
    _write_full_reports(tmp_path)
    (tmp_path / "target_portfolio.json").write_text(
        json.dumps(_target_portfolio_report()), encoding="utf-8")
    data = tg.read_reports(tmp_path)
    text = tg.build_summary_message(data, today=date(2026, 6, 16))
    assert "Диагностика:" in text
    assert "Низкодоходный слот: T занимает 25.00% капитала" in text


# D: новый блок не содержит рекомендационных формулировок.
def test_summary_target_block_has_no_recommendation_words(tmp_path):
    _write_full_reports(tmp_path)
    (tmp_path / "target_portfolio.json").write_text(
        json.dumps(_target_portfolio_report()), encoding="utf-8")
    data = tg.read_reports(tmp_path)
    text = tg.build_summary_message(data, today=date(2026, 6, 16)).lower()
    for word in _FORBIDDEN_WORDS:
        assert word not in text, word


# F: повреждённый target_portfolio.json не ломает summary.
def test_summary_malformed_target_portfolio(tmp_path):
    _write_full_reports(tmp_path)
    (tmp_path / "target_portfolio.json").write_text("{ broken json", encoding="utf-8")
    data = tg.read_reports(tmp_path)
    assert data["target_portfolio"] == {"malformed": True}
    text = tg.build_summary_message(data, today=date(2026, 6, 16))
    assert "🎯 Целевой портфель (план)" in text
    assert "повреждён" in text
    assert "Реальных заявок нет." in text


# Low-yield диагностика видна даже без preflight (MISSING_REPORTS).
def test_summary_target_block_without_preflight(tmp_path):
    (tmp_path / "target_portfolio.json").write_text(
        json.dumps(_target_portfolio_report()), encoding="utf-8")
    data = tg.read_reports(tmp_path)
    assert data["status"] == "MISSING_REPORTS"
    text = tg.build_summary_message(data, today=date(2026, 6, 16))
    assert "🎯 Целевой портфель (план)" in text
    assert "Низкодоходный слот: T" in text
