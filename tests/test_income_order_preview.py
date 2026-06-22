"""
Тесты income_order_preview — read-only F2 order preview / no-send (ROADMAP F2).

Проверяем: missing decision report → понятная ошибка; нет BUY_CANDIDATE →
пустой preview без crash; unsafe F1-источник (order_send_allowed=true) → hard
fail; T/VTBR с lot_size и ценой → PREVIEW_READY; отсутствует цена → NEEDS_PRICE;
отсутствует lot_size → BLOCKED/LOT_SIZE_UNAVAILABLE; min lot > cap → BLOCKED/
MIN_LOT_EXCEEDS_CAP; commission unavailable не роняет; акция → НКД NOT_APPLICABLE;
guard-флаги жёстко зафиксированы (order_send_allowed/auto_execution_allowed=false,
full_access_token_used/orders_service_used=false); markdown содержит no-send
guard-фразы; CLI пишет json/md в temp-пути; нет OrdersService/postOrder/
cancelOrder. Сети нет (read-only API мокается фейковым клиентом).
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from modules import income_order_preview as iop

# conftest помощник Quotation
from tests.conftest import quotation


# ─── helpers ───────────────────────────────────────────────────────────────────

def _candidate(ticker="T", action="BUY_CANDIDATE", **kw):
    base = {
        "ticker": ticker,
        "proposed_action": action,
        "score": 70,
        "class_code": "TQBR",
        "name": f"{ticker} name",
        "asset_type": "dividend_candidate",
        "source_role": "dividend_candidate",
        "owner_review_eligible": True,
        "risk_flags": [],
        # F1 guard-флаги (безопасный источник)
        "order_send_allowed": False,
        "auto_execution_allowed": False,
    }
    base.update(kw)
    return base


def _decision(candidates):
    return {
        "kind": "income_owner_decision_report",
        "read_only": True,
        "candidates": list(candidates),
        "guards": {"order_send_allowed": False, "auto_execution_allowed": False},
    }


class _FakeClient:
    """Фейковый read-only клиент: только find_instrument / get_last_price.

    НЕ имеет order/execution методов. Любой вызов несуществующего метода упал бы.
    """

    def __init__(self, *, lot=None, price=None, instrument_type="share",
                 figi="BBG-T", time=None):
        self._lot = lot
        self._price = price
        self._instrument_type = instrument_type
        self._figi = figi
        self._time = time

    def find_instrument(self, ticker, class_code):
        if self._lot is None and self._figi is None:
            return None
        inst = {
            "figi": self._figi, "uid": "uid-" + ticker, "isin": "ISIN" + ticker,
            "name": ticker + " full name", "instrumentType": self._instrument_type,
        }
        if self._lot is not None:
            inst["lot"] = self._lot
        return inst

    def get_last_price(self, instrument_id):
        if self._price is None:
            return None
        out = {"price": quotation(self._price)}
        if self._time is not None:
            out["time"] = self._time
        return out


def _build(candidates, **kw):
    defaults = dict(
        candidate_action="BUY_CANDIDATE", tickers=None, max_candidates=5,
        max_order_rub=1000, min_lots=1, max_lots=None, commission_bps=None,
        mode="offline", source_decision_report="x.json", client=None)
    defaults.update(kw)
    return iop.build_report(_decision(candidates), **defaults)


# ─── A. missing / empty / unsafe inputs ────────────────────────────────────────

def test_missing_decision_report_raises(tmp_path):
    with pytest.raises(iop.OrderPreviewError) as exc:
        iop.run(decision_json=str(tmp_path / "nope.json"),
                output_json=str(tmp_path / "o.json"),
                output_md=str(tmp_path / "o.md"))
    assert "decision report" in str(exc.value).lower()


def test_unreadable_decision_report_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(iop.OrderPreviewError):
        iop.run(decision_json=str(p), output_json=str(tmp_path / "o.json"),
                output_md=str(tmp_path / "o.md"))


def test_no_buy_candidate_gives_empty_previews_no_crash():
    report = _build([_candidate("X", action="WAIT")])
    assert report["previews"] == []
    assert report["summary"]["selected_candidates"] == 0
    # total_decision_candidates всё равно посчитан
    assert report["summary"]["total_decision_candidates"] == 1


def test_unsafe_source_order_send_allowed_true_hard_fail():
    with pytest.raises(iop.OrderPreviewError) as exc:
        _build([_candidate("T", order_send_allowed=True)])
    assert "order_send_allowed" in str(exc.value)


def test_unsafe_source_auto_execution_allowed_true_hard_fail():
    with pytest.raises(iop.OrderPreviewError) as exc:
        _build([_candidate("T", auto_execution_allowed=True)])
    assert "auto_execution_allowed" in str(exc.value)


# ─── B. preview ready / lots / price ───────────────────────────────────────────

def test_t_like_with_lot_and_price_preview_ready():
    client = _FakeClient(lot=1, price=100)
    report = _build([_candidate("T")], client=client, mode="auto",
                    max_order_rub=1000)
    row = report["previews"][0]
    assert row["preview_status"] == iop.PREVIEW_READY
    assert row["lot_size"] == 1
    assert row["preview_lots"] == 10  # 1000 / (1 * 100)
    assert row["preview_quantity"] == 10
    assert row["reference_price"] == 100.0
    assert row["reference_price_status"] == iop.PRICE_OK
    assert row["estimated_notional_rub"] == 1000.0
    assert row["estimated_total_rub"] == 1000.0  # commission unavailable → notional


def test_vtbr_like_with_lot_and_price_preview_ready():
    client = _FakeClient(lot=10, price=50, figi="BBG-VTBR")
    report = _build([_candidate("VTBR")], client=client, mode="auto",
                    max_order_rub=1000)
    row = report["previews"][0]
    assert row["preview_status"] == iop.PREVIEW_READY
    assert row["lot_size"] == 10
    assert row["preview_lots"] == 2  # 1000 / (10 * 50) = 2
    assert row["preview_quantity"] == 20


def test_candidate_embedded_lot_and_price_offline_preview_ready():
    # offline-режим: lot_size и reference_price берутся прямо из кандидата
    cand = _candidate("T", lot_size=1, reference_price=200)
    report = _build([cand], mode="offline", max_order_rub=1000)
    row = report["previews"][0]
    assert row["preview_status"] == iop.PREVIEW_READY
    assert row["preview_lots"] == 5  # 1000 / 200
    assert row["reference_price_source"] == "decision_report.reference_price"


def test_missing_price_needs_price():
    client = _FakeClient(lot=1, price=None)
    report = _build([_candidate("T")], client=client, mode="auto")
    row = report["previews"][0]
    assert row["preview_status"] == iop.PREVIEW_NEEDS_PRICE
    assert row["reference_price"] is None
    assert row["estimated_notional_rub"] is None
    assert row["reference_price_status"] in (iop.PRICE_UNAVAILABLE, iop.PRICE_NEEDS)


def test_offline_no_price_source_needs_price():
    report = _build([_candidate("T", lot_size=1)], mode="offline")
    row = report["previews"][0]
    assert row["preview_status"] == iop.PREVIEW_NEEDS_PRICE
    assert row["reference_price_status"] == iop.PRICE_NEEDS


def test_missing_lot_size_blocked():
    client = _FakeClient(lot=None, price=100, figi=None)
    report = _build([_candidate("T")], client=client, mode="auto")
    row = report["previews"][0]
    assert row["preview_status"] == iop.PREVIEW_BLOCKED
    assert iop.BLOCK_LOT_SIZE_UNAVAILABLE in row["preview_blockers"]
    assert row["lot_size"] is None


def test_min_lot_exceeds_cap_blocked():
    client = _FakeClient(lot=1, price=5000)
    report = _build([_candidate("T")], client=client, mode="auto",
                    max_order_rub=1000)
    row = report["previews"][0]
    assert row["preview_status"] == iop.PREVIEW_BLOCKED
    assert iop.BLOCK_MIN_LOT_EXCEEDS_CAP in row["preview_blockers"]


def test_max_lots_caps_preview_lots():
    client = _FakeClient(lot=1, price=10)
    report = _build([_candidate("T")], client=client, mode="auto",
                    max_order_rub=1000, max_lots=3)
    row = report["previews"][0]
    assert row["preview_lots"] == 3  # cap allows 100, max_lots=3 wins


# ─── C. commission / nkd ────────────────────────────────────────────────────────

def test_commission_unavailable_does_not_fail():
    client = _FakeClient(lot=1, price=100)
    report = _build([_candidate("T")], client=client, mode="auto",
                    commission_bps=None)
    row = report["previews"][0]
    assert row["estimated_commission_status"] == iop.EST_UNAVAILABLE
    assert row["estimated_commission_rub"] is None
    assert row["preview_status"] == iop.PREVIEW_READY


def test_commission_available_uses_fee_model():
    client = _FakeClient(lot=1, price=100)
    report = _build([_candidate("T")], client=client, mode="auto",
                    max_order_rub=1000, commission_bps=Decimal("5"))
    row = report["previews"][0]
    assert row["estimated_commission_status"] == iop.EST_OK
    # notional 1000 * 5bps = 0.5
    assert row["estimated_commission_rub"] == 0.5
    assert row["estimated_commission_source"] == "settings.commission_bps"
    assert row["estimated_total_rub"] == 1000.5


def test_stock_nkd_not_applicable():
    client = _FakeClient(lot=1, price=100)
    report = _build([_candidate("T")], client=client, mode="auto")
    row = report["previews"][0]
    assert row["estimated_nkd_status"] == iop.EST_NOT_APPLICABLE
    assert row["estimated_nkd_rub"] is None


def test_bond_nkd_unavailable_without_data():
    client = _FakeClient(lot=1, price=100, instrument_type="bond")
    cand = _candidate("SU26240", source_role="bond_candidate",
                      asset_type="bond_candidate")
    report = _build([cand], client=client, mode="auto")
    row = report["previews"][0]
    assert row["estimated_nkd_status"] == iop.EST_UNAVAILABLE


# ─── D. guard-флаги (жёсткий контракт F2) ───────────────────────────────────────

def test_every_preview_guard_flags_locked():
    client = _FakeClient(lot=1, price=100)
    report = _build([_candidate("T"), _candidate("VTBR", figi="BBG-VTBR")],
                    client=client, mode="auto")
    for row in report["previews"]:
        assert row["manual_confirmation_required"] is True
        assert row["order_send_allowed"] is False
        assert row["auto_execution_allowed"] is False
        assert row["full_access_token_required"] is False
        assert row["orders_service_allowed"] is False


def test_summary_and_guards_safety_counts():
    client = _FakeClient(lot=1, price=100)
    report = _build([_candidate("T")], client=client, mode="auto")
    s = report["summary"]
    assert s["order_send_allowed_count"] == 0
    assert s["auto_execution_allowed_count"] == 0
    assert s["full_access_token_used"] is False
    assert s["orders_service_used"] is False
    g = report["guards"]
    assert g["stage"] == iop.STAGE
    assert g["order_send_allowed"] is False
    assert g["auto_execution_allowed"] is False
    assert g["full_access_token_used"] is False
    assert g["orders_service_used"] is False
    assert g["execution_requires_manual_confirmation"] is True


def test_cash_check_status_unknown():
    client = _FakeClient(lot=1, price=100)
    report = _build([_candidate("T")], client=client, mode="auto")
    assert report["previews"][0]["cash_check_status"] == iop.CASH_UNKNOWN


# ─── E. markdown / source safety ────────────────────────────────────────────────

def test_markdown_contains_no_send_guards():
    client = _FakeClient(lot=1, price=100)
    report = _build([_candidate("T")], client=client, mode="auto")
    md = iop.render_md(report)
    for phrase in (
        "F2 order preview / no-send",
        "Заявки не отправляются",
        "OrdersService не используется",
        "full-access token не используется",
        "order_send_allowed=false",
        "auto_execution_allowed=false",
        "manual confirmation required",
        "No orders were sent.",
        "No full-access token was used.",
        "No portfolio/config mutation.",
        iop.NEXT_STAGE,
    ):
        assert phrase in md, phrase


def test_markdown_has_no_recommendation_wording():
    client = _FakeClient(lot=1, price=100)
    report = _build([_candidate("T")], client=client, mode="auto")
    md = iop.render_md(report).lower()
    for bad in ("купить сейчас", "продать сейчас", "отправить заявку",
                "гарантированная доходность", "guaranteed income", "safe profit"):
        assert bad not in md


def test_module_source_has_no_order_execution_apis():
    import pathlib
    src = pathlib.Path(iop.__file__).read_text(encoding="utf-8")
    for forbidden in ("OrdersService", "postOrder", "cancelOrder",
                      "place_order", "submit_order", "place_limit_order",
                      "order_client", "LIVE_EXECUTION"):
        # допускаются только в комментариях/docstring как negative guard-фразы;
        # реальных вызовов (со скобкой) быть не должно
        assert f"{forbidden}(" not in src, forbidden


# ─── F. CLI / run() пишет json+md ──────────────────────────────────────────────

def test_run_writes_json_and_md(tmp_path):
    decision = _decision([_candidate("T", lot_size=1, reference_price=100),
                          _candidate("VTBR", lot_size=10, reference_price=50)])
    dpath = tmp_path / "decision.json"
    dpath.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    out_json = tmp_path / "preview.json"
    out_md = tmp_path / "preview.md"

    iop.run(
        decision_json=str(dpath), output_json=str(out_json),
        output_md=str(out_md), price_mode="offline",
        tickers=["T", "VTBR"], max_order_rub=1000)

    assert out_json.exists() and out_md.exists()
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["guards"]["stage"] == iop.STAGE
    assert len(data["previews"]) == 2
    assert all(r["order_send_allowed"] is False for r in data["previews"])
    assert any(r["ticker"] in ("T", "VTBR") for r in data["previews"])
