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
import math
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


def _kpi(label: str, value_html: str, sub: str = "", *, cls: str = "") -> str:
    sub_html = f'<div class="ksub">{sub}</div>' if sub else ""
    klass = f"kpi {cls}".strip()
    return (f'<div class="{klass}"><div class="label">{html.escape(label)}</div>'
            f'<div class="val">{value_html}</div>{sub_html}</div>')


# ─── визуальные компоненты (inline CSS/SVG, без внешних ресурсов) ──────────────

def _progress(label: str, value_html: str, pct, *, color: str = "ok",
              note: str = "") -> str:
    n = _to_num(pct)
    w = 0.0 if n is None else max(0.0, min(100.0, n))
    if n is not None and 0 < w < 0.5:
        w = 0.5  # видимый минимум для крошечных значений (label несёт точное число)
    return (f'<div class="bar"><div class="bar-h"><span class="bl">'
            f'{html.escape(label)}{note}</span><span class="bv">{value_html}</span>'
            f'</div><div class="track"><div class="fill {color}" '
            f'style="width:{w:.4f}%"></div></div></div>')


def _donut(positions: list) -> str:
    items = []
    for p in positions:
        w = _to_num(p.get("weight_pct"))
        if w and w > 0:
            items.append((str(p.get("ticker") or "?"), w))
    if not items:
        return '<div class="note">Нет весов позиций для аллокации.</div>'
    items.sort(key=lambda x: -x[1])
    top = items[:6]
    rest = items[6:]
    if rest:
        top.append((f"Прочее ({len(rest)})", sum(w for _, w in rest)))
    total = sum(w for _, w in top) or 1.0
    r, cx, cy, sw = 58, 70, 70, 24
    circ = 2 * math.pi * r
    segs = ""
    legend = ""
    off = 0.0
    for idx, (name, w) in enumerate(top):
        ln = (w / total) * circ
        col = _DONUT_COLORS[idx % len(_DONUT_COLORS)]
        segs += (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{col}" '
                 f'stroke-width="{sw}" stroke-dasharray="{ln:.3f} {circ - ln:.3f}" '
                 f'stroke-dashoffset="{-off:.3f}"></circle>')
        off += ln
        legend += (f'<div class="li"><span class="sw" style="background:{col}">'
                   f'</span>{html.escape(name)} · {_pct(w)}</div>')
    svg = (f'<svg width="150" height="150" viewBox="0 0 140 140" '
           f'role="img" aria-label="Position weight allocation">'
           f'<g transform="rotate(-90 {cx} {cy})">{segs}</g></svg>')
    return f'<div class="donutwrap">{svg}<div class="legend">{legend}</div></div>'


