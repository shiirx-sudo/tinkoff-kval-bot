"""
Read-only резолвер инструментов: figi / instrument_uid → тикер, имя, тип.

Использует InstrumentsService/GetInstrumentBy. Кэширует ответы в памяти на время
запуска. Ошибки резолва НЕ ломают расчёт — возвращается пустой результат + warning.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger

from brokers.tinkoff.rest_client import (
    INSTRUMENT_ID_TYPE_FIGI,
    INSTRUMENT_ID_TYPE_UID,
)


@dataclass(frozen=True)
class InstrumentInfo:
    """Разрешённые атрибуты инструмента."""
    ticker: str = ""
    name: str = ""
    instrument_type: str = ""
    class_code: str = ""
    figi: str = ""
    instrument_uid: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.ticker or self.name)


class _InstrumentClient(Protocol):
    def get_instrument_by(self, id_type: str, id_value: str) -> dict[str, Any]: ...


def _parse_instrument(payload: dict[str, Any]) -> InstrumentInfo:
    instr = (payload or {}).get("instrument") or {}
    return InstrumentInfo(
        ticker=str(instr.get("ticker") or ""),
        name=str(instr.get("name") or ""),
        instrument_type=str(instr.get("instrumentType") or ""),
        class_code=str(instr.get("classCode") or ""),
        figi=str(instr.get("figi") or ""),
        instrument_uid=str(instr.get("uid") or ""),
    )


class InstrumentResolver:
    """Резолвит инструменты по figi/uid с кэшем в памяти."""

    def __init__(self, client: _InstrumentClient) -> None:
        self._client = client
        self._cache: dict[tuple[str, str], InstrumentInfo] = {}

    def _lookup(self, id_type: str, id_value: str) -> InstrumentInfo | None:
        if not id_value:
            return None
        key = (id_type, id_value)
        if key in self._cache:
            return self._cache[key]
        try:
            payload = self._client.get_instrument_by(id_type, id_value)
            info = _parse_instrument(payload)
        except Exception as exc:  # noqa: BLE001 — резолв не должен ломать расчёт
            logger.warning(f"Не удалось резолвить инструмент {id_type}={id_value[:8]}…: {exc}")
            info = InstrumentInfo()
        self._cache[key] = info
        return info if info.resolved else None

    def resolve(self, figi: str = "", instrument_uid: str = "") -> InstrumentInfo:
        """
        Пытается резолвить сначала по figi, затем по uid. Всегда возвращает
        InstrumentInfo (на неудаче — пустой, но с известными figi/uid).
        """
        info = None
        if figi:
            info = self._lookup(INSTRUMENT_ID_TYPE_FIGI, figi)
        if info is None and instrument_uid:
            info = self._lookup(INSTRUMENT_ID_TYPE_UID, instrument_uid)
        if info is not None:
            return info
        # не разрешилось — отдаём то, что знаем
        return InstrumentInfo(figi=figi, instrument_uid=instrument_uid)
