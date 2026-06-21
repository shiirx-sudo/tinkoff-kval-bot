"""
Тесты income_universe_disabled_audit — read-only классификация disabled-кандидатов.

Проверяем группы A/B/C/D/E, auto_enable_allowed=false, понятные ошибки на
missing/malformed builder-report и «чистоту» markdown (нет рекомендательных слов).
"""
from __future__ import annotations

import json

import pytest

from modules import income_universe_audit as audit


def _entry(ticker, *, class_code="TQBR", role=audit.ROLE_DIVIDEND,
           policy_bucket="", excluded_reason="", notes=""):
    return {
        "ticker": ticker,
        "class_code": class_code,
        "role": role,
        "policy_bucket": policy_bucket,
        "excluded_reason": excluded_reason,
        "notes": notes,
    }


def _group(entry) -> str:
    return audit.audit_row(entry)["audit_group"]


# ─── A. manual income → group A ───────────────────────────────────────────────

def test_sber_income_manual_group_a():
    e = _entry("SBER", role=audit.ROLE_DIVIDEND, policy_bucket="income_manual",
               notes="disabled: income_policy=income_manual; not base-eligible")
    row = audit.audit_row(e)
    assert row["audit_group"] == "A"
    assert row["audit_group_name"] == "manual_audit"
    assert row["auto_enable_allowed"] is False
    assert row["recommendation_guard"] == "candidate_for_analysis_only"


# ─── B. estimated income → group B ────────────────────────────────────────────

def test_nvtk_income_estimated_group_b():
    e = _entry("NVTK", role=audit.ROLE_DIVIDEND, policy_bucket="income_estimated",
               notes="disabled: income_policy=income_estimated; not base-eligible")
    row = audit.audit_row(e)
    assert row["audit_group"] == "B"
    assert row["audit_group_name"] == "policy_review"
    assert row["auto_enable_allowed"] is False


# ─── C. OFZ-PK pending coupon → group C ───────────────────────────────────────

def test_ofz_pk_pending_coupon_group_c():
    e = _entry("SU29009RMFS6", class_code="TQOB", role=audit.ROLE_OFZ,
               policy_bucket="income_reliable",
               notes="disabled: ofz_pk_candidate pending coupon/income validation")
    row = audit.audit_row(e)
    assert row["audit_group"] == "C"
    assert row["audit_group_name"] == "coupon_validation"
    assert row["auto_enable_allowed"] is False


def test_ofz_pk_income_unknown_still_group_c():
    # ofz_pk имеет приоритет coupon validation над income_unknown guard
    e = _entry("SU29024RMFS5", class_code="TQOB", role=audit.ROLE_OFZ,
               policy_bucket="income_unknown", excluded_reason="income_unknown")
    assert _group(e) == "C"


# ─── C2. bond_candidate с pending coupon → group C ────────────────────────────

def test_bond_candidate_pending_coupon_group_c():
    e = _entry("RU000A100ABC", class_code="TQCB", role=audit.ROLE_BOND,
               policy_bucket="income_reliable",
               notes="disabled: bond_candidate pending coupon/income validation")
    assert _group(e) == "C"


# ─── C3. non-coupon роли НЕ попадают в group C ────────────────────────────────

def test_money_market_pending_income_validation_not_group_c():
    # LQDT/SBMM: money_market с notes о coupon/income validation НЕ должен быть C —
    # это не облигация, купонного календаря у фонда нет.
    e = _entry("LQDT", class_code="TQTF", role="money_market",
               policy_bucket="income_variable",
               notes="disabled: money_market pending coupon/income validation; "
                     "income_policy=income_variable; source=manual_override")
    row = audit.audit_row(e)
    assert row["audit_group"] != "C"
    assert row["audit_group"] == "A"  # аудит источника дохода
    assert row["auto_enable_allowed"] is False


def test_money_market_income_unknown_not_group_c():
    e = _entry("SBMM", class_code="TQTF", role="money_market",
               policy_bucket="income_unknown", excluded_reason="income_unknown",
               notes="disabled: income_unknown")
    g = _group(e)
    assert g != "C"
    assert g == "E"  # явный income_unknown guard


