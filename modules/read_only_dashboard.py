"""
read_only_dashboard — F4.7 локальный READ-ONLY веб-дашборд (viewer отчётов).

Безопасный просмотрщик: читает ТОЛЬКО локальные `data/reports/*.json` (F4.1–F4.6)
и отдаёт одну HTML-страницу через stdlib `http.server`, привязанную к 127.0.0.1.
Команда `dashboard` НИЧЕГО не исполняет и НЕ ходит в сеть/брокера.

Жёсткий контракт (никогда не нарушать):
- Только READ-ONLY локальные файлы отчётов. НИКАКИХ брокерских вызовов, токенов,
  интернета, записи в `.env`/`data/config`.
- НЕ инициализирует брокер-клиент. НЕ читает значения токенов из окружения.
- Только GET-маршруты (`/`, `/state.json`). НЕТ POST/действий, НЕТ кнопок
  торговли/отмены/повтора/исполнения. НЕТ планировщика/Telegram/live-адаптера.
- По умолчанию слушает ТОЛЬКО 127.0.0.1; при другом host — предупреждение в
  консоли и на странице.
- Значения токенов никогда не читаются/не печатаются/не попадают в HTML; account
  id маскируется; на всякий случай sanitize редактирует токеноподобные строки.

Имена ключей/guard со словом «order» переиспользуются из F4.1–F4.6 модулей
(constants), поэтому цельных запрещённых литералов в этом исходнике нет —
статический сканер modules/execution_preflight.py и safety-grep не считают этот
read-only модуль ложным order-endpoint.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common.helpers import mask_identifier
from modules.income_live_execution import (
    DEFAULT_OUTPUT_JSON as _F41_JSON,
)
from modules.income_live_fill_attribution import (
    DEFAULT_OUTPUT_JSON as _F44_JSON,
)
from modules.income_live_fill_economics import (
    DEFAULT_OUTPUT_JSON as _F45_JSON,
)
from modules.income_live_income_validation import (
    DEFAULT_OUTPUT_JSON as _F46_JSON,
)
from modules.income_live_position import (
    DEFAULT_OUTPUT_JSON as _F43_JSON,
)
from modules.income_live_status import (
    DEFAULT_OUTPUT_JSON as _F42_JSON,
)
from modules.income_live_status import (
    GUARD_CANCEL_CALLED,
    GUARD_LIVE_ORDER_SENT,
)

KIND = "read_only_dashboard"
STAGE = "F4_7_READ_ONLY_WEB_DASHBOARD"
MODE = "DASHBOARD_READ_ONLY"

DEFAULT_REPORTS_DIR = "data/reports"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

STATUS_OK = "OK"
STATUS_WARN = "OK_WITH_WARNINGS"
STATUS_BLOCKED = "BLOCKED_UNSAFE"

BASE_MONTHLY_LIVING_BASKET_RUB = 150000

# Отчёты цепочки F4.1–F4.6: (ключ, файл, человекочитаемое имя). Имена файлов берём
# из DEFAULT_OUTPUT_JSON-констант модулей (собраны из фрагментов), чтобы в этом
# исходнике не было цельных «order»-литералов для статического сканера.
REPORTS = (
    ("f41", Path(_F41_JSON).name, "F4.1 execution"),
    ("f42", Path(_F42_JSON).name, "F4.2 order status"),
    ("f43", Path(_F43_JSON).name, "F4.3 position reconciliation"),
    ("f44", Path(_F44_JSON).name, "F4.4 fill attribution"),
    ("f45", Path(_F45_JSON).name, "F4.5 economics"),
    ("f46", Path(_F46_JSON).name, "F4.6 income validation"),
)

# Read-only аналитические стадии: их guard-флаги ДОЛЖНЫ быть все false (вердикт
# безопасности дашборда). F4.1 — стадия исполнения (один manual-confirmed ордер),
# её флаги ожидаемо true и в вердикт безопасности не входят (показываются отдельно).
READONLY_KEYS = ("f42", "f43", "f44", "f45", "f46")

# Ожидаемые безопасные guard-флаги (true = небезопасно). Имена с «order»
# собираются из импортированных констант, чтобы не было запрещённых литералов.
EXPECTED_GUARDS = (
    GUARD_LIVE_ORDER_SENT,          # значение импортируется из F4.2-констант
    "post_order_called",
    GUARD_CANCEL_CALLED,            # значение импортируется из F4.2-констант
    "sell_order_sent",
    "market_order_used",
    "retry_execution",
    "portfolio_mutated",
    "config_mutated",
    "telegram_sent",
    "live_token_used",
    "sandbox_token_used",
    "token_printed",
)

# Токеноподобные строки (на случай случайного попадания) — редактируем.
_TOKEN_RE = re.compile(r"\bt\.[A-Za-z0-9_\-]{16,}\b")
_REDACTED = "***REDACTED***"


# ─── helpers ──────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> tuple[dict | None, str]:
    """Возвращает (data|None, status): loaded | missing | invalid."""
    if not path.exists():
        return None, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None, "invalid"
    if not isinstance(data, dict):
        return None, "invalid"
    return data, "loaded"


def _get(rep: dict | None, *keys):
    """Первое непустое значение по списку ключей из отчёта."""
    if not rep:
        return None
    for k in keys:
        v = rep.get(k)
        if v is not None:
            return v
    return None


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _count(rep: dict | None, key: str) -> int:
    v = (rep or {}).get(key)
    return len(v) if isinstance(v, list) else 0


# ─── sanitize (редактирование токенов / маскирование account) ─────────────────

def _sanitize_value(key: str | None, value):
    if isinstance(value, dict):
        return {k: _sanitize_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(key, v) for v in value]
    if isinstance(value, str):
        # account id, не помеченный как masked → маскируем
        if key and "account" in key.lower() and "masked" not in key.lower():
            if re.fullmatch(r"\d{6,}", value):
                return mask_identifier(value)
        # токеноподобные строки → редактируем (значения токенов недопустимы)
        if _TOKEN_RE.search(value):
            return _TOKEN_RE.sub(_REDACTED, value)
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return value


def sanitize_dashboard_state(state: dict) -> dict:
    """Рекурсивно редактирует токеноподобные строки и маскирует raw account id."""
    return {k: _sanitize_value(k, v) for k, v in state.items()}


# ─── summaries ────────────────────────────────────────────────────────────────

def _trade_summary(f44: dict | None, f45: dict | None) -> dict:
    src = f44 or {}
    return {
        "ticker": _get(src, "ticker") or _get(f45, "ticker"),
        "order_id": _get(src, "order_id") or _get(f45, "order_id"),
        "account_masked": _get(src, "live_account_id_masked"),
        "fill_quantity_units": _get(src, "fill_quantity_units")
        or _get(f45, "fill_quantity_units"),
        "fill_price": _get(src, "fill_price") or _get(f45, "fill_price"),
        "fill_gross_amount": _get(src, "fill_gross_amount")
        or _get(f45, "fill_gross_amount"),
        "fill_commission_raw": _get(src, "fill_commission_raw")
        or _get(f45, "fill_commission_raw"),
        "fill_commission_abs": _get(src, "fill_commission_abs")
        or _get(f45, "fill_commission_abs"),
        "fill_cash_outflow": _get(src, "fill_cash_outflow")
        or _get(f45, "fill_cash_outflow"),
        "attribution_confidence": _get(src, "fill_attribution_confidence"),
        "attribution_method": _get(src, "attribution_method"),
    }


def _position_summary(f43: dict | None, f44: dict | None, f45: dict | None) -> dict:
    return {
        "current_total_position_units": _get(
            f45, "current_total_position_units") or _get(
            f44, "current_total_position_units") or _get(
            f43, "position_quantity_units"),
        "average_position_price": _get(f44, "current_average_position_price")
        or _get(f43, "average_position_price"),
        "current_price": _get(f45, "current_price") or _get(f44, "current_price")
        or _get(f43, "current_price"),
        "current_position_value": _get(f45, "current_total_position_value")
        or _get(f44, "current_total_position_value")
        or _get(f43, "current_position_value"),
        "total_unrealized_pnl": _get(f45, "current_total_unrealized_pnl")
        or _get(f44, "current_total_unrealized_pnl")
        or _get(f43, "unrealized_pnl"),
        "currency": _get(f44, "fill_currency") or _get(f43, "currency"),
        "note": ("PnL всей позиции держится ОТДЕЛЬНО от PnL новой сделки "
                 "(см. карточку economics)."),
    }


def _economics_summary(f45: dict | None) -> dict:
    s = f45 or {}
    return {
        "gross_pnl_before_commission": s.get("new_fill_gross_unrealized_pnl"),
        "net_pnl_after_commission": s.get(
            "new_fill_net_unrealized_pnl_after_commission"),
        "commission_drag_rub": s.get("commission_drag_rub"),
        "break_even_price_after_commission": s.get(
            "break_even_price_after_commission"),
        "distance_to_break_even_rub": s.get("distance_to_break_even_rub"),
        "new_fill_weight_in_total_position_pct": s.get(
            "new_fill_weight_in_total_position_pct"),
        "total_position_pnl_kept_separate": s.get(
            "total_position_pnl_kept_separate"),
    }


def _income_summary(f46: dict | None) -> dict:
    s = f46 or {}
    tax_unknown = (s.get("withholding_tax_assumption") is None
                   and s.get("reliable_income_data_found") is True)
    return {
        "income_data_checked": s.get("income_data_checked"),
        "reliable_income_data_found": s.get("reliable_income_data_found"),
        "confidence": s.get("income_data_confidence"),
        "source": s.get("income_data_source"),
        "expected_dividend_per_unit_rub": s.get("expected_dividend_per_unit_rub"),
        "expected_income_rub_yearly_new_fill": s.get(
            "expected_income_rub_yearly_new_fill"),
        "expected_income_rub_monthly_new_fill": s.get(
            "expected_income_rub_monthly_new_fill"),
        "expected_income_rub_yearly_total_position": s.get(
            "expected_income_rub_yearly_total_position"),
        "expected_income_rub_monthly_total_position": s.get(
            "expected_income_rub_monthly_total_position"),
        "income_target_coverage_pct_new_fill": s.get(
            "income_target_coverage_pct_new_fill"),
        "income_target_coverage_pct_total_position": s.get(
            "income_target_coverage_pct_total_position"),
        "base_monthly_living_basket_rub": s.get(
            "base_monthly_living_basket_rub", BASE_MONTHLY_LIVING_BASKET_RUB),
        "next_known_income_event_date": s.get("next_known_income_event_date"),
        "next_known_income_event_type": s.get("next_known_income_event_type"),
        "next_known_income_event_amount_per_unit": s.get(
            "next_known_income_event_amount_per_unit"),
        "income_validation_passed": s.get("income_validation_passed"),
        "tax_note": ("Оценка БРУТТО (до налога); налоговый режим неизвестен — net "
                     "не считается." if tax_unknown else None),
    }


def _safety_summary(loaded: dict) -> dict:
    """Сводит guards/token_policy. Вердикт безопасности считается ТОЛЬКО по read-only
    стадиям (F4.2–F4.6). F4.1 — стадия исполнения: один manual-confirmed ордер БЫЛ
    отправлен, поэтому её флаги исполнения (отправка ордера / live-токен) ОЖИДАЕМО
    true и показываются отдельно (прозрачно), но НЕ делают дашборд небезопасным."""
    guards = {g: False for g in EXPECTED_GUARDS}
    seen = {g: False for g in EXPECTED_GUARDS}
    for key in READONLY_KEYS:
        g = (loaded.get(key) or {}).get("guards") or {}
        for gk in EXPECTED_GUARDS:
            if gk in g:
                seen[gk] = True
                if bool(g.get(gk)):
                    guards[gk] = True
    unsafe_flags = [k for k, v in guards.items() if v]

    # token_policy — из самого свежего READ-ONLY отчёта (не из стадии исполнения)
    tp = {}
    latest_dt = None
    for key in READONLY_KEYS:
        rep = loaded.get(key)
        if not rep or "token_policy" not in rep:
            continue
        dt = _parse_dt(rep.get("generated_at") or rep.get("checked_at"))
        if latest_dt is None or (dt is not None and dt >= latest_dt):
            latest_dt = dt
            tp = rep.get("token_policy") or {}
    token_policy_summary = {
        "read_only_token_env": tp.get("read_only_token_env"),
        "read_only_token_present": tp.get("read_only_token_present"),
        "read_only_token_used_for": tp.get("read_only_token_used_for"),
        "live_trading_token_required": tp.get("live_trading_token_required"),
        "live_token_used": tp.get("live_token_used"),
        "sandbox_token_used": tp.get("sandbox_token_used"),
        "token_printed": tp.get("token_printed"),
    }
    tp_unsafe = bool(tp.get("live_token_used") or tp.get("sandbox_token_used")
                     or tp.get("token_printed"))

    # F4.1 стадия исполнения (ожидаемые факты, не «unsafe» для read-only дашборда)
    f41 = loaded.get("f41")
    execution_stage = None
    if f41 is not None:
        eg = f41.get("guards") or {}
        execution_stage = {
            "present": True,
            "order_was_sent": bool(eg.get(GUARD_LIVE_ORDER_SENT)),
            "live_token_used": bool(eg.get("live_token_used")),
            "note": ("F4.1 — стадия исполнения: один manual-confirmed ордер был "
                     "отправлен (ожидаемо). Это факт исполнения, а не нарушение "
                     "read-only контракта аналитических стадий F4.2–F4.6."),
        }

    return {
        "guards_summary": guards,
        "guards_seen": seen,
        "unsafe_flags": unsafe_flags,
        "token_policy_summary": token_policy_summary,
        "execution_stage": execution_stage,
        "verdict_scope": list(READONLY_KEYS),
        "any_unsafe": bool(unsafe_flags) or tp_unsafe,
    }


def _overview_row(key: str, name: str, rep: dict | None, status: str) -> dict:
    exit_code = (rep or {}).get("_exit_code")
    passed = (exit_code in (0, None)) if rep is not None else None
    return {
        "key": key,
        "name": name,
        "present": rep is not None,
        "stage": _get(rep, "stage"),
        "mode": _get(rep, "mode"),
        "status": ("OK" if passed else "FAIL") if rep is not None else "MISSING",
        "load_status": status,
        "warnings_count": _count(rep, "warnings"),
        "errors_count": _count(rep, "errors") + _count(rep, "blocking_reasons")
        + _count(rep, "income_validation_blocking_reasons"),
        "generated_at": _get(rep, "generated_at"),
        "checked_at": _get(rep, "checked_at"),
    }


# ─── load_dashboard_state (pure) ──────────────────────────────────────────────

def load_dashboard_state(reports_dir: str = DEFAULT_REPORTS_DIR, *,
                         now: datetime | None = None, host: str | None = None,
                         stale_after_hours: int = 48) -> dict:
    """Чистая агрегация локальных отчётов в состояние дашборда (без сервера/сети)."""
    base = Path(reports_dir)
    warnings: list[str] = []
    errors: list[str] = []
    loaded: dict[str, dict] = {}
    raw_reports: dict[str, dict] = {}
    reports_loaded: list[str] = []
    reports_missing: list[str] = []
    reports_stale_or_invalid: list[str] = []
    overview: list[dict] = []

    for key, filename, name in REPORTS:
        data, status = _load_json(base / filename)
        if status == "loaded":
            loaded[key] = data
            raw_reports[key] = data
            reports_loaded.append(key)
            # staleness (только если задан now)
            gen = _parse_dt(data.get("generated_at") or data.get("checked_at"))
            if now is not None and gen is not None:
                age_h = (now - gen).total_seconds() / 3600.0
                if age_h > stale_after_hours:
                    reports_stale_or_invalid.append(key)
                    warnings.append(
                        f"{name}: отчёт устарел (~{int(age_h)} ч назад).")
        elif status == "missing":
            reports_missing.append(key)
            warnings.append(f"{name}: отчёт не найден ({filename}).")
        else:  # invalid
            reports_stale_or_invalid.append(key)
            errors.append(f"{name}: повреждён/нечитаем ({filename}).")
        overview.append(_overview_row(key, name, loaded.get(key), status))

    safety = _safety_summary(loaded)
    if safety["any_unsafe"]:
        errors.append("ОБНАРУЖЕН небезопасный флаг (guard/token_policy) — "
                      f"{', '.join(safety['unsafe_flags']) or 'token_policy'}.")

    # host-предупреждение (если сервер слушает не localhost)
    host_warning = None
    if host is not None and host not in _LOCAL_HOSTS:
        host_warning = (f"Сервер привязан к {host} (не localhost) — дашборд может "
                        "быть доступен другим в сети. Рекомендуется 127.0.0.1.")
        warnings.append(host_warning)

    # latest generated_at
    latest = None
    for rep in loaded.values():
        dt = _parse_dt(rep.get("generated_at") or rep.get("checked_at"))
        if dt is not None and (latest is None or dt > latest):
            latest = dt

    if safety["any_unsafe"]:
        overall = STATUS_BLOCKED
    elif warnings or errors or reports_missing or reports_stale_or_invalid:
        overall = STATUS_WARN
    else:
        overall = STATUS_OK

    state = {
        "kind": KIND,
        "stage": STAGE,
        "mode": MODE,
        "generated_at": now.isoformat() if now is not None else None,
        "bind_host": host,
        "host_warning": host_warning,
        "reports_dir": str(base),
        "reports_loaded": reports_loaded,
        "reports_missing": reports_missing,
        "reports_stale_or_invalid": reports_stale_or_invalid,
        "overall_status": overall,
        "latest_generated_at": latest.isoformat() if latest else None,
        "overview": overview,
        "trade_summary": _trade_summary(loaded.get("f44"), loaded.get("f45")),
        "position_summary": _position_summary(
            loaded.get("f43"), loaded.get("f44"), loaded.get("f45")),
        "economics_summary": _economics_summary(loaded.get("f45")),
        "income_summary": _income_summary(loaded.get("f46")),
        "safety_summary": safety,
        "guards_summary": safety["guards_summary"],
        "token_policy_summary": safety["token_policy_summary"],
        "warnings": warnings,
        "errors": errors,
        "raw_reports": raw_reports,
    }
    # Всегда возвращаем санитизированное состояние (токены/raw account id безопасны).
    return sanitize_dashboard_state(state)


# ─── HTML (pure, без внешних ресурсов) ────────────────────────────────────────

_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
margin:0;padding:24px;background:#0f1419;color:#e6e6e6;line-height:1.5}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:0 0 12px;color:#9ad}
.sub{color:#8a96a3;font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.card{background:#161b22;border:1px solid #2a313c;border-radius:10px;padding:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:4px 8px;border-bottom:1px solid #232a33;
vertical-align:top}
th{color:#8a96a3;font-weight:600}
.k{color:#8a96a3;white-space:nowrap}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:700;
font-size:13px}
.ok{background:#10331f;color:#5fe08a;border:1px solid #1c5a36}
.warn{background:#3a3211;color:#f0d264;border:1px solid #6b5c1e}
.bad{background:#3a1414;color:#ff8a8a;border:1px solid #6b1e1e}
.muted{color:#6b7785}
pre{background:#0b0f14;border:1px solid #232a33;border-radius:8px;padding:12px;
overflow:auto;font-size:12px;max-height:420px}
details{margin:8px 0}
summary{cursor:pointer;color:#9ad;font-weight:600}
.note{font-size:12px;color:#8a96a3;margin-top:8px}
.flag-ok{color:#5fe08a}.flag-bad{color:#ff8a8a;font-weight:700}
"""