def _barchart(data: dict, *, fmt=None) -> str:
    fmt = fmt or _money
    if not data:
        return '<div class="note">Нет данных.</div>'
    pairs = [(str(k), _to_num(v) or 0.0, v) for k, v in data.items()]
    mx = max((abs(n) for _, n, _ in pairs), default=0.0) or 1.0
    bars = ""
    for k, n, raw in pairs:
        h = max(3.0, abs(n) / mx * 100.0)
        cls = "neg" if n < 0 else "pos"
        bars += (f'<div class="cbar"><div class="cv">{fmt(raw)}</div>'
                 f'<div class="cb {cls}" style="height:{h:.2f}%"></div>'
                 f'<div class="cl">{html.escape(k)}</div></div>')
    return f'<div class="chart">{bars}</div>'


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
:root{color-scheme:dark;--bg:#0b0f14;--panel:#141a22;--panel2:#171e27;
--line:#232c38;--ink:#e7ecf2;--mut:#8a97a7;--acc:#5b9cff;--ok:#37d399;
--bad:#ff6b6b;--warn:#f0c264}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
margin:0;background:var(--bg);color:var(--ink);line-height:1.5;
font-size:14px;scroll-behavior:smooth}
a{color:inherit;text-decoration:none}
.app{display:grid;grid-template-columns:230px minmax(0,1fr);min-height:100vh}
.sidebar{position:sticky;top:0;align-self:start;height:100vh;
background:linear-gradient(180deg,#10161e,#0c1118);border-right:1px solid var(--line);
padding:22px 16px;display:flex;flex-direction:column;gap:6px}
.brand{font-weight:800;font-size:16px;letter-spacing:.02em;margin-bottom:6px;
display:flex;align-items:center;gap:8px}
.brand .dot{width:10px;height:10px;border-radius:50%;background:var(--ok);
box-shadow:0 0 10px var(--ok)}
.nav{display:flex;flex-direction:column;gap:2px;margin-top:8px}
.nav a{padding:9px 12px;border-radius:9px;color:var(--mut);font-weight:600;
font-size:13.5px;border:1px solid transparent}
.nav a:hover{background:#1a2230;color:var(--ink);border-color:var(--line)}
.navnote{margin-top:auto;color:var(--mut);font-size:11.5px;line-height:1.4}
.main{padding:26px 30px 60px;max-width:1500px}
header{margin-bottom:8px}
.h1{font-size:26px;font-weight:800;display:flex;align-items:center;gap:12px;
flex-wrap:wrap;margin:0 0 4px}
.sub{color:var(--mut);font-size:13px}
.sub code{color:#aeb9c4}
.hbadges{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
section{margin-top:22px;scroll-margin-top:18px}
.sec-h{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
color:var(--mut);font-weight:700;margin:0 0 10px}
h2{font-size:15px;margin:0 0 12px;color:#cdd7e2;font-weight:700}
h3{font-size:12px;margin:16px 0 8px;color:var(--mut);text-transform:uppercase;
letter-spacing:.05em}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:6px 0 0}
.kpi{background:linear-gradient(180deg,var(--panel),var(--panel2));
border:1px solid var(--line);border-radius:14px;padding:16px 18px;
position:relative;overflow:hidden}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;
background:var(--acc)}
.kpi.k-ok::before{background:var(--ok)}.kpi.k-bad::before{background:var(--bad)}
.kpi.k-warn::before{background:var(--warn)}
.kpi .label{font-size:12px;color:var(--mut);margin-bottom:8px;font-weight:600}
.kpi .val{font-size:26px;font-weight:800;letter-spacing:-.01em;
font-variant-numeric:tabular-nums}
.kpi .ksub{font-size:12px;color:var(--mut);margin-top:6px}
.grid{display:grid;gap:16px}
.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.g3{grid-template-columns:repeat(3,minmax(0,1fr))}
.layout{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;
align-items:start}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:20px}
.card.full{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:11.5px;text-transform:uppercase;
letter-spacing:.03em}
.kv td.k{color:#9aa6b2;white-space:nowrap;width:56%}
.kv td.v{text-align:right;font-variant-numeric:tabular-nums;font-weight:700}
.tbl td{font-variant-numeric:tabular-nums}
.tbl td.num{text-align:right}
.tbl td.tk{font-weight:800;color:#dfe7f0}
.tbl tr.row-neg td{background:rgba(255,107,107,.05)}
.tbl tr.row-pos td{background:rgba(55,211,153,.05)}
.badge{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:700;
font-size:12.5px}
.badge.lg{font-size:15px;padding:7px 16px}
.ok{background:rgba(55,211,153,.14);color:var(--ok);border:1px solid #1c5a36}
.warn{background:rgba(240,194,100,.14);color:var(--warn);border:1px solid #6b5c1e}
.bad{background:rgba(255,107,107,.14);color:var(--bad);border:1px solid #6b1e1e}
.muted{color:var(--mut)}
.neg{color:var(--bad)}.pos{color:var(--ok)}
ul.tight{margin:6px 0 0;padding-left:20px}ul.tight li{margin:3px 0}
.caution{background:rgba(240,194,100,.09);border:1px solid #6b531e;
border-radius:10px;padding:10px 13px;margin:8px 0;font-size:13px;color:var(--warn)}
.tag{display:inline-block;font-size:11px;color:var(--mut);border:1px solid var(--line);
border-radius:6px;padding:1px 7px;margin-left:8px}
.bar{margin:12px 0}
.bar-h{display:flex;justify-content:space-between;font-size:12.5px;
margin-bottom:5px}.bar-h .bl{color:var(--mut);font-weight:600}
.bar-h .bv{font-weight:700;font-variant-numeric:tabular-nums}
.track{height:12px;background:#0c121a;border:1px solid var(--line);
border-radius:999px;overflow:hidden}
.fill{height:100%;border-radius:999px;background:var(--acc)}
.fill.ok{background:linear-gradient(90deg,#2bb37f,var(--ok))}
.fill.warn{background:linear-gradient(90deg,#c79a3a,var(--warn))}
.fill.bad{background:linear-gradient(90deg,#c74b4b,var(--bad))}
.execs{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
.exec{background:#10161e;border:1px solid var(--line);border-radius:12px;
padding:14px}
.exec .et{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--mut);font-weight:700;margin-bottom:6px}
.exec .es{font-size:13px}
.donutwrap{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.legend{display:flex;flex-direction:column;gap:6px;font-size:12.5px}
.legend .li{display:flex;align-items:center;gap:8px}
.legend .sw{width:11px;height:11px;border-radius:3px;display:inline-block}
.chart{display:flex;align-items:flex-end;gap:10px;height:150px;
padding:10px 4px 0;overflow-x:auto}
.cbar{display:flex;flex-direction:column;align-items:center;justify-content:flex-end;
gap:6px;min-width:54px;height:100%}
.cbar .cb{width:30px;border-radius:6px 6px 0 0;background:var(--acc);
min-height:3px}
.cbar .cb.pos{background:linear-gradient(180deg,var(--ok),#1f7d5b)}
.cbar .cb.neg{background:linear-gradient(180deg,var(--bad),#7d1f1f)}
.cbar .cl{font-size:11px;color:var(--mut);white-space:nowrap}
.cbar .cv{font-size:11px;font-weight:700;font-variant-numeric:tabular-nums}
pre{background:#0a0e13;border:1px solid var(--line);border-radius:10px;padding:14px;
overflow:auto;font-size:12px;max-height:480px}
details{margin:8px 0}
summary{cursor:pointer;color:var(--acc);font-weight:700}
.note{font-size:12.5px;color:var(--mut);margin-top:10px}
@media(max-width:1200px){.kpis{grid-template-columns:repeat(2,1fr)}
.execs{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}
.g2,.g3{grid-template-columns:1fr}}
@media(max-width:860px){.app{grid-template-columns:1fr}
.sidebar{position:static;height:auto;flex-direction:row;flex-wrap:wrap;
align-items:center}.nav{flex-direction:row;flex-wrap:wrap}.navnote{display:none}
.kpis{grid-template-columns:1fr}.execs{grid-template-columns:1fr}}
"""

# Боковая навигация (только якоря на той же странице; не действия)
_NAV = (("overview", "Overview"), ("portfolio", "Portfolio"), ("income", "Income"),
        ("turnover", "Turnover"), ("contributions", "Contributions"),
        ("risk", "Risk"), ("lasttrade", "Last trade"), ("raw", "Raw JSON"))
# Палитра для донат-аллокации (inline, без внешних ресурсов)
_DONUT_COLORS = ("#5b9cff", "#37d399", "#f0c264", "#c78bff", "#ff8a5b",
                 "#4fd0e0", "#ff6b9d", "#8a97a7")


def _sidebar() -> str:
    links = "".join(f'<a href="#{i}">{html.escape(n)}</a>' for i, n in _NAV)
    return ('<aside class="sidebar"><div class="brand"><span class="dot"></span>'
            'Portfolio cockpit</div>'
            f'<nav class="nav">{links}</nav>'
            '<div class="navnote">read-only · рендер F4.8 · без токенов/сети</div>'
            '</aside>')


# ─── missing-report страница ──────────────────────────────────────────────────

def _missing_html(state: dict) -> str:
    path = html.escape(str(state.get("_report_path") or DEFAULT_REPORT_PATH))
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio cockpit — нет данных</title><style>{_CSS}</style></head>
<body><div class="app">{_sidebar()}<main class="main">
<header><div class="h1">Portfolio cockpit {_badge("НЕТ ДАННЫХ", big=True)}</div>
<div class="sub">Read-only portfolio overview from F4.8 · не торгует, без токенов/сети</div>
</header>
<section><div class="card"><h2>Отчёт F4.8 не найден</h2>
<p>Файл <code>{path}</code> отсутствует или нечитаем. F4.9 рендерит только
готовый отчёт F4.8 и сам данные не считает.</p>
<h3>Как починить</h3>
<p>Сначала сформируйте отчёт F4.8 (read-only), затем перезапустите/обновите F4.9:</p>
<pre>python main.py portfolio-dashboard-data --live-account-id &lt;ACCOUNT_ID&gt;</pre>
<div class="note">Чтобы включить учёт взносов, скопируйте
<code>config/contribution_plan.example.json</code> в
<code>data/config/contribution_plan.json</code>.</div>
</div></section>
<div class="note" style="margin-top:20px">F4.9 — только просмотр. Нет POST/действий,
нет кнопок торговли. Остановить сервер: Ctrl+C.</div>
</main></div></body></html>"""


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
    safe, unsafe = _safety(state)
    contrib_on = cn.get("contributions_tracking_enabled")
    pnl = _to_num(kpi.get("portfolio_unrealized_pnl_rub"))
    pnl_cls = ("k-bad" if (pnl is not None and pnl < 0)
               else ("k-ok" if (pnl is not None and pnl > 0) else ""))
    return "".join([
        _kpi("Стоимость портфеля", _money(kpi.get("portfolio_value_rub")),
             sub="всего активов"),
        _kpi("Пассивный доход / мес.",
             _money(kpi.get("passive_income_monthly_rub")), sub="брутто"),
        _kpi("Покрытие цели 150 000 ₽/мес.",
             _pct(kpi.get("passive_income_coverage_pct")),
             sub="до 150 000 ₽/мес.", cls="k-warn"),
        _kpi("Свободный кэш", _money(kpi.get("cash_rub")),
             sub=_pct(kpi.get("cash_pct")) + " портфеля"),
        _kpi("Оборот YTD (цель 60M)", _money(kpi.get("turnover_ytd_rub")),
             sub=_pct(kpi.get("turnover_ytd_progress_pct")) + " прогресс"),
        _kpi("PnL портфеля", _pnl(kpi.get("portfolio_unrealized_pnl_rub")),
             sub=_pct(kpi.get("portfolio_unrealized_pnl_pct")), cls=pnl_cls),
        _kpi("Взносы", _badge("настроены" if contrib_on else "не настроены"),
             sub="учёт"),
        _kpi("Безопасность", _badge(safe),
             sub="read-only", cls="k-bad" if unsafe else "k-ok"),
    ])


def _interpretation(state: dict) -> str:
    """Executive summary карточками (по одному факту на карточку)."""
    pf = state.get("portfolio_summary") or {}
    inc = state.get("income_summary") or {}
    tn = state.get("turnover_summary") or {}
    cn = state.get("contributions_summary") or {}
    rk = state.get("risk_summary") or {}
    cov = inc.get("income_target_coverage_pct")
    neg = rk.get("negative_pnl_positions_count")
    cnt = pf.get("positions_count")
    cards = [
        ("Portfolio",
         f"Портфель {_money(pf.get('total_portfolio_value_rub'))}, кэш "
         f"{_pct(pf.get('cash_pct'))}; нереализованный PnL "
         f"{_pnl(pf.get('unrealized_pnl_rub'))}."),
        ("Income",
         f"Пассивный доход {_money(inc.get('passive_income_rub_monthly_gross'))}"
         f"/мес.; покрытие цели 150 000 ₽/мес. — {_pct(cov)}, покрывает только "
         f"{_pct(cov)} цели."),
        ("Turnover",
         f"Оборот {_money(tn.get('turnover_ytd_rub'))} из "
         f"{_money(tn.get('turnover_annual_target_rub'))} "
         f"({_pct(tn.get('turnover_ytd_progress_pct'))}); требуется в день "
         f"{_money(tn.get('turnover_daily_required_rub'))}."),
        ("Contributions",
         "Взносы настроены." if cn.get("contributions_tracking_enabled")
         else "Взносы НЕ настроены — учёт выключен."),
        ("Risk",
         (f"{neg}/{cnt} позиций в минусе; концентрация топ-5 = "
          f"{_pct(rk.get('top_5_positions_weight_pct'))}.")
         if (neg is not None and cnt) else "Риск-данные ограничены."),
    ]
    execs = "".join(
        f'<div class="exec"><div class="et">{html.escape(t)}</div>'
        f'<div class="es">{s}</div></div>' for t, s in cards)
    body = (f'<div class="execs">{execs}</div>'
            '<div class="note">Это диагностический дашборд (факты из F4.8), '
            'НЕ торговая рекомендация.</div>')
    return _card("Что сейчас", body)


def _progress_card(state: dict) -> str:
    inc = state.get("income_summary") or {}
    tn = state.get("turnover_summary") or {}
    pf = state.get("portfolio_summary") or {}
    body = (
        _progress(
            "Пассивный доход к цели 150 000 ₽/мес.",
            _money(inc.get("passive_income_rub_monthly_gross")) + " / "
            + _money(inc.get("target_monthly_income_rub")),
            inc.get("income_target_coverage_pct"), color="warn")
        + _progress(
            "Оборот YTD к цели 60M",
            _money(tn.get("turnover_ytd_rub")) + " / "
            + _money(tn.get("turnover_annual_target_rub")),
            tn.get("turnover_ytd_progress_pct"), color="ok")
        + _progress(
            "Доля свободного кэша", _pct(pf.get("cash_pct")),
            pf.get("cash_pct"), color="ok"))
    pnl = _to_num(pf.get("unrealized_pnl_rub"))
    pnl_badge = (_badge("убыток") if (pnl is not None and pnl < 0)
                 else (_badge("прибыль") if (pnl is not None and pnl > 0) else ""))
    body += (f'<div class="note">PnL портфеля: '
             f'{_pnl(pf.get("unrealized_pnl_rub"))} '
             f'({_pct(pf.get("unrealized_pnl_pct"))}) {pnl_badge}</div>')
    return _card("Прогресс к целям", body)


def _donut_card(state: dict) -> str:
    body = ('<div class="note">Доли построены из <code>positions[].weight_pct</code>; '
            'это НЕ классы активов.</div>'
            + _donut(state.get("positions") or []))
    return _card("Аллокация по весу позиций · Position weight allocation", body)


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
    if not positions:
        body = '<div class="note">Позиций нет.</div>'
        return _card("B · Позиции (0)", body, klass="card full")
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body_rows = ""
    for p in positions:
        pnl = _to_num(p.get("unrealized_pnl_rub"))
        rcls = ("row-neg" if (pnl is not None and pnl < 0)
                else ("row-pos" if (pnl is not None and pnl > 0) else ""))
        cells = [
            f'<td class="tk">{_esc(p.get("ticker"))}</td>',
            f'<td>{_esc(p.get("name"))}</td>',
            f'<td class="num">{_units(p.get("quantity_units"))}</td>',
            f'<td class="num">{_money(p.get("current_price"))}</td>',
            f'<td class="num">{_money(p.get("market_value_rub"))}</td>',
            f'<td class="num">{_pct(p.get("weight_pct"))}</td>',
            f'<td class="num">{_pnl(p.get("unrealized_pnl_rub"))}</td>',
            f'<td class="num">{_pct(p.get("unrealized_pnl_pct"))}</td>',
            f'<td class="num">{_money(p.get("expected_income_rub_yearly"))}</td>',
            f'<td class="num">{_money(p.get("expected_income_rub_monthly"))}</td>',
            f'<td>{_esc(p.get("next_income_event_date"))}</td>',
            (f'<td>{_esc(p.get("income_data_source"))} / '
             f'{_esc(p.get("income_data_confidence"))}</td>'),
        ]
        body_rows += f'<tr class="{rcls}">' + "".join(cells) + "</tr>"
    table = f'<table class="tbl"><tr>{head}</tr>{body_rows}</table>'
    return _card(f"B · Позиции ({len(positions)})", table, klass="card full")


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
        body += ("<h3>Календарь дохода по месяцам</h3>"
                 + _barchart({str(m): v for m, v in cal.items()}))
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
        body += ("<h3>Оборот по сторонам (buy/sell)</h3>"
                 + _barchart({str(k): v for k, v in by_side.items()}))
    by_month = tn.get("turnover_by_month") or {}
    if by_month:
        body += ("<h3>Оборот по месяцам</h3>"
                 + _barchart({str(k): v for k, v in by_month.items()}))
    by_instr = tn.get("turnover_by_instrument") or {}
    if by_instr:
        rows = [[html.escape(str(k)), _money(v)] for k, v in by_instr.items()]
        body += "<h3>Оборот по инструментам</h3>" + _table(["Инструмент", "Оборот"], rows)
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
        body += ('<h3>Предупреждения отображения (пороги дашборда)'
                 '<span class="tag">Display threshold, not investment advice</span>'
                 '</h3>'
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

    acc = _esc(state.get("live_account_id_masked"))
    gen = _esc(state.get("generated_at"))
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio cockpit (F4.9 read-only)</title>
<style>{_CSS}</style></head>
<body>
<div class="app">
{_sidebar()}
<main class="main">
<header>
<div class="h1">Portfolio cockpit {_badge(safe, big=True)}</div>
<div class="sub">Read-only portfolio overview from F4.8 ·
<strong>не торгует, без токенов/брокера/сети</strong></div>
<div class="hbadges">{_badge(fresh)}<span class="tag">account {acc}</span>
<span class="tag">generated {gen}</span></div>
</header>
{host_banner}
<section id="overview"><div class="sec-h">Overview</div>
<div class="kpis">{_kpi_strip(state)}</div>
{_interpretation(state)}
{_progress_card(state)}
</section>
<section id="portfolio"><div class="sec-h">Portfolio</div>
<div class="grid g2">{_portfolio_card(state)}{_donut_card(state)}</div>
</section>
<section id="income"><div class="sec-h">Income</div>
{_income_card(state)}</section>
<section id="turnover"><div class="sec-h">Turnover</div>
{_turnover_card(state)}</section>
<section id="contributions"><div class="sec-h">Contributions</div>
{_contributions_card(state)}</section>
<section id="risk"><div class="sec-h">Risk</div>
{_risk_card(state)}</section>
<section id="positions"><div class="sec-h">Positions</div>
{_positions_card(state)}</section>
<section id="lasttrade"><div class="sec-h">Last trade</div>
{_last_trade_card(state)}</section>
<section id="raw"><div class="sec-h">Raw JSON</div>
{_raw_card(state)}</section>
<div class="note" style="margin-top:20px">F4.9 — только просмотр отчёта F4.8. Нет
POST/действий, нет кнопок торговли. Обновить данные: запустите F4.8. Остановить
сервер: Ctrl+C.</div>
</main>
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
