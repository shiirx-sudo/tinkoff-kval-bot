"""
Тесты income_owner_decision_report — read-only owner-only decision support (F1).

Проверяем: graceful degrade на missing inputs (NEEDS_DATA / понятная ошибка);
group D unresolved → NEEDS_MAPPING; floating coupon → NEEDS_POLICY; resolved
income-ready кандидат → BUY_CANDIDATE; low-score → WAIT; hard excluded → BLOCKED;
missing income fields → NEEDS_DATA; guard-флаги жёстко зафиксированы для каждого
кандидата; summary order_send/auto_execution == 0; markdown содержит guard-фразы
и не содержит опасных торговых фраз; CLI пишет json/md в temp-пути. Сети нет.
"""
from __future__ import annotations

import json

import pytest

from modules import income_owner_decision_report as odr


# ─── helpers: входные отчёты ───────────────────────────────────────────────────

def _builder(enabled=None, disabled=None):
    return {
        "kind": "income_universe_builder_report",
        "generated_at_utc": "2026-06-22T00:00:00Z",
        "mode": "disabled",
        "enabled_entries": list(enabled or []),
        "disabled_entries": list(disabled or []),
    }


def _audit(candidates):
    return {"kind": "income_universe_disabled_audit", "candidates": list(candidates)}


def _coupon(candidates):
    return {"kind": "income_coupon_validation", "candidates": list(candidates)}


def _floating(candidates):
    return {"kind": "income_floating_coupon_policy", "candidates": list(candidates)}


def _resolver(candidates):
    return {"kind": "income_resolver_mapping_diagnostics", "candidates": list(candidates)}


def _target(eligible=None, allocation=None, current=None, excluded=None):
    return {
        "kind": "target_portfolio",
        "eligible_universe": list(eligible or []),
        "excluded_universe": list(excluded or []),
        "target_allocation": list(allocation or []),
        "current_vs_target": list(current or []),
    }


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _build(**kw):
    """Удобная обёртка build_report с дефолтами."""
    defaults = dict(builder=None, audit=None, coupon=None, floating=None,
                    resolver=None, target=None, inputs={}, missing_inputs=[],
                    mode="report_join")
    defaults.update(kw)
    return odr.build_report(**defaults)


# ─── A. missing / empty inputs ─────────────────────────────────────────────────

def test_run_all_inputs_missing_raises(tmp_path):
    with pytest.raises(odr.OwnerDecisionError) as exc:
        odr.run(
            universe_report=str(tmp_path / "nope1.json"),
            audit_json=str(tmp_path / "nope2.json"),
            coupon_json=str(tmp_path / "nope3.json"),
            floating_policy_json=str(tmp_path / "nope4.json"),
            resolver_json=str(tmp_path / "nope5.json"),
            target_json=str(tmp_path / "nope6.json"),
            output_json=str(tmp_path / "o.json"),
            output_md=str(tmp_path / "o.md"),
        )
    assert "smoke chain" in str(exc.value)


def test_missing_target_degrades_with_missing_inputs(tmp_path):
    builder = _write(tmp_path, "builder.json", _builder(
        enabled=[{"ticker": "LQDT", "class_code": "TQTF", "role": "money_market",
                  "policy_bucket": "income_reliable", "source": "api_trailing",
                  "enabled": True}]))
    result = odr.run(
        universe_report=builder,
        audit_json=str(tmp_path / "missing_audit.json"),
        coupon_json=str(tmp_path / "missing_coupon.json"),
        floating_policy_json=str(tmp_path / "missing_fl.json"),
        resolver_json=str(tmp_path / "missing_rs.json"),
        target_json=str(tmp_path / "missing_target.json"),
        output_json=str(tmp_path / "o.json"),
        output_md=str(tmp_path / "o.md"),
    )
    assert any("missing_target.json" in m for m in result["missing_inputs"])
    assert result["summary"]["total_candidates"] == 1


def test_empty_report_summary_is_zeroed():
    report = _build(builder=_builder())
    s = report["summary"]
    assert s["total_candidates"] == 0
    assert s["order_send_allowed_count"] == 0
    assert s["auto_execution_allowed_count"] == 0


# ─── B. group D unresolved → NEEDS_MAPPING ─────────────────────────────────────

