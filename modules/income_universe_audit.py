"""
income_universe_disabled_audit — read-only диагностический отчёт по disabled-
кандидатам income universe.

Что делает:
- читает ТОЛЬКО income_universe_builder_report.json (результат build-income-universe);
- классифицирует disabled-кандидатов по группам A/B/C/D/E;
- объясняет, что нужно для их безопасного анализа (manual audit, policy review,
  coupon validation, resolver/mapping, keep disabled);
- пишет json + md в data/reports/.

Чего НЕ делает (жёсткий контракт):
- НЕ читает data/config/*.yaml;
- НЕ вызывает T-Invest API;
- НЕ меняет income policy / target portfolio / income universe / resolver;
- НЕ включает (auto-enable) ни одного disabled-кандидата;
- НЕ даёт инвестиционных рекомендаций — это аналитика, а не рекомендация.

auto_enable_allowed всегда false. recommendation_guard всегда
"candidate_for_analysis_only".
"""
from __future__ import annotations

import json
from pathlib import Path

# роли (совпадают с income_universe_builder)
ROLE_DIVIDEND = "dividend_candidate"
ROLE_BOND = "bond_candidate"
ROLE_OFZ = "ofz_pk_candidate"

DEFAULT_BUILDER_REPORT = "data/reports/income_universe_builder_report.json"
DEFAULT_OUTPUT_JSON = "data/reports/income_universe_disabled_audit.json"
DEFAULT_OUTPUT_MD = "data/reports/income_universe_disabled_audit.md"

RECOMMENDATION_GUARD = "candidate_for_analysis_only"

RECOMMENDED_NEXT_PR = (
    "local-rules audit/report only is complete; next implementation candidates: "
    "coupon-validation PR, resolver/mapping PR, or manual-income policy PR. "
    "Do not auto-enable disabled candidates."
)

# человекочитаемые названия групп для секций markdown
GROUP_NAMES = {
    "A": "manual audit",
    "B": "policy review",
    "C": "coupon validation",
    "D": "resolver/mapping",
    "E": "keep disabled",
}

# метаданные групп: почему disabled + что нужно дальше + какие PR/правила требуются
GROUP_INFO = {
    "A": {
        "name": "manual_audit",
        "why": ("Найден, но manual income не base-eligible; manual-доход нельзя "
                "считать надёжным базовым доходом без аудита."),
        "next": ("Manual audit / дизайн доверенного источника дохода. Local rules "
                 "сами по себе не меняют policy bucket."),
        "requires_code_pr": False,
        "requires_local_rules": True,
    },
    "B": {
        "name": "policy_review",
        "why": ("Estimated income не проходит base eligibility; оценочный доход по "
                "умолчанию не используется в базовом плане."),
        "next": ("Отдельное policy-решение (income policy review). Auto-enable нельзя."),
        "requires_code_pr": True,
        "requires_local_rules": False,
    },
    "C": {
        "name": "coupon_validation",
        "why": ("Облигация/флоатер: доход зависит от купонного календаря/плавающего "
                "купона и пока не валидирован."),
        "next": ("Coupon schedule / floating coupon validation / annualization guard "
                 "(отдельный PR). Auto-enable нельзя."),
        "requires_code_pr": True,
        "requires_local_rules": False,
    },
    "D": {
        "name": "resolver_mapping",
        "why": ("Short-name / неразрешённый инструмент: нет проверенного "
                "secid/ISIN/ticker/class_code."),
        "next": ("Verified secid/ISIN/ticker/class_code mapping (resolver/mapping PR "
                 "или data cleanup). Auto-enable нельзя."),
        "requires_code_pr": True,
        "requires_local_rules": False,
    },
    "E": {
        "name": "keep_disabled",
        "why": ("Risk/policy guard: оставить disabled (cap/override). Менять "
                "cap/override без отдельного review нельзя."),
        "next": ("Оставить disabled; cap/override не менять без отдельного review. "
                 "Auto-enable нельзя."),
        "requires_code_pr": False,
        "requires_local_rules": False,
    },
}

GROUP_ORDER = ["A", "B", "C", "D", "E"]


class AuditError(Exception):
    """Понятная ошибка аудита (например, нет builder-report)."""


# ─── чтение builder-report (read-only) ────────────────────────────────────────

