"""
floating_coupon_policy — read-only policy-диагностика floating-coupon кандидатов
(ОФЗ-ПК / SU29…) из отчёта income-coupon-validation.

Что делает:
- читает ТОЛЬКО локальный отчёт data/reports/income_coupon_validation.json;
- выбирает ТОЛЬКО floating-coupon / OFZ-ПК кандидатов;
- объясняет, что нужно для будущей floating-coupon policy, и почему сейчас
  annualization и forecast доходности запрещены;
- пишет json + md в data/reports/.

Чего НЕ делает (жёсткий контракт):
- НЕ обращается к trading/order/execution API, НЕ использует full-access токен;
- НЕ обращается к сети вообще — работает только по локальному отчёту;
- НЕ меняет income policy / target portfolio / income universe builder enable
  logic / resolver behavior;
- НЕ пишет в data/config/*.yaml;
- НЕ включает (auto-enable) ни одного кандидата;
- НЕ считает прогноз доходности как факт и НЕ даёт инвестиционных рекомендаций.

Для каждого кандидата forecast_allowed=false, annualization_allowed=false,
auto_enable_allowed=false; policy_status="needs_floating_coupon_policy",
readiness="policy_required", forecast_method="not_supported_yet".
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_INPUT_JSON = "data/reports/income_coupon_validation.json"
DEFAULT_OUTPUT_JSON = "data/reports/income_floating_coupon_policy.json"
DEFAULT_OUTPUT_MD = "data/reports/income_floating_coupon_policy.md"

# диагностические placeholders (ни одно значение НЕ означает auto-enable/forecast)
FORECAST_METHOD = "not_supported_yet"
POLICY_STATUS = "needs_floating_coupon_policy"
READINESS = "policy_required"
RECOMMENDATION_GUARD = "candidate_for_analysis_only"

# coupon_validation_status, который считаем floating
STATUS_FLOATING = "floating_coupon_detected"

# что потребуется для будущей утверждённой floating-coupon policy
POLICY_REQUIREMENTS = [
    "coupon formula / reference rate policy",
    "RUONIA/key-rate/official coupon mechanism policy",
    "historical coupon observation",
    "liquidity/price source check",
    "maturity/date sanity check",
    "manual policy approval",
]

REASON_FLOATING = (
    "floating coupon (ОФЗ-ПК): будущая ставка купона неизвестна; наивный annualize "
    "последней/следующей выплаты дал бы ложную доходность; forecast доходности как "
    "факт запрещён до отдельной утверждённой floating-coupon policy."
)

NEXT_PR_HINT = (
    "floating-coupon policy diagnostics only; следующие кандидаты на реализацию "
    "(отдельными PR): official reference-rate policy design, resolver/mapping group "
    "D, manual income policy A/B. Auto-enable кандидатов запрещён."
)


class FloatingCouponPolicyError(Exception):
    """Понятная ошибка (например, нет income_coupon_validation.json)."""


# ─── чтение локального отчёта (read-only) ─────────────────────────────────────

def load_validation_report(path: str | None = None) -> dict:
    p = Path(path or DEFAULT_INPUT_JSON)
    if not p.exists():
        raise FloatingCouponPolicyError(
            f"Не найден отчёт coupon-validation: {p}. Сначала выполните:\n"
            f"  python main.py build-income-universe --force\n"
            f"  python main.py income-universe-audit\n"
            f"  python main.py income-coupon-validation"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise FloatingCouponPolicyError(
            f"Не удалось прочитать отчёт {p}: {exc}. "
            f"Перегенерируйте его (python main.py income-coupon-validation)."
        ) from exc
    if not isinstance(data, dict):
        raise FloatingCouponPolicyError(
            f"Отчёт {p} имеет неожиданный формат (ожидался JSON-объект). "
            f"Перегенерируйте его (python main.py income-coupon-validation)."
        )
    return data


# ─── классификация floating / OFZ-ПК (pure) ───────────────────────────────────

def _looks_ofz_floater(ticker: str) -> bool:
    """OFZ-ПК / SU29… secid (флоатеры)."""
    t = (ticker or "").strip().upper()
    return t.startswith("SU29") or (t.startswith("SU") and t[2:5].isdigit())


def is_floating_candidate(candidate: dict) -> bool:
    """True, если кандидат — floating-coupon / OFZ-ПК.

    Floating определяется по любому из признаков coupon-validation отчёта:
    coupon_type, coupon_validation_status или OFZ-ПК-тикер. Не угадывает доход.
    """
    if not isinstance(candidate, dict):
        return False
    coupon_type = str(candidate.get("coupon_type") or "").strip().lower()
    status = str(candidate.get("coupon_validation_status") or "").strip().lower()
    ticker = str(candidate.get("ticker") or "")
    return (coupon_type == "floating"
            or status == STATUS_FLOATING
            or _looks_ofz_floater(ticker))


# ─── строка кандидата (pure) ──────────────────────────────────────────────────

def build_candidate_row(candidate: dict) -> dict:
    """Строит одну policy-строку для floating-coupon кандидата.

    forecast_allowed / annualization_allowed / auto_enable_allowed всегда False.
    """
    ticker = str(candidate.get("ticker") or "")
    return {
        "ticker": ticker,
        "secid": ticker,
        "class_code": str(candidate.get("class_code") or ""),
        "role": str(candidate.get("role") or ""),
        "name": candidate.get("name"),
        "coupon_validation_status": str(
            candidate.get("coupon_validation_status") or ""),
        "income_readiness": str(candidate.get("income_readiness") or ""),
        "floating_coupon_detected": True,
        "annualization_allowed": False,
        "forecast_allowed": False,
        "auto_enable_allowed": False,
        "analysis_only": True,
        "reason": REASON_FLOATING,
        "source_block_reason": str(
            candidate.get("annualization_block_reason") or ""),
        "forecast_method": FORECAST_METHOD,
        "policy_status": POLICY_STATUS,
        "readiness": READINESS,
        "recommendation_guard": RECOMMENDATION_GUARD,
        "policy_requirements": list(POLICY_REQUIREMENTS),
    }


# ─── сборка отчёта ────────────────────────────────────────────────────────────

def build_report(validation_report: dict) -> dict:
    """Полный floating-coupon policy отчёт по floating-кандидатам входного отчёта."""
    candidates = validation_report.get("candidates")
    if not isinstance(candidates, list):
        candidates = []

    rows = [build_candidate_row(c) for c in candidates
            if is_floating_candidate(c)]

    by_policy_status: dict[str, int] = {}
    by_readiness: dict[str, int] = {}
    for r in rows:
        by_policy_status[r["policy_status"]] = \
            by_policy_status.get(r["policy_status"], 0) + 1
        by_readiness[r["readiness"]] = by_readiness.get(r["readiness"], 0) + 1

    summary = {
        "total_candidates": len(candidates),
        "floating_coupon_candidates": len(rows),
        "annualization_allowed_count": sum(
            1 for r in rows if r["annualization_allowed"]),
        "forecast_allowed_count": sum(1 for r in rows if r["forecast_allowed"]),
        "auto_enable_allowed_count": sum(
            1 for r in rows if r["auto_enable_allowed"]),
        "by_policy_status": by_policy_status,
        "by_readiness": by_readiness,
        "recommended_next_pr": NEXT_PR_HINT,
    }
    return {
        "kind": "income_floating_coupon_policy",
        "read_only": True,
        "policy_status": POLICY_STATUS,
        "forecast_method": FORECAST_METHOD,
        "summary": summary,
        "candidates": rows,
    }


# ─── markdown (pure) ──────────────────────────────────────────────────────────

def _md_cell(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _row_line(r: dict) -> str:
    return (
        f"- {_md_cell(r['ticker'])} ({_md_cell(r['class_code'])}, "
        f"role={_md_cell(r['role'])}) — "
        f"status={_md_cell(r['coupon_validation_status'])}, "
        f"readiness={_md_cell(r['readiness'])}, "
        f"policy_status={_md_cell(r['policy_status'])}; "
        f"annualization_allowed=false, forecast_allowed=false, "
        f"auto_enable_allowed=false; forecast_method={_md_cell(r['forecast_method'])}. "
        f"Причина: {_md_cell(r['reason'])}"
    )


def render_md(report: dict) -> str:
    s = report["summary"]
    rows = report["candidates"]

    lines = [
        "# Floating coupon policy — read-only (ОФЗ-ПК)",
        "",
        "Аналитика, не рекомендация. Заявки не отправляются.",
        "Ни один инструмент не включается автоматически (auto_enable_allowed=false).",
        "forecast_allowed=false, annualization_allowed=false для всех кандидатов.",
        "",
        "## Почему floating coupon не annualize-ится и не прогнозируется",
        "",
        "- ОФЗ-ПК (SU29…) имеют плавающий (floating) купон: будущая ставка зависит "
        "от внешнего ориентира (RUONIA / ключевая ставка) и официального механизма "
        "расчёта купона.",
        "- Наивно annualize-ить последнюю или следующую выплату нельзя — это дало бы "
        "ложную доходность.",
        "- Пока нет отдельной утверждённой политики floating-coupon forecast, эти "
        "инструменты остаются только `candidate_for_analysis_only`.",
        "- Отчёт не является сигналом к сделке и не является инвестиционной "
        "рекомендацией.",
        "",
        "## Summary",
        "",
        f"- total candidates: {s['total_candidates']}",
        f"- floating coupon candidates: {s['floating_coupon_candidates']}",
        f"- annualization_allowed_count: {s['annualization_allowed_count']}",
        f"- forecast_allowed_count: {s['forecast_allowed_count']}",
        f"- auto_enable_allowed_count: {s['auto_enable_allowed_count']}",
        "",
        "by_policy_status:",
    ]
    if s["by_policy_status"]:
        for k, v in sorted(s["by_policy_status"].items()):
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- —")
    lines += ["", "by_readiness:"]
    if s["by_readiness"]:
        for k, v in sorted(s["by_readiness"].items()):
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- —")
    lines += ["", "## Кандидаты (ОФЗ-ПК, floating coupon)", ""]
    if rows:
        lines += [_row_line(r) for r in rows]
    else:
        lines.append("_(нет floating-кандидатов)_")
    lines += [
        "",
        "## Что нужно для будущей floating-coupon policy",
        "",
    ]
    lines += [f"- {req}" for req in POLICY_REQUIREMENTS]
    lines += [
        "",
        f"Следующие шаги (отдельными PR): {s['recommended_next_pr']}",
        "",
        "## Safety contract",
        "",
        "- read-only: только локальный отчёт income-coupon-validation, без сети;",
        "- заявки не отправляются, исполнения нет, live нет, full-access токена нет;",
        "- floating coupon не annualize-ится и не прогнозируется как факт;",
        "- annualization_allowed=false, forecast_allowed=false, "
        "auto_enable_allowed=false для всех кандидатов;",
        "- не меняет income policy, target portfolio, resolver, builder enable logic;",
        "- это аналитика, не инвестиционная рекомендация.",
        "",
        "_Generated by income-floating-coupon-policy; read-only; не включает "
        "кандидатов._",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация / оркестрация ───────────────────────────────────────────────

def run(*, input_json: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None) -> dict:
    """Читает income_coupon_validation.json, строит policy-отчёт, пишет json+md.

    Возвращает отчёт-словарь (+ пути в _output_json / _output_md). Без сети.
    """
    validation_report = load_validation_report(input_json)
    report = build_report(validation_report)

    out_json = Path(output_json or DEFAULT_OUTPUT_JSON)
    out_md = Path(output_md or DEFAULT_OUTPUT_MD)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8")
    out_md.write_text(render_md(report), encoding="utf-8")
    report["_output_json"] = str(out_json)
    report["_output_md"] = str(out_md)
    return report