def test_group_d_unresolved_needs_mapping():
    audit = _audit([{"ticker": "SHORTNAME", "class_code": "", "role": "bond_candidate",
                     "audit_group": "D", "excluded_reason": "unresolved",
                     "why_disabled": "short-name"}])
    resolver = _resolver([{"original_ticker": "SHORTNAME",
                           "mapping_status": "unresolved"}])
    report = _build(audit=audit, resolver=resolver)
    row = report["candidates"][0]
    assert row["proposed_action"] == odr.ACTION_NEEDS_MAPPING
    assert report["summary"]["needs_mapping_count"] == 1


def test_resolver_ambiguous_needs_mapping():
    resolver = _resolver([{"original_ticker": "AMBIG", "class_code": "",
                           "mapping_status": "ambiguous_matches"}])
    report = _build(resolver=resolver)
    assert report["candidates"][0]["proposed_action"] == odr.ACTION_NEEDS_MAPPING


# ─── C. floating coupon → NEEDS_POLICY ─────────────────────────────────────────

def test_floating_coupon_needs_policy():
    coupon = _coupon([{"ticker": "SU29024RMFS5", "class_code": "TQOB",
                       "role": "ofz_pk_candidate", "coupon_type": "floating",
                       "coupon_validation_status": "floating_coupon_detected"}])
    floating = _floating([{"ticker": "SU29024RMFS5", "class_code": "TQOB",
                           "role": "ofz_pk_candidate",
                           "policy_status": "needs_floating_coupon_policy",
                           "readiness": "policy_required"}])
    report = _build(coupon=coupon, floating=floating)
    row = report["candidates"][0]
    assert row["proposed_action"] == odr.ACTION_NEEDS_POLICY
    assert report["summary"]["needs_policy_count"] == 1


def test_estimated_bucket_needs_policy():
    builder = _builder(disabled=[{"ticker": "ABC", "class_code": "TQBR",
                                  "role": "dividend_candidate",
                                  "policy_bucket": "income_estimated"}])
    audit = _audit([{"ticker": "ABC", "class_code": "TQBR",
                     "role": "dividend_candidate", "audit_group": "B",
                     "policy_bucket": "income_estimated"}])
    report = _build(builder=builder, audit=audit)
    assert report["candidates"][0]["proposed_action"] == odr.ACTION_NEEDS_POLICY


# ─── D. resolved income-ready → BUY_CANDIDATE ──────────────────────────────────

def test_resolved_income_ready_buy_candidate():
    builder = _builder(enabled=[{"ticker": "LQDT", "class_code": "TQTF",
                                 "role": "money_market",
                                 "policy_bucket": "income_reliable",
                                 "source": "api_trailing", "enabled": True}])
    target = _target(
        eligible=[{"ticker": "LQDT", "policy_bucket": "income_reliable",
                   "conservative_net_yield_pct": 12.5}],
        allocation=[{"ticker": "LQDT", "target_weight_pct": 25,
                     "net_yield_pct": 12.5}],
        current=[{"ticker": "LQDT", "diff_value_rub": 50000,
                  "action_hint": "add"}])
    report = _build(builder=builder, target=target)
    row = report["candidates"][0]
    assert row["proposed_action"] == odr.ACTION_BUY_CANDIDATE
    assert row["score"] >= odr.BUY_SCORE_THRESHOLD
    assert report["summary"]["buy_candidate_count"] == 1
    # score_components прозрачны
    assert "income_data_present" in row["score_components"]
    assert "resolved_identity" in row["score_components"]


# ─── E. low score → WAIT ───────────────────────────────────────────────────────

def test_low_score_resolved_candidate_waits():
    # resolved, есть income-данные (variable bucket, source), но без надёжного
    # bucket / underweight / known income score не дотягивает до BUY → WAIT
    builder = _builder(enabled=[{"ticker": "XYZ", "class_code": "TQBR",
                                 "role": "share", "policy_bucket": "income_variable",
                                 "source": "", "enabled": True}])
    report = _build(builder=builder)
    row = report["candidates"][0]
    assert row["proposed_action"] == odr.ACTION_WAIT
    assert row["score"] < odr.BUY_SCORE_THRESHOLD


# ─── F. hard excluded → BLOCKED ────────────────────────────────────────────────