def load_builder_report(path: str | None = None) -> dict:
    """Грузит income_universe_builder_report.json. Только чтение, без сети/config."""
    p = Path(path or DEFAULT_BUILDER_REPORT)
    if not p.exists():
        raise AuditError(
            f"Не найден builder-report: {p}. Сначала запустите "
            f"`python main.py build-income-universe`, чтобы создать "
            f"{DEFAULT_BUILDER_REPORT}, затем повторите income-universe-audit."
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise AuditError(
            f"Не удалось прочитать builder-report {p}: {exc}. Перегенерируйте его "
            f"через `python main.py build-income-universe`."
        ) from exc
    if not isinstance(data, dict):
        raise AuditError(
            f"Builder-report {p} имеет неожиданный формат (ожидался JSON-объект). "
            f"Перегенерируйте его через `python main.py build-income-universe`."
        )
    return data


def extract_disabled(report: dict) -> list[dict]:
    """Достаёт disabled-кандидатов из report['disabled_entries'] или ['entries']."""
    disabled = report.get("disabled_entries")
    if isinstance(disabled, list) and disabled:
        return [e for e in disabled if isinstance(e, dict)]
    entries = report.get("entries")
    if isinstance(entries, list):
        return [e for e in entries
                if isinstance(e, dict) and not e.get("enabled", False)]
    return []


# ─── классификация (pure) ─────────────────────────────────────────────────────

def _looks_ofz(ticker: str) -> bool:
    """OFZ-PK / SU29… secid (флоатеры) — кандидаты на coupon validation."""
    t = (ticker or "").strip().upper()
    return t.startswith("SU29") or (t.startswith("SU") and t[2:5].isdigit())


def classify_group(entry: dict) -> str:
    """
    Возвращает группу A/B/C/D/E для disabled-кандидата.

    Приоритет (если подходит под несколько условий):
        D unresolved → C coupon_validation → E explicit guards →
        A manual → B estimated → E keep_disabled.
    """
    ticker = str(entry.get("ticker") or "").strip()
    class_code = str(entry.get("class_code") or "").strip()
    role = str(entry.get("role") or "").strip()
    bucket = str(entry.get("policy_bucket") or "").strip()
    reason = str(entry.get("excluded_reason") or "").strip()
    notes = str(entry.get("notes") or "").lower()

    is_unresolved = (
        reason == "unresolved"
        or not class_code
        or "class_code unresolved" in notes
        or "short-name" in notes
    )
    is_coupon = (
        role in (ROLE_OFZ, ROLE_BOND)
        or "pending coupon" in notes
        or "coupon/income validation" in notes
        or "income validation" in notes
        or _looks_ofz(ticker)
    )
    is_guard = reason in ("override_disable", "trailing_yield_above_cap", "income_unknown")
    is_manual = role == ROLE_DIVIDEND and bucket == "income_manual"
    is_estimated = bucket == "income_estimated"

    if is_unresolved:
        return "D"
    if is_coupon:
        return "C"
    if is_guard:
        return "E"
    if is_manual:
        return "A"
    if is_estimated:
        return "B"
    return "E"


def audit_row(entry: dict) -> dict:
    """Строит одну audit-строку для disabled-кандидата."""
    group = classify_group(entry)
    info = GROUP_INFO[group]
    return {
        "ticker": str(entry.get("ticker") or ""),
        "class_code": str(entry.get("class_code") or ""),
        "role": str(entry.get("role") or ""),
        "policy_bucket": str(entry.get("policy_bucket") or ""),
        "excluded_reason": str(entry.get("excluded_reason") or ""),
        "notes": str(entry.get("notes") or ""),
        "audit_group": group,
        "audit_group_name": info["name"],
        "why_disabled": info["why"],
        "required_next_step": info["next"],
        "requires_code_pr": info["requires_code_pr"],
        "requires_local_rules": info["requires_local_rules"],
        "auto_enable_allowed": False,
        "recommendation_guard": RECOMMENDATION_GUARD,
    }


def build_audit(report: dict) -> dict:
    """Полный read-only аудит: disabled-кандидаты → строки + summary."""
    disabled = extract_disabled(report)
    candidates = [audit_row(e) for e in disabled]

    group_counts = {g: 0 for g in GROUP_ORDER}
    for row in candidates:
        group_counts[row["audit_group"]] += 1

    summary = {
        "total_disabled": len(candidates),
        "group_counts": group_counts,
        "auto_enable_allowed_count": sum(1 for r in candidates if r["auto_enable_allowed"]),
        "requires_code_pr_count": sum(1 for r in candidates if r["requires_code_pr"]),
        "requires_local_rules_count": sum(1 for r in candidates if r["requires_local_rules"]),
        "recommended_next_pr": RECOMMENDED_NEXT_PR,
    }
    return {
        "kind": "income_universe_disabled_audit",
        "read_only": True,
        "source_builder_report": report.get("output_path", ""),
        "builder_generated_at_utc": report.get("generated_at_utc", ""),
        "builder_mode": report.get("mode", ""),
        "summary": summary,
        "candidates": candidates,
    }


# ─── markdown (pure) ──────────────────────────────────────────────────────────

def _md_cell(value) -> str:
    """Безопасная ячейка markdown (экранируем |, схлопываем переносы)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def render_md(audit: dict) -> str:
    summary = audit["summary"]
    gc = summary["group_counts"]
    lines = [
        "# Income universe disabled audit — READ ONLY",
        "",
        "Аналитика, не рекомендация. Заявки не отправляются.",
        "",
        "Диагностический отчёт по disabled-кандидатам income universe. Ни один "
        "инструмент не включается автоматически (auto_enable_allowed=false для всех).",
        "",
        "## Summary",
        "",
        f"- total disabled: {summary['total_disabled']}",
        f"- group A (manual audit): {gc['A']}",
        f"- group B (policy review): {gc['B']}",
        f"- group C (coupon validation): {gc['C']}",
        f"- group D (resolver/mapping): {gc['D']}",
        f"- group E (keep disabled): {gc['E']}",
        f"- auto_enable_allowed: {summary['auto_enable_allowed_count']}",
        f"- requires code PR: {summary['requires_code_pr_count']}",
        f"- requires local rules: {summary['requires_local_rules_count']}",
        "",
        f"Recommended next PR: {summary['recommended_next_pr']}",
        "",
        "## Candidates",
        "",
        "| ticker | role | bucket | excluded_reason | group | why_disabled | "
        "required_next_step | requires_code_pr | auto_enable_allowed |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in audit["candidates"]:
        lines.append(
            f"| {_md_cell(r['ticker'])} | {_md_cell(r['role'])} | "
            f"{_md_cell(r['policy_bucket'])} | {_md_cell(r['excluded_reason'])} | "
            f"{_md_cell(r['audit_group'])} | {_md_cell(r['why_disabled'])} | "
            f"{_md_cell(r['required_next_step'])} | {_md_cell(r['requires_code_pr'])} | "
            f"{_md_cell(r['auto_enable_allowed'])} |"
        )

    for g in GROUP_ORDER:
        rows = [r for r in audit["candidates"] if r["audit_group"] == g]
        lines += ["", f"## Group {g} — {GROUP_NAMES[g]}", ""]
        info = GROUP_INFO[g]
        lines.append(f"_{info['why']}_")
        lines.append("")
        lines.append(f"Required next step: {info['next']}")
        lines.append("")
        if not rows:
            lines.append("- (нет кандидатов в этой группе)")
            continue
        for r in rows:
            tk = _md_cell(r["ticker"])
            cls = _md_cell(r["class_code"]) or "—"
            lines.append(
                f"- {tk} ({cls}, role={_md_cell(r['role'])}, "
                f"bucket={_md_cell(r['policy_bucket']) or '—'}, "
                f"reason={_md_cell(r['excluded_reason']) or '—'}) — "
                f"auto_enable_allowed=false"
            )

    lines += [
        "",
        "_Generated by income-universe-audit; read-only; не включает disabled-кандидатов._",
        "",
    ]
    return "\n".join(lines)


# ─── оркестрация (read-only запись отчётов) ───────────────────────────────────

def run_audit(*, builder_report_path: str | None = None,
              output_json: str | None = None,
              output_md: str | None = None) -> dict:
    """Читает builder-report, строит аудит, пишет json+md. Возвращает audit-словарь."""
    report = load_builder_report(builder_report_path)
    audit = build_audit(report)

    out_json = Path(output_json or DEFAULT_OUTPUT_JSON)
    out_md = Path(output_md or DEFAULT_OUTPUT_MD)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_md(audit), encoding="utf-8")
    audit["_output_json"] = str(out_json)
    audit["_output_md"] = str(out_md)
    return audit
