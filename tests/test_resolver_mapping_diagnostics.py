"""
Тесты resolver_mapping_diagnostics — read-only resolver/mapping диагностика для
неразрешённых income-кандидатов из audit group D.

Проверяем: понятная ошибка на missing input; пустой audit → валидный пустой
отчёт; кандидаты вне group D игнорируются; group D кандидаты включаются;
auto_enable_allowed / auto_mapping_allowed всегда false; offline-режим не
вызывает API/enricher; mocked API exact match → candidate_matches_found (но
auto_mapping_allowed=false); множественные matches → ambiguous_matches; нет
matches → no_matches; markdown содержит guard-фразы и не содержит торговых
рекомендаций; CLI пишет json/md в переданные temp-пути. Сети нет.
"""
from __future__ import annotations

import json

import pytest

from modules import resolver_mapping_diagnostics as rmd


# ─── helpers ──────────────────────────────────────────────────────────────────

def _group_d(ticker, *, role="quasi_currency_bond_candidate"):
    return {
        "ticker": ticker,
        "class_code": "",
        "role": role,
        "policy_bucket": "",
        "excluded_reason": "unresolved",
        "notes": "disabled: class_code unresolved; source short-name, not a verified ticker",
        "audit_group": "D",
        "audit_group_name": "resolver_mapping",
        "auto_enable_allowed": False,
        "recommendation_guard": "candidate_for_analysis_only",
    }


def _group_c(ticker):
    return {
        "ticker": ticker,
        "class_code": "TQOB",
        "role": "ofz_pk_candidate",
        "audit_group": "C",
        "audit_group_name": "coupon_validation",
    }


def _audit(candidates):
    return {"kind": "income_universe_disabled_audit", "candidates": candidates}


def _write_input(tmp_path, report):
    p = tmp_path / "income_universe_disabled_audit.json"
    p.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return str(p)


class _StubClient:
    """Мок read-only клиента: возвращает заранее заданные FindInstrument matches."""

    def __init__(self, matches):
        self._matches = matches
        self.calls = []

    def find_instruments(self, query):
        self.calls.append(query)
        return list(self._matches)


class _ExplodingClient:
    """Клиент, который НЕ должен вызываться (offline)."""

    def find_instruments(self, query):  # pragma: no cover - не должен вызваться
        raise AssertionError("find_instruments не должен вызываться в offline")


def _instr(ticker, *, class_code="TQCB", figi="BBG00FIGI001",
           name="Some Bond", instrument_type="bond", currency="rub",
           exchange="MOEX"):
    return {
        "ticker": ticker,
        "classCode": class_code,
        "figi": figi,
        "uid": "uid-" + ticker,
        "isin": "RU000A0" + ticker,
        "name": name,
        "instrumentType": instrument_type,
        "currency": currency,
        "exchange": exchange,
    }


# ─── A. missing / invalid input → понятная ошибка ─────────────────────────────

def test_missing_input_clear_error(tmp_path):
    with pytest.raises(rmd.ResolverMappingError) as exc:
        rmd.load_audit_report(str(tmp_path / "nope.json"))
    assert "income-universe-audit" in str(exc.value)


def test_invalid_json_clear_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(rmd.ResolverMappingError):
        rmd.load_audit_report(str(p))


# ─── B. пустой audit → валидный пустой отчёт ──────────────────────────────────

def test_empty_audit_valid_empty_report():
    report = rmd.build_report(rmd.extract_group_d(_audit([])))
    s = report["summary"]
    assert report["candidates"] == []
    assert s["total_candidates"] == 0
    assert s["unresolved_count"] == 0
    assert s["candidate_matches_found_count"] == 0
    assert s["ambiguous_matches_count"] == 0
    assert s["no_matches_count"] == 0
    assert s["auto_mapping_allowed_count"] == 0
    assert s["auto_enable_allowed_count"] == 0
    assert s["by_mapping_status"] == {}


def test_missing_candidates_key_is_safe():
    report = rmd.build_report(rmd.extract_group_d({"kind": "x"}))
    assert report["candidates"] == []
    assert report["summary"]["total_candidates"] == 0


# ─── C. кандидаты вне group D игнорируются, group D включаются ─────────────────

def test_non_group_d_ignored():
    group_d = rmd.extract_group_d(_audit([
        _group_c("SU29024RMFS5"),
        _group_d("ГазКЗ-37Д"),
        {"ticker": "SBER", "audit_group": "A"},
    ]))
    assert [c["ticker"] for c in group_d] == ["ГазКЗ-37Д"]


