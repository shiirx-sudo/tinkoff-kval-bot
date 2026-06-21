"""
Тесты income_coupon_validation — read-only coupon-validation диагностика
disabled-кандидатов audit group C.

Проверяем: понятные ошибки на missing input; выбор только группы C;
auto_enable_allowed=false и recommendation_guard у всех; floating блокирует
annualization; missing coupon data → coupon_data_missing/insufficient_data;
fixed с достаточными данными → annualization только при всех пройденных guard;
markdown содержит guard-фразы и не содержит торговых рекомендаций;
CLI 0 на валидных отчётах и 1 на отсутствующих. Сети нет.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from modules import income_coupon_validation as cv


# ─── helpers ──────────────────────────────────────────────────────────────────

def _cand(ticker, *, class_code="TQOB", role=cv.ROLE_OFZ, audit_group="C",
          policy_bucket="income_unknown", excluded_reason="", notes=""):
    return {
        "ticker": ticker,
        "class_code": class_code,
        "role": role,
        "policy_bucket": policy_bucket,
        "excluded_reason": excluded_reason,
        "notes": notes,
        "audit_group": audit_group,
        "audit_group_name": "coupon_validation" if audit_group == "C" else "other",
    }


def _audit_report(candidates):
    return {"kind": "income_universe_disabled_audit", "candidates": candidates}


def _write(tmp_path, builder=None, audit=None):
    b = tmp_path / "builder.json"
    a = tmp_path / "audit.json"
    b.write_text(json.dumps(builder if builder is not None
                            else {"disabled_entries": []}, ensure_ascii=False),
                 encoding="utf-8")
    a.write_text(json.dumps(audit if audit is not None
                            else _audit_report([]), ensure_ascii=False),
                 encoding="utf-8")
    return str(b), str(a)


# ─── A. missing input → понятная ошибка ───────────────────────────────────────

def test_missing_builder_report_clear_error(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(cv.CouponValidationError) as exc:
        cv.load_builder_report(str(missing))
    assert "build-income-universe" in str(exc.value)


def test_missing_audit_report_clear_error(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(cv.CouponValidationError) as exc:
        cv.load_audit_report(str(missing))
    assert "income-universe-audit" in str(exc.value)


def test_malformed_report_clear_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(cv.CouponValidationError):
        cv.load_builder_report(str(bad))


# ─── B. выбор только группы C ─────────────────────────────────────────────────

def test_select_only_group_c():
    report = _audit_report([
        _cand("SU29009RMFS6", audit_group="C"),
        {"ticker": "SBER", "audit_group": "A", "audit_group_name": "manual_audit"},
        {"ticker": "NVTK", "audit_group": "B", "audit_group_name": "policy_review"},
        {"ticker": "GAZKZ", "audit_group": "D", "audit_group_name": "resolver_mapping"},
        {"ticker": "LKOH", "audit_group": "E", "audit_group_name": "keep_disabled"},
    ])
    rows = cv.select_group_c(report)
    assert [r["ticker"] for r in rows] == ["SU29009RMFS6"]


def test_select_group_c_by_name_when_group_field_lowercase():
    report = _audit_report([
        {"ticker": "X", "audit_group": "c", "audit_group_name": "coupon_validation"},
        {"ticker": "Y", "audit_group_name": "coupon_validation"},
        {"ticker": "Z", "audit_group": "A", "audit_group_name": "manual_audit"},
    ])
    rows = cv.select_group_c(report)
    assert sorted(r["ticker"] for r in rows) == ["X", "Y"]


# ─── C. auto_enable_allowed=false / guard у всех ───────────────────────────────

def test_every_candidate_blocks_auto_enable():
    group = [
        _cand("SU29009RMFS6", role=cv.ROLE_OFZ),
        _cand("RU000ABC", role=cv.ROLE_BOND, class_code="TQCB"),
        _cand("LQDT", role=cv.ROLE_MONEY_MARKET, class_code="TQTF"),
        _cand("VTBR", role=cv.ROLE_DIVIDEND, class_code="TQBR"),
        _cand("ShortName", role=cv.ROLE_BOND, class_code="",
              excluded_reason="unresolved", notes="source short-name"),
    ]
    report = cv.build_report(group)
    assert report["summary"]["auto_enable_allowed_count"] == 0
    for r in report["candidates"]:
        assert r["auto_enable_allowed"] is False
        assert r["recommendation_guard"] == cv.RECOMMENDATION_GUARD
        assert r["required_next_step"]


# ─── D. floating блокирует annualization ──────────────────────────────────────

def test_floating_ofz_blocks_annualization():
    row = cv.build_candidate_row(_cand("SU29009RMFS6", role=cv.ROLE_OFZ))
    assert row["coupon_type"] == cv.COUPON_FLOATING
    assert row["coupon_validation_status"] == cv.STATUS_FLOATING
    assert row["income_readiness"] == cv.READY_NEEDS_FLOATING_POLICY
    assert row["annualization_allowed"] is False
    assert "floating" in row["annualization_block_reason"].lower()
    assert row["estimated_gross_yield_pct"] is None


def test_floating_detected_from_coupon_events():
    events = [{"couponDate": "2026-09-01T00:00:00Z",
               "couponType": "COUPON_TYPE_OFZ_PK",
               "payOneBond": {"units": "30", "nano": 0}}]
    t = cv.classify_coupon_type(role=cv.ROLE_BOND, ticker="RU000X",
                                coupon_events=events)
    assert t == cv.COUPON_FLOATING


# ─── E. missing coupon data → coupon_data_missing / insufficient ──────────────

def test_bond_without_schedule_offline_is_data_missing():
    row = cv.build_candidate_row(
        _cand("RU000ABC", role=cv.ROLE_BOND, class_code="TQCB"))
    assert row["coupon_type"] == cv.COUPON_FIXED
    assert row["coupon_validation_status"] == cv.STATUS_DATA_MISSING
    assert row["income_readiness"] == cv.READY_DATA_MISSING
    assert row["annualization_allowed"] is False


def test_money_market_in_group_c_is_insufficient():
    row = cv.build_candidate_row(
        _cand("LQDT", role=cv.ROLE_MONEY_MARKET, class_code="TQTF"))
    assert row["coupon_validation_status"] == cv.STATUS_INSUFFICIENT
    assert row["annualization_allowed"] is False


def test_unresolved_instrument_status():
    row = cv.build_candidate_row(
        _cand("ShortName", role=cv.ROLE_BOND, class_code="",
              excluded_reason="unresolved", notes="source short-name"))
    assert row["coupon_validation_status"] == cv.STATUS_UNRESOLVED
    assert row["income_readiness"] == cv.READY_NEEDS_MANUAL_REVIEW


# ─── F. fixed с достаточными данными → annualization только при guard ─────────

class _FakeClient:
    """Минимальный read-only фейк T-Invest для fixed-bond с полными данными."""

    def __init__(self, *, floating=False, freq=2, with_price=True,
                 with_schedule=True):
        self._floating = floating
        self._freq = freq
        self._with_price = with_price
        self._with_schedule = with_schedule

    def find_instrument(self, ticker, class_code):
        return {
            "figi": "BBG00FIXED01",
            "uid": "uid-fixed-01",
            "isin": "RU000FIXED01",
            "name": "Test fixed bond",
            "nominal": {"units": "1000", "nano": 0},
            "couponQuantityPerYear": self._freq,
            "floatingCouponFlag": self._floating,
        }

    def get_bond_coupons(self, instrument_id, frm, to):
        if not self._with_schedule:
            return []
        ctype = "COUPON_TYPE_FLOATING" if self._floating else "COUPON_TYPE_FIXED"
        return [{"couponDate": "2099-01-01T00:00:00Z", "couponType": ctype,
                 "payOneBond": {"units": "40", "nano": 0}, "couponNumber": "5"}]

    def get_last_price(self, instrument_id):
        if not self._with_price:
            return None
        return {"price": {"units": "1000", "nano": 0}}


def test_fixed_full_data_allows_annualization_diagnostic():
    row = cv.build_candidate_row(
        _cand("FIXBOND", role=cv.ROLE_BOND, class_code="TQCB"),
        client=_FakeClient(floating=False, freq=2))
    assert row["coupon_type"] == cv.COUPON_FIXED
    assert row["coupon_validation_status"] == cv.STATUS_FIXED
    assert row["annualization_allowed"] is True
    # 40 * 2 / 1000 * 100 = 8.0 (диагностический gross yield)
    assert row["estimated_gross_yield_pct"] == Decimal("8.0000")
    assert row["income_readiness"] == cv.READY_FUTURE_POLICY_REVIEW
    assert row["auto_enable_allowed"] is False


def test_fixed_missing_price_blocks_annualization():
    row = cv.build_candidate_row(
        _cand("FIXBOND", role=cv.ROLE_BOND, class_code="TQCB"),
        client=_FakeClient(floating=False, freq=2, with_price=False))
    assert row["annualization_allowed"] is False
    assert row["estimated_gross_yield_pct"] is None
    assert row["income_readiness"] == cv.READY_NEEDS_ANNUALIZATION_GUARD


def test_api_floating_blocks_even_with_full_data():
    row = cv.build_candidate_row(
        _cand("SU29009RMFS6", role=cv.ROLE_OFZ, class_code="TQOB"),
        client=_FakeClient(floating=True, freq=2))
    assert row["coupon_type"] == cv.COUPON_FLOATING
    assert row["annualization_allowed"] is False


def test_annualization_guard_pure_blocks_on_each_gap():
    base = dict(coupon_type=cv.COUPON_FIXED, next_coupon_value=Decimal("40"),
                nominal=Decimal("1000"), price=Decimal("1000"),
                coupon_freq_per_year=2, schedule_available=True)
    ok, reason, yld = cv.annualization_guard(**base)
    assert ok is True and yld == Decimal("8.0000") and reason == ""
    # каждый пробел блокирует
    for override in (
        {"coupon_type": cv.COUPON_FLOATING},
        {"next_coupon_value": None},
        {"nominal": None},
        {"price": None},
        {"coupon_freq_per_year": None},
        {"schedule_available": False},
    ):
        ok2, reason2, yld2 = cv.annualization_guard(**{**base, **override})
        assert ok2 is False and yld2 is None and reason2


# ─── G. markdown guard-фразы и отсутствие рекомендаций ─────────────────────────

def test_markdown_has_guard_phrases_and_no_recommendation_words():
    group = [
        _cand("SU29009RMFS6", role=cv.ROLE_OFZ),
        _cand("RU000ABC", role=cv.ROLE_BOND, class_code="TQCB"),
        _cand("LQDT", role=cv.ROLE_MONEY_MARKET, class_code="TQTF"),
    ]
    md = cv.render_md(cv.build_report(group))
    assert "Аналитика, не рекомендация" in md
    assert "Заявки не отправляются" in md
    assert "auto_enable_allowed=false" in md
    low = md.lower()
    for forbidden in ("купить", "продать", "исключить", "recommendation"):
        assert forbidden not in low, f"forbidden word in markdown: {forbidden}"
    # отдельно: buy/sell как самостоятельные слова (не часть служебных строк)
    assert "buy" not in low
    assert "sell" not in low


# ─── H. summary counts ────────────────────────────────────────────────────────

def test_summary_counts():
    group = [
        _cand("SU29009RMFS6", role=cv.ROLE_OFZ),       # floating
        _cand("SU29010RMFS4", role=cv.ROLE_OFZ),       # floating
        _cand("RU000ABC", role=cv.ROLE_BOND, class_code="TQCB"),  # fixed/missing
        _cand("LQDT", role=cv.ROLE_MONEY_MARKET, class_code="TQTF"),  # insufficient
    ]
    s = cv.build_report(group)["summary"]
    assert s["total_candidates"] == 4
    assert s["floating_coupon_count"] == 2
    assert s["fixed_coupon_count"] == 1
    assert s["auto_enable_allowed_count"] == 0
    assert s["annualization_allowed_count"] == 0
    assert s["recommended_next_pr"] == cv.RECOMMENDED_NEXT_PR


# ─── I. end-to-end run пишет файлы ────────────────────────────────────────────

def test_run_writes_files_offline(tmp_path):
    audit = _audit_report([
        _cand("SU29009RMFS6", role=cv.ROLE_OFZ),
        {"ticker": "SBER", "audit_group": "A", "audit_group_name": "manual_audit"},
    ])
    b, a = _write(tmp_path, builder={"disabled_entries": []}, audit=audit)
    out_json = tmp_path / "cv.json"
    out_md = tmp_path / "cv.md"
    cv.run(builder_report_path=b, audit_report_path=a,
           output_json=str(out_json), output_md=str(out_md), offline=True)
    assert out_json.exists() and out_md.exists()
    loaded = json.loads(out_json.read_text(encoding="utf-8"))
    assert loaded["summary"]["total_candidates"] == 1  # только группа C
    assert loaded["mode"] == "offline"
    assert all(r["auto_enable_allowed"] is False for r in loaded["candidates"])


# ─── J. CLI handler коды возврата ─────────────────────────────────────────────

def test_cli_returns_zero_on_valid_reports(tmp_path):
    import argparse

    from main import cmd_income_coupon_validation
    audit = _audit_report([_cand("SU29009RMFS6", role=cv.ROLE_OFZ)])
    b, a = _write(tmp_path, builder={"disabled_entries": []}, audit=audit)
    args = argparse.Namespace(
        builder_report=b, audit_report=a,
        output_json=str(tmp_path / "cv.json"),
        output_md=str(tmp_path / "cv.md"), offline=True)
    assert cmd_income_coupon_validation(args) == 0


def test_cli_returns_one_on_missing_reports(tmp_path):
    import argparse

    from main import cmd_income_coupon_validation
    args = argparse.Namespace(
        builder_report=str(tmp_path / "nope_b.json"),
        audit_report=str(tmp_path / "nope_a.json"),
        output_json=str(tmp_path / "cv.json"),
        output_md=str(tmp_path / "cv.md"), offline=True)
    assert cmd_income_coupon_validation(args) == 1