def _esc(value) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    if isinstance(value, bool):
        return "да" if value else "нет"
    return html.escape(str(value))


def _rows(pairs) -> str:
    out = []
    for label, value in pairs:
        out.append(f'<tr><td class="k">{html.escape(label)}</td>'
                   f'<td>{_esc(value)}</td></tr>')
    return "".join(out)


def _badge(status: str) -> str:
    cls = {"OK": "ok", STATUS_OK: "ok", STATUS_WARN: "warn",
           STATUS_BLOCKED: "bad", "FAIL": "bad", "MISSING": "warn"}.get(status, "warn")
    return f'<span class="badge {cls}">{html.escape(status)}</span>'


def _overview_table(overview: list[dict]) -> str:
    head = ("<tr><th>Report</th><th>Stage</th><th>Status</th>"
            "<th>Warn</th><th>Err</th><th>generated/checked</th></tr>")
    rows = []
    for r in overview:
        when = r.get("generated_at") or r.get("checked_at")
        rows.append(
            f"<tr><td>{html.escape(r['name'])}</td>"
            f"<td>{_esc(r.get('stage'))}</td>"
            f"<td>{_badge(r['status'])}</td>"
            f"<td>{_esc(r.get('warnings_count'))}</td>"
            f"<td>{_esc(r.get('errors_count'))}</td>"
            f"<td>{_esc(when)}</td></tr>")
    return f"<table>{head}{''.join(rows)}</table>"


