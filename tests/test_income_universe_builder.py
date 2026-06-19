"""
Тесты income_universe_builder_v1 — read-only генератор income universe.
Сеть мокается через watchlist_fn; никаких заявок/мутаций портфеля.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from modules import income_universe_builder as B
from modules.income_universe import universe_watchlist

RULES = {
    "version": 1,
    "_source_path": "test-rules",
    "profiles": {
        "base_income": {"max_items": 20},
        "extended_income": {"max_items": 100},
    },
    "filters": {
        "money_market": {"enabled": True, "role": "money_market",
                         "candidate_tickers": ["LQDT", "SBMM"],
                         "allowed_class_codes": ["TQTF"]},
        "shares": {"enabled": True, "role": "dividend_candidate",
                   "candidate_tickers": ["VTBR", "NVTK", "GAZP", "NOCLS"],
                   "allowed_class_codes": ["TQBR"]},
        "ofz_pk": {"enabled": True, "role": "ofz_pk_candidate",
                   "allowed_class_codes": ["TQOB"],
                   "candidate_secids": ["SU29024RMFS5"]},
        "quasi_currency_bonds": {"enabled": True, "role": "quasi_currency_bond_candidate",
                                 "candidate_names": ["ГазКЗ-37Д"]},
    },
    "overrides": {
        "disable": [{"ticker": "GAZP", "class_code": "TQBR", "reason": "state risk"}],
    },
    "manual_notes": {"default_warning": "candidate_for_analysis_only; not a recommendation"},
}


@dataclass
class FakeItem:
    ticker: str
    class_code: str = ""
    figi: str = "F"
    instrument_uid: str = "U"
    source_type: str = "dividend"
    policy_bucket: str = "income_reliable"
    policy_reasons: list = field(default_factory=list)
    conservative_yield_pct: Decimal | None = Decimal("10")
    net_yield_pct: Decimal | None = Decimal("9")
    income_data_source: str = "api_known_future"
    risk_notes: list = field(default_factory=list)
    fundamental_verdict: str = "quality_pass"
    income_verdict: str = "income_candidate"


# карта поведения по тикеру для фейкового watchlist_fn
_BEHAVIOR = {
    "LQDT": dict(source_type="money_market", class_code="TQTF",
                 policy_bucket="income_variable", income_data_source="manual_override"),
    "SBMM": dict(source_type="money_market", class_code="TQTF",
                 policy_bucket="income_variable", income_data_source="trailing_30d"),
    "VTBR": dict(class_code="TQBR", policy_bucket="income_reliable"),
    "NVTK": dict(class_code="TQBR", policy_bucket="income_estimated",
                 income_data_source="api_trailing_12m", conservative_yield_pct=None),
    "GAZP": dict(class_code="TQBR", policy_bucket="income_unknown", figi="", instrument_uid="",
                 conservative_yield_pct=None, income_data_source="unknown"),
    "NOCLS": dict(class_code="", figi="", instrument_uid="", policy_bucket="income_unknown",
                  conservative_yield_pct=None, income_data_source="unknown"),
    # OFZ with a readable coupon schedule classifies as income_reliable, but the
    # builder must still keep bond/ofz roles disabled (coupon-calendar validation).
    "SU29024RMFS5": dict(class_code="TQOB", source_type="coupon",
                         policy_bucket="income_reliable",
                         income_data_source="api_coupon_schedule"),
}


def _fake_watchlist_fn(client, raw_items, config, env, fdata, priority=None, policy_env=None):
    out = []
    for raw in raw_items:
        ticker = raw.split(":")[-1].upper()
        beh = _BEHAVIOR.get(ticker, {})
        out.append(FakeItem(ticker=ticker, **beh))
    return out


def _build(mode="policy", include_disabled=True):
    return B.build_universe(rules=RULES, mode=mode, include_disabled=include_disabled,
                            output="out.yaml", dry_run=True, watchlist_fn=_fake_watchlist_fn)


# ─── seeds ────────────────────────────────────────────────────────────────────

def test_gather_seeds_roles_and_dedup():
    seeds = B.gather_seeds(RULES)
    by = {s.ticker: s for s in seeds}
    assert by["LQDT"].role == "money_market" and by["LQDT"].class_code == "TQTF"
    assert by["VTBR"].role == "dividend_candidate" and by["VTBR"].class_code == "TQBR"
    assert by["SU29024RMFS5"].role == "ofz_pk_candidate" and by["SU29024RMFS5"].class_code == "TQOB"
    quasi = [s for s in seeds if s.role == "quasi_currency_bond_candidate"]
    assert quasi and all(s.resolvable is False for s in quasi)   # quasi source names


def test_max_bonds_caps_bond_seeds():
    rules = {**RULES, "filters": {**RULES["filters"],
             "ofz_pk": {"enabled": True, "role": "ofz_pk_candidate",
                        "allowed_class_codes": ["TQOB"],
                        "candidate_secids": ["A", "B", "C", "D"]}}}
    seeds = B.gather_seeds(rules, max_bonds=2)
    ofz = [s for s in seeds if s.role == "ofz_pk_candidate"]
    assert len(ofz) == 2


# ─── disabled mode ────────────────────────────────────────────────────────────

def test_disabled_mode_all_false():
    res = _build(mode="disabled")
    for e in res.entries:
        assert e.enabled is False


# ─── policy mode enables only eligible ───────────────────────────────────────

def test_policy_mode_enables_only_eligible():
    res = _build(mode="policy")
    by = {e.ticker: e for e in res.entries}
    assert by["LQDT"].enabled is True          # income_variable
    assert by["SBMM"].enabled is True          # income_variable
    assert by["VTBR"].enabled is True          # income_reliable
    assert by["NVTK"].enabled is False         # income_estimated
    assert by["GAZP"].enabled is False         # override disable + unknown
    assert "override" in by["GAZP"].notes
    # OFZ-PK eligible by policy but role-gated → still disabled pending validation
    assert by["SU29024RMFS5"].enabled is False
    assert "pending coupon/income validation" in by["SU29024RMFS5"].notes


def test_conservative_mode_only_money_market():
    res = _build(mode="conservative")
    by = {e.ticker: e for e in res.entries}
    assert by["LQDT"].enabled is True and by["SBMM"].enabled is True
    assert by["VTBR"].enabled is False         # reliable dividend, but not MM


# ─── unresolved / missing class handled safely ───────────────────────────────

def test_unresolved_disabled_with_reason():
    res = _build(mode="policy")
    by = {e.ticker: e for e in res.entries}
    assert by["NOCLS"].enabled is False
    assert by["NOCLS"].excluded_reason == "unresolved"
    assert "unresolved" in by["NOCLS"].notes
    # quasi source-name is unresolvable too
    quasi = [e for e in res.entries if e.role == "quasi_currency_bond_candidate"]
    assert quasi and all(e.enabled is False for e in quasi)


# ─── профили + дедуп ──────────────────────────────────────────────────────────

def test_profiles_assignment_and_base_clean():
    res = _build(mode="policy")
    base = {e.ticker for e in res.profiles["base_income"]}
    assert base == {"LQDT", "SBMM", "VTBR"}      # only base-eligible enabled
    assert "NVTK" not in base and "GAZP" not in base
    mm = {e.ticker for e in res.profiles["money_market"]}
    assert mm == {"LQDT", "SBMM"}
    ofz = {e.ticker for e in res.profiles["ofz_pk_candidates"]}
    assert "SU29024RMFS5" in ofz
    audit = {e.ticker for e in res.profiles["disabled_research_candidates"]}
    assert {"GAZP", "NOCLS"} <= audit


def test_include_disabled_false_drops_disabled():
    res = _build(mode="policy", include_disabled=False)
    for name, entries in res.profiles.items():
        for e in entries:
            assert e.enabled is True, f"{name}:{e.ticker}"
    assert res.profiles["disabled_research_candidates"] == []


# ─── рендер YAML совместим со схемой income_universe ──────────────────────────

def test_rendered_yaml_matches_schema_and_loads():
    res = _build(mode="policy")
    text = B.render_universe_yaml(res, mode="policy", rules_path="test-rules")
    assert text.startswith("# Auto-generated by build-income-universe.")
    data = yaml.safe_load(text)
    assert "profiles" in data
    # схема: только ticker/class_code/role/enabled/notes
    allowed = {"ticker", "class_code", "role", "enabled", "notes"}
    for prof in data["profiles"].values():
        assert set(prof.keys()) == {"description", "instruments"}
        for inst in prof["instruments"]:
            assert set(inst.keys()) == allowed
    # loader income_universe умеет развернуть профиль
    wl = universe_watchlist(data, "base_income")
    assert "TQTF:LQDT" in wl and "TQBR:VTBR" in wl


def test_report_fields():
    res = _build(mode="policy")
    rep = res.report
    for key in ("mode", "dry_run", "instruments_scanned", "included_by_profile",
                "disabled_by_reason", "unresolved", "policy_excluded_count",
                "unknown_income_count", "generated_profiles"):
        assert key in rep


def test_dry_run_writes_nothing(tmp_path):
    # build_universe сам ничего не пишет; запись делает только CLI вне dry-run
    out = tmp_path / "income_universe.generated.yaml"
    B.build_universe(rules=RULES, mode="policy", output=str(out), dry_run=True,
                     watchlist_fn=_fake_watchlist_fn)
    assert not out.exists()


# ─── safety scan ──────────────────────────────────────────────────────────────

def test_no_order_or_execution_imports():
    files = ["modules/income_universe_builder.py",
             "config/income_universe_rules.example.yaml"]
    forbidden = ("OrdersService", "postOrder", "cancelOrder", "place_order",
                 "submit_order", "place_limit_order", "order_client",
                 "LIVE_EXECUTION", "full_token")
    for f in files:
        src = Path(f).read_text(encoding="utf-8")
        for tok in forbidden:
            assert tok not in src, f"{f}: {tok}"


def test_builder_module_has_no_execution_imports():
    import inspect

    from modules import income_universe_builder as mod
    src = inspect.getsource(mod)
    for tok in ("postOrder", "cancelOrder", "place_order", "submit_order",
                "OrdersService", "order_client", "get_portfolio", "get_positions"):
        assert tok not in src, tok
