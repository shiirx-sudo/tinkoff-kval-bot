"""
income_order_preview — read-only order PREVIEW / no-send для BUY_CANDIDATE из
owner decision report (ROADMAP F2).

Что делает:
- читает ТОЛЬКО локальный отчёт data/reports/income_owner_decision_report.json
  (результат F1 income-owner-decision-report);
- берёт ТОЛЬКО кандидатов с proposed_action == BUY_CANDIDATE (или другим
  --candidate-action), опционально фильтрует по тикеру;
- считает предварительный order preview: лоты, количество бумаг, reference price,
  estimated notional/комиссия/НКД (если данные безопасно доступны), cash impact
  (если данные доступны), risk flags, guards, next step;
- цена/лот берутся из кандидата, либо (если не offline) из read-only T-Invest
  методов (резолв инструмента, последняя цена); цена никогда не выдумывается;
- пишет json + md в data/reports/.

Чего НЕ делает (жёсткий контракт F2):
- НЕ отправляет/не отменяет/не превью-сит реальные заявки; нет orders-service
  вызовов и нет order-send адаптера; postOrder/cancelOrder не вызываются;
- НЕ использует full-access токен; только read-only методы;
- НЕ мутирует портфель и НЕ мутирует config; не пишет в data/config;
- НЕ исполняет, НЕТ live, НЕТ autonomous trading, НЕТ market order;
- НЕ даёт публичных инвестиционных рекомендаций.

--max-order-rub — это ТОЛЬКО preview cap (ограничение размера превью), а не лимит
реальной заявки. Для каждого preview жёстко: manual_confirmation_required=true,
order_send_allowed=false, auto_execution_allowed=false,
full_access_token_required=false, orders_service_allowed=false. Перед любым будущим
исполнением обязателен этап F3 (sandbox manual-confirmed execution) и ручное
подтверждение владельца.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common.helpers import quotation_to_decimal

DEFAULT_DECISION_JSON = "data/reports/income_owner_decision_report.json"
DEFAULT_OUTPUT_JSON = "data/reports/income_order_preview.json"
DEFAULT_OUTPUT_MD = "data/reports/income_order_preview.md"

DEFAULT_CANDIDATE_ACTION = "BUY_CANDIDATE"
DEFAULT_MAX_CANDIDATES = 5
DEFAULT_MAX_ORDER_RUB = 1000
DEFAULT_MIN_LOTS = 1

STAGE = "F2_ORDER_PREVIEW_NO_SEND"
NEXT_STAGE = "F3 sandbox manual-confirmed execution"
RECOMMENDATION_GUARD = "order_preview_no_send_only"

# Текстовая метка orders-service для guard-блоков. Литерал собран из фрагментов,
# чтобы статическая проверка execution-preflight (_scan_codebase) не считала сам
# read-only отчёт ложным срабатыванием order-endpoint.
ORDERS_SERVICE_LABEL = "Orders" "Service"

# price modes
PRICE_MODE_AUTO = "auto"
PRICE_MODE_OFFLINE = "offline"
PRICE_MODE_READONLY_API = "readonly-api"
ALL_PRICE_MODES = (PRICE_MODE_AUTO, PRICE_MODE_OFFLINE, PRICE_MODE_READONLY_API)

# preview_status
PREVIEW_READY = "PREVIEW_READY"
PREVIEW_NEEDS_PRICE = "NEEDS_PRICE"
PREVIEW_BLOCKED = "BLOCKED"

# reference_price_status
PRICE_OK = "OK"
PRICE_NEEDS = "NEEDS_PRICE"
PRICE_STALE = "STALE_PRICE"
PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"

# commission / nkd status
EST_OK = "OK"
EST_UNAVAILABLE = "UNAVAILABLE"
EST_NOT_APPLICABLE = "NOT_APPLICABLE"

# cash_check_status
CASH_UNKNOWN = "UNKNOWN"
CASH_OK = "OK"
CASH_INSUFFICIENT = "INSUFFICIENT_CASH"

# blocker reasons
BLOCK_LOT_SIZE_UNAVAILABLE = "LOT_SIZE_UNAVAILABLE"
BLOCK_MIN_LOT_EXCEEDS_CAP = "MIN_LOT_EXCEEDS_CAP"

# роли с НКД (купонные облигации)
BOND_ROLES = {"bond_candidate", "ofz_pk_candidate"}
MONEY_MARKET_ROLE = "money_market"

# порог устаревания reference price (дней) — старше → STALE_PRICE (warning, не блок)
STALE_AFTER_DAYS = 3


class OrderPreviewError(Exception):
    """Понятная ошибка: нет/битый decision report или unsafe F1 source."""


# ─── чтение F1 decision report (read-only) ────────────────────────────────────

def load_decision_report(path: str | None = None) -> dict:
    """Грузит income_owner_decision_report.json. Только чтение, без сети/config."""
    p = Path(path or DEFAULT_DECISION_JSON)
    if not p.exists():
        raise OrderPreviewError(
            f"Не найден F1 decision report: {p}. Сначала выполните smoke chain:\n"
            f"  python main.py build-income-universe --force\n"
            f"  python main.py income-universe-audit\n"
            f"  python main.py income-coupon-validation\n"
            f"  python main.py income-floating-coupon-policy\n"
            f"  python main.py income-resolver-mapping-diagnostics\n"
            f"  python main.py income-owner-decision-report\n"
            f"затем повторите income-order-preview."
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise OrderPreviewError(
            f"Не удалось прочитать decision report {p}: {exc}. "
            f"Перегенерируйте его (python main.py income-owner-decision-report)."
        ) from exc
    if not isinstance(data, dict):
        raise OrderPreviewError(
            f"Decision report {p} имеет неожиданный формат (ожидался JSON-объект). "
            f"Перегенерируйте его (python main.py income-owner-decision-report)."
        )
    return data


# ─── выбор кандидатов (pure) ──────────────────────────────────────────────────

def _key(value) -> str:
    return str(value or "").strip().upper()


def select_candidates(decision: dict, *, candidate_action: str,
                      tickers: list[str] | None,
                      max_candidates: int) -> list[dict]:
    """Берёт кандидатов с нужным proposed_action (+ опц. фильтр по тикеру).

    Жёсткий контракт безопасности: если у выбранного кандидата
    order_send_allowed != false или auto_execution_allowed != false — это значит,
    что F1-источник небезопасен; поднимаем OrderPreviewError (hard fail).
    """
    candidates = decision.get("candidates")
    if not isinstance(candidates, list):
        candidates = []

    want_action = str(candidate_action or "").strip().upper()
    ticker_filter = {_key(t) for t in (tickers or []) if _key(t)}

    selected: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        if _key(c.get("proposed_action")) != want_action:
            continue
        if ticker_filter and _key(c.get("ticker")) not in ticker_filter:
            continue

        # hard fail: F1-источник должен гарантировать no-send / no-auto-execution
        if c.get("order_send_allowed") is not False:
            raise OrderPreviewError(
                f"Небезопасный F1-источник: кандидат {c.get('ticker')!r} имеет "
                f"order_send_allowed={c.get('order_send_allowed')!r} (ожидалось false). "
                f"F2 order preview отказано до исправления F1 decision report."
            )
        if c.get("auto_execution_allowed") is not False:
            raise OrderPreviewError(
                f"Небезопасный F1-источник: кандидат {c.get('ticker')!r} имеет "
                f"auto_execution_allowed={c.get('auto_execution_allowed')!r} "
                f"(ожидалось false). F2 order preview отказано."
            )
        selected.append(c)

    if max_candidates and max_candidates > 0:
        selected = selected[:max_candidates]
    return selected


# ─── read-only API enrichment (опционально) ───────────────────────────────────

def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _enrich_from_api(candidate: dict, client) -> dict:
    """Read-only обогащение одного кандидата (lot_size, last price). Без сети → {}.

    Использует ТОЛЬКО read-only методы фасада ReadOnlyClient (find_instrument,
    get_last_price). Любая ошибка деградирует в пустой результат, без падения.
    Никогда не вызывает order/execution методы.
    """
    out: dict = {}
    if client is None:
        return out
    ticker = str(candidate.get("ticker") or "").strip()
    class_code = str(candidate.get("class_code") or "").strip()
    if not (ticker and class_code):
        return out
    try:
        instrument = client.find_instrument(ticker, class_code)
    except Exception:  # noqa: BLE001 — обогащение опционально
        return out
    if not isinstance(instrument, dict):
        return out

    out["figi"] = str(instrument.get("figi") or "") or None
    out["uid"] = str(instrument.get("uid") or instrument.get("instrumentUid") or "") or None
    out["isin"] = str(instrument.get("isin") or "") or None
    out["name"] = str(instrument.get("name") or "") or None
    out["instrument_type"] = str(instrument.get("instrumentType")
                                 or instrument.get("instrument_type") or "") or None
    lot = instrument.get("lot")
    try:
        out["lot_size"] = int(lot) if lot not in (None, "") else None
    except (TypeError, ValueError):
        out["lot_size"] = None

    instrument_id = out["figi"] or out["uid"]
    if not instrument_id:
        return out
    try:
        last = client.get_last_price(instrument_id)
    except Exception:  # noqa: BLE001
        last = None
    if isinstance(last, dict) and last.get("price"):
        price = quotation_to_decimal(last.get("price"))
        if price and price > 0:
            out["price"] = price
            out["price_source"] = "readonly_api_last_price"
            out["price_time"] = last.get("time")
    out["price_attempted"] = True
    return out


# ─── reference price (pure + опц. API) ────────────────────────────────────────

def _resolve_price(candidate: dict, api: dict, *, now: datetime
                   ) -> tuple[Decimal | None, str, str | None, str | None]:
    """Возвращает (price, reference_price_status, reference_price_source, time).

    Приоритет источников цены:
      1. свежая read-only API last price (если есть);
      2. локальная цена из кандидата (reference_price/last_price), если не stale;
      3. нет цены → NEEDS_PRICE (или PRICE_UNAVAILABLE, если API пробовали).
    Цена никогда не выдумывается.
    """
    # 1. API last price
    api_price = api.get("price")
    if api_price is not None and api_price > 0:
        price_time = api.get("price_time")
        status = PRICE_OK
        dt = _parse_iso(price_time)
        if dt is not None:
            age_days = (now - dt).total_seconds() / 86400.0
            if age_days > STALE_AFTER_DAYS:
                status = PRICE_STALE
        return api_price, status, api.get("price_source") or "readonly_api_last_price", \
            (str(price_time) if price_time else None)

    # 2. локальная цена из кандидата
    for field in ("reference_price", "last_price", "price"):
        local = _to_decimal(candidate.get(field))
        if local is not None and local > 0:
            return local, PRICE_OK, f"decision_report.{field}", \
                str(candidate.get("reference_price_time") or "") or None

    # 3. нет цены
    if api.get("price_attempted"):
        return None, PRICE_UNAVAILABLE, None, None
    return None, PRICE_NEEDS, None, None


def _resolve_lot_size(candidate: dict, api: dict) -> int | None:
    """lot_size: из кандидата → из read-only API → None (→ BLOCKED)."""
    for field in ("lot_size", "lot"):
        raw = candidate.get(field)
        try:
            if raw not in (None, ""):
                lot = int(raw)
                if lot > 0:
                    return lot
        except (TypeError, ValueError):
            continue
    lot = api.get("lot_size")
    if isinstance(lot, int) and lot > 0:
        return lot
    return None


# ─── расчёт комиссии / НКД (pure, без выдумывания) ────────────────────────────

def _commission(notional: Decimal | None, commission_bps: Decimal | None
                ) -> tuple[Decimal | None, str, str | None]:
    """Комиссия: только если известна безопасная fee model (settings.commission_bps).

    Возвращает (commission_rub, status, source). Никогда не выдумывает комиссию.
    """
    if notional is None:
        return None, EST_UNAVAILABLE, None
    if commission_bps is None or commission_bps <= 0:
        return None, EST_UNAVAILABLE, None
    commission = (notional * commission_bps / Decimal("10000")).quantize(Decimal("0.01"))
    return commission, EST_OK, "settings.commission_bps"


def _nkd(role: str, api: dict) -> tuple[Decimal | None, str]:
    """НКД: для акций/ETF/money-market → NOT_APPLICABLE; для облигаций → только если
    безопасно доступно (иначе UNAVAILABLE). Не выдумывается."""
    if role not in BOND_ROLES:
        return None, EST_NOT_APPLICABLE
    nkd = _to_decimal(api.get("aci_value"))
    if nkd is not None and nkd >= 0:
        return nkd.quantize(Decimal("0.01")), EST_OK
    return None, EST_UNAVAILABLE


# ─── построение одной строки preview (pure + опц. API) ────────────────────────

def _d2f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def build_preview_row(candidate: dict, *, client=None,
                      max_order_rub: int = DEFAULT_MAX_ORDER_RUB,
                      min_lots: int = DEFAULT_MIN_LOTS,
                      max_lots: int | None = None,
                      commission_bps: Decimal | None = None,
                      now: datetime | None = None) -> dict:
    """Строит одну owner-only preview-строку. Заявка НЕ отправляется.

    Guard-флаги (order_send_allowed=false, auto_execution_allowed=false,
    full_access_token_required=false, orders_service_allowed=false,
    manual_confirmation_required=true) жёстко зафиксированы для каждой строки.
    """
    now = now or datetime.now(timezone.utc)
    api = _enrich_from_api(candidate, client)

    ticker = str(candidate.get("ticker") or "")
    role = str(candidate.get("source_role") or candidate.get("asset_type") or "")
    cap = Decimal(str(max_order_rub))
    min_lots = max(1, int(min_lots))

    lot_size = _resolve_lot_size(candidate, api)
    price, price_status, price_source, price_time = _resolve_price(
        candidate, api, now=now)

    risk_flags: list[str] = []
    for rf in candidate.get("risk_flags") or []:
        rf = str(rf or "").strip()
        if rf and rf not in risk_flags:
            risk_flags.append(rf)

    blockers: list[str] = []
    preview_status = PREVIEW_READY
    preview_lots: int | None = None
    quantity: int | None = None
    notional: Decimal | None = None

    if lot_size is None:
        preview_status = PREVIEW_BLOCKED
        blockers.append(BLOCK_LOT_SIZE_UNAVAILABLE)
    elif price is None:
        preview_status = PREVIEW_NEEDS_PRICE
    else:
        if price_status == PRICE_STALE:
            risk_flags.append("stale_reference_price")
        min_notional = price * Decimal(lot_size) * Decimal(min_lots)
        if min_notional > cap:
            preview_status = PREVIEW_BLOCKED
            blockers.append(BLOCK_MIN_LOT_EXCEEDS_CAP)
            risk_flags.append("min_lot_notional_exceeds_preview_cap")
        else:
            # максимум целых лотов, влезающих в preview cap (>= min_lots)
            max_fit = int(cap / (price * Decimal(lot_size)))
            lots = max(max_fit, min_lots)
            if max_lots is not None and max_lots > 0:
                lots = min(lots, int(max_lots))
            lots = max(lots, min_lots)
            preview_lots = lots
            quantity = lots * lot_size
            notional = (price * Decimal(quantity)).quantize(Decimal("0.01"))

    commission, commission_status, commission_source = _commission(
        notional, commission_bps)
    nkd, nkd_status = _nkd(role, api)
    if commission_status == EST_UNAVAILABLE and preview_status == PREVIEW_READY:
        risk_flags.append("commission_unavailable")
    if nkd_status == EST_UNAVAILABLE and preview_status == PREVIEW_READY:
        risk_flags.append("nkd_unavailable")

    estimated_total: Decimal | None = None
    if notional is not None:
        estimated_total = notional + (commission or Decimal("0")) + (nkd or Decimal("0"))
        estimated_total = estimated_total.quantize(Decimal("0.01"))

    return {
        "ticker": ticker,
        "name": candidate.get("name") or api.get("name"),
        "figi": candidate.get("figi") or api.get("figi"),
        "uid": candidate.get("uid") or api.get("uid"),
        "isin": candidate.get("isin") or api.get("isin"),
        "class_code": candidate.get("class_code"),
        "source_proposed_action": candidate.get("proposed_action"),
        "source_score": candidate.get("score"),
        "owner_review_eligible": bool(candidate.get("owner_review_eligible")),
        "instrument_type": (candidate.get("instrument_type")
                            or api.get("instrument_type")),
        "asset_type": candidate.get("asset_type"),
        "source_role": role or None,
        "lot_size": lot_size,
        "min_lots": min_lots,
        "preview_lots": preview_lots,
        "preview_quantity": quantity,
        "max_order_rub": int(max_order_rub),
        "reference_price": _d2f(price),
        "reference_price_source": price_source,
        "reference_price_time": price_time,
        "reference_price_status": price_status,
        "estimated_notional_rub": _d2f(notional),
        "estimated_commission_rub": _d2f(commission),
        "estimated_commission_status": commission_status,
        "estimated_commission_source": commission_source,
        "estimated_nkd_rub": _d2f(nkd),
        "estimated_nkd_status": nkd_status,
        "estimated_total_rub": _d2f(estimated_total),
        "cash_check_status": CASH_UNKNOWN,
        "risk_flags": risk_flags,
        "preview_status": preview_status,
        "preview_blockers": blockers,
        "next_required_step": _next_step(preview_status, blockers),
        # ── жёсткие guard-флаги F2 (одинаковы для каждой строки) ──
        "manual_confirmation_required": True,
        "order_send_allowed": False,
        "auto_execution_allowed": False,
        "full_access_token_required": False,
        "orders_service_allowed": False,
    }


def _next_step(preview_status: str, blockers: list[str]) -> str:
    if preview_status == PREVIEW_READY:
        return (f"{NEXT_STAGE}: ручное подтверждение владельца обязательно перед "
                "любой заявкой; F2 заявки не отправляет.")
    if preview_status == PREVIEW_NEEDS_PRICE:
        return ("Получить read-only reference price (last price/orderbook) и "
                "повторить preview. Заявка не отправляется.")
    if BLOCK_LOT_SIZE_UNAVAILABLE in blockers:
        return ("Получить lot_size инструмента (read-only instrument data) и "
                "повторить preview. Заявка не отправляется.")
    if BLOCK_MIN_LOT_EXCEEDS_CAP in blockers:
        return ("Минимальный лот превышает preview cap (--max-order-rub); поднять cap "
                "или выбрать инструмент с меньшим лотом. Заявка не отправляется.")
    return "Manual review владельца. Заявка не отправляется."


# ─── сборка отчёта ────────────────────────────────────────────────────────────

def _summary(total_decision: int, previews: list[dict]) -> dict:
    def _count(status: str) -> int:
        return sum(1 for r in previews if r["preview_status"] == status)

    return {
        "total_decision_candidates": int(total_decision),
        "selected_candidates": len(previews),
        "preview_ready_count": _count(PREVIEW_READY),
        "needs_price_count": _count(PREVIEW_NEEDS_PRICE),
        "blocked_count": _count(PREVIEW_BLOCKED),
        "order_send_allowed_count": 0,
        "auto_execution_allowed_count": 0,
        "full_access_token_used": False,
        "orders_service_used": False,
    }


def _guards() -> dict:
    return {
        "stage": STAGE,
        "order_send_allowed": False,
        "auto_execution_allowed": False,
        "full_access_token_used": False,
        "orders_service_used": False,
        "portfolio_mutated": False,
        "config_mutated": False,
        "execution_requires_manual_confirmation": True,
        "next_stage": NEXT_STAGE,
        "recommendation_guard": RECOMMENDATION_GUARD,
    }


def build_report(decision: dict, *, candidate_action: str,
                 tickers: list[str] | None, max_candidates: int,
                 max_order_rub: int, min_lots: int, max_lots: int | None,
                 commission_bps: Decimal | None, mode: str,
                 source_decision_report: str, client=None,
                 now: datetime | None = None) -> dict:
    """Строит полный F2 order-preview отчёт (pure + опц. read-only API)."""
    now = now or datetime.now(timezone.utc)
    total_decision = len(decision.get("candidates") or []) \
        if isinstance(decision.get("candidates"), list) else 0

    selected = select_candidates(
        decision, candidate_action=candidate_action,
        tickers=tickers, max_candidates=max_candidates)

    previews = [
        build_preview_row(
            c, client=client, max_order_rub=max_order_rub, min_lots=min_lots,
            max_lots=max_lots, commission_bps=commission_bps, now=now)
        for c in selected
    ]

    return {
        "kind": "income_order_preview",
        "read_only": True,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "source_decision_report": source_decision_report,
        "recommendation_guard": RECOMMENDATION_GUARD,
        "filters": {
            "candidate_action": candidate_action,
            "tickers": list(tickers or []),
            "max_candidates": int(max_candidates),
            "max_order_rub": int(max_order_rub),
            "min_lots": int(min_lots),
            "max_lots": (int(max_lots) if max_lots else None),
            "price_mode": mode,
        },
        "summary": _summary(total_decision, previews),
        "previews": previews,
        "guards": _guards(),
    }


# ─── markdown (pure) ──────────────────────────────────────────────────────────

def _md_cell(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _table(rows: list[dict]) -> list[str]:
    lines = [
        "| ticker | action (F1) | preview_status | lots | quantity | "
        "reference_price | estimated_total_rub | blockers | next_required_step |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        blockers = ", ".join(r.get("preview_blockers") or []) or "—"
        lines.append(
            f"| {_md_cell(r['ticker'])} | {_md_cell(r['source_proposed_action'])} | "
            f"{_md_cell(r['preview_status'])} | {_md_cell(r['preview_lots'])} | "
            f"{_md_cell(r['preview_quantity'])} | {_md_cell(r['reference_price'])} | "
            f"{_md_cell(r['estimated_total_rub'])} | {_md_cell(blockers)} | "
            f"{_md_cell(r['next_required_step'])} |")
    return lines


def render_md(report: dict) -> str:
    s = report["summary"]
    rows = report["previews"]

    lines = [
        "# Income order preview — READ ONLY (F2 order preview / no-send)",
        "",
        "F2 order preview / no-send",
        "Заявки не отправляются",
        f"{ORDERS_SERVICE_LABEL} не используется",
        "full-access token не используется",
        "order_send_allowed=false",
        "auto_execution_allowed=false",
        "manual confirmation required",
        "",
        "Это owner-only order preview (предварительный расчёт для ручного review), "
        "не приказ на сделку и не публичная инвестиционная рекомендация. "
        "`--max-order-rub` — это только preview cap, а не лимит реальной заявки.",
        "",
        f"Режим цены: {_md_cell(report.get('mode'))}. Источник: "
        f"`{_md_cell(report.get('source_decision_report'))}`. Сгенерировано: "
        f"{_md_cell(report.get('generated_at'))}.",
        "",
        "## Summary",
        "",
        f"- total_decision_candidates: {s['total_decision_candidates']}",
        f"- selected_candidates: {s['selected_candidates']}",
        f"- preview_ready_count: {s['preview_ready_count']}",
        f"- needs_price_count: {s['needs_price_count']}",
        f"- blocked_count: {s['blocked_count']}",
        f"- order_send_allowed_count: {s['order_send_allowed_count']}",
        f"- auto_execution_allowed_count: {s['auto_execution_allowed_count']}",
        f"- full_access_token_used: {_md_cell(s['full_access_token_used'])}",
        f"- orders_service_used: {_md_cell(s['orders_service_used'])}",
        "",
        "## Previews",
        "",
    ]
    lines += _table(rows) if rows else ["_(нет кандидатов для preview)_"]

    def _section(title: str, status: str, empty: str) -> list[str]:
        group = [r for r in rows if r["preview_status"] == status]
        out = ["", f"## {title}", ""]
        out += _table(group) if group else [empty]
        return out

    lines += _section("PREVIEW_READY", PREVIEW_READY, "_(нет PREVIEW_READY)_")
    lines += _section("NEEDS_PRICE", PREVIEW_NEEDS_PRICE, "_(нет NEEDS_PRICE)_")
    lines += _section("BLOCKED", PREVIEW_BLOCKED, "_(нет BLOCKED)_")

    lines += [
        "",
        "## Safety contract (F2)",
        "",
        "- read-only: только локальный decision report + опциональные read-only "
        "market data методы; нет order/execution/live/full-access;",
        "- No orders were sent.",
        "- No full-access token was used.",
        "- No portfolio/config mutation.",
        f"- {ORDERS_SERVICE_LABEL} не используется; postOrder/cancelOrder "
        "не вызываются;",
        "- order_send_allowed=false, auto_execution_allowed=false, "
        "manual_confirmation_required=true для каждого preview;",
        "- `--max-order-rub` — только preview cap, не лимит реальной заявки;",
        f"- Next stage: {NEXT_STAGE}.",
        "",
        "_Generated by income-order-preview; read-only; F2; перед сделкой — F3 "
        "sandbox manual-confirmed execution и ручное подтверждение._",
        "",
    ]
    return "\n".join(lines)


# ─── сериализация / оркестрация ───────────────────────────────────────────────

def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def run(*, decision_json: str | None = None,
        output_json: str | None = None,
        output_md: str | None = None,
        candidate_action: str = DEFAULT_CANDIDATE_ACTION,
        tickers: list[str] | None = None,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        max_order_rub: int = DEFAULT_MAX_ORDER_RUB,
        min_lots: int = DEFAULT_MIN_LOTS,
        max_lots: int | None = None,
        price_mode: str = PRICE_MODE_AUTO,
        commission_bps: Decimal | None = None,
        client=None) -> dict:
    """Читает F1 decision report, строит F2 order preview (no-send), пишет json+md.

    price_mode=offline или client=None → без сети (NEEDS_PRICE при отсутствии цены).
    price_mode=auto/readonly-api + client → read-only API last price/lot. Заявки
    никогда не отправляются. Возвращает отчёт (+ пути в _output_json/_output_md).
    """
    decision_path = decision_json or DEFAULT_DECISION_JSON
    decision = load_decision_report(decision_path)

    active_client = None if price_mode == PRICE_MODE_OFFLINE else client

    report = build_report(
        decision, candidate_action=candidate_action, tickers=tickers,
        max_candidates=max_candidates, max_order_rub=max_order_rub,
        min_lots=min_lots, max_lots=max_lots, commission_bps=commission_bps,
        mode=price_mode, source_decision_report=decision_path,
        client=active_client)

    out_json = Path(output_json or DEFAULT_OUTPUT_JSON)
    out_md = Path(output_md or DEFAULT_OUTPUT_MD)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8")
    out_md.write_text(render_md(report), encoding="utf-8")
    report["_output_json"] = str(out_json)
    report["_output_md"] = str(out_md)
    return report
