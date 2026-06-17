"""
fundamental_filter_v1 — read-only качественный фильтр компаний.

Не скрапит интернет и не даёт инвестиционных рекомендаций: читает ручную базу
оценок (YAML) по 4 качественным вопросам и считает балл 0–4 + вердикт. Это
фильтр качества поверх технических сигналов, а НЕ торговый сигнал сам по себе.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

# путь по умолчанию: личная база (gitignored) → пример в репозитории
_DEFAULT_PATH = "data/config/fundamental_filter.yaml"
_FALLBACK_EXAMPLE = "config/fundamental_filter.example.yaml"

_DIMENSIONS = ("management_alignment", "cash_return", "state_role", "market_growth")
_SCORE = {
    "positive": 1.0,
    "neutral": 0.5,
    "mixed": 0.5,
    "weak": 0.0,
    "negative": 0.0,
    "unknown": 0.0,
}
_DIM_LABEL = {
    "management_alignment": "ориентация менеджмента на рост стоимости",
    "cash_return": "возврат денег акционерам",
    "state_role": "роль государства",
    "market_growth": "рост рынка компании",
}


@dataclass
class FundamentalResult:
    ticker: str
    class_code: str = ""
    management_alignment: str = "unknown"
    cash_return: str = "unknown"
    state_role: str = "unknown"
    market_growth: str = "unknown"
    score_0_4: float | None = None
    verdict: str = "quality_unknown"
    reasons: list[str] = field(default_factory=list)


def load_fundamental_filter(path: str | None = None) -> dict:
    """Читает YAML-базу оценок. None при отсутствии/ошибке — это не падение."""
    candidates = [p for p in (path, _DEFAULT_PATH, _FALLBACK_EXAMPLE) if p]
    for p in candidates:
        fp = Path(p)
        if not fp.exists():
            continue
        try:
            import yaml
            data = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                # ключи-тикеры в верхний регистр
                return {str(k).upper(): v for k, v in data.items()}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"fundamental_filter: не удалось прочитать {p}: {exc}")
    return {}


def evaluate_fundamental(ticker: str, class_code: str, data: dict) -> FundamentalResult:
    """Балл 0–4 + вердикт по ручным оценкам. Нет данных → quality_unknown."""
    res = FundamentalResult(ticker=ticker.upper(), class_code=class_code)
    rec = (data or {}).get(ticker.upper())

    # уважаем class_code, если он указан в записи
    if rec and class_code and rec.get("class_code") \
            and str(rec["class_code"]).upper() != class_code.upper():
        rec = None

    if not rec or not isinstance(rec, dict):
        res.reasons.append("нет фундаментальных данных")
        return res

    score = 0.0
    for dim in _DIMENSIONS:
        val = str(rec.get(dim, "unknown")).strip().lower()
        if val not in _SCORE:
            val = "unknown"
        setattr(res, dim, val)
        score += _SCORE[val]

    res.score_0_4 = round(score, 2)
    if score >= 3.0:
        res.verdict = "quality_pass"
    elif score >= 2.0:
        res.verdict = "quality_watch"
    else:
        res.verdict = "quality_risk"

    # причины: из notes + краткая расшифровка измерений
    for note in (rec.get("notes") or []):
        res.reasons.append(str(note))
    for dim in _DIMENSIONS:
        val = getattr(res, dim)
        if val in ("weak", "negative"):
            res.reasons.append(f"слабо: {_DIM_LABEL[dim]} ({val})")
    return res


def apply_to_signal(sig, result: FundamentalResult, require_pass: bool = False):
    """Навешивает фундаментальную оценку на сигнал (read-only).

    BUY + require_pass + (quality_risk|quality_watch) -> понижается до HOLD.
    Для SELL/AVOID/HOLD — только пояснение, действие не меняется.
    quality_unknown никогда не блокирует.
    """
    sig.fundamental_score = result.score_0_4
    sig.fundamental_verdict = result.verdict
    sig.management_alignment = result.management_alignment
    sig.cash_return = result.cash_return
    sig.state_role = result.state_role
    sig.market_growth = result.market_growth
    sig.fundamental_reasons = list(result.reasons)

    if sig.action == "BUY" and require_pass and result.verdict in ("quality_risk", "quality_watch"):
        sig.action = "HOLD"
        sig.blocked_reasons.append(f"fundamental_{result.verdict}")
    return sig
