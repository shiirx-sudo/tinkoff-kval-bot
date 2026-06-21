"""
Тесты floating_coupon_policy — read-only policy-диагностика floating-coupon
(ОФЗ-ПК) кандидатов из income-coupon-validation.

Проверяем: понятная ошибка на missing input; пустой вход → валидный пустой отчёт;
не-floating кандидаты игнорируются; floating ОФЗ-ПК → policy_required;
annualization_allowed / forecast_allowed / auto_enable_allowed всегда false;
markdown содержит guard-фразы и не содержит торговых рекомендаций;
CLI пишет json/md в переданные temp-пути. Сети нет.
"""
from __future__ import annotations

import json

import pytest

from modules import floating_coupon_policy as fcp


# ─── helpers ──────────────────────────────────────────────────────────────────

def _cand(ticker, *, class_code="TQOB", role="ofz_pk_candidate",
          coupon_type="floating", status=fcp.STATUS_FLOATING,
          income_readiness="needs_floating_coupon_policy",
          annualization_block_reason="floating coupon: будущий купон неизвестен"):
    return {
        "ticker": ticker,
        "class_code": class_code,
        "role": role,
        "coupon_type": coupon_type,
        "coupon_validation_status": status,
        "income_readiness": income_readiness,
        "annualization_block_reason": annualization_block_reason,
    }


def _validation_report(candidates):
    return {"kind": "income_coupon_validation", "candidates": candidates}


def _write_input(tmp_path, report):
    p = tmp_path / "income_coupon_validation.json"
    p.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return str(p)


# ─── A. missing input → понятная ошибка ───────────────────────────────────────

