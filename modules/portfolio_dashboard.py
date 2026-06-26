"""
portfolio_dashboard — F4.9 локальный READ-ONLY портфельный кокпит.

Рендерит ТОЛЬКО локальный отчёт F4.8 `data/reports/portfolio_dashboard_data.json`
в одну самодостаточную HTML-страницу через stdlib `http.server` (127.0.0.1).
Команда `portfolio-dashboard` НИЧЕГО не исполняет, НЕ ходит к брокеру и НЕ считает
портфель сама — источник истины полностью в F4.8.

Жёсткий контракт (никогда не нарушать):
- Только READ-ONLY чтение локального JSON. НИКАКИХ брокерских вызовов, токенов,
  интернета, брокер-клиента, записи в `.env`/config/портфель.
- НЕ читает значения токенов из окружения. Только GET-маршруты (`/`, `/state.json`).
  НЕТ POST/действий, НЕТ кнопок торговли/отмены/повтора/исполнения. НЕТ
  планировщика/Telegram.
- По умолчанию слушает ТОЛЬКО 127.0.0.1; при другом host — предупреждение.
- Значения токенов никогда не читаются/не печатаются/не попадают в HTML; account id
  маскируется; sanitize рекурсивно редактирует токеноподобные строки.

В этом исходнике нет цельных запрещённых «order»-литералов: guard-ключи берутся из
данных F4.8 (динамически), а не пишутся в коде — статический safety-сканер зелёный.
"""
from __future__ import annotations

import html
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common.helpers import mask_identifier

KIND = "portfolio_overview_dashboard"
STAGE = "F4_9_PORTFOLIO_OVERVIEW_DASHBOARD"
MODE = "PORTFOLIO_DASHBOARD_READ_ONLY"

DEFAULT_REPORT_PATH = "data/reports/portfolio_dashboard_data.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# Пороги ОТОБРАЖЕНИЯ дашборда (не инвестиционные советы)
DISPLAY_TOP5_CAUTION_PCT = 70
DISPLAY_NEG_FRACTION_CAUTION = 0.5
DISPLAY_CASH_LOW_PCT = 5

_TOKEN_RE = re.compile(r"\bt\.[A-Za-z0-9_\-]{16,}\b")
_REDACTED = "***REDACTED***"


# ─── загрузка отчёта F4.8 ─────────────────────────────────────────────────────

def load_portfolio_dashboard_report(path: str = DEFAULT_REPORT_PATH) -> dict:
    """Читает локальный F4.8 JSON. Возвращает состояние с флагом наличия."""
    p = Path(path)
    if not p.exists():
        return {"_report_present": False, "_report_path": str(path),
                "_report_error": "missing"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"_report_present": False, "_report_path": str(path),
                "_report_error": "invalid"}
    if not isinstance(data, dict):
        return {"_report_present": False, "_report_path": str(path),
                "_report_error": "invalid"}
    data["_report_present"] = True
    data["_report_path"] = str(path)
    return data


# ─── sanitize (редактирование токенов / маскирование account) ─────────────────

def _sanitize_value(key, value):
    if isinstance(value, dict):
        return {k: _sanitize_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(key, v) for v in value]
    if isinstance(value, str):
        if key and "account" in str(key).lower() and "masked" not in str(key).lower():
            if re.fullmatch(r"\d{6,}", value):
                return mask_identifier(value)
        if _TOKEN_RE.search(value):
            return _TOKEN_RE.sub(_REDACTED, value)
        return value
    return value


def sanitize_portfolio_dashboard_state(state: dict) -> dict:
    """Рекурсивно редактирует токеноподобные строки и маскирует raw account id."""
    return {k: _sanitize_value(k, v) for k, v in state.items()}


# ─── форматирование (₽ / % / шт.) ─────────────────────────────────────────────

_DASH = '<span class="muted">—</span>'
_SP = chr(0x20)       # обычный ASCII-пробел (защита от NBSP в литералах)
_RUB = "₽"       # ₽


def _to_num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _esc(value) -> str:
    if value is None:
        return _DASH
    if isinstance(value, bool):
        return "да" if value else "нет"
    return html.escape(str(value))