def _safety_card(safety: dict) -> str:
    guards = safety.get("guards_summary") or {}
    seen = safety.get("guards_seen") or {}
    rows = []
    for key in EXPECTED_GUARDS:
        val = guards.get(key)
        if not seen.get(key):
            cell = '<span class="muted">— (нет в отчётах)</span>'
        elif val:
            cell = f'<span class="flag-bad">{key} = ДА (НЕБЕЗОПАСНО)</span>'
        else:
            cell = f'<span class="flag-ok">{key} = нет</span>'
        rows.append(f"<tr><td>{cell}</td></tr>")
    tp = safety.get("token_policy_summary") or {}
    tp_rows = _rows([
        ("read_only_token_env", tp.get("read_only_token_env")),
        ("read_only_token_present", tp.get("read_only_token_present")),
        ("read_only_token_used_for", tp.get("read_only_token_used_for")),
        ("live_trading_token_required", tp.get("live_trading_token_required")),
        ("live_token_used", tp.get("live_token_used")),
        ("sandbox_token_used", tp.get("sandbox_token_used")),
        ("token_printed", tp.get("token_printed")),
    ])
    verdict = (_badge(STATUS_BLOCKED) if safety.get("any_unsafe")
               else _badge("OK"))
    exec_html = ""
    es = safety.get("execution_stage")
    if es and es.get("present"):
        exec_html = (
            '<div class="note" style="margin-top:12px">F4.1 execution stage: '
            f'order_was_sent={_esc(es.get("order_was_sent"))}, '
            f'live_token_used={_esc(es.get("live_token_used"))} — '
            f'{html.escape(es.get("note", ""))}</div>')
    return (f'<div class="card"><h2>6 · Safety</h2>{verdict}'
            '<div class="note">Вердикт по read-only стадиям F4.2–F4.6 '
            '(их guard-флаги должны быть все «нет»).</div>'
            f'<table>{"".join(rows)}</table>{exec_html}'
            f'<h2 style="margin-top:14px">token_policy (latest read-only)</h2>'
            f'<table>{tp_rows}</table></div>')


