"""
resolver_mapping_diagnostics — read-only диагностика resolver/mapping для
неразрешённых income-кандидатов из audit group D.

Что делает:
- читает ТОЛЬКО локальный отчёт data/reports/income_universe_disabled_audit.json
  (результат income-universe-audit);
- берёт ТОЛЬКО кандидатов с audit_group == "D" (resolver/mapping);
- объясняет, почему кандидат unresolved (нет проверенного secid/ISIN/ticker/
  class_code);
- опционально (НЕ в offline-режиме) делает read-only enrichment через
  FindInstrument фасада ReadOnlyClient, чтобы СОБРАТЬ возможные совпадения для
  ручного review;
- пишет json + md в data/reports/.

Чего НЕ делает (жёсткий контракт):
- НЕ обращается к trading/order/execution API, НЕ использует full-access токен;
- НЕ скрейпит данные;
- НЕ включает (auto-enable) ни одного кандидата;
- НЕ применяет (auto-map) найденные совпадения — это только
  candidates_for_manual_review;
- НЕ меняет source candidate, income policy, target portfolio, income universe,
  builder enable logic, resolver behavior;
- НЕ пишет в data/config/*.yaml;
- НЕ даёт инвестиционных рекомендаций.

Для каждого кандидата auto_enable_allowed=false, auto_mapping_allowed=false,
recommendation_guard="candidate_for_mapping_review_only". Найденные API-совпадения
никогда не применяются автоматически — mapping остаётся ручным и отдельным
PR/изменением.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_INPUT_JSON = "data/reports/income_universe_disabled_audit.json"
DEFAULT_OUTPUT_JSON = "data/reports/income_resolver_mapping_diagnostics.json"
DEFAULT_OUTPUT_MD = "data/reports/income_resolver_mapping_diagnostics.md"

# группа audit, которую обрабатываем (resolver/mapping)
TARGET_AUDIT_GROUP = "D"

RECOMMENDATION_GUARD = "candidate_for_mapping_review_only"

# mapping_status (диагностические; ни одно значение не означает auto-map/enable)
STATUS_UNRESOLVED = "unresolved"
STATUS_CANDIDATE_MATCHES = "candidate_matches_found"
STATUS_AMBIGUOUS = "ambiguous_matches"
STATUS_NO_MATCHES = "no_matches"

REASON_UNRESOLVED = (
    "class_code unresolved: source short-name, нет проверенного "
    "secid/ISIN/ticker/class_code; инструмент не разрешён резолвером, поэтому "
    "не может попасть в income universe без ручного mapping."
)

NEXT_PR_HINT = (
    "resolver/mapping diagnostics only; следующие кандидаты на реализацию "
    "(отдельными PR): manual mapping config review, manual-income policy A/B, "
    "official floating-rate formula policy. Auto-map и auto-enable запрещены."
)


class ResolverMappingError(Exception):
    """Понятная ошибка (например, нет income_universe_disabled_audit.json)."""


# ─── чтение локального audit-отчёта (read-only) ───────────────────────────────

def load_audit_report(path: str | None = None) -> dict:
    """Грузит income_universe_disabled_audit.json. Только чтение, без сети/config."""
    p = Path(path or DEFAULT_INPUT_JSON)
    if not p.exists():
        raise ResolverMappingError(
            f"Не найден audit-отчёт: {p}. Сначала выполните:\n"
            f"  python main.py build-income-universe --force\n"
            f"  python main.py income-universe-audit\n"
            f"затем повторите income-resolver-mapping-diagnostics."
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ResolverMappingError(
            f"Не удалось прочитать audit-отчёт {p}: {exc}. "
            f"Перегенерируйте его (python main.py income-universe-audit)."
        ) from exc
    if not isinstance(data, dict):
        raise ResolverMappingError(
            f"Audit-отчёт {p} имеет неожиданный формат (ожидался JSON-объект). "
            f"Перегенерируйте его (python main.py income-universe-audit)."
        )
    return data


def extract_group_d(report: dict) -> list[dict]:
    """Достаёт ТОЛЬКО кандидатов с audit_group == 'D' из audit-отчёта."""
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [
        c for c in candidates
        if isinstance(c, dict)
        and str(c.get("audit_group") or "").strip().upper() == TARGET_AUDIT_GROUP
    ]


# ─── read-only API enrichment (опционально, без auto-map) ──────────────────────

def _match_row(instrument: dict) -> dict:
    """Нормализует один FindInstrument-результат в строку для ручного review.

    Только справочные поля; найденное совпадение НЕ применяется автоматически.
    """
    figi = str(instrument.get("figi") or "")
    uid = str(instrument.get("uid") or instrument.get("instrumentUid") or "")
    return {
        "ticker": str(instrument.get("ticker") or ""),
        "class_code": str(instrument.get("classCode") or ""),
        "figi": figi,
        "uid": uid,
        "isin": str(instrument.get("isin") or ""),
        "name": str(instrument.get("name") or ""),
        "instrument_type": str(
            instrument.get("instrumentType")
            or instrument.get("instrumentKind") or ""),
        "currency": str(instrument.get("currency") or ""),
        "exchange": str(instrument.get("exchange") or ""),
        "match_reason": "find_instrument_query",
    }


def _enrich_from_api(candidate: dict, client) -> list[dict]:
    """Read-only попытка найти совпадения через FindInstrument. Без сети → [].

    Использует ТОЛЬКО read-only метод фасада ReadOnlyClient.find_instruments.
    Любая ошибка деградирует в пустой список (offline-like), без падения.
    Найденные совпадения — кандидаты на ручной review, НЕ applied mapping.
    """
    query = str(candidate.get("ticker") or "").strip()
    if not query:
        return []
    try:
        found = client.find_instruments(query)
    except Exception:  # noqa: BLE001 — обогащение опционально
        return []
    if not isinstance(found, list):
        return []
    return [_match_row(i) for i in found if isinstance(i, dict)]


def _mapping_status(matches: list[dict], *, enrichment_active: bool) -> str:
    """Определяет mapping_status по числу найденных совпадений.

    Без активного enrichment (offline или нет client) — всегда unresolved:
    попытки сопоставления не было.
    """
    if not enrichment_active:
        return STATUS_UNRESOLVED
    if len(matches) == 0:
        return STATUS_NO_MATCHES
    if len(matches) == 1:
        return STATUS_CANDIDATE_MATCHES
    return STATUS_AMBIGUOUS


# ─── строка кандидата (pure + опц. API) ────────────────────────────────────────

def build_candidate_row(candidate: dict, *, client=None) -> dict:
    """Строит одну resolver/mapping-строку для group D кандидата.

    client=None → offline-режим (без сети, mapping_status=unresolved).
    client задан → read-only enrichment через FindInstrument; найденные совпадения
    попадают в candidates_for_manual_review, но auto_mapping_allowed остаётся
    false. auto_enable_allowed всегда false.
    """
    enrichment_active = client is not None
    matches = _enrich_from_api(candidate, client) if enrichment_active else []
    status = _mapping_status(matches, enrichment_active=enrichment_active)
    return {
        "original_ticker": str(candidate.get("ticker") or ""),
        "name": str(candidate.get("name") or ""),
        "role": str(candidate.get("role") or ""),
        "policy_bucket": str(candidate.get("policy_bucket") or ""),
        "excluded_reason": str(candidate.get("excluded_reason") or ""),
        "class_code": str(candidate.get("class_code") or ""),
        "notes": str(candidate.get("notes") or ""),
        "reason": REASON_UNRESOLVED,
        "mapping_status": status,
        "candidates_for_manual_review": matches,
        "match_count": len(matches),
        "auto_enable_allowed": False,
        "auto_mapping_allowed": False,
        "recommendation_guard": RECOMMENDATION_GUARD,
    }


# ─── сборка отчёта ────────────────────────────────────────────────────────────

def build_report(group_d: list[dict], *, client=None) -> dict:
    """Полный resolver/mapping отчёт по group D кандидатам.

    client=None → offline (без сети). client задан → read-only enrichment.
    auto_mapping_allowed / auto_enable_allowed всегда false для всех кандидатов.
    """
    rows = [build_candidate_row(c, client=client) for c in group_d]

    by_mapping_status: dict[str, int] = {}
    for r in rows:
        by_mapping_status[r["mapping_status"]] = \
            by_mapping_status.get(r["mapping_status"], 0) + 1

    def _count(status: str) -> int:
        return sum(1 for r in rows if r["mapping_status"] == status)

    summary = {
        "total_candidates": len(rows),
        "unresolved_count": _count(STATUS_UNRESOLVED),
        "candidate_matches_found_count": _count(STATUS_CANDIDATE_MATCHES),
        "ambiguous_matches_count": _count(STATUS_AMBIGUOUS),
        "no_matches_count": _count(STATUS_NO_MATCHES),
        "auto_mapping_allowed_count": sum(
            1 for r in rows if r["auto_mapping_allowed"]),
        "auto_enable_allowed_count": sum(
            1 for r in rows if r["auto_enable_allowed"]),
        "by_mapping_status": by_mapping_status,
        "recommended_next_pr": NEXT_PR_HINT,
    }
    return {
        "kind": "income_resolver_mapping_diagnostics",
        "read_only": True,
        "mode": "api" if client is not None else "offline",
        "audit_group": TARGET_AUDIT_GROUP,
        "recommendation_guard": RECOMMENDATION_GUARD,
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


def _match_line(m: dict) -> str:
    return (
        f"  - {_md_cell(m['ticker'])} ({_md_cell(m['class_code'])}) — "
        f"name={_md_cell(m['name'])}, type={_md_cell(m['instrument_type'])}, "
        f"figi={_md_cell(m['figi'])}, uid={_md_cell(m['uid'])}, "
        f"isin={_md_cell(m['isin'])}, currency={_md_cell(m['currency'])}, "
        f"exchange={_md_cell(m['exchange'])}, "
        f"match_reason={_md_cell(m['match_reason'])} "
        f"(candidate_for_mapping_review_only)"
    )


def _row_block(r: dict) -> list[str]:
    lines = [
        f"### {_md_cell(r['original_ticker'])}",
        "",
        f"- role: {_md_cell(r['role'])}",
        f"- policy_bucket: {_md_cell(r['policy_bucket'])}",
        f"- class_code: {_md_cell(r['class_code'])}",
        f"- excluded_reason: {_md_cell(r['excluded_reason'])}",
        f"- notes: {_md_cell(r['notes'])}",
        f"- reason: {_md_cell(r['reason'])}",
        f"- mapping_status: {_md_cell(r['mapping_status'])}",
        "- auto_enable_allowed=false",
        "- auto_mapping_allowed=false",
        "- guard: candidate_for_mapping_review_only",
    ]
    if r["candidates_for_manual_review"]:
        lines.append(f"- candidates_for_manual_review ({r['match_count']}):")
        lines += [_match_line(m) for m in r["candidates_for_manual_review"]]
    else:
        lines.append("- candidates_for_manual_review: (нет)")
    lines.append("")
    return lines


def render_md(report: dict) -> str:
    s = report["summary"]
    rows = report["candidates"]

    lines = [
        "# Income resolver/mapping diagnostics — READ ONLY (group D)",
        "",
        "Аналитика, не рекомендация. Заявки не отправляются.",
        "Ни один инструмент не включается автоматически (auto_enable_allowed=false).",
        "Ни одно совпадение не применяется автоматически (auto_mapping_allowed=false).",
        "Каждый кандидат — candidate_for_mapping_review_only.",
        "",
        f"Режим: {_md_cell(report.get('mode'))}.",
        "",
        "## Важно про найденные совпадения",
        "",
        "- Найденные через read-only FindInstrument совпадения **не применяются "
        "автоматически** — это только `candidates_for_manual_review`.",
        "- Mapping должен быть **ручным и отдельным PR/изменением**; даже один "
        "точный match оставляет `auto_mapping_allowed=false`.",
        "- Этот отчёт **не меняет** universe / config / portfolio / resolver / "
        "builder enable logic / income policy.",
        "",
        "## Summary",
        "",
        f"- total_candidates: {s['total_candidates']}",
        f"- unresolved_count: {s['unresolved_count']}",
        f"- candidate_matches_found_count: {s['candidate_matches_found_count']}",
        f"- ambiguous_matches_count: {s['ambiguous_matches_count']}",
        f"- no_matches_count: {s['no_matches_count']}",
        f"- auto_mapping_allowed_count: {s['auto_mapping_allowed_count']}",
        f"- auto_enable_allowed_count: {s['auto_enable_allowed_count']}",
        "",
        "by_mapping_status:",
    ]
    if s["by_mapping_status"]:
        for k, v in sorted(s["by_mapping_status"].items()):
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- —")

    lines += ["", "## Кандидаты (group D — resolver/mapping)", ""]
    if rows:
        for r in rows:
            lines += _row_block(r)
    else:
        lines.append("_(нет group D кандидатов)_")

    lines += [
        "",
        f"Следующие шаги (отдельными PR): {s['recommended_next_pr']}",
        "",
        "## Safety contract",
        "",
        "- read-only: только локальный audit-отчёт + опциональный read-only "
        "FindInstrument; заявок/исполнения/live/full-access токена нет;",
        "- найденные совпадения не применяются автоматически "
        "(auto_mapping_allowed=false);",
        "- ни один кандидат не включается автоматически "
        "(auto_enable_allowed=false);",
        "- mapping остаётся ручным и отдельным изменением;",
        "- не меняет income policy, target portfolio, resolver, builder enable "
        "logic, Telegram;",
        "- не пишет в data/config; это аналитика, не инвестиционная рекомендация.",
        "",
        "_Generated by income-resolver-mapping-diagnostics; read-only; не включает "
        "и не маппит кандидатов автоматически._",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация / оркестрация ───────────────────────────────────────────────

def run(*, input_json: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None,
        offline: bool = False,
        client=None) -> dict:
    """Читает audit-отчёт, строит resolver/mapping диагностику, пишет json+md.

    offline=True или client=None → без сети (mapping_status=unresolved).
    offline=False + client → read-only enrichment через FindInstrument.
    Возвращает отчёт-словарь (+ пути в _output_json / _output_md).
    """
    report_in = load_audit_report(input_json)
    group_d = extract_group_d(report_in)
    active_client = None if offline else client
    report = build_report(group_d, client=active_client)

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
