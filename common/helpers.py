"""
Общие утилиты. Идеи перенесены из MOEX Advisor
(brokers/_shared.py, brokers/alfa/read_only.py), но без зависимости от pandas.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def utc_now() -> str:
    """ISO-8601 текущего момента в UTC, секундная точность."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y", "да", "истина"}


def clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return default
    return text


def mask_identifier(value: Any) -> str:
    """Маскирует идентификатор счёта: '***1234' (последние 4 символа)."""
    text = clean_text(value)
    if not text:
        return ""
    if len(text) <= 4:
        return "***"
    return "***" + text[-4:]


def stable_hash(value: str, size: int = 12) -> str:
    """Стабильный sha1-хэш фиксированной длины (для txn_id, position_id, dedup)."""
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:size]


def as_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Терпимое преобразование в Decimal (поддерживает запятую и пробелы)."""
    if value is None:
        return default
    try:
        text = str(value).strip().replace(" ", "").replace(",", ".")
        if text == "":
            return default
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return default


def quotation_to_decimal(value: dict[str, Any] | None) -> Decimal:
    """
    Tinkoff Quotation / MoneyValue {units, nano} → Decimal.

    units и nano могут приходить строками (REST JSON). Терпимо к None/{}.
    """
    if not value:
        return Decimal("0")
    units_raw = value.get("units")
    nano_raw = value.get("nano")
    try:
        units = int(units_raw) if units_raw not in (None, "") else 0
    except (TypeError, ValueError):
        units = 0
    try:
        nano = int(nano_raw) if nano_raw not in (None, "") else 0
    except (TypeError, ValueError):
        nano = 0
    return Decimal(units) + Decimal(nano) / Decimal("1000000000")


def money_currency(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    return clean_text(value.get("currency", "")).lower()