def test_group_d_candidates_included():
    tickers = ["ГазКЗ-37Д", "НОВАТЭК1Р5", "СибурХ1Р08"]
    report = rmd.build_report(
        rmd.extract_group_d(_audit([_group_d(t) for t in tickers])))
    assert report["summary"]["total_candidates"] == 3
    assert [r["original_ticker"] for r in report["candidates"]] == tickers


# ─── D. offline: всё unresolved, флаги false, API не вызывается ────────────────

def test_offline_does_not_call_api():
    group_d = rmd.extract_group_d(_audit([_group_d("ГазКЗ-37Д")]))
    # client=None → offline; даже если бы был клиент, run(offline=True) его гасит
    report = rmd.build_report(group_d, client=None)
    r = report["candidates"][0]
    assert r["mapping_status"] == rmd.STATUS_UNRESOLVED
    assert r["candidates_for_manual_review"] == []
    assert report["mode"] == "offline"


def test_run_offline_ignores_client(tmp_path):
    inp = _write_input(tmp_path, _audit([_group_d("ГазКЗ-37Д")]))
    report = rmd.run(
        input_json=inp,
        output_json=str(tmp_path / "o.json"),
        output_md=str(tmp_path / "o.md"),
        offline=True,
        client=_ExplodingClient(),
    )
    assert report["mode"] == "offline"
    assert report["candidates"][0]["mapping_status"] == rmd.STATUS_UNRESOLVED


def test_all_flags_always_false():
    cands = [_group_d(f"Бонд{i}") for i in range(4)]
    report = rmd.build_report(rmd.extract_group_d(_audit(cands)))
    assert all(r["auto_enable_allowed"] is False for r in report["candidates"])
    assert all(r["auto_mapping_allowed"] is False for r in report["candidates"])
    assert all(
        r["recommendation_guard"] == "candidate_for_mapping_review_only"
        for r in report["candidates"])
    assert report["summary"]["auto_mapping_allowed_count"] == 0
    assert report["summary"]["auto_enable_allowed_count"] == 0


# ─── E. mocked API: exact match / ambiguous / no matches ──────────────────────

def test_api_exact_match_is_candidate_not_applied():
    client = _StubClient([_instr("GAZP37D")])
    group_d = rmd.extract_group_d(_audit([_group_d("ГазКЗ-37Д")]))
    report = rmd.build_report(group_d, client=client)
    r = report["candidates"][0]
    assert r["mapping_status"] == rmd.STATUS_CANDIDATE_MATCHES
    assert r["match_count"] == 1
    assert r["auto_mapping_allowed"] is False
    assert r["auto_enable_allowed"] is False
    m = r["candidates_for_manual_review"][0]
    assert m["ticker"] == "GAZP37D"
    assert m["class_code"] == "TQCB"
    assert m["figi"] == "BBG00FIGI001"
    assert m["instrument_type"] == "bond"
    assert m["match_reason"] == "find_instrument_query"
    # source candidate не изменён
    assert r["original_ticker"] == "ГазКЗ-37Д"
    assert report["summary"]["candidate_matches_found_count"] == 1
    assert report["summary"]["auto_mapping_allowed_count"] == 0


def test_api_multiple_matches_ambiguous():
    client = _StubClient([_instr("A1"), _instr("A2")])
    group_d = rmd.extract_group_d(_audit([_group_d("СибурХ1Р08")]))
    report = rmd.build_report(group_d, client=client)
    r = report["candidates"][0]
    assert r["mapping_status"] == rmd.STATUS_AMBIGUOUS
    assert r["match_count"] == 2
    assert r["auto_mapping_allowed"] is False
    assert report["summary"]["ambiguous_matches_count"] == 1


def test_api_no_matches():
    client = _StubClient([])
    group_d = rmd.extract_group_d(_audit([_group_d("Полюс Б1P5")]))
    report = rmd.build_report(group_d, client=client)
    r = report["candidates"][0]
    assert r["mapping_status"] == rmd.STATUS_NO_MATCHES
    assert r["match_count"] == 0
    assert report["summary"]["no_matches_count"] == 1


