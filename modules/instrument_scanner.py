"""
Read-only Instrument Scanner — оценка инструментов под набор оборота.

НИКАКИХ заявок: используются только read-only endpoints (InstrumentsService,
MarketDataService). Сервис заявок не используется; методов размещения или
отмены заявок здесь нет. Это оценка ликвидности/издержек, НЕ рекомендация.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from loguru import logger

from api.client import ReadOnlyClient
from brokers.tinkoff.rest_client import SECURITY_TRADING_STATUS_NORMAL
from common.helpers import quotation_to_decimal

DISCLAIMER = (
    "Это read-only оценка ликвидности и издержек. Это не рекомендация "
    "купить/продать. Перед реальными сделками нужно сверить комиссии, режим "
    "торгов, налоговые последствия и брокерский отчёт."
)

DEFAULT_CLASS_CODE = "TQBR"
DEFAULT_DEPTH = 20


# ─── Модель данных ───────────────────────────────────────────────────────────


@dataclass
class Candidate:
    ticker: str
    class_code: str = DEFAULT_CLASS_CODE
    note: str = ""


@dataclass
class ScanFilters:
    max_spread_bps: Decimal = Decimal("20")
    min_top_depth_rub: Decimal = Decimal("100000")
    depth: int = DEFAULT_DEPTH


@dataclass
class ScanResult:
    ticker: str
    figi: str = ""
    instrument_uid: str = ""
    name: str = ""
    instrument_type: str = ""
    class_code: str = ""
    lot: int = 1
    currency: str = ""
    nominal: Decimal | None = None
    trading_status: str = ""
    bid_best: Decimal | None = None
    ask_best: Decimal | None = None
    mid_price: Decimal | None = None
    last_price: Decimal | None = None
    spread_abs: Decimal | None = None
    spread_bps: Decimal | None = None
    bid_top_depth_lots: int = 0
    ask_top_depth_lots: int = 0
    bid_top_depth_rub: Decimal = Decimal("0")
    ask_top_depth_rub: Decimal = Decimal("0")
    min_side_top_depth_rub: Decimal = Decimal("0")
    depth_3_levels_rub: Decimal = Decimal("0")
    depth_5_levels_rub: Decimal = Decimal("0")
    estimated_one_way_cost_bps: Decimal | None = None
    estimated_roundtrip_cost_bps: Decimal | None = None
    estimated_monthly_cost_rub: Decimal = Decimal("0")
    estimated_year_cost_rub: Decimal = Decimal("0")
    spread_ok: bool = False
    depth_ok: bool = False
    trading_status_ok: bool = False
    data_ok: bool = False
    suitable_for_turnover: bool = False
    requested_class_code: str = ""
    resolved_class_code: str = ""
    resolution_method: str = "not_found"   # get_instrument_by|find_instrument|catalog_fallback|not_found
    resolution_warning: str = ""
    trading_status_warning: str = ""
    alternatives: list[dict] = field(default_factory=list)
    orderbook_empty: bool = False
    score: int = 0
    verdict: str = "NO_DATA"               # GOOD|WATCH|BAD|NOT_FOUND|RESOLVED_NOT_TRADING|NO_ORDERBOOK|NO_DATA
    warnings: list[str] = field(default_factory=list)


@dataclass
class ScanReport:
    as_of: date
    commission_bps: Decimal
    target_monthly_turnover: Decimal
    filters: ScanFilters
    candidates: list[Candidate]
    results: list[ScanResult]
    warnings: list[str]
    generated_at: str
    session_hint: str = ""
    disclaimer: str = DISCLAIMER


# ─── Загрузка кандидатов ─────────────────────────────────────────────────────


def load_candidates(
    symbols: str | None,
    class_code: str = DEFAULT_CLASS_CODE,
    config_path: str | Path = "config/instrument_candidates.yaml",
) -> list[Candidate]:
    """
    Кандидаты из --symbols (CSV) или из YAML-конфига. Пустой результат означает,
    что нужно передать --symbols (см. CLI-подсказку).
    """
    if symbols:
        out: list[Candidate] = []
        for raw in symbols.split(","):
            t = raw.strip().upper()
            if t:
                out.append(Candidate(ticker=t, class_code=class_code, note="cli"))
        return out

    path = Path(config_path)
    if not path.exists():
        return []
    return _load_candidates_yaml(path, class_code)


def _load_candidates_yaml(path: Path, default_class: str) -> list[Candidate]:
    text = path.read_text(encoding="utf-8")
    data = None
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 — fallback на минимальный парсер
        data = _minimal_yaml_candidates(text)
    items = (data or {}).get("candidates") or []
    out: list[Candidate] = []
    for it in items:
        ticker = str(it.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        out.append(Candidate(
            ticker=ticker,
            class_code=str(it.get("class_code") or default_class),
            note=str(it.get("note") or ""),
        ))
    return out


def _minimal_yaml_candidates(text: str) -> dict:
    """Минимальный парсер для структуры candidates без pyyaml."""
    items: list[dict] = []
    cur: dict | None = None
    in_list = False
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("candidates:"):
            in_list = True
            continue
        if not in_list:
            continue
        if s.startswith("- "):
            cur = {}
            items.append(cur)
            s = s[2:].strip()
        if cur is not None and ":" in s:
            k, _, v = s.partition(":")
            cur[k.strip()] = v.strip().strip('"').strip("'")
    return {"candidates": items}


# ─── Чтение цели из kval_plan.json ───────────────────────────────────────────


def target_from_kval_plan(reports_dir: str | Path) -> Decimal | None:
    """
    Берёт suggested_turnover ближайшего будущего месяца из kval_plan.json.
    Если файла нет/пуст — None (без падения).
    """
    path = Path(reports_dir) / "kval_plan.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    for m in data.get("monthly_plan") or []:
        if m.get("status") == "future_required":
            val = m.get("suggested_turnover")
            if val not in (None, "", 0, "0"):
                return Decimal(str(val))
    return None


# ─── Метрики и скоринг ───────────────────────────────────────────────────────


def _round(v: Decimal, places: str = "0.01") -> Decimal:
    return v.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _levels_rub(levels: list[dict], lot: int, n: int) -> Decimal:
    total = Decimal("0")
    for lvl in levels[:n]:
        price = quotation_to_decimal(lvl.get("price"))
        qty = int(lvl.get("quantity") or 0)
        total += price * lot * qty
    return total


def _score_liquidity(r: ScanResult, f: ScanFilters) -> None:
    """Считает score 0-100 и verdict GOOD/WATCH/BAD (только при наличии данных)."""
    max_spread = f.max_spread_bps if f.max_spread_bps > 0 else Decimal("20")
    min_depth = f.min_top_depth_rub if f.min_top_depth_rub > 0 else Decimal("1")

    spread_score = Decimal("40") * (Decimal("1") - r.spread_bps / (2 * max_spread))
    spread_score = max(Decimal("0"), min(Decimal("40"), spread_score))

    depth_ratio = r.min_side_top_depth_rub / min_depth
    depth_score = max(Decimal("0"), min(Decimal("30"), Decimal("30") * depth_ratio))

    quality_score = Decimal("20") if r.trading_status_ok else Decimal("10")

    penalties = Decimal("0")
    if not r.spread_ok:
        penalties += Decimal("5")
    if not r.depth_ok:
        penalties += Decimal("5")

    raw = spread_score + depth_score + quality_score - penalties
    r.score = int(max(Decimal("0"), min(Decimal("100"), raw)).to_integral_value())

    if r.score >= 70 and r.suitable_for_turnover:
        r.verdict = "GOOD"
    elif r.score >= 40:
        r.verdict = "WATCH"
    else:
        r.verdict = "BAD"


def _finalize(r: ScanResult, f: ScanFilters) -> None:
    """
    Назначает финальный verdict по приоритету состояний:
    NOT_FOUND > RESOLVED_NOT_TRADING > NO_ORDERBOOK > NO_DATA > GOOD/WATCH/BAD.
    """
    if r.resolution_method == "not_found":
        r.verdict = "NOT_FOUND"
        r.score = 0
        return
    if not r.trading_status_ok:
        r.verdict = "RESOLVED_NOT_TRADING"
        r.score = 0
        return
    if not r.data_ok:
        r.verdict = "NO_ORDERBOOK" if r.orderbook_empty else "NO_DATA"
        r.score = 0
        return
    _score_liquidity(r, f)


# ─── Сканер ──────────────────────────────────────────────────────────────────


class InstrumentScanner:
    """Оценивает кандидатов по рыночным данным. Read-only."""

    def __init__(self, client: ReadOnlyClient | None = None) -> None:
        self.client = client or ReadOnlyClient()

    @staticmethod
    def _pick(matches: list[dict]) -> dict:
        """Из совпадений по тикеру предпочитаем торгуемый через API."""
        for m in matches:
            if m.get("apiTradeAvailableFlag"):
                return m
        return matches[0]

    def _resolve(self, cand: Candidate) -> tuple[dict | None, str, str, str, list[dict]]:
        """
        Резолвит инструмент. Возвращает
        (instrument, method, resolved_class_code, warning, alternatives).
        method ∈ get_instrument_by | find_instrument | catalog_fallback | not_found.
        """
        ticker = cand.ticker.upper()
        requested = cand.class_code

        # 1) Прямой справочник по ticker+class_code
        try:
            instr = self.client.find_instrument(cand.ticker, requested)
            if instr:
                rcc = str(instr.get("classCode") or requested)
                return instr, "get_instrument_by", rcc, "", []
        except Exception:  # noqa: BLE001 — 404/ошибка → fallback
            pass

        # 2) FindInstrument
        matches: list[dict] = []
        try:
            found = self.client.find_instruments(cand.ticker)
            matches = [i for i in found
                       if str(i.get("ticker", "")).upper() == ticker]
        except Exception:  # noqa: BLE001
            matches = []
        if matches:
            best = self._pick(matches)
            full = None
            try:
                full = self.client.get_instrument_by_figi(best.get("figi", ""))
            except Exception:  # noqa: BLE001
                full = None
            instr = full or best
            rcc = str(instr.get("classCode") or best.get("classCode") or requested)
            warning = (f"requested_class_code={requested}, resolved_class_code={rcc}"
                       if rcc != requested else "")
            alts = ([{"ticker": str(m.get("ticker", "")),
                      "class_code": str(m.get("classCode", "")),
                      "figi": str(m.get("figi", ""))} for m in matches]
                    if len(matches) > 1 else [])
            return instr, "find_instrument", rcc, warning, alts

        # 3) Каталог (Etfs/Shares/Bonds/Currencies)
        try:
            catalog = self.client.instruments_catalog()
            cmatches = [i for i in catalog
                        if str(i.get("ticker", "")).upper() == ticker]
        except Exception:  # noqa: BLE001
            cmatches = []
        if cmatches:
            best = self._pick(cmatches)
            rcc = str(best.get("classCode") or requested)
            warning = (f"requested_class_code={requested}, resolved_class_code={rcc}"
                       if rcc != requested else "")
            alts = ([{"ticker": str(m.get("ticker", "")),
                      "class_code": str(m.get("classCode", "")),
                      "figi": str(m.get("figi", ""))} for m in cmatches]
                    if len(cmatches) > 1 else [])
            return best, "catalog_fallback", rcc, warning, alts

        # 4) Не найден
        return None, "not_found", "", "", []

    def _build_result(
        self, cand: Candidate, commission_bps: Decimal,
        target_monthly_turnover: Decimal, f: ScanFilters,
    ) -> ScanResult:
        r = ScanResult(ticker=cand.ticker, class_code=cand.class_code)
        r.requested_class_code = cand.class_code

        instr, method, rcc, res_warning, alts = self._resolve(cand)
        r.resolution_method = method
        r.alternatives = alts
        if res_warning:
            r.resolution_warning = res_warning
            r.warnings.append(res_warning)

        if not instr:
            r.warnings.append(
                "Не найден по ticker/class_code; попробуйте другой class_code "
                "или проверьте доступность у брокера."
            )
            _finalize(r, f)
            return r

        # Идентификация (всегда заполняем, даже если торгов нет)
        r.figi = str(instr.get("figi") or "")
        r.instrument_uid = str(instr.get("uid") or "")
        r.name = str(instr.get("name") or "")
        r.instrument_type = str(instr.get("instrumentType") or "")
        r.class_code = str(instr.get("classCode") or rcc or cand.class_code)
        r.resolved_class_code = r.class_code
        r.lot = int(instr.get("lot") or 1)
        r.currency = str(instr.get("currency") or "")
        if instr.get("nominal"):
            r.nominal = quotation_to_decimal(instr.get("nominal"))

        instrument_id = r.instrument_uid or r.figi

        # Режим торгов
        try:
            ts = self.client.get_trading_status(instrument_id)
            r.trading_status = str(ts.get("tradingStatus") or "")
        except Exception as exc:  # noqa: BLE001
            r.warnings.append(f"trading_status недоступен: {exc}")
        r.trading_status_ok = r.trading_status == SECURITY_TRADING_STATUS_NORMAL
        if not r.trading_status_ok:
            r.trading_status_warning = (
                f"Инструмент найден, но сейчас trading_status="
                f"{r.trading_status or 'UNKNOWN'}; запустите скан во время основной "
                "торговой сессии или проверьте доступность инструмента у брокера."
            )
            r.warnings.append(r.trading_status_warning)

        # Последняя цена (не критично)
        try:
            lp = self.client.get_last_price(instrument_id)
            if lp:
                r.last_price = quotation_to_decimal(lp.get("price"))
        except Exception as exc:  # noqa: BLE001
            r.warnings.append(f"last_price недоступен: {exc}")

        # Стакан
        try:
            ob = self.client.get_order_book(instrument_id, f.depth)
        except Exception as exc:  # noqa: BLE001
            r.warnings.append(f"order_book недоступен: {exc}")
            _finalize(r, f)
            return r

        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not bids or not asks:
            r.orderbook_empty = True
            r.warnings.append("пустой стакан (нет bid/ask)")
            _finalize(r, f)
            return r

        r.data_ok = True
        r.bid_best = quotation_to_decimal(bids[0].get("price"))
        r.ask_best = quotation_to_decimal(asks[0].get("price"))
        r.bid_top_depth_lots = int(bids[0].get("quantity") or 0)
        r.ask_top_depth_lots = int(asks[0].get("quantity") or 0)
        r.mid_price = _round((r.bid_best + r.ask_best) / 2, "0.0001")
        r.spread_abs = _round(r.ask_best - r.bid_best, "0.0001")
        if r.mid_price and r.mid_price > 0:
            r.spread_bps = _round(r.spread_abs / r.mid_price * 10000)
        else:
            r.spread_bps = Decimal("0")

        r.bid_top_depth_rub = _round(r.bid_best * r.lot * r.bid_top_depth_lots)
        r.ask_top_depth_rub = _round(r.ask_best * r.lot * r.ask_top_depth_lots)
        r.min_side_top_depth_rub = min(r.bid_top_depth_rub, r.ask_top_depth_rub)
        r.depth_3_levels_rub = _round(min(
            _levels_rub(bids, r.lot, 3), _levels_rub(asks, r.lot, 3)))
        r.depth_5_levels_rub = _round(min(
            _levels_rub(bids, r.lot, 5), _levels_rub(asks, r.lot, 5)))

        half_spread_bps = r.spread_bps / 2
        r.estimated_one_way_cost_bps = _round(half_spread_bps + commission_bps)
        r.estimated_roundtrip_cost_bps = _round(r.spread_bps + 2 * commission_bps)
        r.estimated_monthly_cost_rub = _round(
            target_monthly_turnover * r.estimated_roundtrip_cost_bps / 10000)
        r.estimated_year_cost_rub = _round(r.estimated_monthly_cost_rub * 12)

        r.spread_ok = r.spread_bps <= f.max_spread_bps
        r.depth_ok = r.min_side_top_depth_rub >= f.min_top_depth_rub
        r.suitable_for_turnover = (
            r.spread_ok and r.depth_ok and r.trading_status_ok and r.data_ok)

        _finalize(r, f)
        return r

    def scan(
        self,
        candidates: list[Candidate],
        as_of: date | None = None,
        commission_bps: Decimal = Decimal("0"),
        target_monthly_turnover: Decimal = Decimal("0"),
        filters: ScanFilters | None = None,
    ) -> ScanReport:
        as_of = as_of or date.today()
        f = filters or ScanFilters()
        warnings: list[str] = []
        results: list[ScanResult] = []

        for cand in candidates:
            try:
                results.append(self._build_result(
                    cand, commission_bps, target_monthly_turnover, f))
            except Exception as exc:  # noqa: BLE001 — один инструмент не валит скан
                logger.warning(f"Скан {cand.ticker} упал: {exc}")
                r = ScanResult(ticker=cand.ticker, class_code=cand.class_code)
                r.warnings.append(f"ошибка скана: {exc}")
                results.append(r)
                warnings.append(f"{cand.ticker}: {exc}")

        # Подсказка про торговую сессию: если все НАЙДЕННЫЕ инструменты не торгуются
        # или с пустым стаканом — вероятно, скан вне активной сессии.
        resolved = [r for r in results if r.resolution_method != "not_found"]
        session_hint = ""
        if resolved and all(
            r.verdict in ("RESOLVED_NOT_TRADING", "NO_ORDERBOOK") for r in resolved
        ):
            session_hint = (
                "Похоже, скан запущен вне активной торговой сессии или инструменты "
                "сейчас недоступны. Для оценки спреда/глубины запускайте во время торгов."
            )
            warnings.append(session_hint)

        logger.info(
            f"Сканер: кандидатов={len(candidates)}, "
            f"commission_bps={commission_bps}, "
            f"target_monthly_turnover={target_monthly_turnover} ₽"
        )

        return ScanReport(
            as_of=as_of, commission_bps=commission_bps,
            target_monthly_turnover=target_monthly_turnover, filters=f,
            candidates=candidates, results=results, warnings=warnings,
            generated_at=datetime.now(timezone.utc).isoformat(),
            session_hint=session_hint,
        )