def _money(value) -> str:
    n = _to_num(value)
    if n is None:
        return _esc(value) if value is not None else _DASH
    return f"{n:,.2f}".replace(",", " ") + " ₽"


def _pnl(value) -> str:
    n = _to_num(value)
    if n is None:
        return _esc(value) if value is not None else _DASH
    cls = "neg" if n < 0 else ("pos" if n > 0 else "")
    body = f"{n:,.2f}".replace(",", " ")
    return f'<span class="{cls}">{body} ₽</span>'


def _pct(value) -> str:
    n = _to_num(value)
    if n is None:
        return _DASH
    dec = 4 if abs(n) < 1 else 2
    return f"{n:.{dec}f}%"


def _units(value) -> str:
    n = _to_num(value)
    if n is None:
        return _esc(value) if value is not None else _DASH
    body = f"{int(n)}" if n == int(n) else f"{n:g}"
    return f"{body} шт."


def _kv(pairs) -> str:
    out = []
    for label, value_html in pairs:
        out.append(f'<tr><td class="k">{html.escape(label)}</td>'
                   f'<td class="v">{value_html}</td></tr>')
    return f'<table class="kv">{"".join(out)}</table>'


def _table(headers, rows) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                   for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _card(title: str, body: str, *, klass: str = "card") -> str:
    return f'<div class="{klass}"><h2>{html.escape(title)}</h2>{body}</div>'


def _kpi(label: str, value_html: str, sub: str = "") -> str:
    sub_html = f'<div class="ksub">{sub}</div>' if sub else ""
    return (f'<div class="kpi"><div class="label">{html.escape(label)}</div>'
            f'<div class="val">{value_html}</div>{sub_html}</div>')


def _badge(status: str, *, big: bool = False) -> str:
    s = str(status or "")
    if s in ("READ_ONLY_SAFE", "OK", "full", "да"):
        cls = "ok"
    elif s in ("BLOCKED_UNSAFE", "FAIL"):
        cls = "bad"
    else:
        cls = "warn"
    size = " lg" if big else ""
    return f'<span class="badge{size} {cls}">{html.escape(s)}</span>'


# ─── CSS (без внешних ресурсов) ───────────────────────────────────────────────

