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
from modules.balance import holdings_map, lookup_holding
from strategies.trend_signal_v1 import (
    Signal,
    SignalConfig,
    apply_portfolio_state,
    evaluate,
    parse_watchlist_item,
    resolve_instrument,
)

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
        "watchlist": [t.strip() for t in
                      os.getenv("SIGNALS_WATCHLIST", "LQDT").split(",") if t.strip()],
        "allowed_class_codes": [c.strip().upper() for c in
                                os.getenv("SIGNALS_ALLOWED_CLASS_CODES", "TQBR,TQTF").split(",") if c.strip()],
        "default_class_code": os.getenv("SIGNALS_DEFAULT_CLASS_CODE", "TQBR").strip().upper(),
        "class_code_priority": [c.strip().upper() for c in
                                os.getenv("SIGNALS_CLASS_CODE_PRIORITY", "TQBR,TQTF,SPBRU").split(",") if c.strip()],
        "timeframe": os.getenv("SIGNALS_TIMEFRAME", "day"),
        "lookback_days": int(os.getenv("SIGNALS_LOOKBACK_DAYS", "260") or "260"),
        "dedup_hours": int(os.getenv("SIGNALS_DEDUP_HOURS", "12") or "12"),
        "max_per_run": int(os.getenv("SIGNALS_MAX_PER_RUN", "10") or "10"),
        "notify_on_hold": _b(os.getenv("SIGNALS_NOTIFY_ON_HOLD", "false")),
        "sell_only_if_held": _b(os.getenv("SIGNALS_SELL_ONLY_IF_HELD", "true")),
        "include_portfolio_state": _b(os.getenv("SIGNALS_INCLUDE_PORTFOLIO_STATE", "true")),
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


def _resolve_meta(client, ticker: str, explicit_class: str | None,
                  priority: list[str]) -> tuple[dict, str, list[str]]:
    """Резолв инструмента (read-only) с учётом class_code/приоритета."""
    try:
        found = client.find_instruments(ticker)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"find_instruments({ticker}) недоступен: {exc}")
        found = []
    chosen, selected_by, cand_classes = resolve_instrument(
        found, ticker, explicit_class, priority)
    if chosen is None:
        return {}, selected_by, cand_classes

    meta = {
        "ticker": ticker, "class_code": chosen["class_code"], "figi": chosen["figi"],
        "instrument_uid": chosen["uid"], "instrument_name": chosen["name"],
        "instrument_type": chosen["instrument_type"],
        "trading_status": "", "spread_bps": None, "liquidity_value_rub": None,
    }
    figi = chosen["figi"] or chosen["uid"]
    if not figi:
        return meta, selected_by, cand_classes
    try:
        ts = client.get_trading_status(figi)
        meta["trading_status"] = str(ts.get("tradingStatus", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"trading_status недоступен: {exc}")
    try:
        ob = client.get_order_book(figi, depth=1)
        bid = quotation_to_decimal((ob.get("bids") or [{}])[0].get("price"))
        ask = quotation_to_decimal((ob.get("asks") or [{}])[0].get("price"))
        if bid and ask and bid > 0:
            meta["spread_bps"] = (ask - bid) / ((ask + bid) / 2) * Decimal(10000)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"order_book недоступен: {exc}")
    return meta, selected_by, cand_classes


def scan(client, opts: dict, *, as_of=None, account_id=None) -> list:
    """Прогоняет watchlist через стратегию (read-only). Возвращает список Signal."""
    cfg: SignalConfig = opts["config"]
    priority = opts.get("class_code_priority") or ["TQBR", "TQTF", "SPBRU"]
    sell_only_if_held = opts.get("sell_only_if_held", True)

    # read-only карта позиций (один раз). ok=False → held_unknown для всех.
    holdings = {"ok": False, "by_figi": {}, "by_uid": {}, "by_ticker_class": {}}
    if opts.get("include_portfolio_state", True) or sell_only_if_held:
        try:
            holdings = holdings_map(client, account_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"holdings_map недоступен (held_unknown): {exc}")

    signals = []
    for raw in opts["watchlist"][: opts["max_per_run"]]:
        ticker, explicit = parse_watchlist_item(raw)
        meta, selected_by, cand_classes = _resolve_meta(client, ticker, explicit, priority)

        if not meta:
            cands = ",".join(cand_classes) or "—"
            logger.info(f"{ticker} -> SKIP no_allowed_class_code_match; candidates={cands}")
            signals.append(Signal(
                ticker=ticker, class_code=(explicit or ""), action="SKIP",
                selected_by="no_allowed_match", raw_action="SKIP",
                blocked_reasons=[f"no_allowed_class_code_match; candidates={cands}"]))
            continue

        if selected_by == "priority_fallback" and meta["class_code"] == opts.get("default_class_code"):
            selected_by = "default_class_code"

        candles = []
        figi = meta.get("figi") or meta.get("instrument_uid")
        if figi:
            try:
                candles = _candles_for(client, figi, opts["lookback_days"],
                                       opts["timeframe"])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"get_candles({ticker}) недоступен: {exc}")
        if candles:
            closes = [c["c"] for c in candles]
            vols = [c["v"] for c in candles]
            values = [closes[i] * vols[i] for i in range(len(candles))]
            if values:
                meta["liquidity_value_rub"] = median(values)

        sig = evaluate(candles, meta, cfg)
        sig.figi = meta.get("figi", "")
        sig.instrument_uid = meta.get("instrument_uid", "")
        sig.instrument_name = meta.get("instrument_name", "")
        sig.instrument_type = meta.get("instrument_type", "")
        sig.selected_by = selected_by

        rec = lookup_holding(holdings, figi=sig.figi, uid=sig.instrument_uid,
                             ticker=ticker, class_code=meta.get("class_code", ""))
        apply_portfolio_state(sig, rec, holdings.get("ok", False), sell_only_if_held)

        held_str = ("held" if sig.held else
                    ("held_unknown" if sig.held_unknown else "not_held"))
        logger.info(f"{ticker} -> {meta['class_code']} / "
                    f"{meta.get('instrument_name') or '—'} / selected_by={selected_by} / "
                    f"action={sig.action} (raw={sig.raw_action}, {held_str})")
        signals.append(sig)
    return signals
