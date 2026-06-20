"""
Тесты target_portfolio_v1 — read-only план целевого доходного портфеля.
Никаких заявок, портфель не меняется; в выводе нет order-wording.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from modules.income_engine import IncomeEnv
from modules.target_portfolio import (
    LOW_YIELD_TO_BLENDED_RATIO,
    Allocation,
    Candidate,
    TargetEnv,
    TargetPortfolio,
    allocate_target,
    annotate_low_yield_diagnostics,
    build_current_vs_target,
    build_monthly_plan,
    build_new_capital_plan,
    build_target_portfolio,
    classify_eligibility,
)

ENV = TargetEnv(target_monthly_rub=Decimal("100000"), tax_rate_pct=Decimal("13"))


def _cand(ticker, bucket, cons_net=None, source="dividend", net=None,
          fund="quality_pass", risk=None, reasons=None):
    return Candidate(
        ticker=ticker, source_type=source, income_data_source="x",
        policy_bucket=bucket, policy_reasons=reasons or [],
        conservative_net_yield_pct=Decimal(str(cons_net)) if cons_net is not None else None,
        net_yield_pct=Decimal(str(net)) if net is not None else None,
        fundamental_verdict=fund, risk_notes=risk or [])


# ─── 1. reliable включается ──────────────────────────────────────────────────

def test_reliable_eligible_base():
    c = _cand("T", "income_reliable", cons_net=8)
    classify_eligibility(c, ENV)
    assert c.eligible and c.target_layer == "base"


# ─── 2. variable money-market включается ─────────────────────────────────────

def test_variable_money_market_eligible_base():
    c = _cand("LQDT", "income_variable", cons_net=9, source="money_market")
    classify_eligibility(c, ENV)
    assert c.eligible and c.target_layer == "base"


# ─── 3. estimated исключается по умолчанию ───────────────────────────────────

def test_estimated_excluded_by_default():
    c = _cand("NVTK", "income_estimated", net=7)
    classify_eligibility(c, ENV)
    assert not c.eligible
    assert c.excluded_reason == "not_allowed_by_policy"


# ─── 4. estimated включается только при include_estimated ─────────────────────

def test_estimated_included_when_flag():
    env = TargetEnv(target_monthly_rub=Decimal("100000"), include_estimated=True)
    c = _cand("NVTK", "income_estimated", net=7)
    classify_eligibility(c, env)
    assert c.eligible and c.target_layer == "estimate"


# ─── 5. unknown / excluded никогда не в base ──────────────────────────────────

def test_unknown_and_excluded_never_base():
    u = _cand("XXXX", "income_unknown")
    classify_eligibility(u, ENV)
    assert not u.eligible and u.excluded_reason == "unknown_income_data"

    ex = _cand("LKOH", "income_excluded", reasons=["trailing_not_guaranteed",
                                                   "trailing_yield_above_cap"])
    classify_eligibility(ex, ENV)
    assert not ex.eligible and ex.excluded_reason == "trailing_yield_above_cap"


def test_state_control_risk_excluded():
    c = _cand("GAZP", "income_reliable", cons_net=10, risk=["state_control_risk"])
    classify_eligibility(c, ENV)
    assert not c.eligible and c.excluded_reason == "state_control_risk"


def test_no_conservative_yield_excluded():
    c = _cand("T", "income_reliable", cons_net=None)
    classify_eligibility(c, ENV)
    assert not c.eligible and c.excluded_reason == "no_conservative_yield"


def test_min_policy_bucket_reliable_excludes_variable():
    env = TargetEnv(target_monthly_rub=Decimal("100000"),
                    min_policy_bucket="income_reliable")
    c = _cand("LQDT", "income_variable", cons_net=9, source="money_market")
    classify_eligibility(c, env)
    assert not c.eligible and c.excluded_reason == "not_allowed_by_policy"


# ─── 6. max position pct cap ──────────────────────────────────────────────────

def test_max_position_cap_respected():
    elig = [_cand("A", "income_reliable", cons_net=10),
            _cand("B", "income_reliable", cons_net=10)]
    for c in elig:
        classify_eligibility(c, ENV)
    allocs, req, status, _ = allocate_target(elig, ENV)
    assert status == "ok"
    for a in allocs:
        assert a.target_weight_pct <= ENV.max_position_pct
    assert req is not None and req > 0


# ─── 6b. max issuer pct cap ───────────────────────────────────────────────────

def test_max_issuer_cap_respected():
    env = TargetEnv(target_monthly_rub=Decimal("100000"),
                    max_position_pct=Decimal("60"), max_issuer_pct=Decimal("30"))
    # два инструмента одного эмитента (одинаковый issuer)
    elig = [
        Candidate(ticker="SBER", issuer="SBERBANK", source_type="dividend",
                  policy_bucket="income_reliable", fundamental_verdict="quality_pass",
                  conservative_net_yield_pct=Decimal("10"), eligible=True, target_layer="base"),
        Candidate(ticker="SBERP", issuer="SBERBANK", source_type="dividend",
                  policy_bucket="income_reliable", fundamental_verdict="quality_pass",
                  conservative_net_yield_pct=Decimal("10"), eligible=True, target_layer="base"),
    ]
    allocs, _, status, _ = allocate_target(elig, env)
    assert status == "ok"
    issuer_total = sum((a.target_weight_pct for a in allocs), Decimal("0"))
    assert issuer_total <= env.max_issuer_pct + Decimal("0.01")
    assert any("max_issuer_capped" in a.reason for a in allocs)


# ─── 7. max money market pct cap ──────────────────────────────────────────────

def test_max_money_market_cap_respected():
    env = TargetEnv(target_monthly_rub=Decimal("100000"), max_position_pct=Decimal("50"),
                    max_money_market_pct=Decimal("40"))
    elig = [_cand("LQDT", "income_variable", cons_net=9, source="money_market"),
            _cand("AKMM", "income_variable", cons_net=9, source="money_market"),
            _cand("T", "income_reliable", cons_net=8, source="dividend")]
    for c in elig:
        classify_eligibility(c, env)
    allocs, _, status, _ = allocate_target(elig, env)
    assert status == "ok"
    mm_total = sum((a.target_weight_pct for a in allocs
                    if a.ticker in ("LQDT", "AKMM")), Decimal("0"))
    assert mm_total <= env.max_money_market_pct + Decimal("0.01")


# ─── 8. current vs target diff ────────────────────────────────────────────────

def test_current_vs_target_diff():
    allocs = [Allocation("T", target_capital_rub=Decimal("100000"))]
    holdings = {"T": Decimal("40000"), "OLD": Decimal("5000")}
    rows = build_current_vs_target(allocs, holdings, ENV)
    by = {r.ticker: r for r in rows}
    assert by["T"].diff_value_rub == Decimal("60000")
    assert by["T"].action_hint == "underweight"
    assert by["OLD"].action_hint == "not_in_target"


def test_current_vs_target_overweight_and_hold():
    allocs = [Allocation("T", target_capital_rub=Decimal("100000")),
              Allocation("B", target_capital_rub=Decimal("100000"))]
    holdings = {"T": Decimal("200000"), "B": Decimal("100000")}
    by = {r.ticker: r for r in build_current_vs_target(allocs, holdings, ENV)}
    assert by["T"].action_hint == "overweight"
    assert by["B"].action_hint == "hold"


# ─── 9. new capital plan только в eligible underweight ───────────────────────

def test_new_capital_plan_distributes_to_underweight():
    env = TargetEnv(target_monthly_rub=Decimal("100000"), cash_reserve_rub=Decimal("0"))
    allocs = [Allocation("T", target_capital_rub=Decimal("100000"),
                         net_yield_pct=Decimal("8")),
              Allocation("LQDT", target_capital_rub=Decimal("100000"),
                         net_yield_pct=Decimal("10"))]
    holdings = {"T": Decimal("100000")}   # T уже добит → только LQDT underweight
    plan = build_new_capital_plan(allocs, holdings, Decimal("50000"), env)
    assert plan and all(r.ticker == "LQDT" for r in plan)
    assert plan[0].planned_add_rub == Decimal("50000")
    assert plan[0].expected_extra_base_income_month_rub > 0


def test_new_capital_plan_respects_cash_reserve():
    env = TargetEnv(target_monthly_rub=Decimal("100000"), cash_reserve_rub=Decimal("5000"))
    allocs = [Allocation("T", target_capital_rub=Decimal("100000"),
                         net_yield_pct=Decimal("8")),
              Allocation("LQDT", target_capital_rub=Decimal("100000"),
                         net_yield_pct=Decimal("10"))]
    holdings: dict = {}
    plan = build_new_capital_plan(allocs, holdings, Decimal("100000"), env)
    total = sum((r.planned_add_rub for r in plan), Decimal("0"))
    assert total <= Decimal("95000")          # 100000 - 5000 резерва
    assert total > 0


# ─── 10. monthly contribution plan ───────────────────────────────────────────

def test_monthly_plan_generated():
    allocs = [Allocation("LQDT", target_capital_rub=Decimal("600000"),
                         net_yield_pct=Decimal("12"))]
    rows = build_monthly_plan(allocs, {}, Decimal("50000"), 12, ENV)
    assert len(rows) == 12
    assert rows[0].contribution_rub == Decimal("50000")
    assert rows[0].month == 1 and rows[-1].month == 12
    # доход растёт по мере докупки
    assert rows[-1].expected_base_income_after_rub > rows[0].expected_base_income_after_rub


# ─── 11. пустой universe → insufficient_universe ─────────────────────────────

def test_empty_universe_insufficient():
    allocs, req, status, warns = allocate_target([], ENV)
    assert status == "insufficient_universe"
    assert req is None and allocs == []
    assert warns


# ─── 12. отчёты содержат секции ──────────────────────────────────────────────

def _sample_tp() -> TargetPortfolio:
    elig = [_cand("LQDT", "income_variable", cons_net=9, source="money_market"),
            _cand("T", "income_reliable", cons_net=8)]
    for c in elig:
        classify_eligibility(c, ENV)
    allocs, req, status, warns = allocate_target(elig, ENV)
    tp = TargetPortfolio(
        target_monthly_net_rub=Decimal("100000"),
        target_annual_net_rub=Decimal("1200000"),
        target_status=status, required_capital_rub=req,
        eligible_universe=elig, excluded_universe=[_cand("XXXX", "income_unknown")],
        target_allocation=allocs, warnings=warns)
    tp.current_vs_target = build_current_vs_target(allocs, {"T": Decimal("10000")}, ENV)
    tp.new_capital_plan = build_new_capital_plan(allocs, {}, Decimal("100000"), ENV)
    tp.monthly_plan = build_monthly_plan(allocs, {}, Decimal("50000"), 3, ENV)
    return tp


def test_reports_contain_sections(tmp_path):
    import json

    from reports import target_portfolio_reports as rep
    tp = _sample_tp()
    rep.write_target_portfolio(tp, tmp_path)
    for name in ("target_portfolio.json", "target_portfolio.csv",
                 "target_portfolio.md", "target_portfolio_plan.csv"):
        assert (tmp_path / name).exists(), name
    payload = json.loads((tmp_path / "target_portfolio.json").read_text(encoding="utf-8"))
    for key in ("target", "current_summary", "eligible_universe", "excluded_universe",
                "target_allocation", "current_vs_target", "new_capital_plan",
                "monthly_plan", "warnings"):
        assert key in payload, key
    md = (tmp_path / "target_portfolio.md").read_text(encoding="utf-8")
    assert md.startswith("# Target portfolio — READ ONLY")
    assert "Target allocation" in md and "Current vs target" in md


def test_reports_have_no_order_wording(tmp_path):
    from reports import target_portfolio_reports as rep
    tp = _sample_tp()
    rep.write_target_portfolio(tp, tmp_path)
    md = (tmp_path / "target_portfolio.md").read_text(encoding="utf-8").lower()
    console = rep.render_console(tp).lower()
    for w in ("купить", "продать", "buy", "sell", "postorder", "place_order"):
        assert w not in md, w
        assert w not in console, w
    assert "planned_add_rub" in md
    assert "underweight" in md


# ─── 13. Telegram: READ ONLY и без order-wording ─────────────────────────────

def test_telegram_text_safe():
    from reports import target_portfolio_reports as rep
    text = rep.build_telegram(_sample_tp())
    assert "READ ONLY" in text
    assert "Заявки не отправляются" in text
    assert "не рекомендация" in text
    assert "underweight_by_rub" in text
    for w in ("купить", "продать", "BUY", "SELL", "postOrder"):
        assert w not in text


# ─── оркестрация build_target_portfolio (read-only fake client) ──────────────

class _FakeClient:
    CANDS = {
        "LQDT": [{"ticker": "LQDT", "classCode": "TQTF", "figi": "FLQDT",
                  "uid": "ULQDT", "name": "Ликвидность", "instrumentType": "etf"}],
        "SBER": [{"ticker": "SBER", "classCode": "TQBR", "figi": "FSBER",
                  "uid": "USBER", "name": "Сбербанк", "instrumentType": "share"}],
    }
    PRICES = {"FLQDT": "2.01", "FSBER": "300"}

    def find_instruments(self, query):
        return self.CANDS.get(query.upper(), [])

    def get_last_price(self, instrument_id):
        p = self.PRICES.get(instrument_id)
        return {"price": _q(p)} if p else None

    def get_order_book(self, instrument_id, depth=1):
        return {"bids": [], "asks": []}

    def get_candles(self, instrument_id, frm, to, interval="CANDLE_INTERVAL_DAY"):
        return {"candles": []}

    def get_dividends(self, instrument_id, frm, to):
        return []   # SBER без дивидендных данных → unknown

    def get_bond_coupons(self, instrument_id, frm, to):
        return []

    def get_accrued_interests(self, instrument_id, frm, to):
        return []


def _q(value):
    d = Decimal(str(value))
    units = int(d)
    nano = int((d - units) * Decimal("1000000000"))
    return {"units": str(units), "nano": nano}


def test_build_target_portfolio_orchestration():
    income_env = IncomeEnv(target_monthly_rub=Decimal("100000"), tax_rate_pct=Decimal("13"))
    target_env = TargetEnv(target_monthly_rub=Decimal("100000"), tax_rate_pct=Decimal("13"))
    config = {"manual_yields": {"LQDT": {"expected_annual_yield_pct": 14.0}}}
    tp = build_target_portfolio(
        _FakeClient(), raw_watchlist=["TQTF:LQDT", "TQBR:SBER"], account_id=None,
        config=config, income_env=income_env, target_env=target_env)
    elig = {c.ticker for c in tp.eligible_universe}
    excl = {c.ticker: c.excluded_reason for c in tp.excluded_universe}
    assert "LQDT" in elig                      # manual MM → income_variable
    assert "SBER" in excl                       # нет дохода → unknown
    assert excl["SBER"] == "unknown_income_data"
    assert tp.target_status == "ok"
    assert any(a.ticker == "LQDT" for a in tp.target_allocation)
    assert tp.required_capital_rub is not None


def test_build_target_portfolio_empty_universe():
    income_env = IncomeEnv(target_monthly_rub=Decimal("100000"))
    target_env = TargetEnv(target_monthly_rub=Decimal("100000"))
    tp = build_target_portfolio(
        _FakeClient(), raw_watchlist=["TQBR:SBER"], account_id=None,
        config={}, income_env=income_env, target_env=target_env)
    assert tp.target_status == "insufficient_universe"
    assert tp.target_allocation == []


# ─── 13b. low-yield диагностика ──────────────────────────────────────────────

def test_low_yield_slot_flagged_for_high_weight_low_yield():
    # 4 eligible инструмента → equal weight 25% каждый (под cap 25%).
    # T с доходностью ~1.4% получает много капитала, но мало дохода.
    elig = [_cand("VTBR", "income_reliable", cons_net=11),
            _cand("SBMM", "income_reliable", cons_net=9),
            _cand("LQDT", "income_reliable", cons_net=10),
            _cand("T", "income_reliable", cons_net="1.4")]
    for c in elig:
        classify_eligibility(c, ENV)
    allocs, _, status, warns = allocate_target(elig, ENV)
    assert status == "ok"
    by = {a.ticker: a for a in allocs}
    # allocation math не изменилась: равные веса 25%
    for a in allocs:
        assert a.target_weight_pct == Decimal("25")
    # T помечен низкодоходным слотом
    assert by["T"].low_yield_slot is True
    assert by["T"].capital_share_pct == Decimal("25")
    assert by["T"].yield_vs_blended_ratio is not None
    assert by["T"].yield_vs_blended_ratio < LOW_YIELD_TO_BLENDED_RATIO
    assert by["T"].income_share_pct < by["T"].capital_share_pct
    # высокодоходные слоты не помечены
    assert by["VTBR"].low_yield_slot is False
    # warning есть и упоминает T, без order-wording / без «исключите/продайте»
    low_warns = [w for w in warns if "Низкодоходный слот" in w]
    assert low_warns and any("T" in w for w in low_warns)
    joined = " ".join(low_warns).lower()
    assert "не рекомендация" in joined
    for w in ("купить", "продать", "исключите", "купите", "buy", "sell"):
        assert w not in joined


def test_no_low_yield_slot_when_yields_similar():
    elig = [_cand("A", "income_reliable", cons_net=9),
            _cand("B", "income_reliable", cons_net="9.5"),
            _cand("C", "income_reliable", cons_net=10),
            _cand("D", "income_reliable", cons_net="8.5")]
    for c in elig:
        classify_eligibility(c, ENV)
    allocs, _, status, warns = allocate_target(elig, ENV)
    assert status == "ok"
    assert all(a.low_yield_slot is False for a in allocs)
    assert not [w for w in warns if "Низкодоходный слот" in w]


def test_low_yield_diagnostics_zero_income_safe():
    # total_expected_monthly_income = 0 → ratios без падения
    allocs = [Allocation("X", target_weight_pct=Decimal("25"),
                         net_yield_pct=Decimal("8"),
                         expected_base_income_month_rub=Decimal("0")),
              Allocation("Y", target_weight_pct=Decimal("25"),
                         net_yield_pct=Decimal("9"),
                         expected_base_income_month_rub=Decimal("0"))]
    warns = annotate_low_yield_diagnostics(allocs, Decimal("8.5"))
    assert warns == []
    for a in allocs:
        assert a.income_share_pct == Decimal("0")
        assert a.low_yield_slot is False


def test_low_yield_diagnostics_zero_blended_safe():
    # blended_yield_pct = 0 → yield_vs_blended_ratio = None, без падения
    allocs = [Allocation("X", target_weight_pct=Decimal("25"),
                         net_yield_pct=Decimal("1"),
                         expected_base_income_month_rub=Decimal("100"))]
    warns = annotate_low_yield_diagnostics(allocs, Decimal("0"))
    assert warns == []
    assert allocs[0].yield_vs_blended_ratio is None
    assert allocs[0].low_yield_slot is False
    # None blended тоже безопасно
    annotate_low_yield_diagnostics(allocs, None)
    assert allocs[0].yield_vs_blended_ratio is None


def test_low_yield_diagnostics_in_reports(tmp_path):
    import json

    from reports import target_portfolio_reports as rep
    elig = [_cand("VTBR", "income_reliable", cons_net=11),
            _cand("SBMM", "income_reliable", cons_net=9),
            _cand("LQDT", "income_reliable", cons_net=10),
            _cand("T", "income_reliable", cons_net="1.4")]
    for c in elig:
        classify_eligibility(c, ENV)
    allocs, req, status, warns = allocate_target(elig, ENV)
    tp = TargetPortfolio(target_status=status, required_capital_rub=req,
                         target_allocation=allocs, warnings=warns)
    rep.write_target_portfolio(tp, tmp_path)

    payload = json.loads((tmp_path / "target_portfolio.json").read_text(encoding="utf-8"))
    t_row = next(a for a in payload["target_allocation"] if a["ticker"] == "T")
    for key in ("capital_share_pct", "income_share_pct", "income_efficiency_ratio",
                "yield_vs_blended_ratio", "low_yield_slot"):
        assert key in t_row, key
    assert t_row["low_yield_slot"] is True

    csv_text = (tmp_path / "target_portfolio.csv").read_text(encoding="utf-8-sig")
    assert "low_yield_slot" in csv_text.splitlines()[0]
    md = (tmp_path / "target_portfolio.md").read_text(encoding="utf-8")
    assert "low_yield_slot" in md and "yield_vs_blended_ratio" in md


# ─── 14. safety scan ─────────────────────────────────────────────────────────

def test_no_order_endpoints_in_target_sources():
    files = ["modules/target_portfolio.py", "reports/target_portfolio_reports.py"]
    forbidden = ("OrdersService", "postOrder", "cancelOrder", "place_order",
                 "submit_order", "place_limit_order", "order_client",
                 "LIVE_EXECUTION", "full_token")
    for f in files:
        src = Path(f).read_text(encoding="utf-8")
        for tok in forbidden:
            assert tok not in src, f"{f}: {tok}"