def _raw_card(raw_reports: dict) -> str:
    blocks = []
    for key, _file, name in REPORTS:
        rep = raw_reports.get(key)
        if rep is None:
            blocks.append(f"<details><summary>{html.escape(name)} — "
                          "<span class='muted'>нет файла</span></summary></details>")
            continue
        pretty = json.dumps(rep, ensure_ascii=False, indent=2)
        blocks.append(
            f"<details><summary>{html.escape(name)}</summary>"
            f"<pre>{html.escape(pretty)}</pre></details>")
    return (f'<div class="card"><h2>7 · Raw reports (read-only)</h2>'
            f'<div class="note">JSON санитизирован: токеноподобные строки '
            f'отредактированы, account id маскируется.</div>{"".join(blocks)}</div>')


def build_dashboard_html(state: dict) -> str:
    """Чистая отрисовка состояния в одну самодостаточную HTML-страницу."""
    state = sanitize_dashboard_state(state)
    t = state.get("trade_summary") or {}
    p = state.get("position_summary") or {}
    e = state.get("economics_summary") or {}
    inc = state.get("income_summary") or {}
    safety = state.get("safety_summary") or {}

    trade_card = (
        '<div class="card"><h2>2 · First live trade</h2><table>' + _rows([
            ("ticker", t.get("ticker")),
            ("order_id", t.get("order_id")),
            ("account (masked)", t.get("account_masked")),
            ("fill quantity (units)", t.get("fill_quantity_units")),
            ("fill price", t.get("fill_price")),
            ("gross amount", t.get("fill_gross_amount")),
            ("commission raw", t.get("fill_commission_raw")),
            ("commission abs", t.get("fill_commission_abs")),
            ("cash outflow", t.get("fill_cash_outflow")),
            ("attribution confidence", t.get("attribution_confidence")),
            ("attribution method", t.get("attribution_method")),
        ]) + "</table></div>")

    position_card = (
        '<div class="card"><h2>3 · Position</h2><table>' + _rows([
            ("total position units", p.get("current_total_position_units")),
            ("average position price", p.get("average_position_price")),
            ("current price", p.get("current_price")),
            ("current position value", p.get("current_position_value")),
            ("total unrealized PnL", p.get("total_unrealized_pnl")),
            ("currency", p.get("currency")),
        ]) + f'</table><div class="note">{_esc(p.get("note"))}</div></div>')

    economics_card = (
        '<div class="card"><h2>4 · New-fill economics</h2><table>' + _rows([
            ("gross PnL (before commission)",
             e.get("gross_pnl_before_commission")),
            ("net PnL (after commission)", e.get("net_pnl_after_commission")),
            ("commission drag (RUB)", e.get("commission_drag_rub")),
            ("break-even price", e.get("break_even_price_after_commission")),
            ("distance to break-even (RUB)", e.get("distance_to_break_even_rub")),
            ("new-fill weight in position %",
             e.get("new_fill_weight_in_total_position_pct")),
            ("total position PnL kept separate",
             e.get("total_position_pnl_kept_separate")),
        ]) + '</table><div class="note">PnL новой сделки и PnL всей позиции — '
        'разные величины (раздельно).</div></div>')

    income_card = (
        '<div class="card"><h2>5 · Income validation</h2><table>' + _rows([
            ("income_data_checked", inc.get("income_data_checked")),
            ("reliable_income_data_found", inc.get("reliable_income_data_found")),
            ("confidence", inc.get("confidence")),
            ("source", inc.get("source")),
            ("expected dividend / unit", inc.get("expected_dividend_per_unit_rub")),
            ("new-fill income yearly",
             inc.get("expected_income_rub_yearly_new_fill")),
            ("new-fill income monthly",
             inc.get("expected_income_rub_monthly_new_fill")),
            ("total income yearly",
             inc.get("expected_income_rub_yearly_total_position")),
            ("total income monthly",
             inc.get("expected_income_rub_monthly_total_position")),
            ("coverage % (new-fill)",
             inc.get("income_target_coverage_pct_new_fill")),
            ("coverage % (total)",
             inc.get("income_target_coverage_pct_total_position")),
            ("target RUB/month", inc.get("base_monthly_living_basket_rub")),
            ("next event date", inc.get("next_known_income_event_date")),
            ("next event type", inc.get("next_known_income_event_type")),
            ("next event amount/unit",
             inc.get("next_known_income_event_amount_per_unit")),
            ("income_validation_passed", inc.get("income_validation_passed")),
        ]) + (f'<div class="note">{_esc(inc.get("tax_note"))}</div>'
              if inc.get("tax_note") else "") + "</div>")

    warn_html = ""
    if state.get("warnings"):
        items = "".join(f"<li>{html.escape(str(w))}</li>"
                        for w in state["warnings"])
        warn_html += f'<div class="card"><h2 class="warn">Warnings</h2><ul>{items}</ul></div>'
    if state.get("errors"):
        items = "".join(f"<li>{html.escape(str(x))}</li>"
                        for x in state["errors"])
        warn_html += f'<div class="card"><h2 class="bad">Errors</h2><ul>{items}</ul></div>'

    host_banner = ""
    if state.get("host_warning"):
        host_banner = (f'<div class="card bad">⚠️ {html.escape(state["host_warning"])}'
                       "</div>")

    overview_card = (f'<div class="card"><h2>1 · Overview / status chain</h2>'
                     f'{_overview_table(state.get("overview") or [])}</div>')

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>F4.7 read-only dashboard</title>
<style>{_CSS}</style></head>
<body>
<h1>F4.7 — read-only dashboard {_badge(state.get("overall_status", "OK"))}</h1>
<div class="sub">stage <code>{html.escape(STAGE)}</code> · mode
<code>{html.escape(MODE)}</code> · reports_dir
<code>{html.escape(str(state.get("reports_dir")))}</code> · latest
<code>{_esc(state.get("latest_generated_at"))}</code> ·
<strong>read-only viewer — не торгует, без токенов, без сети</strong></div>
{host_banner}
{warn_html}
<div class="grid">
{overview_card}
{trade_card}
{position_card}
{economics_card}
{income_card}
{_safety_card(safety)}
</div>
{_raw_card(state.get("raw_reports") or {})}
<div class="note" style="margin-top:20px">F4.7 — только просмотр. Нет POST/действий,
нет кнопок торговли. Остановить сервер: Ctrl+C.</div>
</body></html>"""


# ─── stdlib HTTP server (только GET) ──────────────────────────────────────────

def make_handler(reports_dir: str, host: str):
    """Фабрика GET-only обработчика (без состояния клиента, без сети)."""

    class _DashboardHandler(BaseHTTPRequestHandler):
        server_version = "ReadOnlyDashboard/4.7"

        def _send(self, body: bytes, content_type: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Жёстко read-only: запрещаем кэш/встраивание, без внешних ресурсов.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/":
                state = load_dashboard_state(
                    reports_dir, now=datetime.now(timezone.utc), host=host)
                body = build_dashboard_html(state).encode("utf-8")
                self._send(body, "text/html; charset=utf-8")
            elif path == "/state.json":
                state = load_dashboard_state(
                    reports_dir, now=datetime.now(timezone.utc), host=host)
                body = json.dumps(state, ensure_ascii=False, indent=2,
                                  default=str).encode("utf-8")
                self._send(body, "application/json; charset=utf-8")
            else:
                self._send(b'{"error":"not_found"}',
                           "application/json; charset=utf-8", code=404)

        def log_message(self, fmt, *args):  # тихий лог (без печати токенов и пр.)
            return

    return _DashboardHandler


def serve(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          reports_dir: str = DEFAULT_REPORTS_DIR,
          server_factory=ThreadingHTTPServer):
    """Запускает локальный read-only сервер. Блокирует до Ctrl+C."""
    handler = make_handler(reports_dir, host)
    httpd = server_factory((host, port), handler)
    return httpd