def test_dividend_pending_income_validation_not_group_c():
    # VTBR/T: dividend_candidate с notes о coupon/income validation НЕ должен быть C.
    e = _entry("VTBR", class_code="TQBR", role=audit.ROLE_DIVIDEND,
               policy_bucket="income_reliable",
               notes="disabled: dividend_candidate pending coupon/income validation; "
                     "income_policy=income_reliable; source=api_known_future")
    row = audit.audit_row(e)
    assert row["audit_group"] != "C"
    assert row["audit_group"] == "A"
    assert row["auto_enable_allowed"] is False


def test_dividend_income_unknown_not_group_c():
    e = _entry("GMKN", role=audit.ROLE_DIVIDEND, policy_bucket="income_unknown",
               excluded_reason="income_unknown")
    g = _group(e)
    assert g != "C"
    assert g == "E"


def test_share_candidate_with_income_validation_not_group_c():
    e = _entry("XXXX", class_code="TQBR", role="share_candidate",
               notes="pending income validation")
    assert _group(e) != "C"


def test_non_coupon_excluded_from_group_c_summary():
    # Фикстура из реальных post-merge инструментов: LQDT/SBMM/VTBR/T не должны
    # попадать в group C; в C остаются только OFZ/bond-like кандидаты.
    report = {
        "disabled_entries": [
            _entry("LQDT", class_code="TQTF", role="money_market",
                   policy_bucket="income_variable",
                   notes="money_market pending coupon/income validation"),
            _entry("SBMM", class_code="TQTF", role="money_market",
                   policy_bucket="income_variable",
                   notes="money_market pending coupon/income validation"),
            _entry("VTBR", class_code="TQBR", role=audit.ROLE_DIVIDEND,
                   policy_bucket="income_reliable",
                   notes="dividend_candidate pending coupon/income validation"),
            _entry("T", class_code="TQBR", role=audit.ROLE_DIVIDEND,
                   policy_bucket="income_reliable",
                   notes="dividend_candidate pending coupon/income validation"),
            _entry("SU29009RMFS6", class_code="TQOB", role=audit.ROLE_OFZ,
                   policy_bucket="income_reliable",
                   notes="ofz_pk_candidate pending coupon/income validation"),
            _entry("SU29024RMFS5", class_code="TQOB", role=audit.ROLE_OFZ,
                   policy_bucket="income_unknown", excluded_reason="income_unknown"),
        ]
    }
    res = audit.build_audit(report)
    c_tickers = {r["ticker"] for r in res["candidates"] if r["audit_group"] == "C"}
    assert c_tickers == {"SU29009RMFS6", "SU29024RMFS5"}
    assert not ({"LQDT", "SBMM", "VTBR", "T"} & c_tickers)
    assert res["summary"]["group_counts"]["C"] == 2
    assert res["summary"]["auto_enable_allowed_count"] == 0
    assert all(r["auto_enable_allowed"] is False for r in res["candidates"])


# ─── D. quasi short-name unresolved → group D ─────────────────────────────────

def test_quasi_unresolved_group_d():
    e = _entry("ГазКЗ-37Д", class_code="", role="quasi_currency_bond_candidate",
               excluded_reason="unresolved",
               notes="disabled: class_code unresolved; source short-name, not a verified ticker")
    row = audit.audit_row(e)
    assert row["audit_group"] == "D"
    assert row["audit_group_name"] == "resolver_mapping"
    assert row["auto_enable_allowed"] is False


# ─── E. risk/policy guards → group E ──────────────────────────────────────────

def test_lkoh_trailing_yield_above_cap_group_e():
    e = _entry("LKOH", role=audit.ROLE_DIVIDEND, policy_bucket="income_excluded",
               excluded_reason="trailing_yield_above_cap")
    row = audit.audit_row(e)
    assert row["audit_group"] == "E"
    assert row["audit_group_name"] == "keep_disabled"
    assert row["auto_enable_allowed"] is False


def test_gazp_override_disable_group_e():
    e = _entry("GAZP", role=audit.ROLE_DIVIDEND, policy_bucket="income_unknown",
               excluded_reason="override_disable",
               notes="disabled: override; reason=state_control_risk")
    assert _group(e) == "E"


def test_gmkn_income_unknown_group_e():
    e = _entry("GMKN", role=audit.ROLE_DIVIDEND, policy_bucket="income_unknown",
               excluded_reason="income_unknown")
    assert _group(e) == "E"


# ─── summary / build ──────────────────────────────────────────────────────────