def test_api_error_degrades_to_no_matches():
    class _BadClient:
        def find_instruments(self, query):
            raise RuntimeError("network down")

    group_d = rmd.extract_group_d(_audit([_group_d("ВЭБ2Р-54В")]))
    report = rmd.build_report(group_d, client=_BadClient())
    r = report["candidates"][0]
    assert r["mapping_status"] == rmd.STATUS_NO_MATCHES
    assert r["candidates_for_manual_review"] == []


# ─── F. markdown: guard-фразы, без торговых рекомендаций ───────────────────────

def test_markdown_contains_guard_phrases():
    client = _StubClient([_instr("GAZP37D")])
    report = rmd.build_report(
        rmd.extract_group_d(_audit([_group_d("ГазКЗ-37Д")])), client=client)
    md = rmd.render_md(report)
    for phrase in (
        "Аналитика, не рекомендация.",
        "Заявки не отправляются.",
        "auto_enable_allowed=false",
        "auto_mapping_allowed=false",
        "candidate_for_mapping_review_only",
    ):
        assert phrase in md
    # явные требования к содержанию
    assert "не применяются автоматически" in md
    assert "ручным и отдельным PR" in md
    assert "не меняет" in md


def test_markdown_no_trading_recommendation_words():
    report = rmd.build_report(
        rmd.extract_group_d(_audit([_group_d("ГазКЗ-37Д"), _group_d("НорНик1P14")])))
    md = rmd.render_md(report)
    for forbidden in ("купить", "продать", "исключить", "buy", "sell",
                      "recommendation"):
        assert forbidden not in md


def test_empty_markdown_is_valid_and_safe():
    md = rmd.render_md(rmd.build_report(rmd.extract_group_d(_audit([]))))
    assert "Аналитика, не рекомендация." in md
    assert "_(нет group D кандидатов)_" in md
    for forbidden in ("купить", "продать", "исключить", "buy", "sell",
                      "recommendation"):
        assert forbidden not in md


# ─── G. модуль не содержит order/execution implementation ─────────────────────

def test_module_has_no_order_execution_code():
    from pathlib import Path
    src = Path(rmd.__file__).read_text(encoding="utf-8")
    for forbidden in ("postOrder", "cancelOrder", "OrdersService", "place_order",
                      "submit_order", "place_limit_order", "order_client",
                      "get_portfolio", "get_positions"):
        assert forbidden not in src


# ─── H. CLI/run: пишет json+md в переданные temp-пути ─────────────────────────

def test_run_writes_json_and_md(tmp_path):
    inp = _write_input(tmp_path, _audit(
        [_group_d("ГазКЗ-37Д"), _group_d("НОВАТЭК1Р5")]))
    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    report = rmd.run(input_json=inp, output_json=str(out_json),
                     output_md=str(out_md), offline=True)
    assert out_json.exists()
    assert out_md.exists()
    assert report["_output_json"] == str(out_json)
    assert report["_output_md"] == str(out_md)

    on_disk = json.loads(out_json.read_text(encoding="utf-8"))
    s = on_disk["summary"]
    assert s["total_candidates"] == 2
    assert s["auto_mapping_allowed_count"] == 0
    assert s["auto_enable_allowed_count"] == 0


def test_run_missing_input_raises(tmp_path):
    with pytest.raises(rmd.ResolverMappingError):
        rmd.run(input_json=str(tmp_path / "missing.json"),
                output_json=str(tmp_path / "o.json"),
                output_md=str(tmp_path / "o.md"))


def test_cli_writes_to_provided_paths(tmp_path):
    import main as cli
    inp = _write_input(tmp_path, _audit([_group_d("ГазКЗ-37Д")]))
    out_json = tmp_path / "cli_out.json"
    out_md = tmp_path / "cli_out.md"
    rc = cli.main([
        "income-resolver-mapping-diagnostics",
        "--input-json", inp,
        "--output-json", str(out_json),
        "--output-md", str(out_md),
        "--offline",
    ])
    assert rc == 0
    assert out_json.exists()
    assert out_md.exists()
    on_disk = json.loads(out_json.read_text(encoding="utf-8"))
    assert on_disk["mode"] == "offline"
    assert on_disk["summary"]["total_candidates"] == 1


def test_cli_missing_input_returns_1(tmp_path):
    import main as cli
    rc = cli.main([
        "income-resolver-mapping-diagnostics",
        "--input-json", str(tmp_path / "missing.json"),
        "--output-json", str(tmp_path / "o.json"),
        "--output-md", str(tmp_path / "o.md"),
        "--offline",
    ])
    assert rc == 1
