"""
Read-only фасад над REST-коннектором Tinkoff.

Только read-only REST-вызовы (по факту HTTP POST к Tinkoff Invest API — это
особенность gRPC-over-REST у Т-Инвестиций, а не запись). Токен берётся из
настроек (read-only). Методы записи (postOrder/cancelOrder) не реализованы.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from brokers.tinkoff.rest_client import TinkoffReadOnlyClient
from config.settings import settings

# Типы операций, которые запрашиваем у API (фильтрацию делаем и в operation_filter)
_FETCH_OPERATION_TYPES = [
    "OPERATION_TYPE_BUY",
    "OPERATION_TYPE_SELL",
    "OPERATION_TYPE_BUY_CARD",
]


def _to_iso(dt: datetime) -> str:
    """datetime → RFC3339 c 'Z' для UTC."""
    s = dt.isoformat()
    return s.replace("+00:00", "Z")


class ReadOnlyClient:
    """Фасад: брокерские счета + операции за период. Read-only."""

    def __init__(self, rest: TinkoffReadOnlyClient | None = None) -> None:
        self._rest = rest or TinkoffReadOnlyClient(settings.read_token)

    def get_broker_accounts(self) -> list[dict[str, Any]]:
        return self._rest.get_broker_accounts()

    def get_all_accounts(self) -> list[dict[str, Any]]:
        """Все счета по токену (без фильтра по типу) — для команды accounts."""
        return self._rest.get_accounts()

    def instrument_resolver(self):
        """Резолвер инструментов, привязанный к этому REST-клиенту."""
        from brokers.tinkoff.instruments import InstrumentResolver
        return InstrumentResolver(self._rest)

    # ─── Рыночные данные (read-only) ────────────────────────────────────────

    def find_instrument(self, ticker: str, class_code: str) -> dict[str, Any] | None:
        """Инструмент по тикеру и class_code (или None при отсутствии)."""
        from brokers.tinkoff.rest_client import INSTRUMENT_ID_TYPE_TICKER
        resp = self._rest.get_instrument_by(
            INSTRUMENT_ID_TYPE_TICKER, ticker, class_code=class_code
        )
        return (resp or {}).get("instrument")

    def get_order_book(self, instrument_id: str, depth: int = 20) -> dict[str, Any]:
        return self._rest.get_order_book(instrument_id, depth)

    def get_last_price(self, instrument_id: str) -> dict[str, Any] | None:
        resp = self._rest.get_last_prices([instrument_id])
        prices = (resp or {}).get("lastPrices") or []
        return prices[0] if prices else None

    def get_trading_status(self, instrument_id: str) -> dict[str, Any]:
        return self._rest.get_trading_status(instrument_id)

    def get_candles(self, instrument_id: str, from_iso: str, to_iso: str,
                    interval: str = "CANDLE_INTERVAL_DAY") -> dict[str, Any]:
        return self._rest.get_candles(instrument_id, from_iso, to_iso, interval)

    def get_portfolio(self, account_id: str) -> dict[str, Any]:
        return self._rest.get_portfolio(account_id)

    def get_positions(self, account_id: str) -> dict[str, Any]:
        return self._rest.get_positions(account_id)

    def find_instruments(self, query: str) -> list[dict[str, Any]]:
        """FindInstrument: список инструментов-кандидатов по строке (или [])."""
        resp = self._rest.find_instruments(query)
        return (resp or {}).get("instruments") or []

    def get_instrument_by_figi(self, figi: str) -> dict[str, Any] | None:
        from brokers.tinkoff.rest_client import INSTRUMENT_ID_TYPE_FIGI
        resp = self._rest.get_instrument_by(INSTRUMENT_ID_TYPE_FIGI, figi)
        return (resp or {}).get("instrument")

    def get_instrument_by_uid(self, uid: str) -> dict[str, Any] | None:
        """GetInstrumentBy по instrument uid (read-only). None при отсутствии."""
        from brokers.tinkoff.rest_client import INSTRUMENT_ID_TYPE_UID
        resp = self._rest.get_instrument_by(INSTRUMENT_ID_TYPE_UID, uid)
        return (resp or {}).get("instrument")

    def instruments_catalog(self) -> list[dict[str, Any]]:
        """Объединённый каталог Etfs/Shares/Bonds/Currencies (read-only fallback)."""
        out: list[dict[str, Any]] = []
        for getter in (self._rest.get_etfs, self._rest.get_shares,
                       self._rest.get_bonds, self._rest.get_currencies):
            try:
                resp = getter()
                out.extend((resp or {}).get("instruments") or [])
            except Exception:  # noqa: BLE001 — каталог опционален
                continue
        return out

    # ─── Доходные данные (read-only) ────────────────────────────────────────

    def get_dividends(self, instrument_id: str, from_iso: str,
                      to_iso: str) -> list[dict[str, Any]]:
        """История/график дивидендов по акции (read-only). [] при ошибке."""
        try:
            resp = self._rest.get_dividends(instrument_id, from_iso, to_iso)
        except Exception as exc:  # noqa: BLE001 — доходные данные опциональны
            logger.warning(f"GetDividends({instrument_id}) недоступен: {exc}")
            return []
        return (resp or {}).get("dividends") or []

    def get_bond_coupons(self, instrument_id: str, from_iso: str,
                         to_iso: str) -> list[dict[str, Any]]:
        """График купонов облигации (read-only). [] при ошибке."""
        try:
            resp = self._rest.get_bond_coupons(instrument_id, from_iso, to_iso)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"GetBondCoupons({instrument_id}) недоступен: {exc}")
            return []
        return (resp or {}).get("events") or []

    def get_accrued_interests(self, instrument_id: str, from_iso: str,
                              to_iso: str) -> list[dict[str, Any]]:
        """Накопленный купонный доход (НКД), read-only. [] при ошибке."""
        try:
            resp = self._rest.get_accrued_interests(instrument_id, from_iso, to_iso)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"GetAccruedInterests({instrument_id}) недоступен: {exc}")
            return []
        return (resp or {}).get("accruedInterests") or []

    def get_asset_fundamentals(self, asset_uids: list[str]) -> list[dict[str, Any]]:
        """Фундаментальные показатели по assetUid (read-only). [] при ошибке."""
        try:
            resp = self._rest.get_asset_fundamentals(asset_uids)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"GetAssetFundamentals недоступен: {exc}")
            return []
        return (resp or {}).get("fundamentals") or []

    def get_asset_reports(self, instrument_id: str) -> list[dict[str, Any]]:
        """Календарь отчётностей эмитента (read-only). [] при ошибке."""
        try:
            resp = self._rest.get_asset_reports(instrument_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"GetAssetReports({instrument_id}) недоступен: {exc}")
            return []
        return (resp or {}).get("events") or []

    def get_operations(
        self,
        account_id: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[dict[str, Any]]:
        ops = self._rest.get_operations(
            account_id,
            _to_iso(from_dt),
            _to_iso(to_dt),
            operation_types=_FETCH_OPERATION_TYPES,
        )
        logger.info(f"Счёт {account_id}: {len(ops)} операций за {from_dt.date()} … {to_dt.date()}")
        return ops
