"""
trend_signal_v1 — read-only стратегия сигналов BUY/SELL/HOLD/SKIP.

Только аналитика по свечам + метаданным. НИКАКОГО исполнения: не размещает и не
отменяет заявок, не меняет портфель. Сигнал — это уведомление, а не приказ.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

NORMAL = "SECURITY_TRADING_STATUS_NORMAL_TRADING"


@dataclass
class SignalConfig:
    min_score: int = 70
    spread_bps_limit: Decimal = Decimal("10")
    min_daily_value_rub: Decimal = Decimal("10000000")
    atr_period: int = 14
    rsi_period: int = 14
    fast_ema: int = 20
    mid_ema: int = 50
    slow_ema: int = 200
    stop_atr_multiplier: Decimal = Decimal("2.0")
    take_profit_r_multiplier: Decimal = Decimal("2.0")


@dataclass
class Signal:
    ticker: str
    class_code: str
    action: str                       # BUY | SELL | HOLD | SKIP
    score: int = 0
    price: Decimal | None = None
    entry: Decimal | None = None
    stop: Decimal | None = None
    take_profit: Decimal | None = None
    rsi: Decimal | None = None
    ema20: Decimal | None = None
    ema50: Decimal | None = None
    ema200: Decimal | None = None
    atr: Decimal | None = None
    spread_bps: Decimal | None = None
    liquidity_value_rub: Decimal | None = None
    reasons: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    notified: bool = False


# ─── индикаторы (чистые функции, без внешних зависимостей) ───────────────────

def ema(values: list[Decimal], period: int) -> Decimal | None:
    if not values or len(values) < period:
        return None
    k = Decimal(2) / Decimal(period + 1)
    e = sum(values[:period]) / Decimal(period)        # SMA как seed
    for v in values[period:]:
        e = v * k + e * (Decimal(1) - k)
    return e


def rsi(closes: list[Decimal], period: int = 14) -> Decimal | None:
    if len(closes) < period + 1:
        return None
    gains = Decimal(0)
    losses = Decimal(0)
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / Decimal(period)
    avg_loss = losses / Decimal(period)
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else Decimal(0)
        loss = -diff if diff < 0 else Decimal(0)
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)
    if avg_loss == 0:
        return Decimal(100)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def atr(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal],
        period: int = 14) -> Decimal | None:
    n = len(closes)
    if n < period + 1:
        return None
    trs: list[Decimal] = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    a = sum(trs[:period]) / Decimal(period)
    for tr in trs[period:]:
        a = (a * Decimal(period - 1) + tr) / Decimal(period)
    return a


# ─── оценка инструмента ──────────────────────────────────────────────────────

def evaluate(candles: list[dict], meta: dict, config: SignalConfig) -> Signal:
    """candles: хронологический список {o,h,l,c,v}. meta: метаданные инструмента."""
    ticker = str(meta.get("ticker", ""))
    class_code = str(meta.get("class_code", ""))
    sig = Signal(ticker=ticker, class_code=class_code, action="SKIP")

    spread = meta.get("spread_bps")
    sig.spread_bps = Decimal(str(spread)) if spread is not None else None
    liq = meta.get("liquidity_value_rub")
    sig.liquidity_value_rub = Decimal(str(liq)) if liq is not None else None
    trading_status = str(meta.get("trading_status", ""))

    # ── SKIP-условия (данные/режим) ──
    need = config.slow_ema + 1
    if not candles or len(candles) < need:
        sig.blocked_reasons.append(
            f"мало истории ({len(candles) if candles else 0} < {need})")
        return sig
    if trading_status != NORMAL:
        sig.blocked_reasons.append(f"торги не NORMAL_TRADING ({trading_status or '—'})")
        return sig
    if sig.spread_bps is not None and sig.spread_bps > config.spread_bps_limit:
        sig.blocked_reasons.append(
            f"широкий spread ({sig.spread_bps} > {config.spread_bps_limit} bps)")
        return sig
    if (sig.liquidity_value_rub is not None
            and sig.liquidity_value_rub < config.min_daily_value_rub):
        sig.blocked_reasons.append(
            f"низкая ликвидность ({sig.liquidity_value_rub} < {config.min_daily_value_rub})")
        return sig

    closes = [Decimal(str(c["c"])) for c in candles]
    highs = [Decimal(str(c["h"])) for c in candles]
    lows = [Decimal(str(c["l"])) for c in candles]
    close = closes[-1]
    sig.price = close

    sig.ema20 = ema(closes, config.fast_ema)
    sig.ema50 = ema(closes, config.mid_ema)
    sig.ema200 = ema(closes, config.slow_ema)
    sig.rsi = rsi(closes, config.rsi_period)
    sig.atr = atr(highs, lows, closes, config.atr_period)

    if None in (sig.ema20, sig.ema50, sig.ema200, sig.rsi, sig.atr):
        sig.blocked_reasons.append("не удалось рассчитать индикаторы")
        return sig

    # локальный high (без последней свечи) для подтверждения пробоя
    local_high = max(highs[-(config.fast_ema + 1):-1]) if len(highs) > config.fast_ema else close
    breakout = close > sig.ema20 or close > local_high

    # ── SELL / EXIT ──
    if close < sig.ema50 or sig.ema20 < sig.ema50:
        sig.action = "SELL"
        if close < sig.ema50:
            sig.reasons.append("close < EMA50")
        if sig.ema20 < sig.ema50:
            sig.reasons.append("EMA20 теряет импульс (EMA20 < EMA50)")
        sig.reasons.append("риск продолжения снижения")
        return sig

    # ── BUY-кандидат ──
    buy_trend = close > sig.ema200
    buy_ema = sig.ema20 > sig.ema50
    buy_rsi = Decimal("45") <= sig.rsi <= Decimal("70")
    liq_ok = (sig.liquidity_value_rub is None
              or sig.liquidity_value_rub >= config.min_daily_value_rub)
    spread_ok = sig.spread_bps is None or sig.spread_bps <= config.spread_bps_limit

    if buy_trend and buy_ema and buy_rsi and breakout and spread_ok and liq_ok:
        score = 0
        if buy_trend:
            score += 25
            sig.reasons.append("close > EMA200")
        if buy_ema:
            score += 20
            sig.reasons.append("EMA20 > EMA50")
        if buy_rsi:
            score += 15
            sig.reasons.append(f"RSI{config.rsi_period} = {sig.rsi:.0f}")
        if liq_ok:
            score += 15
            sig.reasons.append("ликвидность нормальная")
        if spread_ok:
            score += 15
            sig.reasons.append(
                f"spread = {sig.spread_bps if sig.spread_bps is not None else '—'} bps")
        if breakout:
            score += 10
            sig.reasons.append("breakout/pullback подтверждён")
        sig.score = score

        # риск-модель (информационно, НЕ рекомендация к покупке)
        entry = close
        stop = entry - sig.atr * config.stop_atr_multiplier
        tp = entry + (entry - stop) * config.take_profit_r_multiplier
        sig.entry, sig.stop, sig.take_profit = entry, stop, tp

        sig.action = "BUY" if score >= config.min_score else "HOLD"
        if sig.action == "HOLD":
            sig.blocked_reasons.append(
                f"score {score} < min_score {config.min_score}")
        return sig

    # ── иначе HOLD ──
    sig.action = "HOLD"
    sig.reasons.append("нет подтверждённого входа")
    return sig
