"""
income_universe_builder_v1 — read-only генератор income universe.

Собирает кандидатов из локальных правил (rules), резолвит их через УЖЕ
существующие read-only функции проекта (income_engine.build_watchlist →
resolve + income policy classification), и пишет YAML в формате, который умеет
читать modules/income_universe.py. Ничего не отправляет во внешний мир, не
меняет портфель, не использует full-токен, не скрапит и не даёт рекомендаций.

enabled: true в сгенерированном файле означает «eligible_for_analysis», а НЕ
инвестиционную рекомендацию. Кредитные рейтинги не выдумываются: если read-only
API их не даёт — поле не проставляется, в notes пишется «rating unavailable».
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

# роли инструментов
ROLE_MONEY_MARKET = "money_market"
ROLE_DIVIDEND = "dividend_candidate"
ROLE_BOND = "bond_candidate"
ROLE_OFZ = "ofz_pk_candidate"
ROLE_QUASI = "quasi_currency_bond_candidate"
ROLE_RESEARCH = "research_candidate"

# policy-бакеты, которые считаются базово-eligible
BASE_ELIGIBLE_BUCKETS = {"income_reliable", "income_variable"}

# enable-режимы
MODE_DISABLED = "disabled"
MODE_POLICY = "policy"
MODE_CONSERVATIVE = "conservative"

# роли, которые вообще можно авто-включать (bond/ofz/quasi требуют отдельной
# проверки купонного календаря/расчёта дохода и остаются disabled до неё)
ENABLE_ROLES_POLICY = {ROLE_MONEY_MARKET, ROLE_DIVIDEND}
ENABLE_ROLES_CONSERVATIVE = {ROLE_MONEY_MARKET}

# порядок профилей в сгенерированном YAML
PROFILE_ORDER = [
    "base_income", "extended_income", "money_market", "dividend_candidates",
    "bond_candidates", "ofz_pk_candidates", "quasi_currency_bond_candidates",
    "disabled_research_candidates",
]

_DEFAULT_WARNING = "candidate_for_analysis_only; not an investment recommendation"
_RULES_DEFAULT = "data/config/income_universe_rules.yaml"
_RULES_EXAMPLE = "config/income_universe_rules.example.yaml"
_BOND_BOARDS = ["TQOB", "TQCB", "TQIR", "TQOD"]


# ─── модели ───────────────────────────────────────────────────────────────────

@dataclass
class Seed:
    ticker: str
    class_code: str = ""
    role: str = ROLE_RESEARCH
    resolvable: bool = True
    seed_notes: str = ""


@dataclass
class Entry:
    ticker: str
    class_code: str
    role: str
    enabled: bool
    notes: str
    policy_bucket: str = ""
    excluded_reason: str = ""


@dataclass
class BuilderResult:
    profiles: dict[str, list[Entry]] = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    entries: list[Entry] = field(default_factory=list)


# ─── правила ──────────────────────────────────────────────────────────────────

def load_rules(path: str | None = None) -> dict:
    """Грузит rules. Приоритет: explicit → data/config → example. {} fallback."""
    for p in [x for x in (path, _RULES_DEFAULT, _RULES_EXAMPLE) if x]:
        fp = Path(p)
        if not fp.exists():
            continue
        try:
            import yaml
            data = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                data.setdefault("_source_path", str(fp))
                return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"income_universe_builder: не прочитать rules {p}: {exc}")
    return {}


def _warning(rules: dict) -> str:
    return str(((rules or {}).get("manual_notes") or {}).get("default_warning")
               or _DEFAULT_WARNING)


# ─── сбор seed-кандидатов (pure) ──────────────────────────────────────────────

def gather_seeds(rules: dict, *, max_bonds: int = 100) -> list[Seed]:
    """Собирает seed-кандидатов из rules.filters + overrides.include (без сети)."""
    filters = (rules or {}).get("filters") or {}
    seeds: list[Seed] = []

    def _class_for(flt: dict) -> str:
        codes = flt.get("allowed_class_codes") or []
        return str(codes[0]).strip().upper() if len(codes) == 1 else ""

    def _add_simple(key: str, list_key: str):
        flt = filters.get(key) or {}
        if not flt.get("enabled", False):
            return
        role = str(flt.get("role") or ROLE_RESEARCH)
        cls = _class_for(flt)
        for tk in flt.get(list_key) or []:
            t = str(tk).strip().upper()
            if t:
                seeds.append(Seed(ticker=t, class_code=cls, role=role))

    _add_simple("money_market", "candidate_tickers")
    _add_simple("shares", "candidate_tickers")

    bond_count = 0
    for key, list_key in (("ofz_pk", "candidate_secids"),
                          ("bonds", "candidate_tickers")):
        flt = filters.get(key) or {}
        if not flt.get("enabled", False):
            continue
        role = str(flt.get("role") or ROLE_BOND)
        cls = _class_for(flt)
        for tk in flt.get(list_key) or []:
            if bond_count >= max_bonds:
                break
            t = str(tk).strip().upper()
            if t:
                seeds.append(Seed(ticker=t, class_code=cls, role=role))
                bond_count += 1

    quasi = filters.get("quasi_currency_bonds") or {}
    if quasi.get("enabled", False):
        cls = _class_for(quasi)
        for tk in quasi.get("candidate_secids") or []:
            t = str(tk).strip().upper()
            if t:
                seeds.append(Seed(ticker=t, class_code=cls, role=ROLE_QUASI))
        # имена-источники: не резолвятся как тикеры → остаются disabled
        for nm in quasi.get("candidate_names") or []:
            n = str(nm).strip()
            if n:
                seeds.append(Seed(ticker=n, class_code="", role=ROLE_QUASI,
                                  resolvable=False,
                                  seed_notes="source short-name, not a verified ticker"))

    for ov in ((rules or {}).get("overrides") or {}).get("include") or []:
        if not isinstance(ov, dict):
            continue
        t = str(ov.get("ticker", "")).strip().upper()
        if not t:
            continue
        seeds.append(Seed(ticker=t, class_code=str(ov.get("class_code", "")).strip().upper(),
                          role=str(ov.get("role") or ROLE_RESEARCH),
                          seed_notes=str(ov.get("notes") or "")))

    # дедуп по (ticker, class_code)
    out: list[Seed] = []
    seen: set[tuple[str, str]] = set()
    for s in seeds:
        key = (s.ticker, s.class_code)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _disable_index(rules: dict) -> dict[str, str]:
    """{TICKER: reason} из overrides.disable (по тикеру)."""
    out: dict[str, str] = {}
    for ov in ((rules or {}).get("overrides") or {}).get("disable") or []:
        if isinstance(ov, dict) and ov.get("ticker"):
            out[str(ov["ticker"]).strip().upper()] = str(ov.get("reason") or "manual disable")
    return out


def resolver_priority(rules: dict) -> list[str]:
    """Приоритет class_code для резолвера: дефолты + allowed из rules + бонд-доски."""
    from modules.income_engine import DEFAULT_CLASS_CODE_PRIORITY
    codes = list(DEFAULT_CLASS_CODE_PRIORITY)
    for flt in ((rules or {}).get("filters") or {}).values():
        if isinstance(flt, dict):
            for c in flt.get("allowed_class_codes") or []:
                codes.append(str(c).strip().upper())
    codes += _BOND_BOARDS
    out: list[str] = []
    for c in codes:
        if c and c not in out:
            out.append(c)
    return out


# ─── классификация одного кандидата (pure) ───────────────────────────────────

def _excluded_reason(item) -> str:
    risk = getattr(item, "risk_notes", []) or []
    if "state_control_risk" in risk:
        return "state_control_risk"
    bucket = getattr(item, "policy_bucket", "")
    if bucket == "income_excluded":
        if "trailing_yield_above_cap" in (getattr(item, "policy_reasons", []) or []):
            return "trailing_yield_above_cap"
        return "policy_excluded"
    if bucket == "income_unknown":
        return "income_unknown"
    return ""


def _resolved(item) -> bool:
    return bool(getattr(item, "figi", "") or getattr(item, "instrument_uid", ""))


def classify_entry(seed: Seed, item, *, mode: str, disable_index: dict[str, str],
                   warning: str) -> Entry:
    """Строит Entry (enabled/notes/bucket) по seed + WatchlistItem (или None)."""
    role = seed.role
    cls = (getattr(item, "class_code", "") or seed.class_code or "").upper()
    bucket = getattr(item, "policy_bucket", "") if item is not None else ""
    src = getattr(item, "income_data_source", "") if item is not None else ""

    # ручной disable приоритетен
    if seed.ticker in disable_index:
        return Entry(seed.ticker, cls or seed.class_code, role, False,
                     f"disabled: override; reason={disable_index[seed.ticker]}",
                     policy_bucket=bucket, excluded_reason="override_disable")

    # нерезолвибельные имена / нерезолвленные тикеры
    if not seed.resolvable or item is None or not _resolved(item):
        extra = f"; {seed.seed_notes}" if seed.seed_notes else ""
        return Entry(seed.ticker, seed.class_code, role, False,
                     f"disabled: class_code unresolved{extra}",
                     excluded_reason="unresolved")

    excl = _excluded_reason(item)
    eligible = bucket in BASE_ELIGIBLE_BUCKETS

    if mode == MODE_DISABLED:
        allow_roles: set[str] = set()
    elif mode == MODE_CONSERVATIVE:
        allow_roles = ENABLE_ROLES_CONSERVATIVE
    else:  # policy
        allow_roles = ENABLE_ROLES_POLICY
    enabled = eligible and role in allow_roles

    if enabled:
        notes = f"auto: income_policy={bucket}; source={src}; role={role}; {warning}"
    elif excl:
        detail = "policy excluded" if excl != "income_unknown" else "income_unknown"
        notes = f"disabled: {detail}; reason={excl}"
    elif eligible:
        # eligible по policy, но роль требует отдельной проверки (bond/ofz/quasi)
        notes = (f"disabled: {role} pending coupon/income validation; "
                 f"income_policy={bucket}; source={src}; {warning}")
    elif bucket:
        notes = f"disabled: income_policy={bucket}; not base-eligible; {warning}"
    else:
        notes = f"disabled: candidate_for_audit; {warning}"
    return Entry(seed.ticker, cls or seed.class_code, role, enabled, notes,
                 policy_bucket=bucket, excluded_reason=excl)


# ─── раскладка по профилям (pure) ─────────────────────────────────────────────

def _dedup(entries: list[Entry]) -> list[Entry]:
    out: list[Entry] = []
    seen: set[str] = set()
    for e in entries:
        key = f"{(e.class_code or '').upper()}:{e.ticker.upper()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _max_items(rules: dict, profile: str, default: int = 1000) -> int:
    prof = ((rules or {}).get("profiles") or {}).get(profile) or {}
    try:
        return int(prof.get("max_items") or default)
    except (TypeError, ValueError):
        return default


def assign_profiles(entries: list[Entry], rules: dict, *,
                    include_disabled: bool = True) -> dict[str, list[Entry]]:
    """Распределяет Entry по 8 профилям выходного YAML (детерминированно)."""
    out: dict[str, list[Entry]] = {name: [] for name in PROFILE_ORDER}

    def _keep(e: Entry) -> bool:
        return include_disabled or e.enabled

    mm = [e for e in entries if e.role == ROLE_MONEY_MARKET]
    div = [e for e in entries if e.role == ROLE_DIVIDEND]
    bonds = [e for e in entries if e.role == ROLE_BOND]
    ofz = [e for e in entries if e.role == ROLE_OFZ]
    quasi = [e for e in entries if e.role == ROLE_QUASI]

    base = [e for e in (mm + div)
            if e.enabled and e.policy_bucket in BASE_ELIGIBLE_BUCKETS]
    out["base_income"] = _dedup(base)[:_max_items(rules, "base_income", 20)]
    out["extended_income"] = _dedup([e for e in (mm + div) if _keep(e)]
                                    )[:_max_items(rules, "extended_income", 100)]
    out["money_market"] = _dedup([e for e in mm if _keep(e)]
                                 )[:_max_items(rules, "money_market", 20)]
    out["dividend_candidates"] = _dedup([e for e in div if _keep(e)]
                                        )[:_max_items(rules, "dividend_candidates", 50)]
    out["bond_candidates"] = _dedup([e for e in bonds if _keep(e)]
                                    )[:_max_items(rules, "bond_candidates", 100)]
    out["ofz_pk_candidates"] = _dedup([e for e in ofz if _keep(e)]
                                      )[:_max_items(rules, "ofz_pk_candidates", 100)]
    out["quasi_currency_bond_candidates"] = _dedup(
        [e for e in quasi if _keep(e)])[:_max_items(rules, "quasi_currency_bond_candidates", 50)]
    # очередь на ручной аудит: всё не-enabled с unknown/excluded/unresolved
    audit = [e for e in entries if not e.enabled
             and (e.excluded_reason or e.policy_bucket in ("", "income_unknown", "income_excluded"))]
    out["disabled_research_candidates"] = _dedup(audit) if include_disabled else []
    return out


# ─── рендер YAML (pure) ───────────────────────────────────────────────────────

_PROFILE_DESCRIPTIONS = {
    "base_income": "Auto: cleanest base-eligible candidates (money_market + reliable/variable).",
    "extended_income": "Auto: broader income candidates for analysis/scenarios.",
    "money_market": "Auto: money-market / cash-like benchmark candidates.",
    "dividend_candidates": "Auto: dividend equity candidates for reliability audit.",
    "bond_candidates": "Auto: corporate bond candidates (verify coupon schedule before enabling).",
    "ofz_pk_candidates": "Auto: OFZ-PK / floater candidates (disabled until coupon smoke passes).",
    "quasi_currency_bond_candidates": "Auto: quasi-currency/FX bond candidates (disabled; FX/tax/liquidity risk).",
    "disabled_research_candidates": "Auto: queue for manual audit (unresolved / unknown / policy-excluded).",
}


def _header(timestamp: str, mode: str, rules_path: str) -> str:
    return (
        "# Auto-generated by build-income-universe.\n"
        "# Read-only analytics universe.\n"
        "# Not investment recommendations.\n"
        "# Do not edit generated sections manually; change rules/overrides instead.\n"
        f"# Generated at: {timestamp}\n"
        "# Source: read-only T-Invest API + local rules.\n"
        f"# Rules: {rules_path}\n"
        f"# Enable mode: {mode}\n"
        "# enabled:true means eligible for analysis, not a recommendation.\n"
    )


def render_yaml(profiles: dict[str, list[Entry]], *, timestamp: str, mode: str,
                rules_path: str) -> str:
    import yaml
    doc: dict = {"profiles": {}}
    for name in PROFILE_ORDER:
        entries = profiles.get(name) or []
        doc["profiles"][name] = {
            "description": _PROFILE_DESCRIPTIONS.get(name, ""),
            "instruments": [
                {"ticker": e.ticker, "class_code": e.class_code, "role": e.role,
                 "enabled": bool(e.enabled), "notes": e.notes}
                for e in entries
            ],
        }
    body = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return _header(timestamp, mode, rules_path) + "\n" + body


# ─── отчёт (pure) ─────────────────────────────────────────────────────────────

def build_report(seeds: list[Seed], entries: list[Entry],
                 profiles: dict[str, list[Entry]], *, mode: str, output: str,
                 rules_path: str, dry_run: bool, timestamp: str) -> dict:
    unresolved = [e.ticker for e in entries if e.excluded_reason == "unresolved"]
    policy_excluded = [e.ticker for e in entries
                       if e.excluded_reason in ("policy_excluded", "trailing_yield_above_cap")]
    unknown = [e.ticker for e in entries if e.excluded_reason == "income_unknown"]
    disabled_by_reason: dict[str, int] = {}
    for e in entries:
        if not e.enabled:
            r = e.excluded_reason or "not_base_eligible"
            disabled_by_reason[r] = disabled_by_reason.get(r, 0) + 1
    return {
        "generated_at_utc": timestamp,
        "mode": mode,
        "dry_run": dry_run,
        "output_path": output,
        "rules_path": rules_path,
        "instruments_scanned": len(seeds),
        "included_by_profile": {k: len(v) for k, v in profiles.items()},
        "enabled_by_profile": {k: sum(1 for e in v if e.enabled)
                               for k, v in profiles.items()},
        "disabled_by_reason": disabled_by_reason,
        "unresolved": unresolved,
        "policy_excluded_count": len(policy_excluded),
        "policy_excluded": policy_excluded,
        "unknown_income_count": len(unknown),
        "unknown_income": unknown,
        "generated_profiles": list(profiles.keys()),
    }


def render_report_md(report: dict) -> str:
    lines = ["# Income universe builder report — READ ONLY", "",
             "Read-only analytics. Not investment recommendations. No orders sent.", "",
             f"- mode: {report['mode']} | dry_run: {report['dry_run']}",
             f"- output: {report['output_path']}",
             f"- rules: {report['rules_path']}",
             f"- instruments scanned: {report['instruments_scanned']}",
             f"- unresolved: {len(report['unresolved'])} {report['unresolved'] or ''}",
             f"- policy-excluded: {report['policy_excluded_count']}",
             f"- unknown-income: {report['unknown_income_count']}", "",
             "Included by profile:"]
    for k in report["generated_profiles"]:
        lines.append(f"- {k}: {report['included_by_profile'].get(k, 0)} "
                     f"(enabled {report['enabled_by_profile'].get(k, 0)})")
    lines += ["", "Disabled by reason:"]
    for r, n in sorted(report["disabled_by_reason"].items()):
        lines.append(f"- {r}: {n}")
    lines += ["", "_Generated by build-income-universe; rules-driven; read-only._", ""]
    return "\n".join(lines)


# ─── оркестрация (read-only) ──────────────────────────────────────────────────

def build_universe(*, rules: dict, mode: str = MODE_DISABLED, max_bonds: int = 100,
                   include_disabled: bool = True, output: str = "", dry_run: bool = True,
                   client=None, config: dict | None = None, income_env=None,
                   fundamental_data: dict | None = None, policy_env=None,
                   watchlist_fn=None, now: _dt.datetime | None = None) -> BuilderResult:
    """
    Полный read-only прогон: seeds → resolve+classify (build_watchlist) → entries
    → профили → report. Сеть только через watchlist_fn (по умолчанию build_watchlist).
    """
    timestamp = (now or _dt.datetime.now(_dt.timezone.utc)).isoformat(timespec="seconds")
    warning = _warning(rules)
    disable_index = _disable_index(rules)
    seeds = gather_seeds(rules, max_bonds=max_bonds)

    if watchlist_fn is None:
        from modules.income_engine import build_watchlist as watchlist_fn

    raw_items: list[str] = []
    for s in seeds:
        if not s.resolvable:
            continue
        raw_items.append(f"{s.class_code}:{s.ticker}" if s.class_code else s.ticker)
    # дедуп raw_items с сохранением порядка
    raw_items = list(dict.fromkeys(raw_items))

    items_by_ticker: dict[str, object] = {}
    if raw_items:
        try:
            items = watchlist_fn(client, raw_items, config or {}, income_env,
                                 fundamental_data or {}, priority=resolver_priority(rules),
                                 policy_env=policy_env)
        except Exception as exc:  # noqa: BLE001 — данные опциональны
            logger.warning(f"income_universe_builder: watchlist недоступен: {exc}")
            items = []
        for it in items:
            items_by_ticker[str(getattr(it, "ticker", "")).upper()] = it

    entries: list[Entry] = []
    for s in seeds:
        item = items_by_ticker.get(s.ticker.upper()) if s.resolvable else None
        entries.append(classify_entry(s, item, mode=mode, disable_index=disable_index,
                                       warning=warning))

    profiles = assign_profiles(entries, rules, include_disabled=include_disabled)
    report = build_report(seeds, entries, profiles, mode=mode, output=output,
                          rules_path=str(rules.get("_source_path", "") or "rules"),
                          dry_run=dry_run, timestamp=timestamp)
    return BuilderResult(profiles=profiles, report=report, entries=entries)


def render_universe_yaml(result: BuilderResult, *, mode: str, rules_path: str,
                         now: _dt.datetime | None = None) -> str:
    timestamp = (now or _dt.datetime.now(_dt.timezone.utc)).isoformat(timespec="seconds")
    return render_yaml(result.profiles, timestamp=timestamp, mode=mode, rules_path=rules_path)
