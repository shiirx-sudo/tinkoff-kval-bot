"""
Read-only чтение баланса и портфеля (для balance-adaptive сайзинга и аналитики).

Ничего не покупает и не продаёт. Только GetPositions / GetPortfolio через
read-only клиент. Никаких order-endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from loguru import logger

from common.helpers import quotation_to_decimal


def _resolve_account_id(client, account_id: str | None) -> str | None:
    if account_id:
        return account_id
    try:
        accounts = client.get_broker_accounts()
        if accounts:
            return str(accounts[0].get("id") or accounts[0].get("accountId") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Не удалось получить список счетов: {exc}")
    return None


def available_cash_rub(client, account_id: str | None) -> Decimal | None:
    """Свободные рубли на счёте (read-only). None при ошибке/отсутствии данных."""
    acc = _resolve_account_id(client, account_id)
    if not acc:
        return None
    try:
        positions = client.get_positions(acc)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"GetPositions недоступен: {exc}")
        return None
    for money in positions.get("money") or []:
        if str(money.get("currency", "")).lower() == "rub":
            return quotation_to_decimal(money)
    return Decimal("0")


@dataclass
class PortfolioBreakdown:
    account_id_masked: str = ""
    free_rub: Decimal = Decimal("0")
    money_market_funds_rub: Decimal = Decimal("0")
    bonds_rub: Decimal = Decimal("0")
    dividend_shares_rub: Decimal = Decimal("0")
    other_rub: Decimal = Decimal("0")
    total_rub: Decimal = Decimal("0")
    expected_yield_rub: Decimal = Decimal("0")
    positions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_MM_FUND_TICKERS = {"LQDT", "TMON", "AKMM", "SBMM"}


def _pos_value(pos: dict) -> Decimal:
    qty = quotation_to_decimal(pos.get("quantity")) or Decimal("0")
    price = quotation_to_decimal(pos.get("currentPrice")) or Decimal("0")
    return qty * price


def portfolio_breakdown(client, account_id: str | None) -> PortfolioBreakdown:
    """Аналитическая разбивка портфеля (read-only). НЕ рекомендация."""
    acc = _resolve_account_id(client, account_id)
    out = PortfolioBreakdown()
    if not acc:
        out.warnings.append("Не удалось определить account_id.")
        return out
    out.account_id_masked = f"***{acc[-4:]}" if len(acc) >= 4 else "***"

    out.free_rub = available_cash_rub(client, acc) or Decimal("0")
    try:
        pf = client.get_portfolio(acc)
    except Exception as exc:  # noqa: BLE001
        out.warnings.append(f"GetPortfolio недоступен: {exc}")
        return out

    out.expected_yield_rub = quotation_to_decimal(pf.get("expectedYield")) or Decimal("0")
    for pos in pf.get("positions") or []:
        itype = str(pos.get("instrumentType", "")).lower()
        figi = str(pos.get("figi", ""))
        value = _pos_value(pos)
        ticker = str(pos.get("ticker", "")).upper()
        record = {"figi": figi, "ticker": ticker, "instrument_type": itype,
                  "value_rub": value}
        out.positions.append(record)
        if itype == "currency":
            continue  # деньги учтены в free_rub
        elif itype == "etf" and ticker in _MM_FUND_TICKERS:
            out.money_market_funds_rub += value
        elif itype == "bond":
            out.bonds_rub += value
        elif itype == "share":
            out.dividend_shares_rub += value
        else:
            out.other_rub += value

    out.total_rub = (out.free_rub + out.money_market_funds_rub + out.bonds_rub
                     + out.dividend_shares_rub + out.other_rub)
    return out


def holdings_map(client, account_id: str | None) -> dict:
    """Read-only карта позиций по figi / instrument_uid / ticker+class_code.

    {"ok": bool, "by_figi": {...}, "by_uid": {...}, "by_ticker_class": {...}}.
    ok=False означает, что портфель прочитать не удалось (held_unknown).
    """
    out: dict = {"ok": False, "by_figi": {}, "by_uid": {}, "by_ticker_class": {}}
    acc = _resolve_account_id(client, account_id)
    if not acc:
        return out
    try:
        pf = client.get_portfolio(acc)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"GetPortfolio недоступен (held_unknown): {exc}")
        return out
    out["ok"] = True
    for pos in pf.get("positions") or []:
        qty = quotation_to_decimal(pos.get("quantity")) or Decimal("0")
        rec = {
            "held": qty != 0,
            "position_quantity": qty,
            "position_value_rub": _pos_value(pos),
            "average_position_price": quotation_to_decimal(pos.get("averagePositionPrice")),
        }
        figi = str(pos.get("figi", ""))
        uid = str(pos.get("instrumentUid") or pos.get("instrument_uid") or "")
        ticker = str(pos.get("ticker", "")).upper()
        cls = str(pos.get("classCode") or pos.get("class_code") or "")
        if figi:
            out["by_figi"][figi] = rec
        if uid:
            out["by_uid"][uid] = rec
        if ticker:
            out["by_ticker_class"][f"{ticker}:{cls}"] = rec
    return out


def lookup_holding(holdings: dict, figi: str = "", uid: str = "",
                   ticker: str = "", class_code: str = "") -> dict | None:
    if figi and figi in holdings.get("by_figi", {}):
        return holdings["by_figi"][figi]
    if uid and uid in holdings.get("by_uid", {}):
        return holdings["by_uid"][uid]
    key = f"{ticker.upper()}:{class_code}"
    return holdings.get("by_ticker_class", {}).get(key)
