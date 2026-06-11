"""Тесты read-only резолвера инструментов."""
from __future__ import annotations

from brokers.tinkoff.instruments import InstrumentResolver


class FakeInstrClient:
    def __init__(self):
        self.calls = 0

    def get_instrument_by(self, id_type, id_value):
        self.calls += 1
        if id_value == "BBG004731489":
            return {"instrument": {
                "figi": "BBG004731489", "ticker": "GAZP", "name": "Газпром",
                "instrumentType": "share", "classCode": "TQBR", "uid": "uid-gazp",
            }}
        raise RuntimeError("instrument not found")


def test_resolve_figi_to_mock_instrument():
    r = InstrumentResolver(FakeInstrClient())
    info = r.resolve(figi="BBG004731489")
    assert info.ticker == "GAZP"
    assert info.name == "Газпром"
    assert info.instrument_type == "share"
    assert info.class_code == "TQBR"
    assert info.resolved is True


def test_resolver_uses_cache():
    c = FakeInstrClient()
    r = InstrumentResolver(c)
    r.resolve(figi="BBG004731489")
    r.resolve(figi="BBG004731489")
    assert c.calls == 1   # второй вызов — из кэша


def test_resolve_failure_returns_known_ids():
    class Boom:
        def get_instrument_by(self, *a):
            raise RuntimeError("boom")
    info = InstrumentResolver(Boom()).resolve(figi="BBGX", instrument_uid="uid-xyz")
    assert info.ticker == ""
    assert info.figi == "BBGX"
    assert info.instrument_uid == "uid-xyz"