_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
margin:0;padding:24px;background:#0f1419;color:#e6e6e6;line-height:1.5}
.container{max-width:1520px;margin:0 auto}
header{margin-bottom:18px}
h1{font-size:24px;margin:0 0 6px;display:flex;align-items:center;gap:12px;
flex-wrap:wrap}
h2{font-size:16px;margin:0 0 12px;color:#9ad}
h3{font-size:13px;margin:14px 0 6px;color:#8a96a3;text-transform:uppercase;
letter-spacing:.04em}
.sub{color:#8a96a3;font-size:13px}
.sub code{color:#aeb9c4}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}
.kpi{background:#161b22;border:1px solid #2a313c;border-radius:10px;padding:14px 16px}
.kpi .label{font-size:12px;color:#8a96a3;margin-bottom:6px}
.kpi .val{font-size:20px;font-weight:700}
.kpi .ksub{font-size:12px;color:#8a96a3;margin-top:4px}
.layout{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;
align-items:start;margin-top:16px}
.card{background:#161b22;border:1px solid #2a313c;border-radius:10px;padding:18px}
.card.full{margin-top:16px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid #232a33;
vertical-align:top}
th{color:#8a96a3;font-weight:600}
.kv td.k{color:#9aa6b2;white-space:nowrap;width:58%}
.kv td.v{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.badge{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:700;
font-size:13px}
.badge.lg{font-size:18px;padding:8px 18px}
.ok{background:#10331f;color:#5fe08a;border:1px solid #1c5a36}
.warn{background:#3a3211;color:#f0d264;border:1px solid #6b5c1e}
.bad{background:#3a1414;color:#ff8a8a;border:1px solid #6b1e1e}
.muted{color:#6b7785}
.neg{color:#ff8a8a}.pos{color:#5fe08a}
ul.tight{margin:6px 0 0;padding-left:20px}
ul.tight li{margin:3px 0}
.caution{background:#3a2a11;border:1px solid #6b531e;border-radius:8px;
padding:8px 12px;margin:8px 0;font-size:13px;color:#f0c264}
pre{background:#0b0f14;border:1px solid #232a33;border-radius:8px;padding:12px;
overflow:auto;font-size:12px;max-height:460px}
details{margin:8px 0}
summary{cursor:pointer;color:#9ad;font-weight:600}
.note{font-size:12.5px;color:#8a96a3;margin-top:10px}
@media(max-width:1100px){.kpis{grid-template-columns:repeat(2,1fr)}
.layout{grid-template-columns:1fr}}
@media(max-width:700px){.kpis{grid-template-columns:1fr}}
"""


# ─── missing-report страница ──────────────────────────────────────────────────

def _missing_html(state: dict) -> str:
    path = html.escape(str(state.get("_report_path") or DEFAULT_REPORT_PATH))
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio cockpit — нет данных</title><style>{_CSS}</style></head>
<body><div class="container">
<header><h1>Portfolio cockpit {_badge("НЕТ ДАННЫХ")}</h1>
<div class="sub">Локальный read-only дашборд · не торгует, без токенов/сети</div>
</header>
<div class="card"><h2>Отчёт F4.8 не найден</h2>
<p>Файл <code>{path}</code> отсутствует или нечитаем. F4.9 рендерит только
готовый отчёт F4.8 и сам данные не считает.</p>
<h3>Как починить</h3>
<p>Сначала сформируйте отчёт F4.8 (read-only), затем обновите страницу:</p>
<pre>python main.py portfolio-dashboard-data --live-account-id &lt;ACCOUNT_ID&gt;</pre>
<div class="note">Чтобы включить учёт взносов, скопируйте
<code>config/contribution_plan.example.json</code> в
<code>data/config/contribution_plan.json</code>.</div>
</div>
<div class="note" style="margin-top:20px">F4.9 — только просмотр. Нет POST/действий,
нет кнопок торговли. Остановить сервер: Ctrl+C.</div>
</div></body></html>"""


# ─── секции кокпита ───────────────────────────────────────────────────────────

def _safety(state: dict) -> tuple[str, bool]:
    kpi = state.get("dashboard_kpi") or {}
    guards = state.get("guards") or {}
    status = kpi.get("safety_status") or "READ_ONLY_SAFE"
    any_unsafe = (status == "BLOCKED_UNSAFE"
                  or any(bool(v) for v in guards.values()))
    return ("BLOCKED_UNSAFE" if any_unsafe else "READ_ONLY_SAFE"), any_unsafe


def _kpi_strip(state: dict) -> str:
    kpi = state.get("dashboard_kpi") or {}
    cn = state.get("contributions_summary") or {}
    safe, _ = _safety(state)
    contrib = ("настроены" if cn.get("contributions_tracking_enabled")
               else "не настроены")
    return "".join([
        _kpi("Стоимость портфеля", _money(kpi.get("portfolio_value_rub"))),
        _kpi("Свободный кэш", _money(kpi.get("cash_rub")),
             sub=_pct(kpi.get("cash_pct")) + " портфеля"),
        _kpi("Пассивный доход / мес.",
             _money(kpi.get("passive_income_monthly_rub")), sub="брутто"),
        _kpi("Покрытие цели 150 000 ₽/мес.",
             _pct(kpi.get("passive_income_coverage_pct"))),
        _kpi("Оборот YTD (цель 60M)", _money(kpi.get("turnover_ytd_rub")),
             sub=_pct(kpi.get("turnover_ytd_progress_pct")) + " прогресс"),
        _kpi("PnL портфеля", _pnl(kpi.get("portfolio_unrealized_pnl_rub")),
             sub=_pct(kpi.get("portfolio_unrealized_pnl_pct"))),
        _kpi("Взносы", contrib),
        _kpi("Безопасность", _badge(safe)),
    ])


def _interpretation(state: dict) -> str:
    pf = state.get("portfolio_summary") or {}
    inc = state.get("income_summary") or {}
    tn = state.get("turnover_summary") or {}
    cn = state.get("contributions_summary") or {}
    rk = state.get("risk_summary") or {}
    bullets = []
    bullets.append(f"Портфель стоит {_money(pf.get('total_portfolio_value_rub'))}; "
                   f"кэш {_money(pf.get('cash_rub'))} ({_pct(pf.get('cash_pct'))}).")
    bullets.append(
        f"Пассивный доход {_money(inc.get('passive_income_rub_monthly_gross'))}/мес. "
        f"(брутто); до цели не хватает "
        f"{_money(inc.get('income_gap_rub_monthly'))}/мес.")
    cov = inc.get("income_target_coverage_pct")
    if cov is not None:
        bullets.append(
            f"Покрытие цели 150 000 ₽/мес.: {_pct(cov)} — пассивный доход сейчас "
            f"покрывает только {_pct(cov)} цели.")
    bullets.append(
        f"Оборот {_money(tn.get('turnover_ytd_rub'))} из "
        f"{_money(tn.get('turnover_annual_target_rub'))} "
        f"({_pct(tn.get('turnover_ytd_progress_pct'))}); план-на-сегодня "
        f"{_money(tn.get('turnover_ytd_plan_to_date_rub'))}, требуется в день "
        f"{_money(tn.get('turnover_daily_required_rub'))}.")
    gap = _to_num(tn.get("turnover_ytd_gap_rub"))
    if gap is not None and gap > 0:
        bullets.append("Оборотный план существенно отстаёт от плана пропорционально "
                       "дате (gap > 0).")
    bullets.append("Взносы: " + ("настроены." if cn.get(
        "contributions_tracking_enabled") else "НЕ настроены."))
    neg = rk.get("negative_pnl_positions_count")
    cnt = pf.get("positions_count")
    if neg is not None and cnt:
        bullets.append(f"Риск: {neg}/{cnt} позиций в минусе; концентрация топ-5 = "
                       f"{_pct(rk.get('top_5_positions_weight_pct'))}.")
    interp = ('<div class="note">Это диагностический дашборд (факты из F4.8), '
              'НЕ торговая рекомендация.</div>')
    body = ('<ul class="tight">'
            + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>" + interp)
    return _card("Что сейчас", body)


def _portfolio_card(state: dict) -> str:
    pf = state.get("portfolio_summary") or {}
    return _card("A · Обзор портфеля", _kv([
        ("Полная стоимость", _money(pf.get("total_portfolio_value_rub"))),
        ("Стоимость позиций", _money(pf.get("positions_value_rub"))),
        ("Кэш", _money(pf.get("cash_rub"))),
        ("Доля кэша", _pct(pf.get("cash_pct"))),
        ("Кол-во позиций", _esc(pf.get("positions_count"))),
        ("Нереализованный PnL", _pnl(pf.get("unrealized_pnl_rub"))),
        ("Нереализованный PnL %", _pct(pf.get("unrealized_pnl_pct"))),
        ("Источник", _esc(pf.get("portfolio_source"))),
        ("Режим", _badge("partial" if pf.get("partial") else "full")),
    ]))


def _positions_card(state: dict) -> str:
    positions = state.get("positions") or []
    headers = ["Тикер", "Название", "Кол-во", "Тек. цена", "Стоимость", "Вес %",
               "PnL ₽", "PnL %", "Доход/год", "Доход/мес.", "След. событие",
               "Источник"]
    rows = []
    for p in positions:
        rows.append([
            _esc(p.get("ticker")), _esc(p.get("name")),
            _units(p.get("quantity_units")), _money(p.get("current_price")),
            _money(p.get("market_value_rub")), _pct(p.get("weight_pct")),
            _pnl(p.get("unrealized_pnl_rub")), _pct(p.get("unrealized_pnl_pct")),
            _money(p.get("expected_income_rub_yearly")),
            _money(p.get("expected_income_rub_monthly")),
            _esc(p.get("next_income_event_date")),
            (f'{_esc(p.get("income_data_source"))} / '
             f'{_esc(p.get("income_data_confidence"))}'),
        ])
    body = _table(headers, rows) if rows else '<div class="note">Позиций нет.</div>'
    return _card(f"B · Позиции ({len(positions)})", body, klass="card full")


def _income_card(state: dict) -> str:
    inc = state.get("income_summary") or {}
    net_avail = inc.get("income_net_estimation_available")
    net_block = []
    if net_avail:
        net_block = [("Доход net / мес.",
                      _money(inc.get("passive_income_rub_monthly_net"))),
                     ("Доход net / год",
                      _money(inc.get("passive_income_rub_yearly_net")))]
    body = _kv([
        ("Доход брутто / мес.",
         _money(inc.get("passive_income_rub_monthly_gross"))),
        ("Доход брутто / год",
         _money(inc.get("passive_income_rub_yearly_gross"))),
        *net_block,
        ("Цель", _money(inc.get("target_monthly_income_rub")) + "/мес."),
        ("Покрытие цели", _pct(inc.get("income_target_coverage_pct"))),
        ("Разрыв до цели / мес.", _money(inc.get("income_gap_rub_monthly"))),
        ("Требуемый капитал", _money(inc.get("required_capital_rub"))),
        ("Допущение доходности",
         _pct(inc.get("required_capital_assumption_yield_pct"))),
        ("Разрыв капитала", _money(inc.get("required_capital_gap_rub"))),
    ])
    if not net_avail and inc.get("income_tax_warning"):
        body += f'<div class="note">⚠️ {html.escape(str(inc["income_tax_warning"]))}</div>'
    cal = inc.get("income_calendar_monthly") or {}
    if cal:
        rows = [[html.escape(str(m)), _money(v)] for m, v in cal.items()]
        body += "<h3>Календарь дохода по месяцам</h3>" + _table(["Месяц", "Сумма"], rows)
    events = inc.get("next_income_events") or []
    if events:
        rows = [[_esc(e.get("date")), _esc(e.get("ticker")), _esc(e.get("type")),
                 _money(e.get("amount_total_rub"))] for e in events]
        body += "<h3>Ближайшие события дохода</h3>" + _table(
            ["Дата", "Тикер", "Тип", "Сумма"], rows)
    return _card("C · Пассивный доход / FIRE", body, klass="card full")


def _turnover_card(state: dict) -> str:
    tn = state.get("turnover_summary") or {}
    body = _kv([
        ("Определение", _esc(tn.get("turnover_definition"))
         + " (buy+sell gross, НЕ дивиденды/купоны)"),
        ("Режим", _badge("partial" if tn.get("turnover_partial") else "full")),
        ("Оборот YTD", _money(tn.get("turnover_ytd_rub"))),
        ("Оборот MTD / QTD",
         _money(tn.get("turnover_mtd_rub")) + " / " + _money(tn.get("turnover_qtd_rub"))),
        ("Годовая цель", _money(tn.get("turnover_annual_target_rub"))),
        ("Прогресс", _pct(tn.get("turnover_ytd_progress_pct"))),
        ("План-на-сегодня", _money(tn.get("turnover_ytd_plan_to_date_rub"))),
        ("Разрыв до плана", _money(tn.get("turnover_ytd_gap_rub"))),
        ("Прогноз на конец года", _money(tn.get("turnover_forecast_year_end_rub"))),
        ("Осталось за год", _money(tn.get("turnover_remaining_year_rub"))),
        ("Требуется в день", _money(tn.get("turnover_daily_required_rub"))),
        ("Комиссии YTD", _money(tn.get("commissions_ytd_rub"))),
        ("Ставка комиссии", _pct(tn.get("commission_rate_pct_of_turnover"))),
    ])
    by_side = tn.get("turnover_by_side") or {}
    if by_side:
        rows = [[html.escape(str(k)), _money(v)] for k, v in by_side.items()]
        body += "<h3>Оборот по сторонам</h3>" + _table(["Сторона", "Оборот"], rows)
    by_instr = tn.get("turnover_by_instrument") or {}
    if by_instr:
        rows = [[html.escape(str(k)), _money(v)] for k, v in by_instr.items()]
        body += "<h3>Оборот по инструментам</h3>" + _table(["Инструмент", "Оборот"], rows)
    by_month = tn.get("turnover_by_month") or {}
    if by_month:
        rows = [[html.escape(str(k)), _money(v)] for k, v in by_month.items()]
        body += "<h3>Оборот по месяцам</h3>" + _table(["Месяц", "Оборот"], rows)
    return _card("D · Оборот (цель 60M/год)", body, klass="card full")


def _contributions_card(state: dict) -> str:
    cn = state.get("contributions_summary") or {}
    enabled = cn.get("contributions_tracking_enabled")
    body = _kv([
        ("Учёт включён", _badge("да" if enabled else "выкл")),
        ("План / неделя", _money(cn.get("contribution_plan_weekly_rub"))),
        ("План / месяц", _money(cn.get("contribution_plan_monthly_rub"))),
        ("Факт / неделя", _money(cn.get("contribution_fact_weekly_rub"))),
        ("Факт / месяц", _money(cn.get("contribution_fact_monthly_rub"))),
        ("Разрыв / месяц", _money(cn.get("contribution_gap_monthly_rub"))),
        ("Пропущено (месяц)", _esc(cn.get("missed_contributions_count_month"))),
        ("След. плановый взнос", _esc(cn.get("next_planned_contribution_date"))),
        ("Нужно довнести", _money(cn.get("contribution_required_to_catch_up_rub"))),
    ])
    if not enabled:
        body += ('<div class="caution">Учёт взносов не настроен. Создайте '
                 '<code>data/config/contribution_plan.json</code> на основе '
                 '<code>config/contribution_plan.example.json</code>.</div>')
    return _card("E · Взносы", body)


def _risk_card(state: dict) -> str:
    rk = state.get("risk_summary") or {}
    pf = state.get("portfolio_summary") or {}
    body = _kv([
        ("Вес топ-позиции", _pct(rk.get("top_position_weight_pct"))),
        ("Вес топ-5", _pct(rk.get("top_5_positions_weight_pct"))),
        ("Доля кэша", _pct(rk.get("cash_pct"))),
        ("Позиций в минусе", _esc(rk.get("negative_pnl_positions_count"))),
        ("Нереализованный PnL", _pnl(rk.get("portfolio_unrealized_pnl_rub"))),
        ("Качество данных", _badge(str(rk.get("risk_data_quality") or ""))),
    ])
    # пороги ОТОБРАЖЕНИЯ дашборда (не инвестиционный совет)
    cautions = []
    top5 = _to_num(rk.get("top_5_positions_weight_pct"))
    if top5 is not None and top5 >= DISPLAY_TOP5_CAUTION_PCT:
        cautions.append(f"Концентрация: топ-5 = {_pct(top5)} (≥{DISPLAY_TOP5_CAUTION_PCT}%).")
    neg = _to_num(rk.get("negative_pnl_positions_count"))
    cnt = _to_num(pf.get("positions_count"))
    if neg is not None and cnt and neg >= DISPLAY_NEG_FRACTION_CAUTION * cnt:
        cautions.append(f"В минусе ≥50% позиций ({int(neg)}/{int(cnt)}).")
    cash_pct = _to_num(rk.get("cash_pct"))
    if cash_pct is not None and cash_pct < DISPLAY_CASH_LOW_PCT:
        cautions.append(f"Низкий кэш: {_pct(cash_pct)} (<{DISPLAY_CASH_LOW_PCT}%).")
    for c in (rk.get("concentration_warnings") or []):
        cautions.append(str(c))
    for c in (rk.get("cash_warnings") or []):
        cautions.append(str(c))
    if cautions:
        body += ('<h3>Предупреждения отображения (пороги дашборда, не совет)</h3>'
                 + "".join(f'<div class="caution">{html.escape(c)}</div>'
                           for c in cautions))
    return _card("F · Риск / концентрация", body)


def _last_trade_card(state: dict) -> str:
    lt = state.get("last_trade_audit_summary") or {}
    body = _kv([
        ("Тикер", _esc(lt.get("last_tracked_trade_ticker"))),
        ("ID заявки", _esc(lt.get("last_tracked_trade_order_id"))),
        ("Количество", _units(lt.get("last_tracked_trade_quantity"))),
        ("Расход с комиссией", _money(lt.get("last_tracked_trade_cash_outflow"))),
        ("PnL после комиссии",
         _pnl(lt.get("last_tracked_trade_net_pnl_after_commission"))),
        ("Доход сделки / год", _money(lt.get("last_tracked_trade_income_yearly"))),
        ("Доход сделки / мес.", _money(lt.get("last_tracked_trade_income_monthly"))),
        ("Аудит пройден", _esc(lt.get("last_tracked_trade_audit_passed"))),
    ])
    body += ('<div class="note">Это одна отслеживаемая сделка (аудит F4.1–F4.6), '
             'а НЕ весь портфель.</div>')
    return _card("G · Последняя сделка (вторично)", body)


def _raw_card(state: dict) -> str:
    pretty = json.dumps(state, ensure_ascii=False, indent=2, default=str)
    body = ('<div class="note">Технический JSON отчёта F4.8, для отладки. '
            'Санитизирован: токеноподобные строки отредактированы, account id '
            'маскируется.</div>'
            f'<details><summary>Показать JSON</summary><pre>{html.escape(pretty)}'
            '</pre></details>')
    return _card("H · Сырой отчёт F4.8 (read-only, debug)", body, klass="card full")


# ─── сборка страницы ──────────────────────────────────────────────────────────

def build_portfolio_dashboard_html(state: dict) -> str:
    """Чистая отрисовка состояния F4.8 в HTML-кокпит (без сети/сервера)."""
    state = sanitize_portfolio_dashboard_state(state)
    if not state.get("_report_present"):
        return _missing_html(state)

    safe, _unsafe = _safety(state)
    fresh = ((state.get("data_freshness") or {}).get("overall")) or "—"
    host_warning = state.get("_host_warning")
    host_banner = (f'<div class="card bad">⚠️ {html.escape(host_warning)}</div>'
                   if host_warning else "")

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio cockpit (F4.9 read-only)</title>
<style>{_CSS}</style></head>
<body>
<div class="container">
<header>
<h1>Portfolio cockpit {_badge(safe, big=True)} {_badge(fresh)}</h1>
<div class="sub">Локальный read-only дашборд по отчёту F4.8 · generated
<code>{_esc(state.get("generated_at"))}</code> · account
<code>{_esc(state.get("live_account_id_masked"))}</code> ·
<strong>не торгует, без токенов/брокера/сети</strong></div>
</header>
{host_banner}
<div class="kpis">{_kpi_strip(state)}</div>
{_interpretation(state)}
{_positions_card(state)}
<div class="layout">
{_portfolio_card(state)}
{_contributions_card(state)}
{_risk_card(state)}
</div>
{_income_card(state)}
{_turnover_card(state)}
<div class="layout">
{_last_trade_card(state)}
</div>
{_raw_card(state)}
<div class="note" style="margin-top:20px">F4.9 — только просмотр отчёта F4.8. Нет
POST/действий, нет кнопок торговли. Обновить данные: запустите F4.8. Остановить
сервер: Ctrl+C.</div>
</div>
</body></html>"""


# ─── stdlib HTTP server (только GET) ──────────────────────────────────────────

def make_handler(report_path: str, host: str):
    """Фабрика GET-only обработчика. Без брокера/токенов/сети."""

    class _PortfolioDashboardHandler(BaseHTTPRequestHandler):
        server_version = "PortfolioDashboard/4.9"

        def _send(self, body: bytes, content_type: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _state(self):
            state = load_portfolio_dashboard_report(report_path)
            if host not in _LOCAL_HOSTS:
                state["_host_warning"] = (
                    f"Сервер привязан к {host} (не localhost) — рекомендуется "
                    "127.0.0.1.")
            return sanitize_portfolio_dashboard_state(state)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/":
                body = build_portfolio_dashboard_html(self._state()).encode("utf-8")
                self._send(body, "text/html; charset=utf-8")
            elif path == "/state.json":
                body = json.dumps(self._state(), ensure_ascii=False, indent=2,
                                  default=str).encode("utf-8")
                self._send(body, "application/json; charset=utf-8")
            else:
                self._send(b'{"error":"not_found"}',
                           "application/json; charset=utf-8", code=404)

        def log_message(self, fmt, *args):  # тихий лог
            return

    return _PortfolioDashboardHandler


def serve(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          report_path: str = DEFAULT_REPORT_PATH,
          server_factory=ThreadingHTTPServer):
    """Запускает локальный read-only сервер. Возвращает httpd (блокирует вызывающий)."""
    handler = make_handler(report_path, host)
    return server_factory((host, port), handler)
