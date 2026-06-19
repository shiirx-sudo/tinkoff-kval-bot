"""
Тесты income_universe_v1 — read-only конфиг вселенной доходных инструментов.
Только чтение YAML; никаких заявок.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modules.income_universe import (
    list_universe_profiles,
    load_income_universe,
    resolve_watchlist,
    universe_watchlist,
)

EXAMPLE = "config/income_universe.example.yaml"

_USER_YAML = """
profiles:
  myprofile:
    description: "test"
    instruments:
      - ticker: LQDT
        class_code: TQTF
        role: money_market
        enabled: true
      - ticker: SBER
        class_code: TQBR
        enabled: true
      - ticker: GAZP
        class_code: TQBR
        enabled: false
      - ticker: NOCLASS
        enabled: true
"""


# ─── 1. загрузка примера ─────────────────────────────────────────────────────

def test_loads_example_universe():
    data = load_income_universe(EXAMPLE)
    assert "profiles" in data
    assert "base_income" in data["profiles"]
    assert "extended_income" in data["profiles"]
    assert set(list_universe_profiles(data)) >= {"base_income", "extended_income"}


# ─── 2. загрузка пользовательского пути ──────────────────────────────────────

def test_loads_user_universe_path(tmp_path):
    p = tmp_path / "income_universe.yaml"
    p.write_text(_USER_YAML, encoding="utf-8")
    data = load_income_universe(str(p))
    assert "myprofile" in data["profiles"]
    assert data["_source_path"] == str(p)


# ─── 3. enabled-инструменты в формате CLASS:TICKER ───────────────────────────

def test_universe_watchlist_base_income():
    data = load_income_universe(EXAMPLE)
    wl = universe_watchlist(data, "base_income")
    assert wl == ["TQTF:LQDT", "TQBR:SBER", "TQBR:VTBR", "TQBR:T"]


# ─── 4. disabled исключаются ─────────────────────────────────────────────────

def test_disabled_instruments_excluded(tmp_path):
    p = tmp_path / "income_universe.yaml"
    p.write_text(_USER_YAML, encoding="utf-8")
    data = load_income_universe(str(p))
    wl = universe_watchlist(data, "myprofile")
    assert "TQBR:GAZP" not in wl          # enabled: false
    assert "TQTF:LQDT" in wl and "TQBR:SBER" in wl


def test_missing_class_code_defaults_to_tqbr(tmp_path):
    p = tmp_path / "income_universe.yaml"
    p.write_text(_USER_YAML, encoding="utf-8")
    data = load_income_universe(str(p))
    wl = universe_watchlist(data, "myprofile")
    assert "TQBR:NOCLASS" in wl           # без class_code → TQBR


# ─── 5. отсутствующий профиль — понятная ошибка ──────────────────────────────

def test_missing_profile_raises():
    data = load_income_universe(EXAMPLE)
    with pytest.raises(ValueError) as exc:
        universe_watchlist(data, "no_such_profile")
    assert "no_such_profile" in str(exc.value)
    assert "base_income" in str(exc.value)   # перечислены доступные


# ─── 6. resolve_watchlist через профиль ──────────────────────────────────────

def test_resolve_watchlist_from_profile():
    items, meta = resolve_watchlist("", "base_income", EXAMPLE)
    assert items == ["TQTF:LQDT", "TQBR:SBER", "TQBR:VTBR", "TQBR:T"]
    assert meta["universe_profile"] == "base_income"
    assert meta["universe_watchlist_count"] == 4
    assert meta["universe_path"].endswith("income_universe.example.yaml")


# ─── 7. --watchlist приоритетнее профиля ─────────────────────────────────────

def test_explicit_watchlist_overrides_profile():
    items, meta = resolve_watchlist("TQBR:AAA, TQBR:BBB", "base_income", EXAMPLE)
    assert items == ["TQBR:AAA", "TQBR:BBB"]
    assert meta["universe_profile"] == ""     # профиль не использован
    assert meta["universe_watchlist_count"] == 2


def test_resolve_watchlist_empty_when_nothing():
    items, meta = resolve_watchlist("", None, None)
    assert items == []
    assert meta["universe_watchlist_count"] == 0


# ─── 8. safety scan ──────────────────────────────────────────────────────────

def test_no_order_endpoints_in_universe_sources():
    files = ["modules/income_universe.py", "reports/target_portfolio_reports.py"]
    forbidden = ("OrdersService", "postOrder", "cancelOrder", "place_order",
                 "submit_order", "place_limit_order", "order_client",
                 "LIVE_EXECUTION", "full_token")
    for f in files:
        src = Path(f).read_text(encoding="utf-8")
        for tok in forbidden:
            assert tok not in src, f"{f}: {tok}"
