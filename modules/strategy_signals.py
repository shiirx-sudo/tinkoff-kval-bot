"""
Оркестратор стратегии сигналов (read-only).

Тянет свечи/стакан/статус через read-only клиент, считает сигналы стратегии,
применяет dedup, пишет отчёты и (опц.) шлёт Telegram-уведомления. Никаких заявок.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from dotenv import find_dotenv, load_dotenv
from loguru import logger

from common.helpers import quotation_to_decimal
from strategies.trend_signal_v1 import SignalConfig, evaluate

_STATE_PATH = "data/state/strategy_signals_state.json"


def _b(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y", "да")


def load_signal_config() -> dict:
    """Читает SIGNALS_* из окружения (.env подхватывается, OS env приоритетнее)."""
    load_dotenv(find_dotenv(usecwd=True), override=False)
    cfg = SignalConfig(
        min_score=int(os.getenv("SIGNALS_MIN_SCORE", "70") or "70"),
        spread_bps_limit=Decimal(os.getenv("SIGNALS_SPREAD_BPS_LIMIT", "10") or "10"),
        min_daily_value_rub=Decimal(os.getenv("SIGNALS_MIN_DAILY_VALUE_RUB", "10000000") or "10000000"),
        atr_period=int(os.getenv("SIGNALS_ATR_PERIOD", "14") or "14"),
        rsi_period=int(os.getenv("SIGNALS_RSI_PERIOD", "14") or "14"),
        fast_ema=int(os.getenv("SIGNALS_FAST_EMA", "20") or "20"),
        mid_ema=int(os.getenv("SIGNALS_MID_EMA", "50") or "50"),
        slow_ema=int(os.getenv("SIGNALS_SLOW_EMA", "200") or "200"),
        stop_atr_multiplier=Decimal(os.getenv("SIGNALS_STOP_ATR_MULTIPLIER", "2.0") or "2.0"),
        take_profit_r_multiplier=Decimal(os.getenv("SIGNALS_TAKE_PROFIT_R_MULTIPLIER", "2.0") or "2.0"),
    )
    return {
        "config": cfg,
        "enabled": _b(os.getenv("SIGNALS_ENABLED", "false")),
        "notify_telegram": _b(os.getenv("SIGNALS_NOTIFY_TELEGRAM", "true")),
        "watchlist": [t.strip().upper() for t in
                      os.getenv("SIGNALS_WATCHLIST", "LQDT").split(",") if t.strip()],
        "timeframe": os.getenv("SIGNALS_TIMEFRAME", "day"),
        "lookback_days": int(os.getenv("SIGNALS_LOOKBACK_DAYS", "260") or "260"),
        "dedup_hours": int(os.getenv("SIGNALS_DEDUP_HOURS", "12") or "12"),
        "max_per_run": int(os.getenv("SIGNALS_MAX_PER_RUN", "10") or "10"),
        "notify_on_hold": _b(os.getenv("SIGNALS_NOTIFY_ON_HOLD", "false")),
        "state_path": _STATE_PATH,
    }


_INTERVAL = {"day": "CANDLE_INTERVAL_DAY", "hour": "CANDLE_INTERVAL_HOUR",
             "week": "CANDLE_INTERVAL_WEEK"}


def _candles_for(client, figi: str, lookback_days: int, timeframe: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    frm = now - timedelta(days=lookback_days + 5)
    raw = client.get_candles(
        figi, frm.isoformat(), now.isoformat(),
        _INTERVAL.get(timeframe, "CANDLE_INTERVAL_DAY"))
    out: list[dict] = []
    for c in raw.get("candles") or []:
        out.append({
            "o": quotation_to_decimal(c.get("open")),
            "h": quotation_to_decimal(c.get("high")),
            "l": quotation_to_decimal(c.get("low")),
            "c": quotation_to_decimal(c.get("close")),
            "v": Decimal(str(c.get("volume", 0) or 0)),
        })
    return out


def _meta_for(client, ticker: str) -> dict:
    """Резолв инструмента + спред + статус + ликвидность (read-only, best-effort)."""
    meta = {"ticker": ticker, "class_code": "", "figi": "",
            "trading_status": "", "spread_bps": None, "liquidity_value_rub": None}
    try:
        found = client.find_instruments(ticker)
        for it in found or []:
            if str(it.get("ticker", "")).upper() == ticker.upper():
                meta["figi"] = str(it.get("figi", ""))
                meta["class_code"] = str(it.get("classCode") or it.get("class_code") or "")
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"find_instruments({ticker}) недоступен: {exc}")
    if not meta["figi"]:
        return meta
    try:
        ts = client.get_trading_status(meta["figi"])
        meta["trading_status"] = str(ts.get("tradingStatus", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"trading_status недоступен: {exc}")
    try:
        ob = client.get_order_book(meta["figi"], depth=1)
        bid = quotation_to_decimal((ob.get("bids") or [{}])[0].get("price"))
        ask = quotation_to_decimal((ob.get("asks") or [{}])[0].get("price"))
        if bid and ask and bid > 0:
            meta["spread_bps"] = (ask - bid) / ((ask + bid) / 2) * Decimal(10000)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"order_book недоступен: {exc}")
    return meta


def scan(client, opts: dict, *, as_of=None) -> list:
    """Прогоняет watchlist через стратегию (read-only). Возвращает список Signal."""
    cfg: SignalConfig = opts["config"]
    signals = []
    for ticker in opts["watchlist"][: opts["max_per_run"]]:
        meta = _meta_for(client, ticker)
        candles = []
        if meta.get("figi"):
            try:
                candles = _candles_for(client, meta["figi"], opts["lookback_days"],
                                       opts["timeframe"])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"get_candles({ticker}) недоступен: {exc}")
        if candles:
            closes = [c["c"] for c in candles]
            vols = [c["v"] for c in candles]
            values = [closes[i] * vols[i] for i in range(len(candles))]
            if values:
                meta["liquidity_value_rub"] = median(values)
        signals.append(evaluate(candles, meta, cfg))
    return signals
