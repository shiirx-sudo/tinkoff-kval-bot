"""
income_universe_v1 — read-only конфиг вселенной доходных инструментов.

Позволяет не передавать длинный --watchlist руками: профиль (base_income /
extended_income / …) разворачивается в список CLASS:TICKER для target-portfolio
и income-watchlist. Это технический список для анализа, НЕ рекомендация и не
гарантия выплат. Никаких заявок, order-сервисов, full-токена, live-исполнения и
веб-скрапинга — только чтение YAML.
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

_DEFAULT_PATH = "data/config/income_universe.yaml"
_FALLBACK_EXAMPLE = "config/income_universe.example.yaml"
_DEFAULT_CLASS_CODE = "TQBR"


def load_income_universe(path: str | None = None) -> dict:
    """
    Грузит конфиг вселенной. Приоритет: явный path → data/config → example.
    Возвращает {} (без падения), если ничего не прочиталось.
    """
    for p in [x for x in (path, _DEFAULT_PATH, _FALLBACK_EXAMPLE) if x]:
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
            logger.warning(f"income_universe: не удалось прочитать {p}: {exc}")
    return {}


def list_universe_profiles(data: dict) -> list[str]:
    """Список доступных профилей вселенной (отсортирован)."""
    profiles = (data or {}).get("profiles") or {}
    return sorted(str(k) for k in profiles)


def universe_watchlist(data: dict, profile: str) -> list[str]:
    """
    Разворачивает профиль в watchlist CLASS:TICKER (только enabled: true).

    Инструмент без class_code получает TQBR по умолчанию (с warning в лог).
    Если профиль не найден — ValueError со списком доступных профилей.
    """
    profiles = (data or {}).get("profiles") or {}
    if profile not in profiles:
        available = ", ".join(list_universe_profiles(data)) or "—"
        raise ValueError(
            f"Профиль вселенной '{profile}' не найден. Доступные: {available}")

    out: list[str] = []
    seen: set[str] = set()
    for item in (profiles[profile] or {}).get("instruments") or []:
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", False):
            continue
        ticker = str(item.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        cls = str(item.get("class_code", "")).strip().upper()
        if not cls:
            cls = _DEFAULT_CLASS_CODE
            logger.warning(
                f"income_universe: {ticker} без class_code — использую {_DEFAULT_CLASS_CODE}")
        entry = f"{cls}:{ticker}"
        if entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
    return out


def resolve_watchlist(explicit_watchlist: str | None, profile: str | None,
                      path: str | None = None) -> tuple[list[str], dict]:
    """
    Единая точка для CLI: --watchlist приоритетнее профиля вселенной.

    Возвращает (raw_items, meta), где meta = {universe_profile, universe_path,
    universe_watchlist_count}. raw_items пуст, если ничего не задано.
    """
    meta = {"universe_profile": "", "universe_path": "", "universe_watchlist_count": 0}
    explicit = [t.strip() for t in (explicit_watchlist or "").split(",") if t.strip()]
    if explicit:
        meta["universe_watchlist_count"] = len(explicit)
        return explicit, meta
    if profile:
        data = load_income_universe(path)
        items = universe_watchlist(data, profile)
        meta["universe_profile"] = profile
        meta["universe_path"] = str(data.get("_source_path", "") or (path or ""))
        meta["universe_watchlist_count"] = len(items)
        return items, meta
    return [], meta