def test_hard_excluded_blocked():
    builder = _builder(disabled=[{"ticker": "GAZP", "class_code": "TQBR",
                                  "role": "dividend_candidate",
                                  "excluded_reason": "state_control_risk"}])
    audit = _audit([{"ticker": "GAZP", "class_code": "TQBR",
                     "role": "dividend_candidate", "audit_group": "E",
                     "excluded_reason": "state_control_risk",
                     "why_disabled": "risk/policy guard"}])
    report = _build(builder=builder, audit=audit)
    row = report["candidates"][0]
    assert row["proposed_action"] == odr.ACTION_BLOCKED
    assert report["summary"]["blocked_count"] == 1


def test_keep_disabled_group_e_blocked():
    audit = _audit([{"ticker": "OVR", "class_code": "TQBR", "role": "share",
                     "audit_group": "E", "excluded_reason": "override_disable"}])
    report = _build(audit=audit)
    assert report["candidates"][0]["proposed_action"] == odr.ACTION_BLOCKED


# ─── G. missing income fields → NEEDS_DATA ─────────────────────────────────────

def test_coupon_data_missing_needs_data():
    coupon = _coupon([{"ticker": "RU000FIX", "class_code": "TQCB",
                       "role": "bond_candidate", "coupon_type": "fixed",
                       "coupon_validation_status": "coupon_data_missing"}])
    audit = _audit([{"ticker": "RU000FIX", "class_code": "TQCB",
                     "role": "bond_candidate", "audit_group": "C"}])
    report = _build(coupon=coupon, audit=audit)
    assert report["candidates"][0]["proposed_action"] == odr.ACTION_NEEDS_DATA


# ─── H. жёсткие guard-флаги для каждого кандидата ──────────────────────────────

def _mixed_report():
    builder = _builder(
        enabled=[{"ticker": "LQDT", "class_code": "TQTF", "role": "money_market",
                  "policy_bucket": "income_reliable", "source": "api", "enabled": True}],
        disabled=[{"ticker": "GAZP", "class_code": "TQBR",
                   "role": "dividend_candidate", "excluded_reason": "state_control_risk"}])
    audit = _audit([
        {"ticker": "GAZP", "class_code": "TQBR", "role": "dividend_candidate",
         "audit_group": "E", "excluded_reason": "state_control_risk"},
        {"ticker": "SHORT", "class_code": "", "role": "bond_candidate",
         "audit_group": "D", "excluded_reason": "unresolved"},
    ])
    floating = _floating([{"ticker": "SU29024RMFS5", "class_code": "TQOB",
                           "role": "ofz_pk_candidate",
                           "policy_status": "needs_floating_coupon_policy"}])
    return _build(builder=builder, audit=audit, floating=floating)


def test_every_candidate_has_guard_flags():
    rows = _mixed_report()["candidates"]
    assert rows
    assert all(r["execution_requires_manual_confirmation"] is True for r in rows)
    assert all(r["order_preview_required"] is True for r in rows)
    assert all(r["order_send_allowed"] is False for r in rows)
    assert all(r["auto_execution_allowed"] is False for r in rows)


def test_summary_zero_send_and_auto():
    report = _mixed_report()
    s = report["summary"]
    assert s["order_send_allowed_count"] == 0
    assert s["auto_execution_allowed_count"] == 0
    assert s["execution_requires_manual_confirmation_count"] == s["total_candidates"]
    assert s["total_candidates"] == len(report["candidates"])


def test_top_level_guards_block():
    g = _mixed_report()["guards"]
    assert g["order_send_allowed"] is False
    assert g["auto_execution_allowed"] is False
    assert g["execution_requires_manual_confirmation"] is True
    assert g["full_access_token_used"] is False
    assert g["portfolio_mutated"] is False


# ─── I. фильтры max_candidates / min_score ─────────────────────────────────────

def test_max_candidates_caps_rows():
    enabled = [{"ticker": f"T{i}", "class_code": "TQBR", "role": "money_market",
                "policy_bucket": "income_reliable", "source": "api", "enabled": True}
               for i in range(10)]
    report = _build(builder=_builder(enabled=enabled), max_candidates=3)
    assert len(report["candidates"]) == 3