def test_missing_input_clear_error(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(fcp.FloatingCouponPolicyError) as exc:
        fcp.load_validation_report(str(missing))
    assert "income-coupon-validation" in str(exc.value)


def test_invalid_json_clear_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(fcp.FloatingCouponPolicyError):
        fcp.load_validation_report(str(p))


# ─── B. пустой вход → валидный пустой отчёт ────────────────────────────────────

def test_empty_input_valid_empty_report():
    report = fcp.build_report(_validation_report([]))
    s = report["summary"]
    assert report["candidates"] == []
    assert s["total_candidates"] == 0
    assert s["floating_coupon_candidates"] == 0
    assert s["annualization_allowed_count"] == 0
    assert s["forecast_allowed_count"] == 0
    assert s["auto_enable_allowed_count"] == 0
    assert s["by_policy_status"] == {}
    assert s["by_readiness"] == {}


def test_missing_candidates_key_is_safe():
    report = fcp.build_report({"kind": "income_coupon_validation"})
    assert report["candidates"] == []
    assert report["summary"]["total_candidates"] == 0


# ─── C. не-floating кандидаты игнорируются ─────────────────────────────────────

def test_non_floating_candidate_ignored():
    fixed = _cand("RU000FIXED01", class_code="TQCB", role="bond_candidate",
                  coupon_type="fixed", status="fixed_coupon_detected")
    mm = _cand("LQDT", class_code="TQTF", role="money_market",
               coupon_type="unknown", status="insufficient_data")
    report = fcp.build_report(_validation_report([fixed, mm]))
    assert report["candidates"] == []
    assert report["summary"]["total_candidates"] == 2
    assert report["summary"]["floating_coupon_candidates"] == 0


def test_floating_selected_among_mixed():
    rows_in = [
        _cand("SU29024RMFS5"),
        _cand("RU000FIXED01", class_code="TQCB", role="bond_candidate",
              coupon_type="fixed", status="fixed_coupon_detected"),
    ]
    report = fcp.build_report(_validation_report(rows_in))
    assert report["summary"]["total_candidates"] == 2
    assert report["summary"]["floating_coupon_candidates"] == 1
    assert [r["ticker"] for r in report["candidates"]] == ["SU29024RMFS5"]


def test_ofz_ticker_alone_marks_floating():
    # даже без coupon_type/статуса OFZ-ПК тикер достаточен
    cand = {"ticker": "SU29009RMFS6", "class_code": "TQOB"}
    assert fcp.is_floating_candidate(cand) is True


# ─── D. floating ОФЗ-ПК → policy_required, всё запрещено ───────────────────────

def test_floating_candidate_row_fields():
    report = fcp.build_report(_validation_report([_cand("SU29024RMFS5")]))
    r = report["candidates"][0]
    assert r["floating_coupon_detected"] is True
    assert r["annualization_allowed"] is False
    assert r["forecast_allowed"] is False
    assert r["auto_enable_allowed"] is False
    assert r["analysis_only"] is True
    assert r["forecast_method"] == "not_supported_yet"
    assert r["policy_status"] == "needs_floating_coupon_policy"
    assert r["readiness"] == "policy_required"
    assert r["recommendation_guard"] == "candidate_for_analysis_only"
    assert r["policy_requirements"] == list(fcp.POLICY_REQUIREMENTS)
    assert r["reason"]


def test_all_flags_always_false_for_many():
    cands = [_cand(f"SU2902{i}RMFS{i}") for i in range(5)]
    report = fcp.build_report(_validation_report(cands))
    assert report["summary"]["floating_coupon_candidates"] == 5
    assert all(r["annualization_allowed"] is False for r in report["candidates"])
    assert all(r["forecast_allowed"] is False for r in report["candidates"])
    assert all(r["auto_enable_allowed"] is False for r in report["candidates"])
    assert report["summary"]["annualization_allowed_count"] == 0
    assert report["summary"]["forecast_allowed_count"] == 0
    assert report["summary"]["auto_enable_allowed_count"] == 0
    assert report["summary"]["by_policy_status"] == {
        "needs_floating_coupon_policy": 5}
    assert report["summary"]["by_readiness"] == {"policy_required": 5}


# ─── E. markdown: guard-фразы и отсутствие торговых рекомендаций ───────────────

def test_markdown_contains_guard_phrases():
    report = fcp.build_report(_validation_report([_cand("SU29024RMFS5")]))
    md = fcp.render_md(report)
    for phrase in (
        "Аналитика, не рекомендация.",
        "Заявки не отправляются.",
        "auto_enable_allowed=false",
        "forecast_allowed=false",
        "annualization_allowed=false",
    ):
        assert phrase in md
    # явные содержательные утверждения
    assert "ОФЗ-ПК" in md
    assert "candidate_for_analysis_only" in md


def test_markdown_has_no_trading_recommendation_words():
    report = fcp.build_report(_validation_report(
        [_cand("SU29024RMFS5"), _cand("SU29009RMFS6")]))
    md = fcp.render_md(report)
    for forbidden in ("купить", "продать", "исключить", "buy", "sell",
                      "recommendation"):
        assert forbidden not in md


def test_empty_markdown_is_valid_and_safe():
    md = fcp.render_md(fcp.build_report(_validation_report([])))
    assert "Аналитика, не рекомендация." in md
    assert "_(нет floating-кандидатов)_" in md
    for forbidden in ("купить", "продать", "исключить", "buy", "sell",
                      "recommendation"):
        assert forbidden not in md


# ─── F. модуль не содержит order/execution implementation ──────────────────────

def test_module_has_no_order_execution_code():
    from pathlib import Path
    src = Path(fcp.__file__).read_text(encoding="utf-8")
    for forbidden in ("postOrder", "cancelOrder", "OrdersService", "place_order",
                      "submit_order", "place_limit_order", "order_client",
                      "get_portfolio", "get_positions"):
        assert forbidden not in src


# ─── G. CLI/run: пишет json+md в переданные temp-пути ─────────────────────────

def test_run_writes_json_and_md(tmp_path):
    inp = _write_input(tmp_path, _validation_report(
        [_cand("SU29024RMFS5"), _cand("SU29009RMFS6")]))
    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    report = fcp.run(input_json=inp, output_json=str(out_json),
                     output_md=str(out_md))
    assert out_json.exists()
    assert out_md.exists()
    assert report["_output_json"] == str(out_json)
    assert report["_output_md"] == str(out_md)

    on_disk = json.loads(out_json.read_text(encoding="utf-8"))
    s = on_disk["summary"]
    assert s["total_candidates"] == 2
    assert s["floating_coupon_candidates"] == 2
    assert s["annualization_allowed_count"] == 0
    assert s["forecast_allowed_count"] == 0
    assert s["auto_enable_allowed_count"] == 0
    tickers = {r["ticker"] for r in on_disk["candidates"]}
    assert all(t.startswith("SU29") for t in tickers)


def test_run_missing_input_raises(tmp_path):
    with pytest.raises(fcp.FloatingCouponPolicyError):
        fcp.run(input_json=str(tmp_path / "missing.json"),
                output_json=str(tmp_path / "o.json"),
                output_md=str(tmp_path / "o.md"))


def test_cli_writes_to_provided_paths(tmp_path):
    import main as cli
    inp = _write_input(tmp_path, _validation_report([_cand("SU29024RMFS5")]))
    out_json = tmp_path / "cli_out.json"
    out_md = tmp_path / "cli_out.md"
    rc = cli.main([
        "income-floating-coupon-policy",
        "--input-json", inp,
        "--output-json", str(out_json),
        "--output-md", str(out_md),
    ])
    assert rc == 0
    assert out_json.exists()
    assert out_md.exists()


def test_cli_missing_input_returns_1(tmp_path):
    import main as cli
    rc = cli.main([
        "income-floating-coupon-policy",
        "--input-json", str(tmp_path / "missing.json"),
        "--output-json", str(tmp_path / "o.json"),
        "--output-md", str(tmp_path / "o.md"),
    ])
    assert rc == 1