def test_build_audit_summary_counts():
    report = {
        "disabled_entries": [
            _entry("SBER", policy_bucket="income_manual"),
            _entry("NVTK", policy_bucket="income_estimated"),
            _entry("SU29009RMFS6", class_code="TQOB", role=audit.ROLE_OFZ,
                   policy_bucket="income_reliable",
                   notes="pending coupon/income validation"),
            _entry("ГазКЗ-37Д", class_code="", role="quasi_currency_bond_candidate",
                   excluded_reason="unresolved", notes="source short-name"),
            _entry("LKOH", policy_bucket="income_excluded",
                   excluded_reason="trailing_yield_above_cap"),
        ]
    }
    res = audit.build_audit(report)
    s = res["summary"]
    assert s["total_disabled"] == 5
    assert s["group_counts"] == {"A": 1, "B": 1, "C": 1, "D": 1, "E": 1}
    assert s["auto_enable_allowed_count"] == 0
    assert s["recommended_next_pr"] == audit.RECOMMENDED_NEXT_PR
    assert all(r["auto_enable_allowed"] is False for r in res["candidates"])


def test_extract_disabled_falls_back_to_entries():
    report = {
        "entries": [
            {"ticker": "VTBR", "enabled": True, "policy_bucket": "income_reliable"},
            _entry("SBER", policy_bucket="income_manual") | {"enabled": False},
        ]
    }
    disabled = audit.extract_disabled(report)
    assert [e["ticker"] for e in disabled] == ["SBER"]


# ─── H. missing builder-report → понятная ошибка ──────────────────────────────

def test_missing_builder_report_clear_error(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(audit.AuditError) as exc:
        audit.load_builder_report(str(missing))
    assert "build-income-universe" in str(exc.value)


# ─── I. malformed builder-report → понятная ошибка ────────────────────────────

def test_malformed_builder_report_clear_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    with pytest.raises(audit.AuditError):
        audit.load_builder_report(str(bad))


def test_non_object_builder_report_clear_error(tmp_path):
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(audit.AuditError):
        audit.load_builder_report(str(arr))


# ─── J. markdown «чистота» ────────────────────────────────────────────────────

def test_markdown_disclaimer_and_no_forbidden_words():
    report = {
        "disabled_entries": [
            _entry("SBER", policy_bucket="income_manual"),
            _entry("NVTK", policy_bucket="income_estimated"),
            _entry("SU29009RMFS6", class_code="TQOB", role=audit.ROLE_OFZ,
                   policy_bucket="income_reliable", notes="pending coupon"),
            _entry("ГазКЗ-37Д", class_code="", role="quasi_currency_bond_candidate",
                   excluded_reason="unresolved", notes="source short-name"),
            _entry("LKOH", policy_bucket="income_excluded",
                   excluded_reason="trailing_yield_above_cap"),
            _entry("GAZP", policy_bucket="income_unknown",
                   excluded_reason="override_disable"),
            _entry("GMKN", policy_bucket="income_unknown",
                   excluded_reason="income_unknown"),
        ]
    }
    md = audit.render_md(audit.build_audit(report))
    assert "Аналитика, не рекомендация" in md
    assert "Заявки не отправляются" in md
    low = md.lower()
    for forbidden in ("купить", "продать", "исключить", "buy", "sell"):
        assert forbidden not in low, f"forbidden word in markdown: {forbidden}"
    # все секции групп присутствуют
    for g in ("A", "B", "C", "D", "E"):
        assert f"## Group {g}" in md


# ─── end-to-end run_audit пишет файлы ─────────────────────────────────────────

def test_run_audit_writes_files(tmp_path):
    report = {
        "output_path": "data/config/income_universe.yaml",
        "generated_at_utc": "2026-06-20T07:27:56+00:00",
        "mode": "policy",
        "disabled_entries": [
            _entry("SBER", policy_bucket="income_manual"),
            _entry("ГазКЗ-37Д", class_code="", role="quasi_currency_bond_candidate",
                   excluded_reason="unresolved", notes="source short-name"),
        ],
    }
    src = tmp_path / "builder.json"
    src.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    out_json = tmp_path / "audit.json"
    out_md = tmp_path / "audit.md"
    res = audit.run_audit(builder_report_path=str(src),
                          output_json=str(out_json), output_md=str(out_md))
    assert out_json.exists() and out_md.exists()
    loaded = json.loads(out_json.read_text(encoding="utf-8"))
    assert loaded["summary"]["total_disabled"] == 2
    assert res["summary"]["group_counts"]["A"] == 1
    assert res["summary"]["group_counts"]["D"] == 1
