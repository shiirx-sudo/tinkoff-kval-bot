"""Telegram-форматирование сигналов и dedup-состояние. Только уведомления."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from strategies.trend_signal_v1 import Signal

_DEDUP_DELTA_SCORE = 15  # заметное изменение score → можно прислать апдейт


def _money(v) -> str:
    if v is None:
        return "—"
    return f"{Decimal(str(v)):,.2f} ₽".replace(",", " ")


def build_signal_message(sig: Signal, strategy: str = "trend_signal_v1") -> str:
    head_ticker = f"{sig.ticker} / {sig.class_code or '—'}"
    if sig.action == "BUY":
        lines = [
            "📈 SIGNAL: BUY WATCH", "",
            f"Инструмент: {head_ticker}",
            f"Стратегия: {strategy}",
            f"Цена: {_money(sig.price)}",
            f"Score: {sig.score}/100", "",
            "Причины:",
        ]
        lines += [f"✅ {r}" for r in sig.reasons]
        rr = ""
        if sig.entry and sig.stop and sig.take_profit and sig.entry != sig.stop:
            r_mult = (sig.take_profit - sig.entry) / (sig.entry - sig.stop)
            rr = f"1:{r_mult:.0f}"
        lines += [
            "", "Риск-модель (informational only):",
            f"Entry: {_money(sig.entry)}",
            f"Stop: {_money(sig.stop)}",
            f"Take-profit: {_money(sig.take_profit)}",
            f"R/R: {rr or '—'}", "",
            "Статус: SIGNAL_ONLY / READ_ONLY",
            "Заявки не отправляются.",
        ]
        return "\n".join(lines)

    if sig.action == "SELL":
        lines = [
            "📉 SIGNAL: SELL / EXIT WATCH", "",
            f"Инструмент: {head_ticker}",
            f"Стратегия: {strategy}",
            f"Цена: {_money(sig.price)}", "",
            "Причины:",
        ]
        lines += [f"❌ {r}" for r in sig.reasons]
        lines += [
            "", "Статус: SIGNAL_ONLY / READ_ONLY",
            "Заявки не отправляются.",
        ]
        return "\n".join(lines)

    # HOLD/SKIP (по умолчанию не отправляются)
    return (f"ℹ️ SIGNAL: {sig.action} — {head_ticker} ({strategy})\n"
            f"Статус: SIGNAL_ONLY / READ_ONLY. Заявки не отправляются.")


# ─── dedup-состояние ─────────────────────────────────────────────────────────

def load_signal_state(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_signal_state(path: str | Path, state: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


def should_notify(sig: Signal, state: dict[str, Any], now: datetime,
                  dedup_hours: int, notify_on_hold: bool = False) -> tuple[bool, str]:
    """Решает, слать ли сигнал (без спама одинаковыми сигналами)."""
    if sig.action == "SKIP":
        return False, "skip_not_notified"
    if sig.action == "HOLD" and not notify_on_hold:
        return False, "hold_not_notified"

    prev = state.get(sig.ticker) or {}
    prev_action = prev.get("last_action")
    prev_score = int(prev.get("last_score") or 0)
    prev_sent = _parse_dt(prev.get("last_sent_utc"))

    if prev_action != sig.action:
        return True, "action_changed"                    # HOLD->BUY, BUY->SELL и т.п.
    if prev_sent is None or (now - prev_sent) >= timedelta(hours=dedup_hours):
        return True, "dedup_window_passed"
    if abs(sig.score - prev_score) >= _DEDUP_DELTA_SCORE:
        return True, "score_changed"
    return False, "dedup_suppressed"


def update_state(state: dict[str, Any], sig: Signal, now: datetime) -> None:
    state[sig.ticker] = {
        "last_action": sig.action,
        "last_score": sig.score,
        "last_sent_utc": now.isoformat(),
    }


def signals_status_text(config, enabled: bool, watchlist: list[str]) -> str:
    return (
        "🛰 Signals status\n"
        f"Включено: {'да' if enabled else 'нет'}\n"
        f"Стратегия: trend_signal_v1\n"
        f"Watchlist: {', '.join(watchlist) or '—'}\n"
        f"min_score: {config.min_score}\n"
        f"spread_limit: {config.spread_bps_limit} bps\n"
        "Режим: SIGNAL_ONLY / READ_ONLY (заявки не отправляются)."
    )


def signals_last_text(reports_dir: str | Path = "data/reports", limit: int = 10) -> str:
    """Текст для /signals — последние сигналы из strategy_signals.json."""
    p = Path(reports_dir) / "strategy_signals.json"
    if not p.exists():
        return "Сигналов пока нет (strategy_signals.json отсутствует)."
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "Не удалось прочитать strategy_signals.json."
    rows = data.get("signals") or []
    actionable = [r for r in rows if r.get("action") in ("BUY", "SELL")][:limit]
    if not actionable:
        return "Последний скан: BUY/SELL сигналов нет."
    lines = [f"🛰 Последние сигналы ({data.get('strategy', '')}):"]
    for r in actionable:
        lines.append(f"{r['action']} {r['ticker']} score={r.get('score')} "
                     f"price={r.get('price') or '—'}")
    lines.append("Режим: SIGNAL_ONLY / READ_ONLY.")
    return "\n".join(lines)
