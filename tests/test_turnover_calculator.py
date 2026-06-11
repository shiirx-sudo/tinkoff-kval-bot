"""Тесты подсчёта оборота (REST-контракт)."""
from __future__ import annotations

from decimal import Decimal

from modules.turnover_calculator import calculate_operation_turnover


def test_single_trade(operation, trade):
    op = operation("OPERATION_TYPE_BUY", trades=[trade("100.50", 10)])
    res = calculate_operation_turnover(op, "acc-1")
    assert res.is_approximate is False
    assert res.turnover_exact == Decimal("1005.00")
    assert res.direction == "BUY"
    assert res.trade_count == 1


def test_multiple_trades_summed(operation, trade):
    op = operation("OPERATION_TYPE_SELL", trades=[trade("100", 5), trade("200", 3)])
    res = calculate_operation_turnover(op, "acc-1")
    assert res.turnover_exact == Decimal("1100.00")
    assert res.direction == "SELL"
    assert res.trade_count == 2


def test_turnover_absolute(operation, trade):
    op = operation("OPERATION_TYPE_SELL", trades=[trade("50", -4)])
    res = calculate_operation_turnover(op, "acc-1")
    assert res.turnover_exact == Decimal("200.00")


def test_no_trades_uses_payment(operation, q):
    op = operation("OPERATION_TYPE_BUY", trades=[], payment=q("-1500.25"))
    res = calculate_operation_turnover(op, "acc-1")
    assert res.is_approximate is True
    assert res.turnover_approximate == Decimal("1500.25")
    assert res.warning.startswith("[APPROXIMATE]")


def test_approximate_record(operation, q):
    op = operation("OPERATION_TYPE_BUY", trades=[], payment=q("-999.99"))
    res = calculate_operation_turnover(op, "acc-1")
    assert len(res.trades) == 1
    assert res.trades[0].is_approximate is True
    assert res.trades[0].raw_payment == Decimal("-999.99")


def test_buy_card_direction(operation, trade):
    op = operation("OPERATION_TYPE_BUY_CARD", trades=[trade("10", 1)])
    res = calculate_operation_turnover(op, "acc-1")
    assert res.direction == "BUY"


def test_uuid_not_written_to_ticker(operation):
    uuid = "3d8e1b6e-1c2d-4f5a-9b0c-aaaabbbbcccc"
    op = operation("OPERATION_TYPE_BUY", instrument_uid=uuid, trades=[])
    res = calculate_operation_turnover(op, "acc-1")  # без резолвера
    assert res.instrument_uid == uuid
    assert res.ticker == ""                       # UUID не попадает в ticker
    assert uuid not in (res.trades[0].ticker or "")
    assert res.trades[0].instrument_uid == uuid


def test_calc_survives_resolver_error(operation, trade):
    class BoomResolver:
        def resolve(self, **kwargs):
            raise RuntimeError("resolver down")
    op = operation("OPERATION_TYPE_BUY", trades=[trade("100", 10)])
    res = calculate_operation_turnover(op, "acc-1", resolver=BoomResolver())
    assert res.turnover_exact == Decimal("1000.00")   # расчёт не упал
    assert res.ticker == ""


def test_resolver_fills_ticker(operation, trade):
    from brokers.tinkoff.instruments import InstrumentInfo
    class R:
        def resolve(self, figi="", instrument_uid=""):
            return InstrumentInfo(ticker="GAZP", name="Газпром",
                                  instrument_type="share", figi=figi)
    op = operation("OPERATION_TYPE_BUY", trades=[trade("100", 10)])
    res = calculate_operation_turnover(op, "acc-1", resolver=R())
    assert res.ticker == "GAZP"
    assert res.instrument_name == "Газпром"
    assert res.trades[0].ticker == "GAZP"


# ─── tradesInfo.trades (фактический формат GetOperationsByCursor) ────────────

def _op_with_tradesinfo(trades, payment=None, **extra):
    op = {
        "id": "80125186351", "operationType": "OPERATION_TYPE_BUY",
        "figi": "BBG004731489", "instrumentUid": "uuid-gmkn",
        "instrumentType": "share", "date": "2026-01-15T10:00:00Z",
        "tradesInfo": {"trades": trades},
    }
    if payment is not None:
        op["payment"] = payment
    op.update(extra)
    return op


def test_tradesinfo_counts_as_exact():
    from tests.conftest import quotation
    op = _op_with_tradesinfo([
        {"num": "16763866946", "date": "2026-01-15T10:00:00Z",
         "quantity": "10", "price": quotation("128.04")},
    ], payment=quotation("-1280.4"))
    res = calculate_operation_turnover(op, "acc-1")
    assert res.is_approximate is False
    assert res.turnover_exact == Decimal("1280.40")
    t0 = res.trades[0]
    assert t0.price == Decimal("128.04")
    assert t0.quantity == 10
    assert t0.turnover == Decimal("1280.40")
    assert t0.is_approximate is False
    assert t0.trade_id == "16763866946"


def test_fallback_only_without_tradesinfo():
    from tests.conftest import quotation
    # есть tradesInfo.trades → payment НЕ используется, не approximate
    op = _op_with_tradesinfo(
        [{"num": "1", "quantity": "10", "price": quotation("128.04")}],
        payment=quotation("-999999"),
    )
    res = calculate_operation_turnover(op, "acc-1")
    assert res.is_approximate is False
    assert res.turnover_exact == Decimal("1280.40")

    # пустой tradesInfo → fallback на payment, approximate
    op2 = _op_with_tradesinfo([], payment=quotation("-1280.4"))
    res2 = calculate_operation_turnover(op2, "acc-1")
    assert res2.is_approximate is True
    assert res2.turnover_approximate == Decimal("1280.40")
    assert res2.warning.startswith("[APPROXIMATE]")
