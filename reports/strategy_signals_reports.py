"""Отчёты сигналов: strategy_signals.json + .csv + .md (read-only)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from common.helpers import utc_now
from strategies.trend_signal_v1 import Signal

COLUMNS = [
    "timestamp", "strategy", "ticker", "class_code", "figi", "instrument_uid",
    "instrument_name", "instrument_type", "selected_by", "raw_action", "action",
    "held", "held_unknown", "position_quantity", "position_value_rub",
    "score", "price", "entry", "stop", "take_profit", "rsi", "ema20", "ema50",
    "ema200", "atr", "spread_bps", "liquidity_value_rub", "reasons",
    "blocked_reasons", "notified",
]


def _s(v) -> str:
    return "" if v is None else str(v)


def _row(sig: Signal, strategy: str, ts: str) -> dict:
    return {
        "timestamp": ts, "strategy": strategy, "ticker": sig.ticker,
        "class_code": sig.class_code, "figi": sig.figi,
        "instrument_uid": sig.instrument_uid, "instrument_name": sig.instrument_name,
        "instrument_type": sig.instrument_type, "selected_by": sig.selected_by,
        "raw_action": sig.raw_action or sig.action, "action": sig.action,
        "held": sig.held, "held_unknown": sig.held_unknown,
        "position_quantity": _s(sig.position_quantity),
        "position_value_rub": _s(sig.position_value_rub),
        "score": sig.score,
        "price": _s(sig.price), "entry": _s(sig.entry), "stop": _s(sig.stop),
        "take_profit": _s(sig.take_profit), "rsi": _s(sig.rsi),
        "ema20": _s(sig.ema20), "ema50": _s(sig.ema50), "ema200": _s(sig.ema200),
        "atr": _s(sig.atr), "spread_bps": _s(sig.spread_bps),
        "liquidity_value_rub": _s(sig.liquidity_value_rub),
        "reasons": " | ".join(sig.reasons),
        "blocked_reasons": " | ".join(sig.blocked_reasons),
        "notified": sig.notified,
    }


def write_all(signals: list[Signal], strategy: str,
              reports_dir: str | Path = "data/reports") -> dict[str, Path]:
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = utc_now()
    rows = [_row(s, strategy, ts) for s in signals]

    json_path = out / "strategy_signals.json"
    json_path.write_text(json.dumps(
        {"generated_at_utc": ts, "strategy": strategy, "signals": rows},
        ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = out / "strategy_signals.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    md = ["# Strategy signals — READ ONLY", "",
          f"- Сгенерировано: {ts}", f"- Стратегия: {strategy}", "",
          "| Ticker | Action | Score | Price | Reasons |",
          "|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['ticker']} | {r['action']} | {r['score']} | "
                  f"{r['price'] or '—'} | {r['reasons'] or r['blocked_reasons']} |")
    md += ["", "_Сигналы — это уведомления, не приказы. Заявки не отправляются._", ""]
    md_path = out / "strategy_signals.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    return {"strategy_signals.json": json_path, "strategy_signals.csv": csv_path,
            "strategy_signals.md": md_path}