def test_min_score_filters_rows():
    builder = _builder(
        enabled=[{"ticker": "GOOD", "class_code": "TQTF", "role": "money_market",
                  "policy_bucket": "income_reliable", "source": "api", "enabled": True}],
        disabled=[{"ticker": "SHORT", "class_code": "", "role": "bond_candidate",
                   "excluded_reason": "unresolved"}])
    report = _build(builder=builder, min_score=1)
    tickers = {r["ticker"] for r in report["candidates"]}
    # unresolved кандидат имеет отрицательный вклад → score 0 → отфильтрован
    assert "SHORT" not in tickers


# ─── J. markdown: guard-фразы и отсутствие опасных торговых фраз ───────────────

def test_markdown_contains_guard_phrases():
    md = odr.render_md(_mixed_report())
    for phrase in (
        "Owner-only decision support.",
        "Заявки не отправляются.",
        "order_send_allowed=false",
        "auto_execution_allowed=false",
        "execution_requires_manual_confirmation=true",
        "No orders were sent.",
        "No full-access token was used.",
        "Manual confirmation is required before any future execution.",
    ):
        assert phrase in md


def test_markdown_has_no_dangerous_trading_phrases():
    md = odr.render_md(_mixed_report())
    for forbidden in ("купить сейчас", "продать сейчас", "отправить заявку",
                      "гарантированная доходность", "guaranteed income",
                      "safe profit"):
        assert forbidden not in md


def test_markdown_lists_missing_inputs_section():
    report = _build(builder=_builder(), missing_inputs=["data/reports/target_portfolio.json"])
    md = odr.render_md(report)
    assert "Missing inputs" in md
    assert "build-income-universe" in md


# ─── K. модуль не содержит order/execution implementation ──────────────────────

def test_module_has_no_order_execution_code():
    from pathlib import Path
    src = Path(odr.__file__).read_text(encoding="utf-8")
    for forbidden in ("postOrder", "cancelOrder", "OrdersService", "place_order",
                      "submit_order", "place_limit_order", "order_client",
                      "LIVE_EXECUTION", "FULL_ACCESS", "EXECUTION_TOKEN"):
        assert forbidden not in src


# ─── L. CLI: пишет json+md в temp-пути ─────────────────────────────────────────

def test_cli_writes_to_provided_paths(tmp_path):
    import main as cli
    builder = _write(tmp_path, "builder.json", _builder(
        enabled=[{"ticker": "LQDT", "class_code": "TQTF", "role": "money_market",
                  "policy_bucket": "income_reliable", "source": "api",
                  "enabled": True}]))
    audit = _write(tmp_path, "audit.json", _audit([]))
    coupon = _write(tmp_path, "coupon.json", _coupon([]))
    floating = _write(tmp_path, "floating.json", _floating([]))
    resolver = _write(tmp_path, "resolver.json", _resolver([]))
    target = _write(tmp_path, "target.json", _target())
    out_json = tmp_path / "decision.json"
    out_md = tmp_path / "decision.md"
    rc = cli.main([
        "income-owner-decision-report",
        "--universe-report", builder,
        "--audit-json", audit,
        "--coupon-json", coupon,
        "--floating-policy-json", floating,
        "--resolver-json", resolver,
        "--target-json", target,
        "--output-json", str(out_json),
        "--output-md", str(out_md),
    ])
    assert rc == 0
    assert out_json.exists()
    assert out_md.exists()
    on_disk = json.loads(out_json.read_text(encoding="utf-8"))
    s = on_disk["summary"]
    assert s["total_candidates"] == len(on_disk["candidates"])
    assert s["order_send_allowed_count"] == 0
    assert s["auto_execution_allowed_count"] == 0
    assert all(r["proposed_action"] in odr.ALL_ACTIONS for r in on_disk["candidates"])


def test_cli_all_missing_returns_1(tmp_path):
    import main as cli
    rc = cli.main([
        "income-owner-decision-report",
        "--universe-report", str(tmp_path / "a.json"),
        "--audit-json", str(tmp_path / "b.json"),
        "--coupon-json", str(tmp_path / "c.json"),
        "--floating-policy-json", str(tmp_path / "d.json"),
        "--resolver-json", str(tmp_path / "e.json"),
        "--target-json", str(tmp_path / "f.json"),
        "--output-json", str(tmp_path / "o.json"),
        "--output-md", str(tmp_path / "o.md"),
    ])
    assert rc == 1
