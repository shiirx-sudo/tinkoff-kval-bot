"""
Read-only Telegram-уведомления для мониторинга квалификации.

Никаких заявок, order-endpoints, live-исполнения или изменения портфеля. Модуль
только читает готовые JSON-отчёты и шлёт короткий статус в Telegram. Токен нигде
не логируется и не попадает в отчёты.
"""
from __future__ import annotations

import hashlib
import json
import os
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
from dotenv import find_dotenv, load_dotenv
from loguru import logger

TELEGRAM_API = "https://api.telegram.org"
MONTH_THRESHOLDS = (10, 5, 3, 1)
QUARTER_THRESHOLDS = (21, 14, 7, 3, 1)
WARNING_STATUSES = ("BLOCKED", "MISSING_REPORTS", "STALE_REPORTS", "ERROR")

_STATUS_EMOJI = {
    "READY_DRY_RUN": "🟢", "BLOCKED": "🔴",
    "STALE_REPORTS": "🟡", "MISSING_REPORTS": "⚠️", "ERROR": "⚠️",
}


def _b(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y", "да")


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    min_interval_minutes: int = 60
    daily_summary_enabled: bool = True
    daily_summary_hour: int = 10
    status_change_only: bool = True

    def __repr__(self) -> str:  # токен не раскрываем
        masked = "set" if self.bot_token else "empty"
        return (f"TelegramConfig(enabled={self.enabled}, bot_token=<{masked}>, "
                f"chat_id=<{'set' if self.chat_id else 'empty'}>, "
                f"min_interval_minutes={self.min_interval_minutes}, "
                f"daily_summary_enabled={self.daily_summary_enabled})")


def load_config() -> TelegramConfig:
    # Подхватываем .env (как и config.settings), ищем от текущего рабочего
    # каталога; НЕ перетираем уже заданные OS env (override=False). Токен не логируем.
    load_dotenv(find_dotenv(usecwd=True), override=False)
    return TelegramConfig(
        enabled=_b(os.getenv("TELEGRAM_ALERTS_ENABLED", "false")),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        min_interval_minutes=int(os.getenv("TELEGRAM_ALERT_MIN_INTERVAL_MINUTES", "60") or 60),
        daily_summary_enabled=_b(os.getenv("TELEGRAM_DAILY_SUMMARY_ENABLED", "true")),
        daily_summary_hour=int(os.getenv("TELEGRAM_DAILY_SUMMARY_HOUR", "10") or 10),
        status_change_only=_b(os.getenv("TELEGRAM_STATUS_CHANGE_ONLY", "true")),
    )


# ─── Состояние антиспама ─────────────────────────────────────────────────────

_DEFAULT_STATE = {
    "last_sent_at_utc": None,
    "last_status": None,
    "last_hash": None,
    "last_daily_summary_date": None,
    "last_month_deadline_alert": None,
    "last_quarter_deadline_alert": None,
}


def load_alert_state(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return dict(_DEFAULT_STATE)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {**_DEFAULT_STATE, **data}
    except Exception:  # noqa: BLE001
        return dict(_DEFAULT_STATE)


def save_alert_state(path: str | Path, state: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── Чтение отчётов и сборка сообщения ───────────────────────────────────────

def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _spread_for(scan: dict | None, ticker: str) -> str:
    if not scan:
        return "—"
    for r in scan.get("results") or []:
        if str(r.get("ticker", "")).upper() == ticker.upper():
            v = r.get("spread_bps")
            return f"{Decimal(str(v)):.2f}" if v not in (None, "") else "—"
    return "—"


def _money(v) -> str:
    if v in (None, ""):
        return "—"
    return f"{Decimal(str(v)):,.0f} ₽".replace(",", " ")


def _weight(v) -> str:
    if v in (None, ""):
        return "—"
    try:
        return f"{Decimal(str(v)):.0f}%"
    except Exception:  # noqa: BLE001
        return "—"


# Маркер диагностики низкодоходного слота в тексте warnings target_portfolio.
_LOW_YIELD_PREFIX = "Низкодоходный слот"


def _read_target_portfolio(reports_dir: str | Path) -> dict[str, Any] | None:
    """Читает target_portfolio.json (read-only). Возвращает компактную сводку.

    Нет файла → None (digest работает как раньше). Повреждённый JSON →
    {"malformed": True} (digest не падает, блок помечается как пропущенный).
    """
    path = Path(reports_dir) / "target_portfolio.json"
    if not path.exists():
        return None
    raw = _load_json(path)
    if raw is None:
        return {"malformed": True}
    target = raw.get("target") or {}
    allocation = []
    for a in raw.get("target_allocation") or []:
        allocation.append({
            "ticker": str(a.get("ticker", "")),
            "weight_pct": a.get("target_weight_pct"),
            "low_yield_slot": bool(a.get("low_yield_slot")),
        })
    return {
        "malformed": False,
        "status": str(target.get("status", "")),
        "monthly_net_rub": target.get("monthly_net_rub"),
        "required_capital_rub": target.get("required_capital_rub"),
        "allocation": allocation,
        "warnings": [str(w) for w in (raw.get("warnings") or [])],
    }


def _target_portfolio_lines(tp: dict[str, Any] | None) -> list[str]:
    """Строит компактный read-only блок целевого портфеля для digest.

    Только аналитика/диагностика — никаких рекомендаций и заявок.
    """
    if not tp:
        return []
    if tp.get("malformed"):
        return ["", "🎯 Целевой портфель (план)",
                "⚠️ target_portfolio.json повреждён — блок диагностики пропущен."]

    lines = [
        "",
        "🎯 Целевой портфель (план)",
        f"Статус: {tp.get('status') or '—'}",
        f"Целевой доход: {_money(tp.get('monthly_net_rub'))}/мес",
        f"Требуемый капитал: {_money(tp.get('required_capital_rub'))}",
    ]
    allocation = tp.get("allocation") or []
    if allocation:
        parts = []
        for a in allocation:
            mark = " ⚠️" if a.get("low_yield_slot") else ""
            parts.append(f"{a.get('ticker', '')} {_weight(a.get('weight_pct'))}{mark}")
        lines.append("Доли капитала: " + ", ".join(parts))

    warnings = tp.get("warnings") or []
    # Диагностику низкодоходных слотов показываем первой, затем прочее; максимум 3.
    ordered = ([w for w in warnings if w.startswith(_LOW_YIELD_PREFIX)]
               + [w for w in warnings if not w.startswith(_LOW_YIELD_PREFIX)])
    if ordered:
        lines.append("Диагностика:")
        lines += [f"⚠️ {w}" for w in ordered[:3]]
    lines.append("Это аналитика, не рекомендация. Заявки не отправляются.")
    return lines


def read_reports(reports_dir: str | Path) -> dict[str, Any]:
    """Собирает данные из готовых JSON-отчётов (read-only)."""
    d = Path(reports_dir)
    preflight = _load_json(d / "execution_preflight.json")
    plan = _load_json(d / "kval_plan.json")
    scan = _load_json(d / "instrument_scan.json")
    target_portfolio = _read_target_portfolio(d)

    if preflight is None:
        return {
            "status": "MISSING_REPORTS",
            "warnings": ["Нет execution_preflight.json — запустите цепочку "
                         "kval-status → kval-plan → instrument-scan → "
                         "execution-plan → execution-preflight."],
            "instrument": {}, "period": "", "check_date": "", "spread": "—",
            "side_notional": None, "broker_trade_count_missing": 0,
            "roundtrip_cycle_count_required": 0,
            "target_portfolio": target_portfolio,
        }

    instr = preflight.get("instrument") or {}
    ticker = str(instr.get("ticker", ""))
    warnings: list[str] = list(preflight.get("errors") or [])
    for c in preflight.get("checks") or []:
        if not c.get("ok") and c.get("blocking"):
            warnings.append(f"{c.get('name')}: {c.get('detail')}")

    exec_plan = _load_json(d / "execution_plan.json") or {}
    sizing = exec_plan.get("sizing") or {}
    planned_actions = len(exec_plan.get("planned_actions") or []) or sizing.get("planned_actions", 0)

    return {
        "status": str(preflight.get("status", "")),
        "instrument": instr,
        "ticker": ticker,
        "verdict": str(instr.get("verdict", "")),
        "trading_status": str(instr.get("trading_status", "")),
        "class_code": str(instr.get("class_code", "")),
        "period": str(preflight.get("period", "")),
        "check_date": str((plan or {}).get("earliest_possible_check_date", "")),
        "spread": _spread_for(scan, ticker),
        "side_notional": preflight.get("side_notional"),
        "broker_trade_count_missing": int(preflight.get("broker_trade_count_missing") or 0),
        "roundtrip_cycle_count_required": int(preflight.get("roundtrip_cycle_count_required") or 0),
        "size_mode": str(sizing.get("mode", "")),
        "available_cash_rub": sizing.get("available_cash_rub"),
        "planned_actions": planned_actions,
        "projected_total_trades": sizing.get("projected_total_trades", 0),
        "kval_min_total_trades": sizing.get("kval_min_total_trades", 41),
        "warnings": warnings,
        "target_portfolio": target_portfolio,
    }


def build_summary_message(data: dict[str, Any], today: date | None = None) -> str:
    today = today or date.today()
    status = data.get("status", "")
    emoji = _STATUS_EMOJI.get(status, "ℹ️")
    ts_short = str(data.get("trading_status", "")).replace(
        "SECURITY_TRADING_STATUS_", "") or "—"

    lines = [
        "📊 T-Invest Kval Monitor",
        "",
        f"Дата: {today.isoformat()}",
        f"Период плана: {data.get('period') or '—'}",
        f"Статус: {emoji} {status}",
    ]
    if data.get("ticker"):
        lines += [
            "",
            f"{data['ticker']}: {data.get('verdict') or '—'} / {ts_short}",
            f"Спред: {data.get('spread', '—')} bps",
            f"Side notional: {_money(data.get('side_notional'))}",
            f"Broker trades missing: {data.get('broker_trade_count_missing', 0)}",
            f"Roundtrip cycles: {data.get('roundtrip_cycle_count_required', 0)}",
        ]
    if data.get("size_mode") == "balance":
        lines += [
            "",
            "Sizing: balance",
            f"Свободный баланс: {_money(data.get('available_cash_rub'))}",
            f"Планируемых действий: {data.get('planned_actions', 0)}",
            f"Сделок за период: {data.get('projected_total_trades', 0)} / "
            f"{data.get('kval_min_total_trades', 41)}",
        ]
    if data.get("warnings"):
        lines += ["", "⚠️ Причины:"]
        lines += [f"• {w}" for w in data["warnings"][:6]]
    lines += _target_portfolio_lines(data.get("target_portfolio"))
    lines += [
        "",
        f"Следующая проверка: {data.get('check_date') or '—'}",
        "Реальных заявок нет.",
    ]
    return "\n".join(lines)


def message_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ─── Дедлайны месяца/квартала ────────────────────────────────────────────────

def _end_of_month(d: date) -> date:
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


def _end_of_quarter(d: date) -> date:
    q_end_month = ((d.month - 1) // 3 + 1) * 3
    return _end_of_month(date(d.year, q_end_month, 1))


def _deadline_bucket(days: int, thresholds: tuple[int, ...]) -> int | None:
    crossed = [t for t in thresholds if days <= t]
    return min(crossed) if crossed else None


def month_deadline_alert(today: date) -> tuple[int | None, str]:
    eom = _end_of_month(today)
    days = (eom - today).days
    bucket = _deadline_bucket(days, MONTH_THRESHOLDS)
    return bucket, (f"{eom.isoformat()}:{bucket}" if bucket is not None else "")


def quarter_deadline_alert(today: date) -> tuple[int | None, str]:
    eoq = _end_of_quarter(today)
    days = (eoq - today).days
    bucket = _deadline_bucket(days, QUARTER_THRESHOLDS)
    return bucket, (f"{eoq.isoformat()}:{bucket}" if bucket is not None else "")


# ─── Решение об отправке ─────────────────────────────────────────────────────

def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


def should_send_alert(
    status: str,
    state: dict[str, Any],
    now: datetime,
    *,
    min_interval_minutes: int,
    daily_summary_enabled: bool,
) -> tuple[bool, str]:
    """Решает, нужно ли слать сообщение по текущему статусу (без дедлайнов)."""
    if status != state.get("last_status"):
        return True, "status_change"

    if status in WARNING_STATUSES:
        last_sent = _parse_dt(state.get("last_sent_at_utc"))
        if last_sent is None or (now - last_sent) >= timedelta(minutes=min_interval_minutes):
            return True, "warning_repeat"
        return False, "antispam"

    if status == "READY_DRY_RUN":
        if daily_summary_enabled and state.get("last_daily_summary_date") != now.date().isoformat():
            return True, "daily_summary"
        return False, "already_sent_today"

    return False, "no_change"


# ─── Отправка ────────────────────────────────────────────────────────────────

def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    enabled: bool,
    dry_run: bool = True,
    force: bool = False,
    timeout: int = 10,
) -> dict[str, Any]:
    """Отправляет сообщение. Токен не логируется и не возвращается."""
    if dry_run:
        logger.info("Telegram: dry-run, сообщение НЕ отправляется.")
        return {"sent": False, "dry_run": True, "reason": "dry_run", "error": None}
    if not enabled and not force:
        logger.warning("Telegram: TELEGRAM_ALERTS_ENABLED=false — отправка заблокирована.")
        return {"sent": False, "dry_run": False, "reason": "alerts_disabled", "error": None}
    if not bot_token or not chat_id:
        logger.error("Telegram: не задан bot_token или chat_id.")
        return {"sent": False, "dry_run": False, "reason": "no_credentials",
                "error": "bot_token/chat_id не заданы"}

    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url, json={"chat_id": chat_id, "text": text}, timeout=timeout)
        ok = resp.status_code == 200
        if ok:
            logger.info("Telegram: сообщение отправлено.")
        else:
            logger.error(f"Telegram: ошибка отправки, HTTP {resp.status_code}.")
        return {"sent": ok, "dry_run": False, "status_code": resp.status_code,
                "error": None if ok else f"HTTP {resp.status_code}"}
    except Exception as exc:  # noqa: BLE001 — не падаем traceback-ом
        logger.error(f"Telegram: исключение при отправке: {type(exc).__name__}")
        return {"sent": False, "dry_run": False, "error": str(exc)}


# ─── Оркестрация notify ──────────────────────────────────────────────────────

@dataclass
class NotifyDecision:
    should_send: bool
    reasons: list[str] = field(default_factory=list)
    status: str = ""
    text: str = ""
    text_hash: str = ""
    deadline_keys: dict[str, str] = field(default_factory=dict)


def decide_notification(
    data: dict[str, Any],
    state: dict[str, Any],
    config: TelegramConfig,
    now: datetime,
) -> NotifyDecision:
    status = data.get("status", "")
    text = build_summary_message(data, today=now.date())
    h = message_hash(text)

    send, reason = should_send_alert(
        status, state, now,
        min_interval_minutes=config.min_interval_minutes,
        daily_summary_enabled=config.daily_summary_enabled,
    )
    reasons = [reason] if send else []
    deadline_keys: dict[str, str] = {}

    m_bucket, m_key = month_deadline_alert(now.date())
    if m_bucket is not None and m_key != state.get("last_month_deadline_alert"):
        send = True
        reasons.append(f"month_deadline_{m_bucket}d")
        deadline_keys["month"] = m_key

    q_bucket, q_key = quarter_deadline_alert(now.date())
    if q_bucket is not None and q_key != state.get("last_quarter_deadline_alert"):
        send = True
        reasons.append(f"quarter_deadline_{q_bucket}d")
        deadline_keys["quarter"] = q_key

    # антиспам по одинаковому тексту в пределах интервала
    if send and reason == "warning_repeat" and h == state.get("last_hash"):
        last_sent = _parse_dt(state.get("last_sent_at_utc"))
        if last_sent and (now - last_sent) < timedelta(minutes=config.min_interval_minutes):
            send = False
            reasons = ["antispam_same_text"]

    return NotifyDecision(should_send=send, reasons=reasons, status=status,
                          text=text, text_hash=h, deadline_keys=deadline_keys)
